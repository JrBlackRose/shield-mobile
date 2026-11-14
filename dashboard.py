#!/usr/bin/env python3
"""
dashboard.py -- Project Shield v3.0 Multi-User Standalone Mobile & Smartwatch App
- Multi-Tenant BYOK: Every user connects their OWN Alpaca, Gemini & Telegram API keys on their phone.
- Smartwatch HUD: Auto/Manual toggle, Instant Execution Feed, 1-Tap Kill Switch.
- Mobile App: Interactive Manual Approval Cards, On-Demand AI Headline Scanner, Microstructure Audit.
"""
import json, math, os, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
import requests, uvicorn
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, StopLossRequest, TakeProfitRequest
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, Response
from google import genai
from google.genai import types
from pydantic import BaseModel
from microstructure import calc_imbalance_and_micro, check_us_session, evaluate_catalyst_vs_tape, fetch_free_iex_snapshot, USMarketRegime
from paper_engine import MAX_SLIPPAGE_COLLAR_PCT, round_to_tick, step4_risk_math

load_dotenv()
NY_TZ, MY_TZ = ZoneInfo("America/New_York"), ZoneInfo("Asia/Kuala_Lumpur")
app = FastAPI(title="Project Shield v3.0 Multi-User App")

# Per-user in-memory session state (isolated by each user's API key hash)
USER_SESSIONS = {}

def get_user_session(alpaca_key: str) -> dict:
    uid = (alpaca_key or "default")[-8:]
    if uid not in USER_SESSIONS:
        USER_SESSIONS[uid] = {
            "mode": "AUTO",
            "last_event": "Account Connected & Armed",
            "updated_at": datetime.now(MY_TZ).strftime("%H:%M:%S MYT"),
            "pending_cards": [],
            "exec_feed": [],
        }
    return USER_SESSIONS[uid]

def record_event(sess: dict, msg: str, tg_token: str = "", tg_chat: str = ""):
    ts = datetime.now(MY_TZ).strftime("%H:%M:%S")
    sess["last_event"] = f"[{ts}] {msg}"
    sess["updated_at"] = f"{ts} MYT"
    sess["exec_feed"].insert(0, {"time": ts, "msg": msg})
    sess["exec_feed"] = sess["exec_feed"][:15]
    if tg_token and tg_chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage", json={"chat_id": tg_chat, "text": f"🛡️ Project Shield: {msg}"}, timeout=4)
        except Exception:
            pass

def resolve_keys(k: str, s: str, g: str, t_tok: str, t_chat: str):
    ak = k.strip() or os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or ""
    as_ = s.strip() or os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or ""
    gk = g.strip() or os.getenv("GEMINI_API_KEY") or ""
    tt = t_tok.strip() or os.getenv("TELEGRAM_BOT_TOKEN") or ""
    tc = t_chat.strip() or os.getenv("TELEGRAM_CHAT_ID") or ""
    if not ak or not as_:
        raise HTTPException(status_code=401, detail="Please tap [🔐 Connect API] to enter your Alpaca API Key & Secret.")
    return ak, as_, gk, tt, tc

def execute_collared_bracket(ak: str, as_: str, ticker: str, timeframe: str, force_offhours: bool = False) -> dict:
    tc = TradingClient(api_key=ak, secret_key=as_, paper=True)
    acct = tc.get_account()
    equity = Decimal(str(acct.equity or "100000"))

    # Get live quote or fallback reference price if testing off-hours
    ask_px = Decimal("0")
    spread_bps, imb, micro = 0.0, 0.0, 0.0
    try:
        snap = fetch_free_iex_snapshot(ticker, ak, as_)
        ask_px = Decimal(str(snap.ask_price))
        spread_bps, imb, micro = snap.spread_bps, snap.imbalance, snap.micro_price
    except Exception:
        if not force_offhours:
            raise HTTPException(status_code=400, detail=f"Market closed or no live IEX quote for {ticker}. Enable 'Allow Off-Hours Staging' to test.")
        dc = StockHistoricalDataClient(api_key=ak, secret_key=as_)
        q = dc.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker)).get(ticker)
        px = float(q.ask_price or q.bid_price or 150.0) if q else 150.0
        ask_px = Decimal(str(px if px > 0 else 150.0))
        micro = float(ask_px)

    plan = step4_risk_math(equity, ask_px, timeframe=timeframe)
    limit_buy = round_to_tick(plan.entry * (Decimal("1") + MAX_SLIPPAGE_COLLAR_PCT))
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "shares": int(plan.shares),
        "entry": float(plan.entry),
        "limit_collar": float(limit_buy),
        "stop_loss": float(plan.stop_loss),
        "take_profit": float(plan.take_profit),
        "max_risk": round(float((plan.entry - plan.stop_loss) * Decimal(str(plan.shares))), 2),
        "micro_price": round(micro, 4),
        "imbalance": round(imb, 2),
        "spread_bps": round(spread_bps, 2),
    }

