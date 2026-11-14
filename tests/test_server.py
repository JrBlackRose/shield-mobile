"""FastAPI server: auth, staging, guards, TTL, price re-check, execution + ledger."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import server
from alpaca.common.exceptions import APIError
from paper_engine import PipelineAbort

ORIGINAL_CHECK = server.check_existing_position   # captured before any fixture stubs it
AUTH = {"x-shield-auth": server.AUTH_TOKEN}   # never hard-code the secret in tests


class Http404:
    def __init__(self, code): self.response = type("R", (), {"status_code": code})()


class Env:
    """Records every side effect so tests can assert nothing happened."""
    def __init__(self):
        self.orders, self.telegram, self.pipeline_calls = [], [], []
        self.ask, self.ask_error = 100.0, None
        self.position, self.pos_error = False, None
        self.pipeline_result = None


@pytest.fixture
def env(monkeypatch):
    e = Env()
    server.PENDING_APPROVALS.clear()
    monkeypatch.setattr(server, "load_credentials", lambda: ("k", "s"))

    def check(ticker):
        if e.pos_error:
            raise e.pos_error
        return e.position
    monkeypatch.setattr(server, "check_existing_position", check)

    def fetch(ticker, k, s):
        if e.ask_error:
            raise e.ask_error
        from decimal import Decimal
        return Decimal(str(e.ask))
    monkeypatch.setattr(server, "fetch_latest_ask", fetch)

    def step5(client, ticker, plan, timeframe="SWING"):
        e.orders.append((ticker, plan, timeframe))
        return "oid-%d" % len(e.orders)
    monkeypatch.setattr(server, "step5_execute", step5)
    monkeypatch.setattr(server, "TradingClient", lambda *a, **k: type("TC", (), {
        "cancel_orders": lambda s: e.orders.append("cancel"),
        "close_all_positions": lambda s, cancel_orders=False: e.orders.append("close_all")})())

    def pipeline(ticker, mode, timeframe):
        e.pipeline_calls.append((ticker, mode, timeframe))
        return e.pipeline_result or {"status": "REJECTED", "ticker": ticker, "reason": "x"}
    monkeypatch.setattr(server, "run_pipeline", pipeline)

    async def tg(token, ticker, plan):
        e.telegram.append((token, ticker, plan))
    monkeypatch.setattr(server, "send_approval_request", tg)
    return e


@pytest.fixture
def client(env):
    # authenticated client; no `with` -> lifespan (Telegram thread) is not started
    return TestClient(server.app, headers=AUTH)


@pytest.fixture
def anon(env):
    return TestClient(server.app)    # sends no auth header


def stage(token="tok", timeframe="SWING", age=0, entry=100.0, ticker="AAA"):
    stop = entry * (0.99 if timeframe == "INTRADAY" else 0.90)
    server.PENDING_APPROVALS[token] = {
        "status": "APPROVED", "ticker": ticker, "staged_at": time.time() - age,
        "trade_plan": {"entry": entry, "stop_loss": round(stop, 2), "take_profit": round(entry * 1.3, 2),
                       "shares": 10, "capital_required": entry * 10, "max_risk_usd": 100.0,
                       "timeframe": timeframe},
    }


# ----------------------------- webhook auth & validation ----------------------------- #
@pytest.mark.parametrize("headers", [{}, {"x-shield-auth": "wrong"}, {"x-shield-auth": ""}])
def test_webhook_rejects_missing_or_bad_secret(anon, env, headers):
    r = anon.post("/webhook", json={"ticker": "AAPL"}, headers=headers)
    assert r.status_code == 401 and env.pipeline_calls == []


def test_webhook_validates_payload(client):
    assert client.post("/webhook", json={"ticker": "AAPL", "timeframe": "WEEKLY"}, headers=AUTH).status_code == 422
    assert client.post("/webhook", json={"ticker": "AAPL", "mode": "YOLO"}, headers=AUTH).status_code == 422


def test_webhook_normalises_ticker_and_queues(client, env):
    r = client.post("/webhook", json={"ticker": " aapl "}, headers=AUTH)
    assert r.status_code == 200 and r.json()["ticker"] == "AAPL" and r.json()["status"] == "QUEUED"
    assert env.pipeline_calls == [("AAPL", "MANUAL", "SWING")]


def test_approved_manual_trade_is_staged_with_timestamp_and_telegram_card(client, env):
    env.pipeline_result = {"status": "APPROVED", "ticker": "AAPL", "trade_plan": {"entry": 100.0}}
    token = client.post("/webhook", json={"ticker": "AAPL"}, headers=AUTH).json()["approval_token"]
    assert time.time() - server.PENDING_APPROVALS[token]["staged_at"] < 5
    assert env.telegram[0][0] == token


def test_rejected_pipeline_is_not_staged_but_is_audited(client, env, db_rows):
    client.post("/webhook", json={"ticker": "AAPL"}, headers=AUTH)
    assert server.PENDING_APPROVALS == {}
    assert db_rows("audit_logs")[-1]["status"] == "REJECTED"


# ----------------------------- duplicate guard (webhook side) ----------------------------- #
def test_existing_position_blocks_pipeline_entirely(client, env, db_rows):
    env.position = True
    client.post("/webhook", json={"ticker": "TSLA"}, headers=AUTH)
    assert env.pipeline_calls == []
    assert "already active" in db_rows("audit_logs")[0]["reason"]


def test_position_check_failure_fails_closed(client, env, db_rows):
    env.pos_error = RuntimeError("alpaca 500")
    client.post("/webhook", json={"ticker": "TSLA"}, headers=AUTH)
    assert env.pipeline_calls == []
    row = db_rows("audit_logs")[0]
    assert row["status"] == "ABORTED" and "alpaca 500" in row["reason"]


# ----------------------------- check_existing_position itself ----------------------------- #
def make_tc(monkeypatch, position=None, orders=(), pos_status=404):
    class TC:
        def __init__(self, *a, **k): pass
        def get_open_position(self, t):
            if position:
                return object()
            raise APIError('{"message":"position does not exist"}', Http404(pos_status))
        def get_orders(self, req):
            self.req = req
            return list(orders)
    monkeypatch.setattr(server, "TradingClient", TC)
    monkeypatch.setattr(server, "load_credentials", lambda: ("k", "s"))


def real_check(monkeypatch):
    return ORIGINAL_CHECK


def test_check_true_when_position_held(monkeypatch):
    check = real_check(monkeypatch); make_tc(monkeypatch, position=True)
    assert check("AAPL") is True


def test_check_true_when_open_order_exists_but_no_position(monkeypatch):
    check = real_check(monkeypatch); make_tc(monkeypatch, orders=[object()])
    assert check("AAPL") is True


def test_check_false_when_nothing_exists(monkeypatch):
    check = real_check(monkeypatch); make_tc(monkeypatch)
    assert check("AAPL") is False


def test_check_reraises_non_404_errors_instead_of_assuming_flat(monkeypatch):
    check = real_check(monkeypatch); make_tc(monkeypatch, pos_status=500)
    with pytest.raises(APIError):
        check("AAPL")


# ----------------------------- /confirm ----------------------------- #
def test_confirm_unknown_token_404(client, env):
    assert client.post("/confirm/nope").status_code == 404 and env.orders == []


def test_confirm_swing_success_places_order_and_ledgers_it(client, env, db_rows):
    stage()
    r = client.post("/confirm/tok")
    assert r.status_code == 200 and r.json()["order_id"] == "oid-1"
    ticker, plan, tf = env.orders[0]
    assert (ticker, plan.shares, tf) == ("AAA", 10, "SWING")
    (row,) = db_rows("trade_executions")
    assert row["order_id"] == "oid-1" and row["timeframe"] == "SWING"


def test_confirm_intraday_uses_intraday_everywhere(client, env, db_rows):
    """Regression: timeframe used to be read from the wrong dict level and silently became SWING/GTC."""
    stage(timeframe="INTRADAY")
    assert client.post("/confirm/tok").status_code == 200
    assert env.orders[0][2] == "INTRADAY"
    assert db_rows("trade_executions")[0]["timeframe"] == "INTRADAY"


def test_double_confirm_executes_once(client, env):
    stage()
    assert client.post("/confirm/tok").status_code == 200
    assert client.post("/confirm/tok").status_code == 404
    assert len(env.orders) == 1


@pytest.mark.parametrize("tf,ttl", [("SWING", 1800), ("INTRADAY", 300)])
def test_expired_approval_is_refused(client, env, db_rows, tf, ttl):
    stage(timeframe=tf, age=ttl + 5)
    r = client.post("/confirm/tok")
    assert r.status_code == 410 and env.orders == []
    assert db_rows("audit_logs")[-1]["status"] == "EXPIRED"


@pytest.mark.parametrize("tf,ttl", [("SWING", 1800), ("INTRADAY", 300)])
def test_approval_just_inside_ttl_is_accepted(client, env, tf, ttl):
    stage(timeframe=tf, age=ttl - 10)
    assert client.post("/confirm/tok").status_code == 200


def test_approval_without_timestamp_is_treated_as_expired(client, env):
    stage()
    del server.PENDING_APPROVALS["tok"]["staged_at"]
    assert client.post("/confirm/tok").status_code == 410 and env.orders == []


def test_expired_token_is_consumed(client, env):
    stage(age=99999)
    client.post("/confirm/tok")
    assert "tok" not in server.PENDING_APPROVALS


# ----- price re-check on approval ----- #
def test_price_unchanged_executes(client, env):
    stage(entry=100.0); env.ask = 100.0
    assert client.post("/confirm/tok").status_code == 200


def test_swing_price_moved_beyond_tolerance_is_refused_and_audited(client, env, db_rows):
    stage(entry=100.0); env.ask = 103.0            # allowed drift is 2.00
    r = client.post("/confirm/tok")
    assert r.status_code == 409 and "Stale price" in r.json()["detail"] and env.orders == []
    assert db_rows("audit_logs")[-1]["status"] == "STALE_PRICE"
    assert db_rows("trade_executions") == []


def test_price_drop_beyond_tolerance_is_also_refused(client, env):
    stage(entry=100.0); env.ask = 97.0
    assert client.post("/confirm/tok").status_code == 409


def test_intraday_tolerance_is_tighter_than_swing(client, env):
    stage("a", "INTRADAY", entry=100.0); env.ask = 100.5     # fine for swing, too far for a 1% stop
    assert client.post("/confirm/a").status_code == 409
    stage("b", "SWING", entry=100.0); env.ask = 100.5
    assert client.post("/confirm/b").status_code == 200


def test_price_unavailable_fails_closed(client, env):
    stage(); env.ask_error = PipelineAbort("market closed")
    r = client.post("/confirm/tok")
    assert r.status_code == 503 and env.orders == []


def test_position_opened_while_staged_blocks_execution(client, env):
    stage(); env.position = True
    r = client.post("/confirm/tok")
    assert r.status_code == 400 and env.orders == []


def test_position_check_error_at_confirm_fails_closed(client, env):
    stage(); env.pos_error = RuntimeError("down")
    assert client.post("/confirm/tok").status_code == 503 and env.orders == []


def test_broker_failure_returns_500_and_writes_no_ledger_row(client, env, monkeypatch, db_rows):
    stage()
    def boom(*a, **k):
        raise PipelineAbort("rejected by broker")
    monkeypatch.setattr(server, "step5_execute", boom)
    assert client.post("/confirm/tok").status_code == 500
    assert db_rows("trade_executions") == []


def test_ledger_failure_does_not_hide_a_placed_order(client, env, monkeypatch):
    stage()
    monkeypatch.setattr(server.ledger, "log_execution", lambda **k: (_ for _ in ()).throw(OSError("disk")))
    r = client.post("/confirm/tok")
    assert r.status_code == 200 and len(env.orders) == 1


# ----------------------------- reject / kill switch ----------------------------- #
def test_reject_removes_token(client, env):
    stage()
    assert client.post("/reject/tok").json()["status"] == "REJECTED"
    assert client.post("/confirm/tok").status_code == 404
    assert client.post("/reject/tok").json()["status"] == "NOT_FOUND"


def test_kill_switch_cancels_then_flattens(client, env):
    assert client.post("/kill-switch").json()["status"] == "EMERGENCY_SHUTDOWN_EXECUTED"
    assert env.orders == ["cancel", "close_all"]


# ----------------------------- auth on mutating endpoints ----------------------------- #
@pytest.mark.parametrize("path", ["/confirm/tok", "/reject/tok", "/kill-switch"])
@pytest.mark.parametrize("headers", [{}, {"x-shield-auth": "wrong"}])
def test_mutating_endpoints_require_auth_and_have_no_side_effects(anon, env, path, headers):
    stage()
    assert anon.post(path, headers=headers).status_code == 401
    assert env.orders == [] and "tok" in server.PENDING_APPROVALS      # nothing consumed or executed


def test_all_post_routes_are_protected():
    """Guards against someone adding a new endpoint and forgetting auth."""
    import inspect
    for route in server.app.routes:
        if getattr(route, "methods", None) and "POST" in route.methods and route.path not in ("/openapi.json",):
            assert "x_shield_auth" in inspect.signature(route.endpoint).parameters, route.path


def test_telegram_notifier_sends_the_same_secret_the_server_expects(monkeypatch):
    """The secret is duplicated in two files; this fails loudly if they drift apart."""
    import telegram_notifier as tn
    seen = {}

    class FakeClient:
        def __init__(self, **kw): seen.update(kw)
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url): return type("R", (), {"is_success": True})()
    monkeypatch.setattr(tn.httpx, "AsyncClient", FakeClient)
    ok, _ = asyncio.run(tn._call_api("/confirm/tok"))
    assert ok and seen["headers"]["x-shield-auth"] == server.AUTH_TOKEN


# ----------------------------- AUTO mode ----------------------------- #
AUTO_RESULT = {"status": "EXECUTED", "ticker": "AAA", "order_id": "auto-1",
               "trade_plan": {"entry": 100.0, "stop_loss": 90.0, "take_profit": 130.0,
                              "shares": 10, "max_risk_usd": 100.0, "timeframe": "SWING"}}


def test_auto_mode_execution_is_ledgered(env, db_rows):
    env.pipeline_result = dict(AUTO_RESULT)
    asyncio.run(server.process_trade_job("AAA", "AUTO", "SWING", "t"))
    (row,) = db_rows("trade_executions")
    assert row["order_id"] == "auto-1" and row["shares"] == 10


def test_auto_mode_never_stages_a_manual_approval(env):
    env.pipeline_result = dict(AUTO_RESULT)
    asyncio.run(server.process_trade_job("AAA", "AUTO", "SWING", "t"))
    assert server.PENDING_APPROVALS == {} and env.telegram == []


# ----------------------------- regression guards ----------------------------- #
def test_auto_mode_ledger_failure_is_contained(env, monkeypatch):
    env.pipeline_result = dict(AUTO_RESULT)
    monkeypatch.setattr(server.ledger, "log_execution", lambda **k: (_ for _ in ()).throw(OSError("disk full")))
    asyncio.run(server.process_trade_job("AAA", "AUTO", "SWING", "t"))     # must not raise


def test_failed_auth_does_not_leak_the_secret(anon, capsys):
    anon.post("/kill-switch", headers={"x-shield-auth": "guess"})
    assert server.AUTH_TOKEN not in capsys.readouterr().out
