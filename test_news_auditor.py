"""
test_news_auditor.py  --  Project Shield: Truth Cross-Examiner

Cross-references what the MEDIA is saying about a stock against what the
company has actually told the SEC, to spot PR hype traps and insider dumping.

    Phase 1  NEWS      Alpaca news feed: the 3 most recent headlines.
    Phase 2  8-K       Is there an official 8-K (material event) filing in the
                       last 14 days? Fresh news + no 8-K = hype without
                       disclosure.
    Phase 3  FORM 4    How many insider-trading reports (Form 4) were filed in
                       the last 30 days? Any at all = check for dumping.
"""

import os
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone

import edgar
from dotenv import load_dotenv
from edgar import Company, CompanyNotFoundError

from alpaca.common.enums import Sort
from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

# SEC requires a descriptive User-Agent identity on every request.
edgar.set_identity("ProjectShield allendraa.anbalagan@gmail.com")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
NEWS_COUNT = 3              # how many recent articles to print
NEWS_LOOKBACK_DAYS = 90     # explicit search window
NEWS_FRESH_DAYS = 7         # newest article younger than this = "recent news"
FORM_8K_WINDOW_DAYS = 14    # an 8-K this recent counts as fresh disclosure
FORM_4_WINDOW_DAYS = 30     # look-back for insider-trading reports
WIDTH = 70                  # width of the terminal receipt

_ASCII_FIXES = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
})


def _ascii(text) -> str:
    return str(text).translate(_ASCII_FIXES).encode("ascii", "ignore").decode("ascii").strip()


def _section(title: str) -> None:
    print("\n" + "=" * WIDTH)
    print(f"  {title}")
    print("=" * WIDTH)


def _row(label: str, value) -> None:
    print(f" {label:<24}: {value}")


def _warning(message: str) -> None:
    print("!" * WIDTH)
    print(textwrap.fill(message, width=WIDTH - 2, initial_indent=" ",
                        subsequent_indent=" " * 11))
    print("!" * WIDTH)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _age_text(age: timedelta) -> str:
    hours = age.total_seconds() / 3600
    return f"{int(hours)}h ago" if hours < 48 else f"{age.days}d ago"


def _to_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fetch_recent_news(api_key: str, secret_key: str, ticker: str, now: datetime) -> list:
    client = NewsClient(api_key, secret_key)
    request = NewsRequest(
        symbols=ticker,
        start=now.replace(microsecond=0) - timedelta(days=NEWS_LOOKBACK_DAYS),
        limit=NEWS_COUNT,
        sort=Sort.DESC.value,
        include_content=False,
    )
    news_set = client.get_news(request)
    articles = news_set.data.get("news", [])
    return sorted(articles, key=lambda a: _as_utc(a.created_at), reverse=True)[:NEWS_COUNT]


def get_latest_8k(company) -> tuple[date, str] | None:
    filings = company.get_filings(form="8-K", trigger_full_load=False)
    if filings is None:
        raise RuntimeError("EDGAR returned no filing list")
    latest = filings.latest()
    if latest is None:
        return None
    filed = _to_date(latest.filing_date)
    if filed is None:
        raise ValueError(f"unreadable filing date: {latest.filing_date!r}")
    return filed, str(latest.form)


def count_recent_form4s(company, since: date) -> int:
    filings = company.get_filings(
        form="4",
        filing_date=f"{since.isoformat()}:",
        amendments=False,
        trigger_full_load=False,
    )
    if filings is None:
        raise RuntimeError("EDGAR returned no filing list")
    return len(filings)