def submit_alpaca_bracket(ak: str, as_: str, plan_dict: dict) -> str:
    tc = TradingClient(api_key=ak, secret_key=as_, paper=True)
    req = LimitOrderRequest(
        symbol=plan_dict["ticker"],
        qty=plan_dict["shares"],
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY if plan_dict["timeframe"] == "INTRADAY" else TimeInForce.GTC,
        limit_price=plan_dict["limit_collar"],
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=plan_dict["take_profit"]),
        stop_loss=StopLossRequest(stop_price=plan_dict["stop_loss"]),
    )
    order = tc.submit_order(order_data=req)
    return str(order.id)

class ModeReq(BaseModel):
    mode: str

class TradeReq(BaseModel):
    ticker: str
    timeframe: str = "SWING"
    force_offhours: bool = True

class NewsSimReq(BaseModel):
    headline: str
    force_offhours: bool = True

@app.get("/manifest.json")
def manifest():
    return {"name": "Project Shield Mobile", "short_name": "Shield", "start_url": "/", "display": "standalone", "background_color": "#020617", "theme_color": "#10b981", "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}

@app.get("/icon.svg")
def icon():
    return Response(content='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="#020617"/><path d="M50 12 L86 26 V50 C86 74 70 90 50 96 C30 90 14 74 14 50 V26 Z" fill="#10b981" fill-opacity="0.25" stroke="#10b981" stroke-width="6"/></svg>', media_type="image/svg+xml")

@app.get("/api/status")
def status(
    x_alpaca_key: str = Header(default=""),
    x_alpaca_secret: str = Header(default=""),
    x_gemini_key: str = Header(default=""),
    x_tg_token: str = Header(default=""),
    x_tg_chat: str = Header(default=""),
):
    now = datetime.now(timezone.utc)
    sess_ok, sess_msg = check_us_session(now, "INTRADAY")
    try:
        ak, as_, _, _, _ = resolve_keys(x_alpaca_key, x_alpaca_secret, x_gemini_key, x_tg_token, x_tg_chat)
    except HTTPException:
        return {"connected": False, "ny_time": now.astimezone(NY_TZ).strftime("%H:%M:%S %Z"), "my_time": now.astimezone(MY_TZ).strftime("%H:%M:%S MYT"), "session_open": sess_ok, "session_msg": sess_msg}

    user_sess = get_user_session(ak)
    acct_info = {"equity": 0.0, "buying_power": 0.0, "daily_pnl": 0.0, "daily_pnl_pct": 0.0}
    pos_list, ord_list = [], []
    try:
        tc = TradingClient(api_key=ak, secret_key=as_, paper=True)
        a = tc.get_account()
        eq, leq = float(a.equity or 0), float(a.last_equity or a.equity or 1)
        pnl = eq - leq
        acct_info = {"equity": round(eq, 2), "buying_power": round(float(a.buying_power or 0), 2), "daily_pnl": round(pnl, 2), "daily_pnl_pct": round(pnl / leq * 100, 2)}
        for p in tc.get_all_positions():
            pos_list.append({"symbol": p.symbol, "qty": str(p.qty), "entry": round(float(p.avg_entry_price or 0), 2), "pnl": round(float(p.unrealized_pl or 0), 2)})
        for o in tc.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))[:10]:
            ord_list.append({"symbol": o.symbol, "side": str(o.side).split(".")[-1], "qty": str(o.qty), "limit_price": str(o.limit_price or "MKT"), "status": str(o.status).split(".")[-1]})
    except Exception as exc:
        return {"connected": False, "error": f"Invalid Alpaca Keys: {exc}", "ny_time": now.astimezone(NY_TZ).strftime("%H:%M:%S %Z"), "my_time": now.astimezone(MY_TZ).strftime("%H:%M:%S MYT")}

    return {
        "connected": True,
        "key_tail": ak[-4:],
        "ny_time": now.astimezone(NY_TZ).strftime("%H:%M:%S %Z"),
        "my_time": now.astimezone(MY_TZ).strftime("%H:%M:%S MYT"),
        "session_open": sess_ok,
        "session_msg": sess_msg,
        "engine_state": user_sess,
        "account": acct_info,
        "positions": pos_list,
        "open_orders": ord_list,
    }

