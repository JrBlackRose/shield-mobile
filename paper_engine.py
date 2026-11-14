#!/usr/bin/env python3
"""
paper_engine.py -- PROJECT SHIELD
Modular 5-step quantitative engine supporting both CLI execution and FastAPI imports.
"""

import argparse
import math
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import requests
from dotenv import load_dotenv
from edgar import Company, set_identity

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from risk_gates import check_order_allowed, new_client_order_id

EDGAR_IDENTITY = "ProjectShield allendraa.anbalagan@gmail.com"
CORPORATE_ACTIONS_URL = "https://data.alpaca.markets/v1/corporate-actions"
HTTP_TIMEOUT = (5, 20)
MAX_CORP_ACTION_PAGES = 20

DILUTION_LIMIT = Decimal("0.15")
QUARTER_MIN_DAYS, QUARTER_MAX_DAYS = 75, 110
PRIOR_YEAR_TOLERANCE_DAYS = 25
PERIOD_END_TOLERANCE_DAYS = 3

WEIGHTED_SHARE_CONCEPTS = (
    ("Basic", r"WeightedAverageNumberOfSharesOutstandingBasic$"),
    ("BasicAndDiluted", r"WeightedAverageNumberOfSharesOutstandingBasicAndDiluted$"),
    ("Diluted", r"WeightedAverageNumberOfDilutedSharesOutstanding$"),
)

NEWS_HEADLINE_COUNT = 3
NEWS_MAX_AGE_DAYS = 7
NEWS_LOOKBACK_DAYS = 365
FORM_8K_LOOKBACK_DAYS = 14
FORM_4_LOOKBACK_DAYS = 30

RISK_PCT = Decimal("0.01")
STOP_PCT = Decimal("0.10")
TARGET_PCT = Decimal("0.30")
INTRADAY_STOP_PCT = Decimal("0.01")
INTRADAY_TARGET_PCT = Decimal("0.03")
MAX_POSITION_PCT = Decimal("0.15")          # hard cap: one position <= 15% of equity

# Companies at/above this estimated market cap generate constant organic news,
# so "news without a fresh 8-K" is not evidence of hype for them.
LARGE_CAP_MARKET_CAP = Decimal("10000000000")

# On approval, the live ask may differ from the staged entry by at most this
# fraction of the stop distance (swing 10% stop -> 2% ; intraday 1% stop -> 0.2%).
MAX_DRIFT_FRACTION_OF_STOP = Decimal("0.20")
TICK_AT_OR_ABOVE_1 = Decimal("0.01")
TICK_BELOW_1 = Decimal("0.0001")

TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class PipelineAbort(Exception):
    """Raised when an unrecoverable data/pricing error occurs."""


class RuleReject(Exception):
    """Raised when a risk or regulatory rule safely blocks the trade."""


@dataclass(frozen=True)
class BracketPlan:
    equity: Decimal
    max_risk: Decimal
    entry: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    risk_per_share: Decimal
    shares: int
    client_order_id: str | None = None

    @property
    def position_cost(self) -> Decimal:
        return self.entry * self.shares


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def ensure_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


def fmt_price(value: Decimal) -> str:
    return f"${value:,.2f}" if value >= 1 else f"${value:,.4f}"


def load_credentials() -> tuple[str, str]:
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        raise PipelineAbort("Missing Alpaca credentials in environment/.env")
    return key, secret


def find_10q_pair(company: Company) -> tuple[tuple, tuple]:
    try:
        filings = company.get_filings(form="10-Q", amendments=False)
        total = len(filings)
    except Exception as exc:
        raise PipelineAbort(f"Could not list 10-Q filings: {exc}") from exc

    candidates = []
    for index in range(min(total, 16)):
        filing = filings[index]
        filed = to_date(filing.filing_date)
        period_end = to_date(getattr(filing, "report_date", None)) or to_date(filing.period_of_report)
        if filed and period_end:
            candidates.append((period_end, filed, filing))

    if not candidates:
        raise PipelineAbort("No usable 10-Q filings found.")

    candidates.sort(key=lambda c: c[0], reverse=True)
    latest = candidates[0]
    one_year_before = latest[0] - timedelta(days=365)
    prior_pool = [c for c in candidates[1:] if abs((c[0] - one_year_before).days) <= PRIOR_YEAR_TOLERANCE_DAYS]
    if not prior_pool:
        raise PipelineAbort("No matching prior-year 10-Q found.")
    prior = min(prior_pool, key=lambda c: abs((c[0] - one_year_before).days))
    return latest, prior


