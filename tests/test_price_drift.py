"""Approval-time price re-check (pure function)."""
from decimal import Decimal as D

import pytest

from paper_engine import PipelineAbort, RuleReject, check_price_drift


# swing: entry 100, stop 90 -> allowed drift = 20% of 10 = 2.00
@pytest.mark.parametrize("ask", ["100", "101.99", "102.00", "98.00", "99.5"])
def test_swing_within_tolerance(ask):
    check_price_drift(D("100"), D("90"), D(ask))


@pytest.mark.parametrize("ask", ["102.01", "97.99", "110", "80"])
def test_swing_outside_tolerance_is_rejected(ask):
    with pytest.raises(RuleReject, match="Stale price"):
        check_price_drift(D("100"), D("90"), D(ask))


# intraday: entry 100, stop 99 -> allowed drift = 0.20
def test_intraday_tolerance_is_much_tighter():
    check_price_drift(D("100"), D("99"), D("100.20"))
    with pytest.raises(RuleReject):
        check_price_drift(D("100"), D("99"), D("100.21"))


def test_returns_the_absolute_drift():
    assert check_price_drift(D("100"), D("90"), D("98.5")) == D("1.5")


@pytest.mark.parametrize("ask", ["0", "-3"])
def test_nonpositive_live_price_aborts(ask):
    with pytest.raises(PipelineAbort):
        check_price_drift(D("100"), D("90"), D(ask))


def test_invalid_staged_stop_aborts():
    with pytest.raises(PipelineAbort):
        check_price_drift(D("100"), D("100"), D("100"))
    with pytest.raises(PipelineAbort):
        check_price_drift(D("100"), D("105"), D("100"))


def test_message_mentions_both_prices():
    with pytest.raises(RuleReject) as e:
        check_price_drift(D("100"), D("90"), D("105"))
    assert "$105.00" in str(e.value) and "$100.00" in str(e.value)
