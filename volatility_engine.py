#!/usr/bin/env python3
"""
volatility_engine.py -- Zero-Cost AI + Microstructure Momentum Scanner
1. Pre-filters noisy websocket headlines locally to protect Free Gemini API quota.
2. Uses Gemini 2.5 Flash to score Source Tier (1-3), Sentiment, and Ticker.
3. Cross-examines the headline against Free Alpaca IEX Micro-Price, Spread & D_tape.
"""

import os
import time
import json
import asyncio
import argparse
import requests
import websockets
from datetime import datetime, timezone
from google import genai
from google.genai import types
from dotenv import load_dotenv

from microstructure import (
    check_us_session,
    check_daily_drawdown,
    fetch_free_iex_snapshot,
    evaluate_catalyst_vs_tape,
    USMarketRegime,
)

load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
SHIELD_TOKEN = "secure_shield_key_2026"

def get_live_execution_mode() -> str:
    try:
        if os.path.exists("engine_state.json"):
            with open("engine_state.json", "r") as f:
                return json.load(f).get("mode", "AUTO").upper()
    except Exception:
        pass
    return "AUTO"


if not GEMINI_KEY:
    raise ValueError("GEMINI_API_KEY not found in .env")

client = genai.Client(api_key=GEMINI_KEY)

# Local zero-cost pre-filter to protect your Free Gemini API quota (15 RPM limit)
CATALYST_ROOTS = (
    "breaking", "explosion", "halt", "fda", "sec ", "8-k", "contract", "pentagon",
    "dod", "sanction", "tariff", "strike", "blockade", "embargo", "recall",
    "guidance", "acquire", "merger", "buyout", "bankrupt", "probe", "subpoena",
    "refinery", "pipeline", "outage", "cyberattack", "breach", "opec", "hormuz"
)
_seen_headlines: set[str] = set()
_last_gemini_call: float = 0.0
MIN_GEMINI_INTERVAL_SEC = 4.2  # Max ~14 calls/min -> Never hits Free Tier 429 error!

SYSTEM_PROMPT = """You are an institutional quantitative catalyst & provenance classifier.
Analyze the financial news headline and summary.
1. Determine if this is a SEVERE, IMMEDIATE macroeconomic shock, regulatory approval/ban, major contract win, or supply chain disruption.
2. Classify source_tier:
   - 1: Official SEC filing (8-K, 10-Q), FDA/DoD government release, or formal company guidance.
   - 2: Major institutional wire (Reuters, Bloomberg, Benzinga Breaking, WSJ).
   - 3: Secondary media, foreign rumor, opinion blog, or social chatter.
3. Score sentiment from 0.0 (neutral/noise) to 1.0 (extreme bullish shock for the target ticker).
4. Identify the single best US stock ticker to BUY (or "NONE" if routine/bearish/ambiguous).
Return raw JSON only:
{"trade": true/false, "ticker": "SYMBOL", "source_tier": 1|2|3, "sentiment": 0.85, "reason": "short reason"}"""


def passes_free_quota_guard(headline: str) -> bool:
    """Zero-cost local filter: deduplicates and checks for catalyst roots before spending Gemini quota."""
    norm = headline.strip().lower()
    if not norm or norm in _seen_headlines:
        return False
    _seen_headlines.add(norm)
    if len(_seen_headlines) > 500:
        _seen_headlines.clear()
    return any(root in norm for root in CATALYST_ROOTS)


async def analyze_and_trade(headline: str, summary: str, force_offhours: bool = False):
    global _last_gemini_call
    print(f"\n[NEWS] {headline}")

    # 1. Rate-limit guard to keep Gemini 100% inside Free Tier (15 RPM)
    elapsed = time.time() - _last_gemini_call
    if elapsed < MIN_GEMINI_INTERVAL_SEC:
        await asyncio.sleep(MIN_GEMINI_INTERVAL_SEC - elapsed)

    try:
        prompt = f"{SYSTEM_PROMPT}\n\nHeadline: {headline}\nSummary: {summary}"
        _last_gemini_call = time.time()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        decision = json.loads(response.text)
        print(f"[GEMINI] {decision}")

        if not decision.get("trade") or decision.get("ticker", "NONE") == "NONE":
            return

        ticker = decision["ticker"].strip().upper()
        tier = int(decision.get("source_tier", 2))
        sentiment = float(decision.get("sentiment", 0.75))

        # 2. Check US Session State Machine
        session_ok, session_msg = check_us_session(datetime.now(timezone.utc), "INTRADAY")
        if not session_ok and not force_offhours:
            print(f"[SESSION GATE BLOCKED] {session_msg}. Use --force-offhours to simulate off-market.")
            return

        # 3. Check -2.5% Daily Drawdown Circuit Breaker
        dd_ok, dd_pct = check_daily_drawdown(ALPACA_KEY, ALPACA_SECRET)
        if not dd_ok:
            print(f"[CIRCUIT BREAKER] Daily drawdown {dd_pct:.2%} breached -2.5% limit! Halting.")
            return

        # 4. Check Free IEX Microstructure (Micro-Price, Spread & D_tape)
        try:
            snap = fetch_free_iex_snapshot(ticker, ALPACA_KEY, ALPACA_SECRET)
            regime, conviction, tape_msg = evaluate_catalyst_vs_tape(tier, sentiment, snap, "INTRADAY")
            print(f"[TAPE AUDIT] {ticker} | {tape_msg}")
            if regime != USMarketRegime.HIGH_VOL_TREND and not force_offhours:
                print(f"[TAPE VETO] Blocked {ticker} due to {regime.name}")
                return
        except Exception as tape_exc:
            print(f"[TAPE WARNING] Live IEX book unavailable ({tape_exc}).")
            if not force_offhours:
                print("[TAPE VETO] Failing closed because market is closed / no IEX quote.")
                return

        # 5. Route Verified Signal to Project Shield Server
        print(f"[!] VERIFIED MACRO SHOCK. Routing {ticker} to Project Shield Pipeline...")
        res = requests.post(
            "http://127.0.0.1:8000/webhook",
            json={"ticker": ticker, "mode": get_live_execution_mode(), "timeframe": "INTRADAY"},
            headers={"x-shield-auth": SHIELD_TOKEN},
            timeout=10,
        )
        print(f"[PIPELINE RESPONSE] {res.status_code} - {res.text}")

    except Exception as e:
        print(f"[-] Analysis failed: {e}")


async def stream_news():
    url = "wss://stream.data.alpaca.markets/v1beta1/news"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"action": "auth", "key": ALPACA_KEY, "secret": ALPACA_SECRET}))
        print(f"[+] Alpaca Auth: {await ws.recv()}")

        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        print(f"[+] Alpaca Subscription: {await ws.recv()}")
        print("[+] Armed & Online: Zero-Cost Gemini + IEX Microstructure Engine")

        while True:
            msg = await ws.recv()
            for event in json.loads(msg):
                if event.get("T") == "n":
                    headline = event.get("headline", "")
                    if passes_free_quota_guard(headline):
                        await analyze_and_trade(headline, event.get("summary", ""))
                    else:
                        print(f"[SKIPPED ROUTINE NOISE] {headline[:80]}...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", type=str, help="Simulate a breaking headline")
    parser.add_argument("--force-offhours", action="store_true", help="Bypass session/IEX gate for off-hours testing")
    args = parser.parse_args()

    if args.test:
        print("[SIMULATION MODE] Injecting test headline...")
        asyncio.run(analyze_and_trade(args.test, "Simulated summary from CLI.", force_offhours=args.force_offhours))
    else:
        asyncio.run(stream_news())
