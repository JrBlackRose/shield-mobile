#!/usr/bin/env python3
"""
verify_precision.py -- Read-Only Live 24/7 & Candle Precision Auditor
- 100% Read-Only: Never places broker orders.
- Safe dynamic field extraction for BracketPlan.
- Pulls LIVE 24/7 candles (BTC/USD) or US Equity 1-min candles (NVDA, AAPL).
- Saves a visual plot (precision_audit_chart.png) showing entry triggers vs. vetoed traps.
"""
import argparse
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from alpaca.data.enums import DataFeed
from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    StockBarsRequest,
)
from alpaca.data.timeframe import TimeFrame

from microstructure import (
    IEXTapeSnapshot,
    USMarketRegime,
    calc_imbalance_and_micro,
    evaluate_catalyst_vs_tape,
)
from paper_engine import (
    MAX_SLIPPAGE_COLLAR_PCT,
    load_credentials,
    round_to_tick,
    step4_risk_math,
)


def run_static_math_demo():
    equity = Decimal("100000.00")
    bid, ask = 120.00, 120.10
    bid_lots, ask_lots = 800, 300
    mid, micro, imb, bps = calc_imbalance_and_micro(bid, ask, bid_lots, ask_lots)

    snap = IEXTapeSnapshot(
        ticker="ALB", bid_price=bid, ask_price=ask,
        bid_size=bid_lots, ask_size=ask_lots,
        mid_price=mid, micro_price=micro,
        imbalance=imb, spread_bps=bps, ofi_z_score=1.8,
    )

    regime, conviction, msg = evaluate_catalyst_vs_tape(
        source_tier=1, s_nlp=0.90, snap=snap, timeframe="INTRADAY"
    )

    plan = step4_risk_math(equity, Decimal(str(ask)), timeframe="INTRADAY")
    limit_collar = round_to_tick(plan.entry * (Decimal("1") + MAX_SLIPPAGE_COLLAR_PCT))

    # Attribute-safe dynamic extraction
    capital = float(plan.shares * plan.entry)
    max_risk = float((plan.entry - plan.stop_loss) * Decimal(str(plan.shares)))

    print("=" * 76)
    print(" PROJECT SHIELD v2.0 -- STATIC PRECISION & SIZING AUDIT")
    print("=" * 76)
    print(f"1. BOOK STATE     : Bid ${bid:.2f} ({bid_lots} sh) | Ask ${ask:.2f} ({ask_lots} sh)")
    print(f"2. MICRO-PRICE    : ${micro:.4f} (Mid: ${mid:.4f} | Spread: {bps:.2f} bps | Imb: {imb:+.2f})")
    print(f"3. TAPE VERDICT   : {regime.name} -> {msg}")
    print(f"4. RISK SIZING    : {plan.shares} shares (Capital: ${capital:,.2f} / 15% Cap: $15,000.00)")
    print(f"5. MAX LOSS BOUND : ${max_risk:,.2f} (Strictly <= 1.0% of ${equity:,.2f} equity)")
    print(f"6. ENTRY COLLAR   : LIMIT BUY @ ${limit_collar:.2f} (Max +15 bps above Ask ${plan.entry:.2f})")
    print(f"7. BRACKET EXITS  : Stop-Loss @ ${plan.stop_loss:.2f} | Take-Profit @ ${plan.take_profit:.2f}")
    print("=" * 76)


