"""
test_dilution.py  --  Project Shield: SEC EDGAR dilution auditor

Compares a company's weighted-average shares outstanding in its most recent
10-Q against the 10-Q for the same quarter one year earlier. If the share
count grew by more than 15% year-over-year, the stock is flagged as a
dilution trap.

Setup:
    pip install edgartools

Run:
    python test_dilution.py            # defaults to AAPL as a smoke test
    python test_dilution.py SNDL       # any ticker

Known limitations (read these before trusting a PASS):
    * Reverse splits: the older 10-Q reports the share count as it was filed,
      NOT split-adjusted. A 1-for-20 reverse split in between will make the
      share count look like it collapsed (a false PASS), and a forward split
      will look like huge dilution (a false REJECT).
    * Only the weighted-average count is compared. Shares issued in the last
      few weeks of the quarter are only partly counted, so very recent
      dilution can lag. Cross-check the cover-page share count if needed.
    * Companies that have not filed two 10-Qs a year apart (recent IPOs) cannot
      be audited; the script reports that instead of guessing.
"""

import sys
from datetime import date

import edgar
from edgar import Company

# SEC requires a descriptive User-Agent identity on every request.
edgar.set_identity("ProjectShield allendraa.anbalagan@gmail.com")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
DILUTION_THRESHOLD_PCT = 15.0   # > this YoY expansion  ->  REJECT
PRIOR_YEAR_TOLERANCE_DAYS = 45  # how far from "exactly 1 year" we'll accept

