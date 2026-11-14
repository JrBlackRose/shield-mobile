"""run_pipeline orchestration with every step mocked."""
from decimal import Decimal as D

import pytest

import paper_engine as pe
from paper_engine import PipelineAbort, RuleReject


@pytest.fixture
def wired(monkeypatch):
    calls = []
    monkeypatch.setattr(pe, "load_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(pe, "set_identity", lambda *_: None)
    monkeypatch.setattr(pe, "Company", lambda t: object())

    def step1(*a):
        calls.append("step1")
        return {"status": "PASS", "dilution_pct": 3.0, "latest_weighted_shares": 1_000_000_000.0}

    def step3(*a):
        calls.append("step3")
        return object(), 100_000.0, 100.0

    def step2(company, ticker, key, secret, market_cap=None):
        calls.append("step2")
        calls.append(("market_cap", market_cap))
        return {"status": "PASS", "form_8k_count": 1, "form_4_count": 2, "large_cap_exempt": False}

    monkeypatch.setattr(pe, "step1_dilution_audit", step1)
    monkeypatch.setattr(pe, "step3_pricing_and_balance", step3)
    monkeypatch.setattr(pe, "step2_truth_cross_examiner", step2)
    monkeypatch.setattr(pe, "step5_execute", lambda c, t, p, tf="SWING": calls.append(("exec", tf)) or "oid-1")
    return calls


def test_happy_path_manual_is_approved_not_executed(wired):
    res = pe.run_pipeline("AAA", "MANUAL", "SWING")
    assert res["status"] == "APPROVED"
    assert res["trade_plan"]["shares"] == 100 and res["trade_plan"]["timeframe"] == "SWING"
    assert not any(isinstance(c, tuple) and c[0] == "exec" for c in wired)


def test_step_order_prices_before_news_and_market_cap_is_passed(wired):
    pe.run_pipeline("AAA")
    assert [c for c in wired if isinstance(c, str)] == ["step1", "step3", "step2"]
    assert ("market_cap", D("1000000000") * D("100.0")) in wired


def test_audit_block_reports_market_cap_and_exemption(wired):
    res = pe.run_pipeline("AAA")
    assert res["audit"]["market_cap_usd"] == 1e11
    assert res["audit"]["large_cap_exempt"] is False
    assert res["audit"]["has_8k"] is True and res["audit"]["form4_count"] == 2


def test_auto_mode_executes_with_timeframe(wired):
    res = pe.run_pipeline("AAA", "AUTO", "INTRADAY")
    assert res["status"] == "EXECUTED" and res["order_id"] == "oid-1"
    assert ("exec", "INTRADAY") in wired


def test_auto_mode_execution_failure_is_reported(wired, monkeypatch):
    def fail(*a, **k):
        raise PipelineAbort("Order submission failed: nope")
    monkeypatch.setattr(pe, "step5_execute", fail)
    res = pe.run_pipeline("AAA", "AUTO")
    assert res["status"] == "EXECUTION_FAILED" and "nope" in res["reason"]


@pytest.mark.parametrize("step", ["step1_dilution_audit", "step2_truth_cross_examiner"])
def test_rule_reject_returns_rejected_and_never_executes(wired, monkeypatch, step):
    def reject(*a, **k):
        raise RuleReject("nope")
    monkeypatch.setattr(pe, step, reject)
    res = pe.run_pipeline("AAA", "AUTO")
    assert res == {"status": "REJECTED", "ticker": "AAA", "reason": "nope"}
    assert not any(isinstance(c, tuple) and c[0] == "exec" for c in wired)


def test_pipeline_abort_returns_aborted(wired, monkeypatch):
    def abort(*a, **k):
        raise PipelineAbort("data missing")
    monkeypatch.setattr(pe, "step3_pricing_and_balance", abort)
    assert pe.run_pipeline("AAA")["status"] == "ABORTED"


def test_unknown_company_is_aborted(wired, monkeypatch):
    def no_company(t):
        raise RuntimeError("not found")
    monkeypatch.setattr(pe, "Company", no_company)
    res = pe.run_pipeline("ZZZZ")
    assert res["status"] == "ABORTED" and "EDGAR resolution error" in res["reason"]


def test_sizing_reject_from_step4_is_rejected(wired, monkeypatch):
    monkeypatch.setattr(pe, "step3_pricing_and_balance", lambda *a: (object(), 100.0, 5000.0))
    assert pe.run_pipeline("AAA")["status"] == "REJECTED"
