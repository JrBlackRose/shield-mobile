"""Step 4: position sizing. Includes the regression that produced 312 TSLA shares."""
from decimal import Decimal

import pytest

import paper_engine as pe
from paper_engine import PipelineAbort, RuleReject, step4_risk_math


def test_swing_sizing_exact_numbers():
    plan = step4_risk_math(100_000, 100.0, "SWING")
    assert plan.stop_loss == Decimal("90.00")
    assert plan.take_profit == Decimal("130.00")
    assert plan.shares == 100                      # 1% risk = $1000 / $10 per share
    assert plan.shares * plan.risk_per_share == Decimal("1000.00")


def test_intraday_is_capped_at_15_percent_of_equity():
    # 1% stop + 1% risk would be 100% of equity; the notional cap must bind.
    plan = step4_risk_math(100_000, 100.0, "INTRADAY")
    assert plan.stop_loss == Decimal("99.00")
    assert plan.shares == 150
    assert plan.position_cost <= Decimal("100000") * pe.MAX_POSITION_PCT


def test_regression_screenshot_account_cannot_hold_312_tsla():
    equity, ask = 98_624, 374.60
    for tf in ("SWING", "INTRADAY"):
        plan = step4_risk_math(equity, ask, tf)
        assert plan.shares < 312
        assert plan.position_cost <= Decimal(str(equity)) * Decimal("0.15")
    assert step4_risk_math(equity, ask, "SWING").shares == 26


def test_aapl_regression_numbers_and_tick_rounding():
    plan = step4_risk_math(98_624, 341.74, "SWING")
    assert plan.stop_loss == Decimal("307.57")     # 307.566 rounded to a valid cent
    assert plan.take_profit == Decimal("444.26")
    assert plan.shares == 28


def test_stock_too_expensive_for_cap_is_rejected_not_forced_to_one_share():
    with pytest.raises(RuleReject, match="0 shares"):
        step4_risk_math(100_000, 20_000.0, "SWING")


def test_tiny_account_is_rejected():
    with pytest.raises(RuleReject):
        step4_risk_math(50, 300.0, "SWING")


@pytest.mark.parametrize("equity,ask", [(0, 10.0), (-5, 10.0), (1000, 0.0), (1000, -1.0)])
def test_nonpositive_inputs_abort(equity, ask):
    with pytest.raises(PipelineAbort):
        step4_risk_math(equity, ask, "SWING")


def test_subdollar_prices_use_four_decimal_ticks():
    plan = step4_risk_math(100_000, 0.5, "SWING")
    assert plan.stop_loss == Decimal("0.4500")
    assert plan.take_profit == Decimal("0.6500")


@pytest.mark.parametrize("timeframe", ["SWING", "INTRADAY"])
@pytest.mark.parametrize("equity", [1_000, 5_000, 98_624, 250_000])
@pytest.mark.parametrize("ask", [0.5, 3.17, 20.0, 341.74, 1_500.0, 25_000.0])
def test_invariants_hold_across_grid(equity, ask, timeframe):
    try:
        plan = step4_risk_math(equity, ask, timeframe)
    except RuleReject:
        return  # rejecting is always safe
    eq = Decimal(str(equity))
    assert plan.shares >= 1
    assert plan.position_cost <= eq * pe.MAX_POSITION_PCT           # notional cap
    assert plan.shares * plan.risk_per_share <= eq * pe.RISK_PCT    # 1% risk cap
    assert plan.stop_loss < plan.entry < plan.take_profit
    for price in (plan.stop_loss, plan.take_profit):
        tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
        assert price % tick == 0, f"{price} is not on a valid tick"


# --------------------------- step 5: order construction --------------------------- #
class FakeBroker:
    def __init__(self, fail=False):
        self.requests, self.fail = [], fail

    def submit_order(self, order_data):
        if self.fail:
            raise RuntimeError("boom")
        self.requests.append(order_data)
        return type("O", (), {"id": "order-123"})()


@pytest.mark.parametrize("timeframe,tif", [("SWING", "gtc"), ("INTRADAY", "day")])
def test_step5_bracket_and_time_in_force(timeframe, tif):
    plan = step4_risk_math(100_000, 100.0, timeframe)
    broker = FakeBroker()
    assert pe.step5_execute(broker, "AAPL", plan, timeframe) == "order-123"
    req = broker.requests[0]
    assert req.symbol == "AAPL" and req.qty == plan.shares
    assert req.order_class.value == "bracket"
    assert req.time_in_force.value == tif
    assert req.stop_loss.stop_price == float(plan.stop_loss)
    assert req.take_profit.limit_price == float(plan.take_profit)


def test_step5_broker_failure_becomes_pipeline_abort():
    plan = step4_risk_math(100_000, 100.0, "SWING")
    with pytest.raises(PipelineAbort, match="Order submission failed"):
        pe.step5_execute(FakeBroker(fail=True), "AAPL", plan)