def _is_dimensional(fact: dict) -> bool:
    if fact.get("is_dimensioned"):
        return True
    return any(k.startswith("dim_") and v not in (None, "") for k, v in fact.items())


def extract_weighted_shares(filing: Any, period_end: date) -> dict[str, Decimal]:
    try:
        xbrl = filing.xbrl()
    except Exception as exc:
        raise PipelineAbort(f"Could not parse XBRL: {exc}") from exc
    if xbrl is None:
        raise PipelineAbort("Filing carries no XBRL data.")

    found: dict[str, Decimal] = {}
    for label, pattern in WEIGHTED_SHARE_CONCEPTS:
        best: tuple[int, Decimal] | None = None
        for fact in xbrl.query().by_concept(pattern).execute():
            if _is_dimensional(fact):
                continue
            val = fact.get("numeric_value")
            start = to_date(fact.get("period_start"))
            end = to_date(fact.get("period_end"))
            if val is None or start is None or end is None or val <= 0:
                continue
            if abs((end - period_end).days) > PERIOD_END_TOLERANCE_DAYS:
                continue
            duration = (end - start).days
            if not QUARTER_MIN_DAYS <= duration <= QUARTER_MAX_DAYS:
                continue
            dist = abs(duration - 91)
            if best is None or dist < best[0]:
                best = (dist, Decimal(str(val)))
        if best is not None:
            found[label] = best[1]
    return found