def fetch_candles(symbol: str, is_crypto: bool):
    now = datetime.now(timezone.utc)
    key, secret = load_credentials()
    if is_crypto:
        c_client = CryptoHistoricalDataClient(api_key=key, secret_key=secret)
        live_q = None
        try:
            q_map = c_client.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=symbol))
            live_q = q_map.get(symbol)
        except Exception:
            pass

        bars_resp = c_client.get_crypto_bars(
            CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=90),
                limit=60,
            )
        )
        bars = []
        if hasattr(bars_resp, "data") and bars_resp.data:
            bars = list(bars_resp.data.get(symbol, []))
        if not bars:
            try:
                bars = list(bars_resp[symbol])
            except Exception:
                for k in getattr(bars_resp, "data", {}):
                    bars = list(bars_resp.data[k])
                    break
        return bars, live_q
    else:
        s_client = StockHistoricalDataClient(api_key=key, secret_key=secret)
        bars_resp = s_client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=now - timedelta(days=5),
                limit=500,
                feed=DataFeed.IEX,
            )
        )
        all_bars = []
        if hasattr(bars_resp, "data") and bars_resp.data:
            all_bars = list(bars_resp.data.get(symbol, []))
        if not all_bars:
            try:
                all_bars = list(bars_resp[symbol])
            except Exception:
                for k in getattr(bars_resp, "data", {}):
                    all_bars = list(bars_resp.data[k])
                    break
        return all_bars[-60:] if len(all_bars) > 60 else all_bars, None


