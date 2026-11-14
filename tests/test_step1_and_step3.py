"""Step 1 (dilution), split adjustment, XBRL extraction, 10-Q pairing, Step 3 (pricing)."""
import re
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

import paper_engine as pe
from paper_engine import PipelineAbort, RuleReject


# --------------------------- find_10q_pair --------------------------- #
class Filing(SimpleNamespace):
    pass


class FakeFilings(list):
    pass


def mk(period_end: date, lag=30):
    return Filing(filing_date=period_end + timedelta(days=lag), report_date=period_end,
                  period_of_report=period_end.isoformat())


class CompanyWith:
    def __init__(self, filings=None, fail=False):
        self._f, self._fail = FakeFilings(filings or []), fail

    def get_filings(self, **_):
        if self._fail:
            raise RuntimeError("EDGAR")
        return self._f


def test_pair_matches_same_quarter_prior_year():
    q = [mk(date(2026, 6, 30)), mk(date(2026, 3, 31)), mk(date(2025, 9, 30)),
         mk(date(2025, 6, 30)), mk(date(2025, 3, 31))]
    latest, prior = pe.find_10q_pair(CompanyWith(q))
    assert latest[0] == date(2026, 6, 30) and prior[0] == date(2025, 6, 30)


def test_pair_tolerates_52_53_week_calendars():
    latest, prior = pe.find_10q_pair(CompanyWith([mk(date(2026, 6, 27)), mk(date(2025, 6, 28))]))
    assert prior[0] == date(2025, 6, 28)


def test_pair_aborts_without_prior_year():
    with pytest.raises(PipelineAbort, match="prior-year"):
        pe.find_10q_pair(CompanyWith([mk(date(2026, 6, 30)), mk(date(2026, 3, 31))]))


def test_pair_aborts_when_no_filings_or_listing_fails():
    with pytest.raises(PipelineAbort, match="No usable"):
        pe.find_10q_pair(CompanyWith([]))
    with pytest.raises(PipelineAbort, match="Could not list"):
        pe.find_10q_pair(CompanyWith(fail=True))


# --------------------------- extract_weighted_shares --------------------------- #
class FakeXBRL:
    def __init__(self, facts):
        self.facts = facts

    def query(self):
        return self

    def by_concept(self, pattern):
        self._hits = [f for f in self.facts if re.search(pattern, f["concept"])]
        return self

    def execute(self):
        return self._hits


def fact(concept="WeightedAverageNumberOfSharesOutstandingBasic", val=1_000, start=date(2026, 4, 1),
         end=date(2026, 6, 30), **extra):
    return {"concept": concept, "numeric_value": val, "period_start": start.isoformat(),
            "period_end": end.isoformat(), **extra}


def extract(facts, period_end=date(2026, 6, 30)):
    filing = SimpleNamespace(xbrl=lambda: FakeXBRL(facts))
    return pe.extract_weighted_shares(filing, period_end)


def test_extract_takes_quarterly_basic_value():
    assert extract([fact(val=1234)]) == {"Basic": D(1234)}


def test_extract_ignores_dimensional_ytd_and_wrong_period_facts():
    facts = [
        fact(val=1, is_dimensioned=True),
        fact(val=2, dim_Class="A"),
        fact(val=3, start=date(2026, 1, 1)),                 # 6-month YTD
        fact(val=4, end=date(2026, 3, 31), start=date(2026, 1, 1)),
        fact(val=-5),
        fact(val=777),                                       # the only valid one
    ]
    assert extract(facts) == {"Basic": D(777)}


def test_extract_returns_several_concepts():
    out = extract([fact(val=10), fact("WeightedAverageNumberOfDilutedSharesOutstanding", 12)])
    assert out == {"Basic": D(10), "Diluted": D(12)}


def test_extract_aborts_on_missing_or_broken_xbrl():
    with pytest.raises(PipelineAbort, match="no XBRL"):
        pe.extract_weighted_shares(SimpleNamespace(xbrl=lambda: None), date(2026, 6, 30))

    def boom():
        raise ValueError("bad")
    with pytest.raises(PipelineAbort, match="Could not parse"):
        pe.extract_weighted_shares(SimpleNamespace(xbrl=boom), date(2026, 6, 30))


# --------------------------- fetch_split_multiplier --------------------------- #
class Resp:
    def __init__(self, payload, ok=True):
        self._p, self._ok = payload, ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._p


def patch_requests(monkeypatch, pages):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(params))
        return pages[len(calls) - 1] if isinstance(pages, list) else pages
    monkeypatch.setattr(pe.requests, "get", fake_get)
    return calls


def fetch():
    return pe.fetch_split_multiplier("XYZ", date(2025, 8, 1), date(2026, 8, 1), "k", "s")


def test_no_splits_multiplier_is_one(monkeypatch):
    patch_requests(monkeypatch, Resp({"corporate_actions": {}}))
    mult, splits = fetch()
    assert mult == 1 and splits == []


def test_forward_and_reverse_splits_compound(monkeypatch):
    patch_requests(monkeypatch, Resp({"corporate_actions": {
        "forward_splits": [{"new_rate": 2, "old_rate": 1}],
        "reverse_splits": [{"new_rate": 1, "old_rate": 20}]}}))
    mult, splits = fetch()
    assert mult == D("0.1") and len(splits) == 2


def test_pagination_follows_next_page_token(monkeypatch):
    calls = patch_requests(monkeypatch, [
        Resp({"corporate_actions": {"forward_splits": [{"new_rate": 3, "old_rate": 1}]}, "next_page_token": "T2"}),
        Resp({"corporate_actions": {"forward_splits": [{"new_rate": 2, "old_rate": 1}]}}),
    ])
    mult, _ = fetch()
    assert mult == 6 and calls[1]["page_token"] == "T2"


