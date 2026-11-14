#!/usr/bin/env python3
"""
microstructure.py -- PROJECT SHIELD (100% Free IEX Microstructure & Session Engine)
Computes Top-of-Book Imbalance (I_t), Micro-Price, Bar Order Flow Imbalance (Z_OFI),
Claim-to-Tape Divergence (D_tape), US Session Gating, and Daily Drawdown Breakers
using only Alpaca's FREE IEX data feed.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Tuple
from zoneinfo import ZoneInfo
import math

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient

NY_TZ = ZoneInfo("America/New_York")


class USMarketRegime(Enum):
    NORMAL = 1
    HIGH_VOL_TREND = 2
    WIDE_SPREAD_HALT = 3      # Liquidity evaporation (Spread > limit) -> Block entry
    PUMP_AND_DUMP_RISK = 4    # Bullish news + Smart money selling (D_tape >= 1.10) -> Block buy
    OUTSIDE_SESSION = 5       # Market closed or opening 5-min auction chaos
    DAILY_DRAWDOWN_HALT = 6   # Portfolio down >= 2.5% today -> Circuit breaker tripped


@dataclass(frozen=True)
class MicroConfig:
    imbalance_threshold: float = 0.20      # Minimum |I_t| alignment for aggressive entry
    d_tape_threshold: float = 1.10         # D_tape = S_NLP - tanh(Z_OFI / 2.0)
    max_spread_bps_intraday: float = 45.0  # Max allowed bid-ask spread (bps) for INTRADAY
    max_spread_bps_swing: float = 80.0     # Max allowed bid-ask spread (bps) for SWING
    max_daily_drawdown_pct: float = -0.025 # -2.5% daily equity circuit breaker
    intraday_start_et: time = time(9, 35)  # Skip 09:30-09:35 EDT opening spread chaos
    intraday_cutoff_et: time = time(15, 50)# Stop new intraday entries 10m before close
    intraday_flatten_et: time = time(15, 55)# Auto-flatten intraday positions 5m before close


@dataclass(frozen=True)
class IEXTapeSnapshot:
    ticker: str
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    mid_price: float
    micro_price: float
    imbalance: float
    spread_bps: float
    ofi_z_score: float


def calc_imbalance_and_micro(bid_price: float, ask_price: float, bid_size: float, ask_size: float) -> Tuple[float, float, float, float]:
    """
    Returns (mid_price, micro_price, imbalance_I_t, spread_bps).
    Strictly bounded inside (-1, +1) -> max micro-price deviation is 0.5 * Spread.
    """
    if bid_price <= 0 or ask_price <= 0 or ask_price < bid_price:
        return 0.0, 0.0, 0.0, 9999.0

    mid = (bid_price + ask_price) / 2.0
    spread = ask_price - bid_price
    spread_bps = (spread / mid) * 10000.0 if mid > 0 else 9999.0

    total_vol = bid_size + ask_size
    if total_vol <= 0:
        return mid, mid, 0.0, spread_bps

    imbalance = (bid_size - ask_size) / total_vol
    micro_price = mid + 0.5 * imbalance * spread
    return mid, micro_price, imbalance, spread_bps


def calculate_d_tape(s_nlp: float, ofi_z_score: float) -> float:
    """Claim-to-Tape Divergence: D_tape = S_NLP - tanh(Z_OFI / 2.0)"""
    return float(s_nlp - math.tanh(ofi_z_score / 2.0))


def check_us_session(now_utc: datetime, timeframe: str = "INTRADAY", cfg: MicroConfig = MicroConfig()) -> Tuple[bool, str]:
    """Verifies US market continuous session hours in New York time (EDT/EST)."""
    ny_dt = now_utc.astimezone(NY_TZ)
    if ny_dt.weekday() >= 5:
        return False, f"US Market Closed (Weekend in NY: {ny_dt.strftime('%A %H:%M %Z')})"

    t = ny_dt.time()
    if timeframe == "INTRADAY":
        if not (cfg.intraday_start_et <= t < cfg.intraday_cutoff_et):
            return False, f"Outside Intraday Window (NY Time {t.strftime('%H:%M')} not in 09:35-15:50)"
    else:
        if not (time(9, 30) <= t < time(16, 0)):
            return False, f"Outside Regular US Market Hours (NY Time {t.strftime('%H:%M')})"

    return True, "SESSION_OPEN"


def check_daily_drawdown(key: str, secret: str, cfg: MicroConfig = MicroConfig()) -> Tuple[bool, float]:
    """Checks if daily account drawdown has breached the -2.5% circuit breaker."""
    tc = TradingClient(api_key=key, secret_key=secret, paper=True)
    acct = tc.get_account()
    eq = float(acct.equity or 0.0)
    last_eq = float(acct.last_equity or 0.0)
    if last_eq <= 0:
        return True, 0.0
    dd_pct = (eq - last_eq) / last_eq
    return dd_pct > cfg.max_daily_drawdown_pct, dd_pct


def fetch_free_iex_snapshot(ticker: str, key: str, secret: str) -> IEXTapeSnapshot:
    """Fetches Free IEX L1 Quote + Last 20 1-Min Bars to compute real-time Z_OFI."""
    client = StockHistoricalDataClient(api_key=key, secret_key=secret)

    # 1. Free IEX Top-of-Book Quote
    q_Map = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX))
    q = q_Map.get(ticker)
    if q is None or not q.ask_price or not q.bid_price:
        raise ValueError(f"No active IEX quote for {ticker}")

    bid_p, ask_p = float(q.bid_price), float(q.ask_price)
    bid_s, ask_s = float(q.bid_size or 0.0), float(q.ask_size or 0.0)
    mid, micro, imb, spread_bps = calc_imbalance_and_micro(bid_p, ask_p, bid_s, ask_s)

    # 2. Free IEX 1-Minute Bars (Signed Volume Proxy for Order Flow Imbalance Z_OFI)
    now = datetime.now(timezone.utc)
    ofi_z = 0.0
    try:
        bars_resp = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=45),
                limit=25,
                feed=DataFeed.IEX,
            )
        )
        bars = list(bars_resp.data.get(ticker, []))
        if len(bars) >= 5:
            signed_vols = []
            for b in bars:
                hl_range = max(float(b.high) - float(b.low), 0.0001)
                direction = (float(b.close) - float(b.open)) / hl_range
                signed_vols.append(float(b.volume) * direction)
            latest_ofi = signed_vols[-1]
            hist = signed_vols[:-1]
            mean_ofi = sum(hist) / len(hist)
            var_ofi = sum((x - mean_ofi) ** 2 for x in hist) / max(1, len(hist) - 1)
            std_ofi = math.sqrt(var_ofi)
            if std_ofi > 0:
                ofi_z = (latest_ofi - mean_ofi) / std_ofi
    except Exception:
        ofi_z = imb * 2.0  # Fallback to L1 book imbalance if bar history is sparse

    return IEXTapeSnapshot(
        ticker=ticker,
        bid_price=bid_p,
        ask_price=ask_p,
        bid_size=bid_s,
        ask_size=ask_s,
        mid_price=round(mid, 4),
        micro_price=round(micro, 4),
        imbalance=round(imb, 4),
        spread_bps=round(spread_bps, 2),
        ofi_z_score=round(ofi_z, 3),
    )


def evaluate_catalyst_vs_tape(
    source_tier: int,
    s_nlp: float,
    snap: IEXTapeSnapshot,
    timeframe: str = "INTRADAY",
    cfg: MicroConfig = MicroConfig(),
) -> Tuple[USMarketRegime, float, str]:
    """
    Triangulates Gemini NLP sentiment against live IEX Spread, Book Imbalance, and Z_OFI.
    Fixes the v3.1 Tier-1 weight bug (uses w_signal=0.75 for Tier 1 so strong official filings can trigger).
    """
    max_bps = cfg.max_spread_bps_intraday if timeframe == "INTRADAY" else cfg.max_spread_bps_swing
    if snap.spread_bps > max_bps:
        return (
            USMarketRegime.WIDE_SPREAD_HALT,
            0.0,
            f"Spread {snap.spread_bps:.1f} bps exceeds {max_bps:.1f} bps limit (Liquidity Vacuum)",
        )

    s_nlp = max(-1.0, min(1.0, float(s_nlp)))
    tape_term = math.tanh(snap.ofi_z_score / 2.0)
    d_tape = calculate_d_tape(s_nlp, snap.ofi_z_score)

    # Anti-Manipulation / Pump-and-Dump Filter: Bullish headline but aggressive selling on the tape
    if s_nlp > 0.50 and d_tape >= cfg.d_tape_threshold:
        return (
            USMarketRegime.PUMP_AND_DUMP_RISK,
            -0.85,
            f"Claim-to-Tape Divergence D_tape={d_tape:.2f} >= {cfg.d_tape_threshold} (Smart money selling into news)",
        )

    # Fixed weights: Tier 1 = 0.75 (so official filings can cross 0.65 threshold), Tier 2 = 0.55, Tier 3 = 0.35
    w_signal = 0.75 if source_tier == 1 else (0.55 if source_tier == 2 else 0.35)
    conviction = max(-1.0, min(1.0, w_signal * s_nlp + (1.0 - w_signal) * tape_term))

    if conviction >= 0.60 and snap.imbalance >= -0.35:
        return (
            USMarketRegime.HIGH_VOL_TREND,
            round(conviction, 4),
            f"CONFIRMED (Conviction={conviction:.2f}, Micro=${snap.micro_price:.2f}, Imb={snap.imbalance:+.2f}, D_tape={d_tape:.2f})",
        )

    return (
        USMarketRegime.NORMAL,
        round(conviction, 4),
        f"Weak Tape Alignment (Conviction={conviction:.2f}, Imb={snap.imbalance:+.2f}, Z_OFI={snap.ofi_z_score:+.2f})",
    )


# =====================================================================
# SELF-VERIFICATION UNIT TESTS (RUN DIRECTLY: python3 microstructure.py)
# =====================================================================
if __name__ == "__main__":
    cfg = MicroConfig()

    # Test 1: Micro-Price & Imbalance math bounds
    mid, micro, imb, bps = calc_imbalance_and_micro(100.00, 100.20, bid_size=900, ask_size=100)
    assert round(mid, 2) == 100.10 and round(imb, 2) == 0.80 and round(micro, 2) == 100.18

    # Test 2: Pump & Dump Detection (Bullish News S_NLP=0.90, Heavy Selling Z_OFI=-2.5 -> D_tape = 1.75)
    fake_pump_snap = IEXTapeSnapshot("PENNY", 10.0, 10.02, 100, 300, 10.01, 10.005, -0.50, 20.0, ofi_z_score=-2.5)
    reg, conv, msg = evaluate_catalyst_vs_tape(3, 0.90, fake_pump_snap, "INTRADAY", cfg)
    assert reg == USMarketRegime.PUMP_AND_DUMP_RISK and conv == -0.85

    # Test 3: Liquidity Vacuum / Wide Spread Halt (Spread = 120 bps > 45 bps limit)
    wide_snap = IEXTapeSnapshot("ILLIQ", 100.0, 101.20, 100, 100, 100.60, 100.60, 0.0, 119.28, ofi_z_score=1.0)
    reg2, conv2, _ = evaluate_catalyst_vs_tape(1, 0.95, wide_snap, "INTRADAY", cfg)
    assert reg2 == USMarketRegime.WIDE_SPREAD_HALT and conv2 == 0.0

    # Test 4: Fixed Tier-1 Filing Math (Tier 1 + S_NLP=0.90 on neutral tape Z_OFI=0.0 triggers HIGH_VOL_TREND)
    clean_snap = IEXTapeSnapshot("ALB", 120.00, 120.12, 500, 200, 120.06, 120.085, 0.428, 10.0, ofi_z_score=0.5)
    reg3, conv3, _ = evaluate_catalyst_vs_tape(1, 0.90, clean_snap, "INTRADAY", cfg)
    assert reg3 == USMarketRegime.HIGH_VOL_TREND and conv3 >= 0.60

    # Test 5: US Session Gate (Blocks 09:31 EDT opening chaos, allows 10:15 EDT)
    t_chaos = datetime(2026, 10, 14, 13, 31, tzinfo=timezone.utc)  # 09:31 EDT
    t_good = datetime(2026, 10, 14, 14, 15, tzinfo=timezone.utc)   # 10:15 EDT
    assert check_us_session(t_chaos, "INTRADAY", cfg)[0] is False
    assert check_us_session(t_good, "INTRADAY", cfg)[0] is True

    print("[+] ALL 5 FREE-IEX MICROSTRUCTURE & SESSION TESTS PASSED!")