def fetch_split_multiplier(ticker: str, start: date, end: date, key: str, secret: str) -> tuple[Decimal, list[dict]]:
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "accept": "application/json"}
    params: dict[str, Any] = {
        "symbols": ticker,
        "types": "forward_split,reverse_split",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "limit": 1000,
    }
    splits: list[dict] = []
    for _ in range(MAX_CORP_ACTION_PAGES):
        try:
            resp = requests.get(CORPORATE_ACTIONS_URL, headers=headers, params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise PipelineAbort(f"Could not verify corporate actions: {exc}") from exc

        actions = payload.get("corporate_actions") or {}
        splits.extend(actions.get("forward_splits") or [])
        splits.extend(actions.get("reverse_splits") or [])
        next_token = payload.get("next_page_token")
        if not next_token:
            break
        params["page_token"] = next_token
    else:
        raise PipelineAbort("Corporate actions pagination overflow.")

    multiplier = Decimal("1")
    for s in splits:
        new_rate = Decimal(str(s["new_rate"]))
        old_rate = Decimal(str(s["old_rate"]))
        if new_rate <= 0 or old_rate <= 0:
            raise PipelineAbort(f"Invalid split ratio: {s}")
        multiplier *= new_rate / old_rate
    return multiplier, splits


def step1_dilution_audit(company: Company, ticker: str, key: str, secret: str) -> dict:
    latest, prior = find_10q_pair(company)
    latest_period, latest_filed, latest_filing = latest
    prior_period, prior_filed, prior_filing = prior

    latest_shares = extract_weighted_shares(latest_filing, latest_period)
    prior_shares = extract_weighted_shares(prior_filing, prior_period)

    shared = [lbl for lbl, _ in WEIGHTED_SHARE_CONCEPTS if lbl in latest_shares and lbl in prior_shares]
    if not shared:
        raise PipelineAbort("No matching weighted-average share concept in both 10-Qs.")

    basis = shared[0]
    latest_count = latest_shares[basis]
    prior_count = prior_shares[basis]

    multiplier, splits = fetch_split_multiplier(ticker, prior_filed, latest_filed, key, secret)
    adjusted_prior = prior_count * multiplier
    if adjusted_prior <= 0:
        raise PipelineAbort("Split-adjusted prior share count is non-positive.")

    growth = (latest_count - adjusted_prior) / adjusted_prior
    if growth > DILUTION_LIMIT:
        raise RuleReject(f"Dilution Trap ({growth:+.2%} expansion exceeds 15% limit)")

    return {
        "status": "PASS",
        "dilution_pct": float(growth * 100),
        "split_multiplier": float(multiplier),
        "splits_detected": len(splits),
        "latest_weighted_shares": float(latest_count),
    }


def estimate_market_cap(weighted_shares: Decimal, ask: Decimal) -> Decimal:
    """Approximate market cap = latest weighted-average shares x current ask."""
    return weighted_shares * ask


def step2_truth_cross_examiner(
    company: Company, ticker: str, key: str, secret: str, market_cap: Decimal | None = None
) -> dict:
    client = NewsClient(api_key=key, secret_key=secret)
    request = NewsRequest(
        symbols=ticker,
        start=utc_now() - timedelta(days=NEWS_LOOKBACK_DAYS),
        limit=NEWS_HEADLINE_COUNT,
        sort="desc",
    )
    try:
        articles = list(client.get_news(request).data.get("news", []))
    except Exception as exc:
        raise PipelineAbort(f"News fetch failed: {exc}") from exc

    now = utc_now()
    recent_news_found = False
    if articles:
        newest = max(ensure_utc(a.created_at) for a in articles)
        recent_news_found = (now - newest) < timedelta(days=NEWS_MAX_AGE_DAYS)

    cutoff_8k = utc_now().date() - timedelta(days=FORM_8K_LOOKBACK_DAYS)
    cutoff_4 = utc_now().date() - timedelta(days=FORM_4_LOOKBACK_DAYS)

    try:
        f8k = company.get_filings(form="8-K", filing_date=f"{cutoff_8k.isoformat()}:", amendments=False)
        form_8k_count = len(f8k) if f8k else 0
        f4 = company.get_filings(form="4", filing_date=f"{cutoff_4.isoformat()}:", amendments=True)
        form_4_count = len(f4) if f4 else 0
    except Exception as exc:
        raise PipelineAbort(f"EDGAR verification failed: {exc}") from exc

    is_large_cap = market_cap is not None and market_cap >= LARGE_CAP_MARKET_CAP
    hype_signal = recent_news_found and form_8k_count == 0

    if hype_signal and not is_large_cap:
        cap_txt = "unknown" if market_cap is None else f"~${market_cap / Decimal('1e9'):,.2f}B"
        raise RuleReject(
            "Unverified Media Hype (Recent news headlines without official Form 8-K; "
            f"market cap {cap_txt} is below the ${LARGE_CAP_MARKET_CAP / Decimal('1e9'):,.0f}B large-cap exemption)"
        )

    return {
        "status": "PASS",
        "recent_news_found": recent_news_found,
        "form_8k_count": form_8k_count,
        "form_4_count": form_4_count,
        "large_cap_exempt": hype_signal and is_large_cap,
    }


def fetch_latest_ask(ticker: str, key: str, secret: str) -> Decimal:
    """Latest IEX ask for ticker. Raises PipelineAbort if unavailable (fail closed)."""
    data_client = StockHistoricalDataClient(api_key=key, secret_key=secret)
    try:
        quotes = data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
        )
    except Exception as exc:
        raise PipelineAbort(f"IEX quote query failed: {exc}") from exc

    quote = quotes.get(ticker)
    ask = float(quote.ask_price) if quote is not None and quote.ask_price else 0.0
    if ask <= 0:
        raise PipelineAbort(f"No valid IEX ask price for {ticker} (market closed or zero liquidity).")
    return Decimal(str(ask))


def step3_pricing_and_balance(ticker: str, key: str, secret: str) -> tuple[TradingClient, float, float]:
    trading_client = TradingClient(api_key=key, secret_key=secret, paper=True)
    try:
        account = trading_client.get_account()
    except Exception as exc:
        raise PipelineAbort(f"Account query failed: {exc}") from exc

    if account.trading_blocked or account.account_blocked:
        raise PipelineAbort("Alpaca account is blocked.")
    equity = float(account.equity or 0.0)
    if equity <= 0:
        raise PipelineAbort(f"Account equity must be positive: {equity}")

    ask = float(fetch_latest_ask(ticker, key, secret))

    return trading_client, equity, ask


