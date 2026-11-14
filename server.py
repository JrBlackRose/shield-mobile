#!/usr/bin/env python3
import hmac, os, time, uuid, asyncio
from typing import Dict, Literal
from contextlib import asynccontextmanager
from decimal import Decimal
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header
from pydantic import BaseModel
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from paper_engine import (run_pipeline, load_credentials, step5_execute, BracketPlan, fetch_latest_ask, check_price_drift, PipelineAbort, RuleReject)
from telegram_notifier import send_approval_request, start_telegram_listener
import ledger

PENDING_APPROVALS: Dict[str, dict] = {}
APPROVAL_TTL_SECONDS = {"INTRADAY": 300, "SWING": 1800}

AUTH_TOKEN = os.getenv("SHIELD_AUTH_TOKEN", "").strip()
if not AUTH_TOKEN:
    raise RuntimeError("SHIELD_AUTH_TOKEN environment variable is required")

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ledger.init_db()
        start_telegram_listener()
        print("[+] Ledger and Telegram Listener initialized.")
    except Exception as exc:
        print(f"[-] Startup Warning: {exc}")
    yield

app = FastAPI(title="Project Shield", lifespan=lifespan)

class WebhookPayload(BaseModel):
    ticker: str
    mode: Literal["AUTO", "MANUAL"] = "MANUAL"
    timeframe: Literal["SWING", "INTRADAY"] = "SWING"

def require_auth(x_shield_auth: str = Header(None)):
    supplied = (x_shield_auth or "").encode("utf-8")
    if not hmac.compare_digest(supplied, AUTH_TOKEN.encode("utf-8")):
        print("\n[AUTH FAILED] Unauthorized request.")
        raise HTTPException(status_code=401, detail="Unauthorized")

def check_existing_position(ticker: str) -> bool:
    key, secret = load_credentials()
    client = TradingClient(key, secret, paper=True)
    try:
        client.get_open_position(ticker)
        return True
    except APIError as exc:
        if getattr(exc, "status_code", None) != 404:
            raise
    open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker]))
    return len(open_orders) > 0

async def process_trade_job(ticker: str, mode: str, timeframe: str, order_token: str):
    try:
        blocked = await asyncio.to_thread(check_existing_position, ticker)
    except Exception as exc:
        ledger.log_audit(ticker, "ABORTED", f"Position check failed: {exc}", timeframe)
        print(f"\n[ABORTED] {ticker}: could not verify existing positions: {exc}")
        return
    if blocked:
        print(f"\n[BLOCKED] Ticker {ticker} rejected. Position or open order already exists.")
        ledger.log_audit(ticker, "REJECTED", "Position or open order already active", timeframe)
        return
    result = await asyncio.to_thread(run_pipeline, ticker, mode, timeframe)
    ledger.log_audit(ticker, result.get("status", "UNKNOWN"), result.get("reason", "OK"), timeframe)
    if result.get("status") == "APPROVED" and mode == "MANUAL":
        result["staged_at"] = time.time()
        PENDING_APPROVALS[order_token] = result
        print(f"\n[STAGED] {ticker} passed audit. Awaiting approval: {order_token}")
        try:
            await send_approval_request(order_token, ticker, result["trade_plan"])
        except Exception as exc:
            print(f"[-] Telegram alert failed: {exc}")
    elif result.get("status") == "EXECUTED" and mode == "AUTO":
        tp = result["trade_plan"]
        try:
            ledger.log_execution(
                order_id=result["order_id"], ticker=ticker, shares=tp["shares"],
                entry=tp["entry"], stop=tp["stop_loss"], target=tp["take_profit"],
                risk=tp["max_risk_usd"], timeframe=timeframe,
            )
        except Exception as exc:
            print(f"[-] AUTO ORDER {result.get('order_id')} PLACED BUT LEDGER WRITE FAILED: {exc}")
        print(f"\n[AUTO-EXECUTED] {ticker} -> {result['order_id']}")
    else:
        print(f"\n[RESULT] {ticker} -> {result.get('status')}: {result.get('reason', 'OK')}")

