"""Every paper-order submit path must stop when the shared gate rejects."""
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import dashboard
import paper_engine as pe
import server

DASH_AUTH = {
    "X-Shield-Auth": dashboard.SHIELD_AUTH_TOKEN,
    "x-alpaca-key": "k",
    "x-alpaca-secret": "s",
    "x-gemini-key": "g",
}
SERVER_AUTH = {"x-shield-auth": server.AUTH_TOKEN}
DASH_PLAN = {
    "ticker": "AAPL",
    "timeframe": "SWING",
    "shares": 10,
    "entry": 100.0,
    "limit_collar": 100.15,
    "stop_loss": 90.0,
    "take_profit": 130.0,
    "max_risk": 100.0,
    "micro_price": 100.0,
    "imbalance": 0.0,
    "spread_bps": 0.0,
}


class Broker:
    def __init__(self):
        self.submitted = []

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return SimpleNamespace(id="must-not-happen")


def reject_gate(*args, **kwargs):
    return False, "SESSION_CLOSED: test rejection"


# ----------------------------- paper_engine step 5 ----------------------------- #
def test_step5_gate_reject_prevents_submit(monkeypatch):
    broker = Broker()
    monkeypatch.setattr(pe, "check_order_allowed", reject_gate)
    plan = pe.step4_risk_math(100_000, 100.0, "SWING")

    with pytest.raises(pe.RuleReject, match="GATE:SESSION_CLOSED"):
        pe.step5_execute(broker, "AAPL", plan)

    assert broker.submitted == []


# ----------------------------- run_pipeline AUTO ----------------------------- #
def test_run_pipeline_auto_gate_reject_prevents_submit(monkeypatch):
    broker = Broker()
    monkeypatch.setattr(pe, "load_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(pe, "set_identity", lambda *_: None)
    monkeypatch.setattr(pe, "Company", lambda ticker: object())
    monkeypatch.setattr(
        pe,
        "step1_dilution_audit",
        lambda *_: {"status": "PASS", "dilution_pct": 1.0, "latest_weighted_shares": 1_000_000_000.0},
    )
    monkeypatch.setattr(pe, "step3_pricing_and_balance", lambda *_: (broker, 100_000.0, 100.0))
    monkeypatch.setattr(
        pe,
        "step2_truth_cross_examiner",
        lambda *a, **k: {"status": "PASS", "form_8k_count": 1, "form_4_count": 1, "large_cap_exempt": False},
    )
    monkeypatch.setattr(pe, "check_order_allowed", reject_gate)

    result = pe.run_pipeline("AAPL", "AUTO", "SWING")

    assert result["status"] == "REJECTED"
    assert result["reason"].startswith("GATE:SESSION_CLOSED:")
    assert broker.submitted == []


# ----------------------------- server /confirm ----------------------------- #
def test_server_confirm_gate_reject_preserves_token_and_prevents_submit(monkeypatch, db_rows):
    broker = Broker()
    staged = {
        "status": "APPROVED",
        "ticker": "AAPL",
        "staged_at": time.time(),
        "trade_plan": {
            "entry": 100.0,
            "stop_loss": 90.0,
            "take_profit": 130.0,
            "shares": 10,
            "capital_required": 1000.0,
            "max_risk_usd": 100.0,
            "timeframe": "SWING",
        },
    }
    staged_at = staged["staged_at"]
    server.PENDING_APPROVALS.clear()
    server.PENDING_APPROVALS["tok"] = staged

    monkeypatch.setattr(server, "load_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(server, "check_existing_position", lambda ticker: False)
    monkeypatch.setattr(server, "fetch_latest_ask", lambda *a: Decimal("100"))
    monkeypatch.setattr(server, "TradingClient", lambda *a, **k: broker)
    monkeypatch.setattr(server, "step5_execute", pe.step5_execute)
    monkeypatch.setattr(pe, "check_order_allowed", reject_gate)

    response = TestClient(server.app).post("/confirm/tok", headers=SERVER_AUTH)

    assert response.status_code == 409
    assert response.json()["detail"].startswith("GATE:SESSION_CLOSED:")
    assert broker.submitted == []
    assert server.PENDING_APPROVALS["tok"] is staged
    assert server.PENDING_APPROVALS["tok"]["staged_at"] == staged_at
    assert db_rows("trade_executions") == []
    assert db_rows("audit_logs")[-1]["status"] == "GATE_REJECTED"


# ----------------------------- dashboard paths ----------------------------- #
@pytest.fixture
def dashboard_env(monkeypatch):
    dashboard.USER_SESSIONS.clear()
    submitted = []

    class FakeTradingClient:
        def __init__(self, *args, **kwargs):
            pass

        def submit_order(self, order_data):
            submitted.append(order_data)
            return SimpleNamespace(id="dashboard-order")

    monkeypatch.setattr(dashboard, "TradingClient", FakeTradingClient)
    monkeypatch.setattr(dashboard, "execute_collared_bracket", lambda *a, **k: dict(DASH_PLAN))
    monkeypatch.setattr(dashboard, "check_order_allowed", reject_gate)
    yield submitted
    dashboard.USER_SESSIONS.clear()


def test_dashboard_trade_auto_gate_reject_returns_409_and_prevents_submit(dashboard_env):
    sess = dashboard.get_user_session("k")
    sess["mode"] = "AUTO"

    response = TestClient(dashboard.app).post(
        "/api/trade", json={"ticker": "AAPL", "timeframe": "SWING"}, headers=DASH_AUTH
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith("SESSION_CLOSED:")
    assert dashboard_env == []
    assert any("GATE BLOCKED" in event["msg"] for event in sess["exec_feed"])
    assert any("coid=shield-" in event["msg"] for event in sess["exec_feed"])


def test_dashboard_ai_scan_auto_gate_reject_returns_409_and_prevents_submit(
    dashboard_env, monkeypatch
):
    class FakeModels:
        def generate_content(self, *args, **kwargs):
            return SimpleNamespace(
                text='{"trade": true, "ticker": "AAPL", "source_tier": 1, "sentiment": 0.9, "reason": "test"}'
            )

    class FakeGenaiClient:
        def __init__(self, *args, **kwargs):
            self.models = FakeModels()

    monkeypatch.setattr(dashboard.genai, "Client", FakeGenaiClient)
    sess = dashboard.get_user_session("k")
    sess["mode"] = "AUTO"

    response = TestClient(dashboard.app).post(
        "/api/ai-scan", json={"headline": "test catalyst"}, headers=DASH_AUTH
    )

    assert response.status_code == 409
    assert response.json()["detail"].startswith("SESSION_CLOSED:")
    assert dashboard_env == []
    assert any("GATE BLOCKED AI AUTO" in event["msg"] for event in sess["exec_feed"])


def test_dashboard_card_gate_reject_keeps_card_and_prevents_submit(dashboard_env):
    sess = dashboard.get_user_session("k")
    card = dict(DASH_PLAN, token="tok", source="Manual Trigger")
    sess["pending_cards"].append(card)

    response = TestClient(dashboard.app).post("/api/card/tok/approve", headers=DASH_AUTH)

    assert response.status_code == 409
    assert response.json()["detail"].startswith("SESSION_CLOSED:")
    assert dashboard_env == []
    assert sess["pending_cards"] == [card]
    assert card["client_order_id"].startswith("shield-")
    assert any("GATE BLOCKED manual card" in event["msg"] for event in sess["exec_feed"])