def round_to_tick(price: Decimal) -> Decimal:
    tick = TICK_AT_OR_ABOVE_1 if price >= Decimal("1.00") else TICK_BELOW_1
    return (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def step4_risk_math(equity: float, ask: float, timeframe: str) -> BracketPlan:
    stop_pct = INTRADAY_STOP_PCT if timeframe == "INTRADAY" else STOP_PCT
    target_pct = INTRADAY_TARGET_PCT if timeframe == "INTRADAY" else TARGET_PCT

    equity_d = Decimal(str(equity))
    entry = Decimal(str(ask))
    if equity_d <= 0 or entry <= 0:
        raise PipelineAbort("Equity and entry price must be positive.")

    max_risk = equity_d * RISK_PCT
    stop_loss = round_to_tick(entry * (Decimal("1") - stop_pct))
    take_profit = round_to_tick(entry * (Decimal("1") + target_pct))
    risk_per_share = entry - stop_loss
    if risk_per_share <= 0:
        raise PipelineAbort("Invalid stop loss price.")

    # Size by the smaller of: 1%-equity risk budget, and the 15%-of-equity notional cap.
    risk_shares = math.floor(max_risk / risk_per_share)
    cap_shares = math.floor(equity_d * MAX_POSITION_PCT / entry)
    shares = min(risk_shares, cap_shares)
    if shares < 1:
        raise RuleReject("0 shares sized. Risk budget or 15% position cap too small for this price.")

    return BracketPlan(equity_d, max_risk, entry, stop_loss, take_profit, risk_per_share, shares)


def check_price_drift(staged_entry: Decimal, stop_loss: Decimal, current_ask: Decimal) -> Decimal:
    """
    Approval-time guard. Returns the absolute drift if acceptable.
    Raises RuleReject when the live ask has moved more than MAX_DRIFT_FRACTION_OF_STOP
    of the stop distance away from the price the human approved.
    """
    stop_distance = staged_entry - stop_loss
    if stop_distance <= 0:
        raise PipelineAbort("Staged plan has an invalid stop distance.")
    if current_ask <= 0:
        raise PipelineAbort("Current ask must be positive.")
    drift = abs(current_ask - staged_entry)
    allowed = stop_distance * MAX_DRIFT_FRACTION_OF_STOP
    if drift > allowed:
        raise RuleReject(
            f"Stale price: ask {fmt_price(current_ask)} vs approved entry {fmt_price(staged_entry)} "
            f"(moved {fmt_price(drift)}, limit {fmt_price(allowed)}). Re-run the audit."
        )
    return drift


from alpaca.trading.requests import (
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

# Maximum allowable adverse price slippage on entry (0.15% = 15 bps)
MAX_SLIPPAGE_COLLAR_PCT = Decimal("0.0015")

def step5_execute(trading_client: TradingClient, ticker: str, plan: BracketPlan, timeframe: str = "SWING") -> str:
    """
    Submits a Collared Limit Bracket Order.
    Guarantees the fill price cannot exceed (Entry + 15 bps).
    If the market gaps past the collar, the order rests or cancels rather than slipping.
    """
    allowed, reason = check_order_allowed(trading_client, ticker, plan, timeframe)
    if not allowed:
        raise RuleReject("GATE:" + (reason or "GATE_ERROR: unknown"))

    client_order_id = getattr(plan, "client_order_id", None) or new_client_order_id()

    # Calculate the maximum allowable price we are willing to pay
    limit_buy_price = round_to_tick(plan.entry * (Decimal("1") + MAX_SLIPPAGE_COLLAR_PCT))

    order_request = LimitOrderRequest(
        symbol=ticker,
        qty=plan.shares,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY if timeframe == "INTRADAY" else TimeInForce.GTC,
        limit_price=float(limit_buy_price),
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=float(plan.take_profit)),
        stop_loss=StopLossRequest(stop_price=float(plan.stop_loss)),
        client_order_id=client_order_id,
    )
    try:
        order = trading_client.submit_order(order_data=order_request)
        return str(order.id)
    except Exception as exc:
        # A timeout/connection drop can happen after Alpaca accepted the order.
        # Recover by client order id before reporting a failure.
        try:
            existing = trading_client.get_order_by_client_id(client_order_id)
            existing_id = getattr(existing, "id", None)
            if existing_id:
                return str(existing_id)
        except Exception:
            pass
        raise PipelineAbort(f"Collared Order submission failed: {exc}") from exc

def run_pipeline(ticker: str, execution_mode: str = "MANUAL", timeframe: str = "SWING") -> dict:
    """
    Unified entry point for both CLI and FastAPI server.
    """
    key, secret = load_credentials()
    set_identity(EDGAR_IDENTITY)

    try:
        company = Company(ticker)
    except Exception as exc:
        return {"status": "ABORTED", "ticker": ticker, "reason": f"EDGAR resolution error: {exc}"}

    try:
        audit_res = step1_dilution_audit(company, ticker, key, secret)
        trading_client, equity, ask = step3_pricing_and_balance(ticker, key, secret)
        market_cap = estimate_market_cap(Decimal(str(audit_res["latest_weighted_shares"])), Decimal(str(ask)))
        news_res = step2_truth_cross_examiner(company, ticker, key, secret, market_cap=market_cap)
        plan = step4_risk_math(equity, ask, timeframe)
        plan = replace(plan, client_order_id=new_client_order_id())
    except RuleReject as reject_err:
        return {"status": "REJECTED", "ticker": ticker, "reason": str(reject_err)}
    except PipelineAbort as abort_err:
        return {"status": "ABORTED", "ticker": ticker, "reason": str(abort_err)}

    signal_event = {
        "status": "APPROVED",
        "ticker": ticker,
        "mode": execution_mode,
        "audit": {
            "dilution_pct": audit_res["dilution_pct"],
            "has_8k": news_res["form_8k_count"] > 0,
            "form4_count": news_res["form_4_count"],
            "market_cap_usd": float(market_cap),
            "large_cap_exempt": news_res["large_cap_exempt"],
        },
        "trade_plan": {
            "entry": float(plan.entry),
            "stop_loss": float(plan.stop_loss),
            "take_profit": float(plan.take_profit),
            "shares": plan.shares,
            "capital_required": float(plan.position_cost),
            "max_risk_usd": float(plan.shares * plan.risk_per_share),
            "reward_risk_ratio": "3.00:1",
            "timeframe": timeframe,
            "client_order_id": plan.client_order_id,
        },
    }

    if execution_mode.upper() == "AUTO":
        try:
            order_id = step5_execute(trading_client, ticker, plan, timeframe)
            signal_event["status"] = "EXECUTED"
            signal_event["order_id"] = order_id
        except RuleReject as exc:
            signal_event["status"] = "REJECTED"
            signal_event["reason"] = str(exc)
        except PipelineAbort as exc:
            signal_event["status"] = "EXECUTION_FAILED"
            signal_event["reason"] = str(exc)

    return signal_event


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Project Shield Engine CLI")
    parser.add_argument("ticker", help="Stock ticker (e.g. TSLA)")
    parser.add_argument("--auto", action="store_true", help="Execute without prompt")
    args = parser.parse_args()

    mode = "AUTO" if args.auto else "MANUAL"
    res = run_pipeline(args.ticker.strip().upper(), execution_mode=mode)

    print("\n" + "=" * 56)
    print(f" PROJECT SHIELD EXECUTION RECEIPT :: {res.get('ticker')}")
    print("=" * 56)
    print(f" Status : {res.get('status')}")
    if res.get("reason"):
        print(f" Reason : {res.get('reason')}")
    if "trade_plan" in res:
        tp = res["trade_plan"]
        print(f" Entry  : ${tp['entry']:,.2f} | Shares: {tp['shares']}")
        print(f" Stop   : ${tp['stop_loss']:,.2f} (-10%) | Target: ${tp['take_profit']:,.2f} (+30%)")
        print(f" Risk   : ${tp['max_risk_usd']:,.2f} (1.00% max equity loss)")
        if res.get("status") == "APPROVED" and mode == "MANUAL":
            confirm = input("\n[?] Execute Bracket Order on Alpaca? (y/N): ").strip().lower()
            if confirm == "y":
                key, secret = load_credentials()
                tc = TradingClient(key, secret, paper=True)
                plan = BracketPlan(
                    Decimal(str(tp["capital_required"])),
                    Decimal(str(tp["max_risk_usd"])),
                    Decimal(str(tp["entry"])),
                    Decimal(str(tp["stop_loss"])),
                    Decimal(str(tp["take_profit"])),
                    Decimal(str(tp["entry"] - tp["stop_loss"])),
                    tp["shares"],
                    client_order_id=tp.get("client_order_id"),
                )
                oid = step5_execute(tc, res["ticker"], plan)
                print(f"[+] SUCCESS: Order Submitted -> {oid}")
            else:
                print("[-] Aborted by user.")
    print("=" * 56 + "\n")