@app.post("/api/mode")
def flip_mode(
    p: ModeReq,
    x_alpaca_key: str = Header(default=""),
    x_alpaca_secret: str = Header(default=""),
    x_gemini_key: str = Header(default=""),
    x_tg_token: str = Header(default=""),
    x_tg_chat: str = Header(default=""),
):
    ak, _, _, tt, tc = resolve_keys(x_alpaca_key, x_alpaca_secret, x_gemini_key, x_tg_token, x_tg_chat)
    sess = get_user_session(ak)
    sess["mode"] = "AUTO" if p.mode.upper() == "AUTO" else "MANUAL"
    record_event(sess, f"Switched to {sess['mode']} Mode", tt, tc)
    return sess

@app.post("/api/trade")
def trigger_trade(
    p: TradeReq,
    x_alpaca_key: str = Header(default=""),
    x_alpaca_secret: str = Header(default=""),
    x_gemini_key: str = Header(default=""),
    x_tg_token: str = Header(default=""),
    x_tg_chat: str = Header(default=""),
):
    ak, as_, _, tt, tc = resolve_keys(x_alpaca_key, x_alpaca_secret, x_gemini_key, x_tg_token, x_tg_chat)
    sess = get_user_session(ak)
    sym = p.ticker.strip().upper()
    plan = execute_collared_bracket(ak, as_, sym, p.timeframe, p.force_offhours)

    if sess["mode"] == "AUTO":
        oid = submit_alpaca_bracket(ak, as_, plan)
        record_event(sess, f"⚡ AUTO EXECUTED {sym}: {plan['shares']} sh @ Collar <= ${plan['limit_collar']} (Stop ${plan['stop_loss']})", tt, tc)
        return {"status": "AUTO_EXECUTED", "order_id": oid, "plan": plan}
    else:
        token = str(uuid.uuid4())[:8]
        plan["token"] = token
        plan["source"] = "Manual Trigger"
        sess["pending_cards"].insert(0, plan)
        record_event(sess, f"📱 STAGED FOR MANUAL APPROVAL: {sym} ({plan['shares']} sh @ ${plan['limit_collar']})", tt, tc)
        return {"status": "STAGED_FOR_APPROVAL", "token": token, "plan": plan}

