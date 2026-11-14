"""
test_stream.py  --  Project Shield: Alpaca real-time data feed test

Connects to the Alpaca market-data WebSocket (alpaca-py StockDataStream),
subscribes to quotes and trades for one ticker, and prints each update.

Setup:
    pip install alpaca-py python-dotenv

    .env file (same folder as this script, or any parent folder):
        ALPACA_API_KEY=your_key_here
        ALPACA_SECRET_KEY=your_secret_here

Run:
    python test_stream.py

Notes:
    * Data only flows while US markets are open (9:30-16:00 ET, Mon-Fri),
      plus pre/post-market for some feeds.
    * Free Alpaca accounts only get the IEX feed (a single exchange), so the
      bid/ask and trade volume will look thinner than the consolidated tape.
      Switch FEED to DataFeed.SIP if you have a paid market-data plan.
    * Alpaca allows ONE websocket connection per account on most plans. If you
      see "connection limit exceeded", another instance is already running.
"""

import os
import sys

from dotenv import load_dotenv
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SYMBOL = "SPY"          # highly active ticker -> you'll see updates quickly
FEED = DataFeed.IEX     # IEX = free tier. Use DataFeed.SIP for paid plans.

# --------------------------------------------------------------------------- #
# Load credentials from .env
# --------------------------------------------------------------------------- #
load_dotenv()  # reads .env and puts the values into os.environ

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    sys.exit("ERROR: ALPACA_API_KEY and/or ALPACA_SECRET_KEY missing from .env")

# --------------------------------------------------------------------------- #
# Create the stream client
# --------------------------------------------------------------------------- #
stream = StockDataStream(API_KEY, SECRET_KEY, feed=FEED)


# --------------------------------------------------------------------------- #
# Async handlers -- alpaca-py awaits these every time a message arrives.
# Each one receives a model object (Quote / Trade) with typed attributes.
# --------------------------------------------------------------------------- #
async def on_quote(quote) -> None:
    """Called on every top-of-book quote update (NBBO-style bid/ask)."""
    ts = quote.timestamp.strftime("%H:%M:%S.%f")[:-3]   # HH:MM:SS.mmm (UTC)
    spread = quote.ask_price - quote.bid_price
    print(
        f"[QUOTE] {ts}  {quote.symbol:<5} "
        f"BID {quote.bid_price:>9.2f} x {quote.bid_size:<6} | "
        f"ASK {quote.ask_price:>9.2f} x {quote.ask_size:<6} | "
        f"spread {spread:.2f}"
    )


async def on_trade(trade) -> None:
    """Called every time a trade prints."""
    ts = trade.timestamp.strftime("%H:%M:%S.%f")[:-3]   # HH:MM:SS.mmm (UTC)
    print(
        f"[TRADE] {ts}  {trade.symbol:<5} "
        f"PRICE {trade.price:>9.2f} | SIZE {trade.size}"
    )


# --------------------------------------------------------------------------- #
# Subscribe + run
# --------------------------------------------------------------------------- #
def main() -> None:
    # Register each handler for the symbol(s) we care about.
    stream.subscribe_quotes(on_quote, SYMBOL)
    stream.subscribe_trades(on_trade, SYMBOL)

    print(f"Connecting to Alpaca ({FEED.value} feed) -- streaming {SYMBOL}. Ctrl+C to stop.\n")

    try:
        stream.run()  # blocking call; manages its own asyncio event loop
    except KeyboardInterrupt:
        print("\nStream stopped by user.")


if __name__ == "__main__":
    main()