# XBRL tags to try, in priority order. Small companies often file only the
# "BasicAndDiluted" variant (they have net losses, so diluted == basic).
SHARE_CONCEPTS = [
    "us-gaap:WeightedAverageNumberOfSharesOutstandingBasic",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap:WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
]
# Last-resort fallback: shares outstanding as of the 10-Q cover-page date.
COVER_PAGE_CONCEPT = "dei:EntityCommonStockSharesOutstanding"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _to_date(value) -> date | None:
    """Turn 'YYYY-MM-DD' strings / date objects into a date (None if unusable)."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _report_date(filing) -> date | None:
    """The period-end date a 10-Q covers (e.g. 2025-06-30)."""
    return _to_date(getattr(filing, "report_date", None)) or _to_date(filing.period_of_report)


def _is_dimensioned(fact: dict) -> bool:
    """True if the fact belongs to a sub-segment (share class, scenario, ...)."""
    if fact.get("is_dimensioned"):
        return True
    return any(k.startswith("dim_") and fact[k] for k in fact)


def find_10q_pair(ticker: str):
    """
    Return (latest_10q, prior_year_10q) for the ticker.
    prior_year_10q is the 10-Q whose period-end is closest to one year before
    the latest one (within PRIOR_YEAR_TOLERANCE_DAYS), or None if not found.
    """
    company = Company(ticker)
    # amendments=False skips 10-Q/A so we compare original filings only.
    filings = company.get_filings(form="10-Q", amendments=False)

    latest = filings.latest()
    if latest is None:
        return None, None

    latest_end = _report_date(latest)
    if latest_end is None:
        return latest, None

    best, best_gap = None, None
    for f in filings:
        end = _report_date(f)
        if end is None or end >= latest_end:
            continue
        # How far is this filing from being exactly 365 days before the latest?
        gap = abs((latest_end - end).days - 365)
        if gap <= PRIOR_YEAR_TOLERANCE_DAYS and (best_gap is None or gap < best_gap):
            best, best_gap = f, gap

    return latest, best


def extract_weighted_shares(filing) -> tuple[float, str] | None:
    """
    Pull the weighted-average shares figure for the quarter a 10-Q covers.
    Returns (share_count, concept_name_used) or None if nothing was found.
    """
    xbrl = filing.xbrl()
    if xbrl is None:
        return None  # filing has no XBRL data attached

    for concept in SHARE_CONCEPTS:
        facts = xbrl.query().by_concept(concept, exact=True).execute()
        # Keep consolidated, numeric, duration facts only.
        facts = [
            f for f in facts
            if not _is_dimensioned(f)
            and f.get("numeric_value") is not None
            and f.get("period_start") and f.get("period_end")
        ]
        if not facts:
            continue

        # A 10-Q also contains prior-year comparatives, so take the newest
        # period-end = the quarter this filing is actually about.
        newest_end = max(_to_date(f["period_end"]) for f in facts)
        current = [f for f in facts if _to_date(f["period_end"]) == newest_end]

        # Q2/Q3 filings carry both a 3-month and a year-to-date figure.
        # Prefer the 3-month one (shortest duration) so we compare like for like.
        current.sort(key=lambda f: (_to_date(f["period_end"]) - _to_date(f["period_start"])).days)
        return float(current[0]["numeric_value"]), concept

    # Fallback: cover-page shares outstanding (a point-in-time number).
    facts = xbrl.query().by_concept(COVER_PAGE_CONCEPT, exact=True).execute()
    facts = [f for f in facts if not _is_dimensioned(f) and f.get("numeric_value") is not None]
    if facts:
        return float(facts[0]["numeric_value"]), COVER_PAGE_CONCEPT

    return None


# --------------------------------------------------------------------------- #
# Main logic
# --------------------------------------------------------------------------- #
def check_dilution(ticker: str) -> bool | None:
    """
    Audit one ticker. Prints the verdict and returns:
        True  -> PASS (clean float)
        False -> REJECT (dilution trap)
        None  -> could not determine (missing filings / data); NOT a pass
    """
    ticker = ticker.upper()
    print(f"\n=== Dilution audit: {ticker} ===")

    # 1. Find the two 10-Q filings -------------------------------------------
    try:
        latest, prior = find_10q_pair(ticker)
    except Exception as exc:  # unknown ticker, network error, SEC throttling...
        print(f"ERROR: could not load filings for {ticker}: {exc}")
        return None

    if latest is None:
        print(f"ERROR: no 10-Q filings found for {ticker}.")
        return None
    if prior is None:
        print(f"ERROR: no 10-Q from ~1 year before {_report_date(latest)} found "
              f"(company may be too new, or a filing is missing).")
        return None

    print(f"Latest 10-Q : period ending {_report_date(latest)} (filed {latest.filing_date})")
    print(f"Prior  10-Q : period ending {_report_date(prior)} (filed {prior.filing_date})")

    # 2. Extract share counts -------------------------------------------------
    try:
        latest_result = extract_weighted_shares(latest)
        prior_result = extract_weighted_shares(prior)
    except Exception as exc:
        print(f"ERROR: failed to parse XBRL data: {exc}")
        return None

    if latest_result is None or prior_result is None:
        print("ERROR: weighted-average share data not found in one or both filings.")
        return None

    latest_shares, latest_tag = latest_result
    prior_shares, prior_tag = prior_result
    if latest_tag != prior_tag:
        print(f"WARNING: different metrics used ({prior_tag.split(':')[1]} vs "
              f"{latest_tag.split(':')[1]}); comparison may be slightly off.")

    if prior_shares <= 0:
        print("ERROR: prior-year share count is zero or negative; cannot compute change.")
        return None

    # 3. Percentage change ----------------------------------------------------
    pct_change = (latest_shares - prior_shares) / prior_shares * 100

    print(f"Prior shares : {prior_shares:>20,.0f}")
    print(f"Latest shares: {latest_shares:>20,.0f}")
    print(f"YoY change   : {pct_change:>+19.1f}%\n")

    # 4. Verdict --------------------------------------------------------------
    if pct_change > DILUTION_THRESHOLD_PCT:
        print(f"REJECT: Dilution Trap ({pct_change:.0f}% expansion)")
        return False

    print("PASS: Clean Float")
    return True


if __name__ == "__main__":
    # Ticker from the command line, e.g. `python test_dilution.py SNDL`.
    # Defaults to AAPL purely as a quick smoke test of the plumbing.
    target = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    check_dilution(target)
