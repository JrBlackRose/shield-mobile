"""Step 2: news vs. 8-K cross-examination, including the large-cap exemption."""
from decimal import Decimal

import pytest

import paper_engine as pe
from paper_engine import PipelineAbort, RuleReject
from conftest import article

SMALL = Decimal("1000000000")        # $1B
LARGE = Decimal("50000000000")       # $50B


class FakeCompany:
    def __init__(self, n8k=0, n4=0, fail=False):
        self.n8k, self.n4, self.fail = n8k, n4, fail

    def get_filings(self, form, **_):
        if self.fail:
            raise RuntimeError("EDGAR down")
        return [object()] * (self.n8k if form == "8-K" else self.n4)


@pytest.fixture
def news(monkeypatch):
    """news(articles) installs a fake Alpaca NewsClient returning those articles."""
    def install(articles=None, fail=False):
        class FakeNewsClient:
            def __init__(self, **_): pass
            def get_news(self, _request):
                if fail:
                    raise RuntimeError("news down")
                return type("R", (), {"data": {"news": articles or []}})()
        monkeypatch.setattr(pe, "NewsClient", FakeNewsClient)
    return install


def run(company, market_cap):
    return pe.step2_truth_cross_examiner(company, "XYZ", "k", "s", market_cap=market_cap)


def test_small_cap_hype_without_8k_is_rejected_with_cap_in_message(news):
    news([article(1)])
    with pytest.raises(RuleReject, match=r"Unverified Media Hype.*\$1\.00B"):
        run(FakeCompany(n8k=0), SMALL)


def test_unknown_market_cap_is_treated_strictly(news):
    news([article(1)])
    with pytest.raises(RuleReject, match="unknown"):
        run(FakeCompany(n8k=0), None)


def test_large_cap_with_news_and_no_8k_now_passes_and_is_flagged(news):
    news([article(1)])
    res = run(FakeCompany(n8k=0), LARGE)
    assert res["status"] == "PASS" and res["large_cap_exempt"] is True


def test_exemption_boundary_is_inclusive(news):
    news([article(1)])
    assert run(FakeCompany(), pe.LARGE_CAP_MARKET_CAP)["large_cap_exempt"] is True
    with pytest.raises(RuleReject):
        run(FakeCompany(), pe.LARGE_CAP_MARKET_CAP - 1)


def test_small_cap_with_8k_passes_without_exemption(news):
    news([article(1)])
    res = run(FakeCompany(n8k=2), SMALL)
    assert res["status"] == "PASS" and res["large_cap_exempt"] is False


def test_large_cap_with_8k_is_not_marked_exempt(news):
    news([article(1)])
    assert run(FakeCompany(n8k=1), LARGE)["large_cap_exempt"] is False


@pytest.mark.parametrize("articles", [[], [article(8)], [article(30)]])
def test_no_recent_news_never_triggers_hype_rule(news, articles):
    news(articles)
    assert run(FakeCompany(n8k=0), SMALL)["recent_news_found"] is False


def test_news_exactly_inside_window_counts_as_recent(news):
    news([article(6.9)])
    assert run(FakeCompany(n8k=1), SMALL)["recent_news_found"] is True


def test_form4_count_is_reported(news):
    news([])
    assert run(FakeCompany(n4=7), SMALL)["form_4_count"] == 7


def test_news_failure_aborts_instead_of_passing(news):
    news(fail=True)
    with pytest.raises(PipelineAbort, match="News fetch failed"):
        run(FakeCompany(), LARGE)


def test_edgar_failure_aborts_instead_of_passing(news):
    news([article(1)])
    with pytest.raises(PipelineAbort, match="EDGAR verification failed"):
        run(FakeCompany(fail=True), LARGE)


def test_estimate_market_cap():
    assert pe.estimate_market_cap(Decimal("15000000000"), Decimal("200")) == Decimal("3000000000000")
