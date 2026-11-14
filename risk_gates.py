#!/usr/bin/env python3
"""
risk_gates.py -- PROJECT SHIELD

Shared, fail-closed pre-submit risk gates for paper-trading orders.

This module intentionally has no dependency on paper_engine.py. It accepts either
a BracketPlan-like object or the dashboard's plan dictionary.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time as dt_time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest

MAX_DAILY_DRAWDOWN_PCT = Decimal("-0.025")
MAX_OPEN_POSITIONS = 4
BUYING_POWER_SAFETY_FACTOR = Decimal("0.95")

CLIENT_ORDER_ID_PREFIX = "shield-"

_NY_TZ = ZoneInfo("America/New_York")
_INTRADAY_START = dt_time(9, 35)
_INTRADAY_END = dt_time(15, 50)
_REGULAR_CLOSE = dt_time(16, 0)


def new_client_order_id() -> str:
    """Return a deterministic-prefixed, unique Alpaca client order id."""
    return CLIENT_ORDER_ID_PREFIX + uuid.uuid4().hex


def _gate_error(message: str) -> tuple[bool, str]:
    return False, f"GATE_ERROR: {message}"


def _decimal(value, field: str) -> Decimal:
    if value is None:
        raise ValueError(f"missing {field}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"invalid {field}: {value!r}")
    return number


def _plan_values(plan) -> tuple[int, Decimal]:
    if isinstance(plan, dict):
        shares_raw = plan.get("shares")
        price_raw = plan.get("limit_collar")
        if price_raw is None:
            price_raw = plan.get("entry")
    else:
        shares_raw = getattr(plan, "shares", None)
        price_raw = getattr(plan, "entry", None)

    shares = _decimal(shares_raw, "shares")
    if shares <= 0 or shares != shares.to_integral_value():
        raise ValueError(f"invalid shares: {shares_raw!r}")

    price = _decimal(price_raw, "price")
    if price <= 0:
        raise ValueError(f"non-positive price: {price_raw!r}")

    return int(shares), price * shares


def _as_ny_datetime(value, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"missing/invalid {field}")
    if value.tzinfo is None:
        return value.replace(tzinfo=_NY_TZ)
    return value.astimezone(_NY_TZ)


def _calendar_date(value, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError as exc:
            raise ValueError(f"invalid {field}: {value!r}") from exc
    raise ValueError(f"missing/invalid {field}")


def _symbol(obj, field: str = "symbol") -> str:
    value = getattr(obj, field, None)
    if value is None or not str(value).strip():
        raise ValueError(f"missing {field}")
    return str(value).strip().upper()


def _side(obj) -> str:
    value = getattr(obj, "side", None)
    if value is None:
        raise ValueError("missing order side")
    value = getattr(value, "value", value)
    return str(value).strip().lower()


def check_order_allowed(
    trading_client,
    ticker: str,
    plan,
    timeframe: str | None = None,
) -> tuple[bool, str | None]:
    """
    Evaluate all pre-submit risk gates.

    Returns ``(True, None)`` only when every check passes. Any Alpaca failure,
    missing required field, malformed plan, or non-positive value returns
    ``(False, "GATE_ERROR: ...")``.
    """
    if not isinstance(ticker, str) or not ticker.strip():
        return _gate_error("missing ticker")
    symbol = ticker.strip().upper()

    try:
        _plan_values(plan)
    except Exception as exc:
        return _gate_error(f"invalid plan: {exc}")

    try:
        account = trading_client.get_account()
    except Exception as exc:
        return _gate_error(f"account lookup failed: {exc}")

    try:
        if (
            bool(getattr(account, "trading_blocked", False))
            or bool(getattr(account, "account_blocked", False))
            or bool(getattr(account, "trade_suspended_by_user", False))
        ):
            return False, "ACCOUNT_BLOCKED: Alpaca account is not permitted to trade"

        equity = _decimal(getattr(account, "equity", None), "equity")
        last_equity = _decimal(getattr(account, "last_equity", None), "last_equity")
        if equity <= 0 or last_equity <= 0:
            return _gate_error("non-positive equity/last_equity")
    except Exception as exc:
        return _gate_error(f"invalid account: {exc}")

    try:
        clock = trading_client.get_clock()
        is_open = getattr(clock, "is_open", None)
        if not isinstance(is_open, bool):
            return _gate_error("missing/invalid clock is_open")
        if not is_open:
            return False, "SESSION_CLOSED: market clock is closed"

        now = _as_ny_datetime(getattr(clock, "timestamp", None), "clock timestamp")
        today = now.date()
        calendars = trading_client.get_calendar(GetCalendarRequest(start=today, end=today))
        if not calendars:
            return False, "SESSION_CLOSED: no trading day today"

        session = None
        for entry in calendars:
            if _calendar_date(getattr(entry, "date", None), "calendar date") == today:
                session = entry
                break
        if session is None:
            return False, "SESSION_CLOSED: no calendar entry for today"

        session_open = _as_ny_datetime(getattr(session, "open", None), "calendar open")
        session_close = _as_ny_datetime(getattr(session, "close", None), "calendar close")
        if session_close.time() < _REGULAR_CLOSE:
            return False, "SESSION_CLOSED: early-close day"
        if now < session_open or now >= session_close:
            return False, "SESSION_CLOSED: outside regular session"

        if timeframe is not None and str(timeframe).upper() == "INTRADAY":
            intraday_start = max(
                session_open,
                datetime.combine(today, _INTRADAY_START, tzinfo=_NY_TZ),
            )
            intraday_end = min(
                session_close,
                datetime.combine(today, _INTRADAY_END, tzinfo=_NY_TZ),
            )
            if now < intraday_start or now >= intraday_end:
                return False, "SESSION_CLOSED: outside intraday entry window"
    except Exception as exc:
        return _gate_error(f"session lookup failed: {exc}")

    drawdown = (equity - last_equity) / last_equity
    if drawdown <= MAX_DAILY_DRAWDOWN_PCT:
        return False, f"DRAWDOWN_HALT: daily change {drawdown:.4%} breached limit"

    try:
        positions = trading_client.get_all_positions()
        orders = trading_client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        )
    except Exception as exc:
        return _gate_error(f"position/order lookup failed: {exc}")

    try:
        position_symbols = {_symbol(position) for position in positions}
        order_symbols = set()
        buy_symbols = set()
        for order in orders:
            order_symbol = _symbol(order)
            order_symbols.add(order_symbol)
            if _side(order) == "buy":
                buy_symbols.add(order_symbol)
    except Exception as exc:
        return _gate_error(f"invalid position/order: {exc}")

    occupied = position_symbols | buy_symbols
    if symbol not in occupied and len(occupied) >= MAX_OPEN_POSITIONS:
        return (
            False,
            f"MAX_POSITIONS: {len(occupied)} of {MAX_OPEN_POSITIONS} slots already occupied",
        )

    if symbol in position_symbols:
        return False, f"DUPLICATE_POSITION: {symbol} already has an open position"
    if symbol in order_symbols:
        return False, f"DUPLICATE_ORDER: {symbol} already has an open order"

    try:
        buying_power = _decimal(getattr(account, "buying_power", None), "buying_power")
        _, required = _plan_values(plan)
    except Exception as exc:
        return _gate_error(f"invalid buying-power input: {exc}")

    if required > buying_power * BUYING_POWER_SAFETY_FACTOR:
        return (
            False,
            "INSUFFICIENT_BUYING_POWER: "
            f"required {required} vs available {buying_power * BUYING_POWER_SAFETY_FACTOR}",
        )

    return True, None