@app.post("/webhook")
def inbound_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks, x_shield_auth: str = Header(None)):
    require_auth(x_shield_auth)
    ticker = payload.ticker.strip().upper()
    order_token = str(uuid.uuid4())[:8]
    background_tasks.add_task(process_trade_job, ticker, payload.mode, payload.timeframe, order_token)
    return {"status": "QUEUED", "ticker": ticker, "approval_token": order_token}

@app.post("/confirm/{token}")
def confirm_order(token: str, x_shield_auth: str = Header(None)):
    require_auth(x_shield_auth)
    staged = PENDING_APPROVALS.pop(token, None)
    if staged is None:
        raise HTTPException(status_code=404, detail="Token not found or expired")
    tp = staged["trade_plan"]
    ticker = staged["ticker"]
    timeframe = tp.get("timeframe", "SWING")
    age = time.time() - staged.get("staged_at", 0)
    if age > APPROVAL_TTL_SECONDS.get(timeframe, 300):
        ledger.log_audit(ticker, "EXPIRED", f"Approval too old ({int(age)}s)", timeframe)
        raise HTTPException(status_code=410, detail="Approval expired. Re-run the audit.")
    try:
        if check_existing_position(ticker):
            ledger.log_audit(ticker, "REJECTED", "Position opened while staging", timeframe)
            raise HTTPException(status_code=400, detail="Position opened while staging.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not verify positions: {exc}")
    key, secret = load_credentials()
    try:
        current_ask = fetch_latest_ask(ticker, key, secret)
        check_price_drift(Decimal(str(tp["entry"])), Decimal(str(tp["stop_loss"])), current_ask)
    except RuleReject as exc:
        ledger.log_audit(ticker, "STALE_PRICE", str(exc), timeframe)
        raise HTTPException(status_code=409, detail=str(exc))
    except PipelineAbort as exc:
        raise HTTPException(status_code=503, detail=f"Price re-check unavailable: {exc}")
    trading_client = TradingClient(key, secret, paper=True)
    entry, stop = Decimal(str(tp["entry"])), Decimal(str(tp["stop_loss"]))
    plan = BracketPlan(
        Decimal(str(tp["capital_required"])), Decimal(str(tp["max_risk_usd"])),
        entry, stop, Decimal(str(tp["take_profit"])), entry - stop, tp["shares"],
    )
    try:
        order_id = step5_execute(trading_client, ticker, plan, timeframe)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Broker execution failed: {exc}")
    try:
        ledger.log_execution(
            order_id=str(order_id), ticker=ticker, shares=tp["shares"],
            entry=tp["entry"], stop=tp["stop_loss"], target=tp["take_profit"],
            risk=tp["max_risk_usd"], timeframe=timeframe,
        )
    except Exception as exc:
        print(f"[-] ORDER {order_id} PLACED BUT LEDGER WRITE FAILED: {exc}")
    return {"status": "EXECUTED", "ticker": ticker, "order_id": str(order_id)}

@app.post("/reject/{token}")
def reject_order(token: str, x_shield_auth: str = Header(None)):
    require_auth(x_shield_auth)
    if token in PENDING_APPROVALS:
        PENDING_APPROVALS.pop(token)
        return {"status": "REJECTED"}
    return {"status": "NOT_FOUND"}

@app.post("/kill-switch")
def emergency_kill_switch(x_shield_auth: str = Header(None)):
    require_auth(x_shield_auth)
    key, secret = load_credentials()
    trading_client = TradingClient(key, secret, paper=True)
    trading_client.cancel_orders()
    trading_client.close_all_positions(cancel_orders=True)
    return {"status": "EMERGENCY_SHUTDOWN_EXECUTED"}