def test_infinite_pagination_aborts(monkeypatch):
    patch_requests(monkeypatch, Resp({"corporate_actions": {}, "next_page_token": "again"}))
    with pytest.raises(PipelineAbort, match="overflow"):
        fetch()


def test_http_failure_aborts(monkeypatch):
    patch_requests(monkeypatch, Resp({}, ok=False))
    with pytest.raises(PipelineAbort, match="Could not verify corporate actions"):
        fetch()


def test_zero_rate_split_aborts(monkeypatch):
    patch_requests(monkeypatch, Resp({"corporate_actions": {"forward_splits": [{"new_rate": 0, "old_rate": 1}]}}))
    with pytest.raises(PipelineAbort, match="Invalid split ratio"):
        fetch()


# --------------------------- step1 end-to-end (mocked inputs) --------------------------- #
def run_step1(monkeypatch, latest_sh, prior_sh, multiplier=D(1), splits=()):
    lat = (date(2026, 6, 30), date(2026, 8, 1), object())
    pri = (date(2025, 6, 30), date(2025, 8, 1), object())
    monkeypatch.setattr(pe, "find_10q_pair", lambda c: (lat, pri))
    shares = iter([{"Basic": D(latest_sh)}, {"Basic": D(prior_sh)}])
    monkeypatch.setattr(pe, "extract_weighted_shares", lambda f, p: next(shares))
    monkeypatch.setattr(pe, "fetch_split_multiplier", lambda *a: (multiplier, list(splits)))
    return pe.step1_dilution_audit(object(), "XYZ", "k", "s")


def test_dilution_just_under_and_exactly_at_limit_pass(monkeypatch):
    assert run_step1(monkeypatch, 114_999, 100_000)["status"] == "PASS"
    assert run_step1(monkeypatch, 115_000, 100_000)["status"] == "PASS"     # limit is strict ">"


def test_dilution_just_over_limit_rejects(monkeypatch):
    with pytest.raises(RuleReject, match="Dilution Trap"):
        run_step1(monkeypatch, 115_001, 100_000)


def test_buyback_shrinking_share_count_passes(monkeypatch):
    assert run_step1(monkeypatch, 90_000, 100_000)["dilution_pct"] < 0


def test_forward_split_does_not_cause_false_reject(monkeypatch):
    # 2-for-1 split: raw count doubled but real dilution is 0%
    res = run_step1(monkeypatch, 200_000, 100_000, multiplier=D(2), splits=[{}])
    assert res["dilution_pct"] == 0 and res["splits_detected"] == 1


def test_reverse_split_does_not_hide_dilution(monkeypatch):
    # 1-for-10 reverse split: prior 1,000,000 -> 100,000 adjusted; latest 130,000 = +30%
    with pytest.raises(RuleReject, match="Dilution Trap"):
        run_step1(monkeypatch, 130_000, 1_000_000, multiplier=D("0.1"), splits=[{}])


def test_step1_exposes_share_count_for_market_cap(monkeypatch):
    assert run_step1(monkeypatch, 123_456, 123_456)["latest_weighted_shares"] == 123_456.0


def test_no_common_share_concept_aborts(monkeypatch):
    lat = (date(2026, 6, 30), date(2026, 8, 1), object())
    pri = (date(2025, 6, 30), date(2025, 8, 1), object())
    monkeypatch.setattr(pe, "find_10q_pair", lambda c: (lat, pri))
    out = iter([{"Basic": D(1)}, {"Diluted": D(1)}])
    monkeypatch.setattr(pe, "extract_weighted_shares", lambda f, p: next(out))
    with pytest.raises(PipelineAbort, match="No matching"):
        pe.step1_dilution_audit(object(), "XYZ", "k", "s")


# --------------------------- step 3 --------------------------- #
class FakeAccount(SimpleNamespace):
    pass


def patch_alpaca(monkeypatch, equity=100_000, blocked=False, ask=50.0, quote_fail=False, no_quote=False):
    class FakeTC:
        def __init__(self, **_): pass
        def get_account(self):
            return FakeAccount(equity=equity, trading_blocked=blocked, account_blocked=False)

    class FakeData:
        def __init__(self, **_): pass
        def get_stock_latest_quote(self, _req):
            if quote_fail:
                raise RuntimeError("quote down")
            return {} if no_quote else {"XYZ": SimpleNamespace(ask_price=ask)}
    monkeypatch.setattr(pe, "TradingClient", FakeTC)
    monkeypatch.setattr(pe, "StockHistoricalDataClient", FakeData)


def test_step3_returns_equity_and_ask(monkeypatch):
    patch_alpaca(monkeypatch, equity="98624.5", ask=341.74)
    _, equity, ask = pe.step3_pricing_and_balance("XYZ", "k", "s")
    assert equity == 98624.5 and ask == 341.74


@pytest.mark.parametrize("kwargs,match", [
    ({"blocked": True}, "blocked"),
    ({"equity": 0}, "positive"),
    ({"ask": 0}, "No valid IEX ask"),
    ({"no_quote": True}, "No valid IEX ask"),
    ({"quote_fail": True}, "IEX quote query failed"),
])
def test_step3_failures_abort(monkeypatch, kwargs, match):
    patch_alpaca(monkeypatch, **kwargs)
    with pytest.raises(PipelineAbort, match=match):
        pe.step3_pricing_and_balance("XYZ", "k", "s")


def test_fetch_latest_ask_returns_decimal(monkeypatch):
    patch_alpaca(monkeypatch, ask=12.34)
    assert pe.fetch_latest_ask("XYZ", "k", "s") == D("12.34")