@app.post("/api/ai-scan")
def ai_headline_scan(
    p: NewsSimReq,
    x_alpaca_key: str = Header(default=""),
    x_alpaca_secret: str = Header(default=""),
    x_gemini_key: str = Header(default=""),
    x_tg_token: str = Header(default=""),
    x_tg_chat: str = Header(default=""),
):
    ak, as_, gk, tt, tc = resolve_keys(x_alpaca_key, x_alpaca_secret, x_gemini_key, x_tg_token, x_tg_chat)
    if not gk:
        raise HTTPException(status_code=400, detail="Please enter your Gemini API Key in [🔐 Connect API].")
    sess = get_user_session(ak)
    gclient = genai.Client(api_key=gk)
    prompt = f"""Analyze this breaking news headline. Determine if it is a severe bullish macro/supply/contract catalyst for a specific US stock.
Return raw JSON only: {{"trade": true/false, "ticker": "SYMBOL_OR_NONE", "source_tier": 1|2|3, "sentiment": 0.0_to_1.0, "reason": "short explanation"}}
Headline: {p.headline}"""
    resp = gclient.models.generate_content(model="gemini-2.5-flash", contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
    dec = json.loads(resp.text)
    if not dec.get("trade") or dec.get("ticker", "NONE") == "NONE":
        record_event(sess, f"🧠 AI Filtered Noise: {dec.get('reason', 'No trade')}", tt, tc)
        return {"ai_decision": dec, "action": "FILTERED_NO_TRADE"}

    sym = dec["ticker"].strip().upper()
    plan = execute_collared_bracket(ak, as_, sym, "SWING", p.force_offhours)
    if sess["mode"] == "AUTO":
        oid = submit_alpaca_bracket(ak, as_, plan)
        record_event(sess, f"🤖 AI AUTO-EXECUTED {sym} (Sent={dec.get('sentiment')}): {plan['shares']} sh @ <=${plan['limit_collar']}", tt, tc)
        return {"ai_decision": dec, "action": "AUTO_EXECUTED", "order_id": oid, "plan": plan}
    else:
        token = str(uuid.uuid4())[:8]
        plan["token"] = token
        plan["source"] = f"AI Shock ({dec.get('reason', '')[:45]})"
        sess["pending_cards"].insert(0, plan)
        record_event(sess, f"📱 AI STAGED {sym} -> Waiting in Mobile App for Manual Approval!", tt, tc)
        return {"ai_decision": dec, "action": "STAGED_FOR_MANUAL_APPROVAL", "plan": plan}

@app.post("/api/card/{token}/{action}")
def handle_card(
    token: str,
    action: str,
    x_alpaca_key: str = Header(default=""),
    x_alpaca_secret: str = Header(default=""),
    x_gemini_key: str = Header(default=""),
    x_tg_token: str = Header(default=""),
    x_tg_chat: str = Header(default=""),
):
    ak, as_, _, tt, tc = resolve_keys(x_alpaca_key, x_alpaca_secret, x_gemini_key, x_tg_token, x_tg_chat)
    sess = get_user_session(ak)
    card = next((c for c in sess["pending_cards"] if c["token"] == token), None)
    if not card:
        raise HTTPException(status_code=404, detail="Card already processed or expired.")
    sess["pending_cards"] = [c for c in sess["pending_cards"] if c["token"] != token]
    if action == "approve":
        oid = submit_alpaca_bracket(ak, as_, card)
        record_event(sess, f"✅ APPROVED & EXECUTED {card['ticker']}: {card['shares']} sh @ <=${card['limit_collar']}", tt, tc)
        return {"status": "EXECUTED", "order_id": oid}
    else:
        record_event(sess, f"❌ REJECTED {card['ticker']} manual card.", tt, tc)
        return {"status": "REJECTED"}

@app.post("/api/kill")
def kill_switch(
    x_alpaca_key: str = Header(default=""),
    x_alpaca_secret: str = Header(default=""),
    x_gemini_key: str = Header(default=""),
    x_tg_token: str = Header(default=""),
    x_tg_chat: str = Header(default=""),
):
    ak, as_, _, tt, tc_id = resolve_keys(x_alpaca_key, x_alpaca_secret, x_gemini_key, x_tg_token, x_tg_chat)
    sess = get_user_session(ak)
    tc = TradingClient(api_key=ak, secret_key=as_, paper=True)
    tc.cancel_orders()
    tc.close_all_positions(cancel_orders=True)
    sess["mode"] = "MANUAL"
    sess["pending_cards"] = []
    record_event(sess, "🚨 KILL-SWITCH: All orders canceled, positions flattened & switched to MANUAL!", tt, tc_id)
    return {"status": "FLATTENED_AND_MANUAL"}

@app.get("/", response_class=HTMLResponse)
def ui():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg"><title>Project Shield v3.0</title><script src="https://cdn.tailwindcss.com"></script></head>
<body class="bg-slate-950 text-slate-100 min-h-screen font-sans">
<!-- Top Nav -->
<div class="bg-slate-900 border-b border-slate-800 px-3 py-2.5 flex justify-between items-center sticky top-0 z-50">
  <div class="flex items-center gap-2">
    <span class="text-emerald-400 font-extrabold text-sm sm:text-base">🛡️ SHIELD v3.0</span>
    <span id="acct-pill" class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-300">No Keys</span>
    <span id="mode-pill" class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300">AUTO</span>
  </div>
  <div class="flex items-center gap-1.5">
    <button onclick="toggleKeys()" class="bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-bold px-2.5 py-1.5 rounded-lg">🔐 Connect API</button>
    <button onclick="toggleWatch()" id="wbtn" class="bg-slate-800 border border-slate-700 text-xs px-2.5 py-1.5 rounded-lg">⌚ Watch HUD</button>
  </div>
</div>

<!-- 1. API KEY VAULT MODAL (Stored locally on each friend's phone!) -->
<div id="key-modal" class="hidden fixed inset-0 bg-black/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
  <div class="bg-slate-900 border border-slate-700 rounded-2xl max-w-md w-full p-5 space-y-3 shadow-2xl">
    <div class="flex justify-between items-center border-b border-slate-800 pb-2">
      <h2 class="font-bold text-base text-emerald-400">🔐 Connect Your Own Accounts</h2>
      <button onclick="toggleKeys()" class="text-slate-400 text-sm">✕</button>
    </div>
    <p class="text-[11px] text-slate-400">Keys are stored <b>only on your own device</b>. Every user trades their own Alpaca Paper account independently.</p>
    <div><label class="text-xs text-slate-300">Alpaca API Key ID *</label><input id="k-ak" class="w-full mt-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono" placeholder="PK..."></div>
    <div><label class="text-xs text-slate-300">Alpaca Secret Key *</label><input id="k-as" type="password" class="w-full mt-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono" placeholder="Your Alpaca Secret"></div>
    <div><label class="text-xs text-slate-300">Gemini API Key (For AI News Scanner)</label><input id="k-gk" type="password" class="w-full mt-1 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs font-mono" placeholder="AIza..."></div>
    <div class="grid grid-cols-2 gap-2">
      <div><label class="text-[11px] text-slate-400">Telegram Bot Token (Opt)</label><input id="k-tt" type="password" class="w-full mt-1 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1.5 text-xs font-mono"></div>
      <div><label class="text-[11px] text-slate-400">Telegram Chat ID (Opt)</label><input id="k-tc" class="w-full mt-1 bg-slate-950 border border-slate-700 rounded-lg px-2 py-1.5 text-xs font-mono"></div>
    </div>
    <div class="flex gap-2 pt-2">
      <button onclick="saveKeys()" class="flex-1 bg-emerald-600 hover:bg-emerald-500 text-white font-bold py-2.5 rounded-xl text-xs">Save & Connect My Account</button>
      <button onclick="clearKeys()" class="bg-slate-800 text-slate-400 px-3 py-2.5 rounded-xl text-xs">Clear</button>
    </div>
  </div>
</div>

<!-- 2. SMARTWATCH WRIST HUD (Friend's Exact Watch Spec) -->
<div id="wview" class="hidden flex-col items-center p-3.5 max-w-[330px] mx-auto mt-3 space-y-2.5 bg-black border-2 border-slate-800 rounded-[40px] shadow-2xl">
  <div id="wclock" class="text-[11px] font-mono text-slate-400">MYT --:--:--</div>
  <div class="text-center">
    <div class="text-[10px] text-slate-500 uppercase">My Account Equity</div>
    <div id="weq" class="text-2xl font-extrabold text-white">$0.00</div>
    <div id="wpnl" class="text-xs font-bold text-emerald-400">+$0.00 (0.00%)</div>
  </div>
  <!-- Watch Mode Switch -->
  <button onclick="flipMode()" id="wmbtn" class="w-full py-3 rounded-2xl font-bold text-xs bg-emerald-600/30 border border-emerald-500 text-emerald-200 shadow-lg">
    <div id="wmtxt" class="text-sm font-extrabold">🤖 AUTO EXECUTION ON</div>
    <div class="text-[10px] opacity-80 mt-0.5">Tap to switch to MANUAL (Mobile App)</div>
  </button>
  <!-- Watch Instant Update Feed -->
  <div class="w-full bg-slate-900 border border-slate-800 rounded-2xl p-2 text-left space-y-1">
    <div class="text-[9px] text-slate-400 uppercase text-center">⚡ Instant Watch Execution Feed</div>
    <div id="wfeed" class="text-[10px] font-mono text-sky-300 space-y-1 max-h-24 overflow-y-auto"></div>
  </div>
  <!-- Watch 1-Tap Kill Switch -->
  <button onclick="killAll()" class="w-full bg-red-600 active:bg-red-700 text-white font-extrabold py-3 rounded-2xl text-xs shadow-lg">
    🚨 KILL SWITCH & FLATTEN
  </button>
  <button onclick="toggleWatch()" class="text-[10px] text-slate-500 underline">Switch to Full Mobile Screen</button>
</div>

<!-- 3. FULL MOBILE & TABLET APPLICATION -->
<div id="fview" class="max-w-6xl mx-auto p-3 sm:p-5 space-y-4">
  <!-- Mode & Kill Banner -->
  <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl flex flex-wrap justify-between items-center gap-3">
    <div>
      <div id="spill" class="text-xs font-bold text-amber-300">Checking US Session...</div>
      <div id="evtpill" class="text-xs font-mono text-sky-300 mt-1">⚡ Ready</div>
      <div id="clocks" class="text-[11px] text-slate-400 font-mono mt-0.5"></div>
    </div>
    <div class="flex gap-2 w-full sm:w-auto">
      <button onclick="flipMode()" id="mbtn" class="flex-1 bg-emerald-600 text-white font-bold px-4 py-2.5 rounded-xl text-xs shadow">🤖 Mode: AUTO (Tap for MANUAL)</button>
      <button onclick="killAll()" class="flex-1 bg-red-600 text-white font-bold px-4 py-2.5 rounded-xl text-xs shadow">🚨 KILL SWITCH</button>
    </div>
  </div>

  <!-- MANUAL APPROVAL CARDS INBOX (Appears when in MANUAL Mode!) -->
  <div id="cards-wrap" class="hidden bg-sky-950/40 border-2 border-sky-500/60 p-4 rounded-2xl space-y-3">
    <div class="text-xs font-extrabold uppercase text-sky-300">📱 Pending Manual Trade Approvals (Review & Tap to Execute)</div>
    <div id="cards-list" class="grid grid-cols-1 md:grid-cols-2 gap-3"></div>
  </div>

  <!-- 4 Portfolio Metrics -->
  <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
    <div class="bg-slate-900 border border-slate-800 p-3.5 rounded-2xl"><div class="text-[11px] text-slate-400">MY EQUITY</div><div id="eq" class="text-xl font-bold mt-0.5">$0</div></div>
    <div class="bg-slate-900 border border-slate-800 p-3.5 rounded-2xl"><div class="text-[11px] text-slate-400">BUYING POWER</div><div id="bp" class="text-xl font-bold text-sky-400 mt-0.5">$0</div></div>
    <div class="bg-slate-900 border border-slate-800 p-3.5 rounded-2xl"><div class="text-[11px] text-slate-400">TODAY P&L</div><div id="pnl" class="text-xl font-bold mt-0.5">$0</div></div>
    <div class="bg-slate-900 border border-slate-800 p-3.5 rounded-2xl"><div class="text-[11px] text-slate-400">PRECISION COLLAR</div><div class="text-xs font-bold text-emerald-400 mt-1">✅ +15 bps Limit Collar<br>✅ 1% Risk / 15% Cap</div></div>
  </div>

  <!-- Actions: Direct Trade + Live AI News Shock Scanner -->
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl space-y-3">
      <div class="font-bold text-sm border-b border-slate-800 pb-1.5">🚀 1-Click Precision Trade (Obeys AUTO vs MANUAL Mode)</div>
      <div class="grid grid-cols-2 gap-2">
        <input id="tk" value="NVDA" class="bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 font-mono uppercase text-sm" placeholder="Ticker">
        <select id="tf" class="bg-slate-950 border border-slate-700 rounded-xl px-2 py-2 text-xs"><option value="SWING">SWING (GTC Bracket)</option><option value="INTRADAY">INTRADAY (Day Bracket)</option></select>
      </div>
      <button onclick="fireTrade()" class="w-full bg-emerald-600 hover:bg-emerald-500 font-bold py-2.5 rounded-xl text-xs sm:text-sm">Calculate Risk & Route Trade</button>
      <div id="tres" class="text-xs font-mono text-slate-300 bg-slate-950 p-2.5 rounded-xl hidden break-all"></div>
    </div>

    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl space-y-3">
      <div class="font-bold text-sm border-b border-slate-800 pb-1.5">🧠 Live Gemini AI Catalyst Scanner (Test AUTO vs MANUAL)</div>
      <input id="news-txt" value="BREAKING: Drone attack halts largest Saudi crude facility, removing 5M barrels/day" class="w-full bg-slate-950 border border-slate-700 rounded-xl px-3 py-2 text-xs">
      <button onclick="scanNews()" class="w-full bg-indigo-600 hover:bg-indigo-500 font-bold py-2.5 rounded-xl text-xs sm:text-sm">Evaluate with Gemini 2.5 Flash & Route</button>
      <div id="nres" class="text-xs font-mono text-slate-300 bg-slate-950 p-2.5 rounded-xl hidden break-all"></div>
    </div>
  </div>

  <!-- Positions/Orders & Instant Execution Feed -->
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl"><div class="font-bold text-sm mb-2">📂 My Open Positions & Collared Orders</div><div id="obox" class="text-xs font-mono space-y-1.5 text-slate-300"></div></div>
    <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl"><div class="font-bold text-sm mb-2">⚡ Instant Automation & Mode Feed</div><div id="fbox" class="text-xs font-mono space-y-1.5 text-sky-300 max-h-52 overflow-y-auto"></div></div>
  </div>
</div>

<script>
let mode='AUTO', watch=false;
function hdrs(){
  return {
    'Content-Type':'application/json',
    'x-alpaca-key':localStorage.getItem('sh_ak')||'',
    'x-alpaca-secret':localStorage.getItem('sh_as')||'',
    'x-gemini-key':localStorage.getItem('sh_gk')||'',
    'x-tg-token':localStorage.getItem('sh_tt')||'',
    'x-tg-chat':localStorage.getItem('sh_tc')||''
  };
}
function toggleKeys(){
  const m=document.getElementById('key-modal');
  document.getElementById('k-ak').value=localStorage.getItem('sh_ak')||'';
  document.getElementById('k-as').value=localStorage.getItem('sh_as')||'';
  document.getElementById('k-gk').value=localStorage.getItem('sh_gk')||'';
  document.getElementById('k-tt').value=localStorage.getItem('sh_tt')||'';
  document.getElementById('k-tc').value=localStorage.getItem('sh_tc')||'';
  m.classList.toggle('hidden');
}
function saveKeys(){
  localStorage.setItem('sh_ak',document.getElementById('k-ak').value.trim());
  localStorage.setItem('sh_as',document.getElementById('k-as').value.trim());
  localStorage.setItem('sh_gk',document.getElementById('k-gk').value.trim());
  localStorage.setItem('sh_tt',document.getElementById('k-tt').value.trim());
  localStorage.setItem('sh_tc',document.getElementById('k-tc').value.trim());
  document.getElementById('key-modal').classList.add('hidden');
  poll();
}
function clearKeys(){ localStorage.clear(); toggleKeys(); poll(); }
function toggleWatch(){
  watch=!watch;
  document.getElementById('wview').className=watch?'flex flex-col items-center p-3.5 max-w-[330px] mx-auto mt-3 space-y-2.5 bg-black border-2 border-slate-800 rounded-[40px] shadow-2xl':'hidden';
  document.getElementById('fview').className=watch?'hidden':'max-w-6xl mx-auto p-3 sm:p-5 space-y-4';
  document.getElementById('wbtn').innerText=watch?'📱 Mobile App':'⌚ Watch HUD';
}
async function poll(){
  try{
    const d=await (await fetch('/api/status',{headers:hdrs()})).json();
    document.getElementById('clocks').innerText=`MYT: ${d.my_time} | EDT: ${d.ny_time}`;
    document.getElementById('wclock').innerText=d.my_time;
    document.getElementById('spill').innerText=d.session_open?'🟢 US MARKET OPEN':'⏸️ '+d.session_msg;
    if(!d.connected){
      document.getElementById('acct-pill').innerText='Tap Connect API';
      return;
    }
    document.getElementById('acct-pill').innerText='Key ..'+d.key_tail;
    const st=d.engine_state;
    mode=st.mode;
    document.getElementById('mode-pill').innerText=mode;
    document.getElementById('evtpill').innerText='⚡ '+st.last_event;
    document.getElementById('mbtn').innerText=mode==='AUTO'?'🤖 Mode: AUTO (Tap for MANUAL)':'📱 Mode: MANUAL (Tap for AUTO)';
    document.getElementById('mbtn').className=mode==='AUTO'?'flex-1 bg-emerald-600 text-white font-bold px-4 py-2.5 rounded-xl text-xs shadow':'flex-1 bg-sky-600 text-white font-bold px-4 py-2.5 rounded-xl text-xs shadow';
    document.getElementById('wmtxt').innerText=mode==='AUTO'?'🤖 AUTO EXECUTION ON':'📱 MANUAL PHONE MODE';
    document.getElementById('wmbtn').className=mode==='AUTO'?'w-full py-3 rounded-2xl font-bold text-xs bg-emerald-600/30 border border-emerald-500 text-emerald-200':'w-full py-3 rounded-2xl font-bold text-xs bg-sky-600/30 border-sky-500 text-sky-200';

    const a=d.account, s=a.daily_pnl>=0?'+':'';
    document.getElementById('eq').innerText='$'+(a.equity||0).toLocaleString();
    document.getElementById('weq').innerText='$'+(a.equity||0).toLocaleString();
    document.getElementById('bp').innerText='$'+(a.buying_power||0).toLocaleString();
    const pstr=`${s}$${(a.daily_pnl||0).toLocaleString()} (${s}${a.daily_pnl_pct||0}%)`;
    document.getElementById('pnl').innerText=pstr; document.getElementById('wpnl').innerText=pstr;

    // Pending Manual Approval Cards
    const cw=document.getElementById('cards-wrap'), cl=document.getElementById('cards-list');
    if(st.pending_cards && st.pending_cards.length>0){
      cw.classList.remove('hidden');
      cl.innerHTML=st.pending_cards.map(c=>`<div class="bg-slate-900 border border-sky-500/50 p-3 rounded-xl space-y-2">
        <div class="flex justify-between font-bold text-sm"><span>📈 ${c.ticker} (${c.timeframe})</span><span class="text-emerald-400">${c.shares} shares</span></div>
        <div class="text-[11px] font-mono text-slate-300">Collar Buy <= $${c.limit_collar} | Stop: $${c.stop_loss} | Target: $${c.take_profit}<br>Max Risk: $${c.max_risk} | Source: ${c.source}</div>
        <div class="flex gap-2 pt-1">
          <button onclick="actCard('${c.token}','approve')" class="flex-1 bg-emerald-600 font-bold py-1.5 rounded-lg text-xs">✅ Approve Fill</button>
          <button onclick="actCard('${c.token}','reject')" class="flex-1 bg-slate-800 text-red-400 font-bold py-1.5 rounded-lg text-xs">❌ Reject</button>
        </div>
      </div>`).join('');
    } else { cw.classList.add('hidden'); }

    const feedHtml=(st.exec_feed||[]).map(f=>`<div class="p-1.5 bg-slate-950 rounded border border-slate-800">[${f.time}] ${f.msg}</div>`).join('')||'<div class="text-slate-500">No events yet.</div>';
    document.getElementById('fbox').innerHTML=feedHtml;
    document.getElementById('wfeed').innerHTML=feedHtml;

    document.getElementById('obox').innerHTML=(d.positions.map(p=>`<div class="p-1.5 bg-slate-950 rounded border border-slate-800">🟢 POS: <b>${p.symbol}</b> (${p.qty} sh @ $${p.entry}) | P&L: $${p.pnl}</div>`).join('')+d.open_orders.map(o=>`<div class="p-1.5 bg-slate-950 rounded border border-slate-800">⏳ ORD: <b>${o.symbol}</b> ${o.side} ${o.qty} @ Limit $${o.limit_price} (${o.status})</div>`).join(''))||'<div class="text-slate-500">Account is flat.</div>';
  }catch(e){}
}
async function flipMode(){ await fetch('/api/mode',{method:'POST',headers:hdrs(),body:JSON.stringify({mode:mode==='AUTO'?'MANUAL':'AUTO'})}); poll(); }
async function fireTrade(){ const b=document.getElementById('tres'); b.classList.remove('hidden'); b.innerText='Calculating 1% risk & collared bracket...'; const r=await fetch('/api/trade',{method:'POST',headers:hdrs(),body:JSON.stringify({ticker:document.getElementById('tk').value,timeframe:document.getElementById('tf').value,force_offhours:true})}); b.innerText=JSON.stringify(await r.json()); poll(); }
async function scanNews(){ const b=document.getElementById('nres'); b.classList.remove('hidden'); b.innerText='Gemini 2.5 Flash analyzing catalyst...'; const r=await fetch('/api/ai-scan',{method:'POST',headers:hdrs(),body:JSON.stringify({headline:document.getElementById('news-txt').value,force_offhours:true})}); b.innerText=JSON.stringify(await r.json()); poll(); }
async function actCard(tok,act){ await fetch(`/api/card/${tok}/${act}`,{method:'POST',headers:hdrs()}); poll(); }
async function killAll(){ if(!confirm('EMERGENCY KILL-SWITCH: Cancel all open orders, flatten positions & return to MANUAL mode?'))return; await fetch('/api/kill',{method:'POST',headers:hdrs()}); poll(); }
poll(); setInterval(poll,3000);
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
