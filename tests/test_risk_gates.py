"""Shared risk gates: every pass/fail branch is fail-closed and offline."""
import re
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import risk_gates as rg

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=NY)


def account(**overrides):
    values = {
        "equity": "100000",
        "last_equity": "100000",
        "buying_power": "100000",
        "trading_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def calendar_entry(now=NOW, close=None):
    return SimpleNamespace(
        date=now.date(),
        open=now.replace(hour=9, minute=30, second=0, microsecond=0),
        close=close or now.replace(hour=16, minute=0, second=0, microsecond=0),
    )


class FakeClient:
    def __init__(
        self,
        *,
        acct=None,
        now=NOW,
        is_open=True,
        calendars=None,
        positions=None,
        orders=None,
        fail_on=(),
    ):
        self.acct = acct or account()
        self.clock = SimpleNamespace(is_open=is_open, timestamp=now)
        self.calendars = [calendar_entry(now)] if calendars is None else calendars
        self.positions = positions or []
        self.orders = orders or []
        self.fail_on = set(fail_on)
        self.last_calendar_request = None
        self.last_orders_request = None

    def _check(self, method):
        if method in self.fail_on:
            raise RuntimeError(f"{method} unavailable")

    def get_account(self):
        self._check("get_account")
        return self.acct

    def get_clock(self):
        self._check("get_clock")
        return self.clock

    def get_calendar(self, request=None):
        self._check("get_calendar")
        self.last_calendar_request = request
        return self.calendars

    def get_all_positions(self):
        self._check("get_all_positions")
        return self.positions

    def get_orders(self, request=None):
        self._check("get_orders")
        self.last_orders_request = request
        return self.orders


def plain_plan(shares=10, entry="100"):
    return SimpleNamespace(shares=shares, entry=Decimal(entry))


_DEFAULT_PLAN = object()


def check(client, plan=_DEFAULT_PLAN, ticker="AAPL", timeframe=None):
    if plan is _DEFAULT_PLAN:
        plan = plain_plan()
    return rg.check_order_allowed(client, ticker, plan, timeframe)


def test_all_gates_pass():
    assert check(FakeClient()) == (True, None)


def test_accepts_dashboard_plan_dict():
    plan = {"shares": 10, "entry": "100", "limit_collar": "101", "timeframe": "SWING"}
    assert check(FakeClient(), plan) == (True, None)


def test_account_blocked():
    ok, reason = check(FakeClient(acct=account(trading_blocked=True)))
    assert not ok and reason.startswith("ACCOUNT_BLOCKED:")


def test_account_suspended_by_user_is_blocked():
    ok, reason = check(FakeClient(acct=account(trade_suspended_by_user=True)))
    assert not ok and reason.startswith("ACCOUNT_BLOCKED:")


@pytest.mark.parametrize("field", ["equity", "last_equity", "buying_power"])
def test_missing_account_field_fails_closed(field):
    acct = account()
    delattr(acct, field)
    ok, reason = check(FakeClient(acct=acct))
    assert not ok and reason.startswith("GATE_ERROR:")


@pytest.mark.parametrize("field", ["equity", "last_equity"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_nonpositive_equity_or_last_equity_fails_closed(field, value):
    ok, reason = check(FakeClient(acct=account(**{field: value})))
    assert not ok and reason.startswith("GATE_ERROR:")


@pytest.mark.parametrize(
    "method",
    ["get_account", "get_clock", "get_calendar", "get_all_positions", "get_orders"],
)
def test_every_alpaca_failure_fails_closed(method):
    ok, reason = check(FakeClient(fail_on=[method]))
    assert not ok and reason.startswith("GATE_ERROR:")


def test_clock_closed_is_session_closed():
    ok, reason = check(FakeClient(is_open=False))
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_holiday_with_no_calendar_entry_is_session_closed():
    ok, reason = check(FakeClient(calendars=[]))
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_calendar_for_a_different_day_is_session_closed():
    other = NOW.replace(day=17)
    ok, reason = check(FakeClient(calendars=[calendar_entry(other)]))
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_before_regular_open_is_session_closed():
    now = NOW.replace(hour=9, minute=29)
    ok, reason = check(FakeClient(now=now), timeframe="SWING")
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_at_regular_open_is_allowed_for_swing():
    now = NOW.replace(hour=9, minute=30)
    assert check(FakeClient(now=now), timeframe="SWING") == (True, None)


def test_early_close_day_is_rejected_even_while_clock_is_open():
    now = NOW.replace(hour=12, minute=0)
    early = calendar_entry(now, close=now.replace(hour=13, minute=0))
    ok, reason = check(FakeClient(now=now, calendars=[early]), timeframe="SWING")
    assert not ok and reason.startswith("SESSION_CLOSED: early-close")


def test_at_regular_close_is_session_closed():
    now = NOW.replace(hour=16, minute=0)
    ok, reason = check(FakeClient(now=now), timeframe="SWING")
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_intraday_window_rejects_before_0935():
    now = NOW.replace(hour=9, minute=34)
    ok, reason = check(FakeClient(now=now), timeframe="INTRADAY")
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_intraday_window_allows_0935():
    now = NOW.replace(hour=9, minute=35)
    assert check(FakeClient(now=now), timeframe="INTRADAY") == (True, None)


def test_intraday_window_allows_1549():
    now = NOW.replace(hour=15, minute=49)
    assert check(FakeClient(now=now), timeframe="INTRADAY") == (True, None)


def test_intraday_window_rejects_1550():
    now = NOW.replace(hour=15, minute=50)
    ok, reason = check(FakeClient(now=now), timeframe="INTRADAY")
    assert not ok and reason.startswith("SESSION_CLOSED:")


def test_swing_ignores_intraday_window():
    now = NOW.replace(hour=15, minute=55)
    assert check(FakeClient(now=now), timeframe="SWING") == (True, None)


def test_drawdown_exact_limit_halts():
    ok, reason = check(FakeClient(acct=account(equity="97500", last_equity="100000")))
    assert not ok and reason.startswith("DRAWDOWN_HALT:")


def test_drawdown_just_inside_limit_passes():
    assert check(FakeClient(acct=account(equity="97501", last_equity="100000"))) == (True, None)


def test_four_occupied_slots_reject_new_ticker():
    positions = [SimpleNamespace(symbol=s) for s in ("AAA", "BBB", "CCC", "DDD")]
    ok, reason = check(FakeClient(positions=positions), ticker="EEE")
    assert not ok and reason.startswith("MAX_POSITIONS:")


def test_open_buy_orders_count_toward_max_positions():
    positions = [SimpleNamespace(symbol=s) for s in ("AAA", "BBB", "CCC")]
    orders = [SimpleNamespace(symbol="DDD", side="buy")]
    ok, reason = check(FakeClient(positions=positions, orders=orders), ticker="EEE")
    assert not ok and reason.startswith("MAX_POSITIONS:")


def test_open_sell_orders_do_not_count_toward_max_positions():
    positions = [SimpleNamespace(symbol=s) for s in ("AAA", "BBB", "CCC")]
    orders = [SimpleNamespace(symbol="DDD", side="sell")]
    assert check(FakeClient(positions=positions, orders=orders), ticker="EEE") == (True, None)


def test_duplicate_position():
    positions = [SimpleNamespace(symbol="AAPL")]
    ok, reason = check(FakeClient(positions=positions))
    assert not ok and reason.startswith("DUPLICATE_POSITION:")


def test_duplicate_open_order_even_when_sell_side():
    orders = [SimpleNamespace(symbol="AAPL", side="sell")]
    ok, reason = check(FakeClient(orders=orders))
    assert not ok and reason.startswith("DUPLICATE_ORDER:")


def test_insufficient_buying_power_uses_95_percent_buffer():
    plan = plain_plan(shares=100, entry="100")
    ok, reason = check(FakeClient(acct=account(buying_power="10000")), plan)
    assert not ok and reason.startswith("INSUFFICIENT_BUYING_POWER:")


def test_buying_power_buffer_just_passes():
    plan = plain_plan(shares=100, entry="100")
    assert check(FakeClient(acct=account(buying_power="10600")), plan) == (True, None)


def test_dashboard_limit_collar_is_the_required_notional():
    plan = {"shares": 10, "entry": "100", "limit_collar": "101"}
    ok, reason = check(FakeClient(acct=account(buying_power="1053")), plan)
    assert not ok and reason.startswith("INSUFFICIENT_BUYING_POWER:")


@pytest.mark.parametrize(
    "plan",
    [
        {"shares": 0, "entry": "100"},
        {"shares": -1, "entry": "100"},
        {"shares": "1.5", "entry": "100"},
        {"shares": 10, "entry": "0"},
        {"shares": 10, "entry": "-5"},
        {"shares": 10},
        {"entry": "100"},
        None,
    ],
)
def test_invalid_plan_fails_closed(plan):
    ok, reason = check(FakeClient(), plan)
    assert not ok and reason.startswith("GATE_ERROR:")


def test_blank_ticker_fails_closed():
    ok, reason = check(FakeClient(), ticker="   ")
    assert not ok and reason.startswith("GATE_ERROR:")


def test_get_orders_uses_open_status_and_500_limit():
    client = FakeClient()
    check(client)
    assert str(client.last_orders_request.status.value) == "open"
    assert client.last_orders_request.limit == 500


def test_calendar_request_is_for_today():
    client = FakeClient()
    check(client)
    request = client.last_calendar_request
    assert request.start is not None and request.end is not None
    assert request.start == request.end == NOW.date()


def test_client_order_id_format_and_uniqueness():
    first = rg.new_client_order_id()
    second = rg.new_client_order_id()
    assert re.fullmatch(r"shield-[0-9a-f]{32}", first)
    assert first != second