def main() -> None:
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        print("[ERROR] ALPACA_API_KEY and/or ALPACA_SECRET_KEY missing from .env")
        sys.exit(1)

    if len(sys.argv) != 2:
        print("Usage: python test_news_auditor.py TICKER   (e.g. python test_news_auditor.py AMC)")
        sys.exit(1)
    ticker = sys.argv[1].strip().upper()

    now = _now()
    today = now.date()
    warnings_raised = 0
    unverified = 0

    try:
        company = Company(ticker)
        company_label = f"{_ascii(company.name)} (CIK {company.cik})"
    except CompanyNotFoundError:
        print(f"[ERROR] Ticker '{ticker}' was not found on SEC EDGAR. Check the symbol.")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Could not reach SEC EDGAR for '{ticker}': {exc}")
        sys.exit(1)

    print("=" * WIDTH)
    print("  PROJECT SHIELD -- TRUTH CROSS-EXAMINER")
    print("=" * WIDTH)
    _row("Ticker", ticker)
    _row("Company (EDGAR)", company_label)
    _row("Run time (UTC)", now.strftime("%Y-%m-%d %H:%M"))

    _section(f"PHASE 1/3 - NEWS INGESTION (last {NEWS_COUNT} articles)")

    recent_news_found = False
    news_status = "UNVERIFIED"

    try:
        articles = fetch_recent_news(api_key, secret_key, ticker, now)
    except Exception as exc:
        print(f" [ERROR] Could not fetch news: {exc}")
        unverified += 1
    else:
        if not articles:
            print(f" No news found for {ticker} in the last {NEWS_LOOKBACK_DAYS} days.")
            news_status = "QUIET"
        else:
            for number, article in enumerate(articles, start=1):
                created = _as_utc(article.created_at)
                print(f" [{number}] {created:%Y-%m-%d %H:%M} UTC  ({_age_text(now - created)})"
                      f"  {_ascii(article.source)}")
                print(textwrap.fill(_ascii(article.headline), width=WIDTH,
                                    initial_indent="     ", subsequent_indent="     "))

            newest_age = now - _as_utc(articles[0].created_at)
            recent_news_found = newest_age < timedelta(days=NEWS_FRESH_DAYS)
            news_status = "RECENT" if recent_news_found else "QUIET"

            print("-" * WIDTH)
            _row("Newest article", _age_text(newest_age))
            _row("recent_news_found", f"{recent_news_found}  (newest < {NEWS_FRESH_DAYS} days old)")

    _section("PHASE 2/3 - FORM 8-K VERIFICATION (corporate honesty)")

    eightk_status = "UNVERIFIED"

    try:
        latest_8k = get_latest_8k(company)
    except Exception as exc:
        print(f" [ERROR] Could not retrieve 8-K filings: {exc}")
        unverified += 1
    else:
        if latest_8k is None:
            _row("Most recent 8-K", "none found in recent EDGAR history")
            eightk_status = "NONE"
        else:
            filed, form_type = latest_8k
            age_days = (today - filed).days
            _row("Most recent 8-K", f"{filed.isoformat()}  ({form_type}, {age_days}d ago)")
            filed_recently = age_days <= FORM_8K_WINDOW_DAYS
            eightk_status = "FOUND" if filed_recently else "NONE"
            _row(f"8-K in last {FORM_8K_WINDOW_DAYS} days", "YES" if filed_recently else "NO")

    print("-" * WIDTH)

    if news_status == "UNVERIFIED" or eightk_status == "UNVERIFIED":
        print(" [UNVERIFIED] Hype trap NOT tested (news or 8-K data unavailable).")
    elif not recent_news_found:
        print(f" [OK] No news in the last {NEWS_FRESH_DAYS} days, so no hype to cross-check.")
    elif eightk_status == "NONE":
        _warning("[WARNING] Media Hype Detected without Official SEC 8-K Disclosure")
        warnings_raised += 1
    else:
        print(f" [OK] Recent news, and an 8-K was filed in the last {FORM_8K_WINDOW_DAYS} days.")
        print("      (Open it and confirm it actually covers what the news claims.)")

    _section("PHASE 3/3 - FORM 4 INSIDER ALIGNMENT (the dump check)")

    form4_count = None

    try:
        form4_count = count_recent_form4s(company, today - timedelta(days=FORM_4_WINDOW_DAYS))
    except Exception as exc:
        print(f" [ERROR] Could not retrieve Form 4 filings: {exc}")
        unverified += 1
    else:
        _row(f"Form 4s, last {FORM_4_WINDOW_DAYS} days", form4_count)
        print("-" * WIDTH)

        if form4_count > 0:
            noun = "Form 4" if form4_count == 1 else "Form 4s"
            _warning(f"[WARNING] Insider Trading Detected: {form4_count} {noun} filed in the "
                     f"last {FORM_4_WINDOW_DAYS} days. Verify if executives are dumping "
                     f"shares into the news hype.")
            print(" (Count includes buys, grants and tax withholding, not only sales.)")
            warnings_raised += 1
        else:
            print(f" [OK] No Form 4s filed in the last {FORM_4_WINDOW_DAYS} days.")

    _section(f"AUDIT SUMMARY -- {ticker}")
    _row(f"News (last {NEWS_FRESH_DAYS} days)", news_status)
    _row(f"8-K (last {FORM_8K_WINDOW_DAYS} days)", eightk_status)
    _row(f"Form 4s (last {FORM_4_WINDOW_DAYS} days)",
         "UNVERIFIED" if form4_count is None else form4_count)
    print("-" * WIDTH)
    _row("Warnings raised", warnings_raised)
    _row("Checks unverified", unverified)
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