def audit_real_candles(symbol: str, is_crypto: bool):
    bars, live_q = fetch_candles(symbol, is_crypto)
    if len(bars) < 15:
        print(f"[-] Not enough 1-minute bars returned for {symbol} (got {len(bars)}).")
        return

    print(f"\n[+] Auditing {len(bars)} Real 1-Minute Candles for {symbol} (READ-ONLY DRY RUN)")
    if live_q and getattr(live_q, "bid_price", None) and getattr(live_q, "ask_price", None):
        mid, micro, imb, bps = calc_imbalance_and_micro(
            float(live_q.bid_price), float(live_q.ask_price),
            float(live_q.bid_size or 1.0), float(live_q.ask_size or 1.0)
        )
        print(
            f"[+] LIVE 24/7 ORDER BOOK: Bid ${float(live_q.bid_price):,.2f} | Ask ${float(live_q.ask_price):,.2f} "
            f"| Micro-Price ${micro:,.2f} | Imb {imb:+.2f} | Spread {bps:.2f} bps"
        )

    print("-" * 92)
    print(f"{'TIME (UTC)':<16} | {'CLOSE':>10} | {'VOL':>7} | {'Z_OFI':>6} | {'IMB':>6} | {'VERDICT (IF NEWS HIT THIS CANDLE)'}")
    print("-" * 92)

    signed_vols = []
    verdicts = []
    first_entry_idx = None

    for idx, b in enumerate(bars):
        o, h, l, c, v = float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume)
        hl = max(h - l, 0.0001)
        direction = (c - o) / hl
        sv = v * direction
        signed_vols.append(sv)

        if idx < 10:
            verdicts.append(("WARMUP", 0.0))
            continue

        hist = signed_vols[max(0, idx - 15):idx]
        mean_sv = sum(hist) / len(hist)
        std_sv = math.sqrt(sum((x - mean_sv) ** 2 for x in hist) / max(1, len(hist) - 1))
        z_ofi = (sv - mean_sv) / std_sv if std_sv > 0 else 0.0

        est_spread = max(c * 0.0004, 0.01)
        bid_p, ask_p = c - est_spread / 2.0, c + est_spread / 2.0
        bid_s = max(1.0, v * (0.5 + 0.4 * direction))
        ask_s = max(1.0, v * (0.5 - 0.4 * direction))
        mid, micro, imb, bps = calc_imbalance_and_micro(bid_p, ask_p, bid_s, ask_s)

        snap = IEXTapeSnapshot(
            ticker=symbol, bid_price=bid_p, ask_price=ask_p,
            bid_size=bid_s, ask_size=ask_s, mid_price=mid,
            micro_price=micro, imbalance=imb, spread_bps=bps, ofi_z_score=z_ofi,
        )
        reg, conv, _ = evaluate_catalyst_vs_tape(2, 0.85, snap, "INTRADAY")
        verdicts.append((reg.name, conv))

        if reg == USMarketRegime.HIGH_VOL_TREND and first_entry_idx is None and idx < len(bars) - 5:
            first_entry_idx = idx

        if idx >= len(bars) - 15 or idx == first_entry_idx:
            t_str = b.timestamp.strftime("%m-%d %H:%M")
            if reg == USMarketRegime.HIGH_VOL_TREND:
                collar = c * (1.0 + float(MAX_SLIPPAGE_COLLAR_PCT))
                tag = f"✅ PRECISION ENTRY (Collar <= ${collar:,.2f}, Conv={conv:.2f})"
            elif reg == USMarketRegime.PUMP_AND_DUMP_RISK:
                tag = f"❌ VETO: SELLING INTO HYPE (D_tape Trap Avoided!)"
            else:
                tag = f"⏸️  WAIT: Weak Flow (Conv={conv:.2f})"
            print(f"{t_str:<16} | {c:>10.2f} | {int(v):>7} | {z_ofi:>+6.2f} | {imb:>+6.2f} | {tag}")

    print("-" * 92)

    if first_entry_idx is not None:
        entry_bar = bars[first_entry_idx]
        entry_px = float(entry_bar.close)
        future_bars = bars[first_entry_idx + 1:]
        max_high = max(float(x.high) for x in future_bars)
        min_low = min(float(x.low) for x in future_bars)
        last_close = float(future_bars[-1].close)
        mfe_pct = ((max_high - entry_px) / entry_px) * 100.0
        mae_pct = ((min_low - entry_px) / entry_px) * 100.0
        net_pct = ((last_close - entry_px) / entry_px) * 100.0

        print(f"\n[POST-ENTRY CANDLE PERFORMANCE FOR {symbol}]")
        print(f" • Trigger Time (UTC) : {entry_bar.timestamp.strftime('%Y-%m-%d %H:%M')}")
        print(f" • Entry Price        : ${entry_px:,.2f} (Collared Limit @ ${entry_px * 1.0015:,.2f})")
        print(f" • Best Peak After    : ${max_high:,.2f} ({mfe_pct:+.2f}% Max Favorable Excursion)")
        print(f" • Worst Dip After    : ${min_low:,.2f} ({mae_pct:+.2f}% Max Adverse Excursion)")
        print(f" • Window End Close   : ${last_close:,.2f} ({net_pct:+.2f}%)")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        times = [b.timestamp.strftime("%H:%M") for b in bars]
        closes = [float(b.close) for b in bars]
        buy_idx = [i for i, v in enumerate(verdicts) if v[0] == "HIGH_VOL_TREND"]
        veto_idx = [i for i, v in enumerate(verdicts) if v[0] == "PUMP_AND_DUMP_RISK"]

        plt.figure(figsize=(12, 5.5))
        plt.plot(range(len(closes)), closes, label=f"{symbol} 1-Min Close", color="#1f77b4", linewidth=1.8)
        if buy_idx:
            plt.scatter(buy_idx, [closes[i] for i in buy_idx], color="green", s=80, zorder=5, label="Confirmed Entry (Flow + AI Aligned)")
        if veto_idx:
            plt.scatter(veto_idx, [closes[i] for i in veto_idx], color="red", marker="x", s=90, zorder=5, label="Vetoed Trap (D_tape Selling)")
        step = max(1, len(times) // 10)
        plt.xticks(range(0, len(times), step), [times[i] for i in range(0, len(times), step)], rotation=30)
        plt.title(f"Project Shield v2.0 -- {symbol} Microstructure Entry vs. Trap Veto Audit")
        plt.xlabel("Time (UTC)")
        plt.ylabel("Price (USD)")
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig("precision_audit_chart.png", dpi=150)
        print("[+] Visual chart saved to: precision_audit_chart.png")
    except ImportError:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-24h", type=str, help="Test on live 24/7 crypto (e.g. BTC/USD)")
    parser.add_argument("--ticker", type=str, help="Test on recent US stock 1-min candles (e.g. NVDA, AAPL)")
    args = parser.parse_args()

    run_static_math_demo()
    if args.live_24h:
        audit_real_candles(args.live_24h.strip().upper(), is_crypto=True)
    elif args.ticker:
        audit_real_candles(args.ticker.strip().upper(), is_crypto=False)
