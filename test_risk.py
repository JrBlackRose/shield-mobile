"""
test_risk.py  --  Project Shield: Anti-Greed Position Calculator

Given an account balance and an entry price, works out the largest position
we are allowed to take so that a 10% stop-loss costs us no more than a fixed
slice of the account (default 1%), with a 30% take-profit (1:3 risk/reward).

Run:
    python test_risk.py

Design notes:
    * All maths uses Decimal internally. Plain floats can turn an exact answer
      like 500.0 into 499.99999999999994, and math.floor() would then silently
      buy one share too few. Inputs and outputs are still ordinary floats.
    * Stop / target prices are rounded to a valid US-stock tick size:
      $0.01 at or above $1.00, $0.0001 below $1.00 (brokers reject sub-penny
      prices on stocks priced at $1 or more).
    * Shares are sized from the ROUNDED stop price, i.e. the loss the broker
      will actually realise, so the loss can never exceed the risk limit.
    * math.floor() rounds the share count DOWN, so the loss at the stop is at
      or just under the limit (equal only when the division is exact).
"""

import math
from decimal import Decimal, ROUND_HALF_UP

# --------------------------------------------------------------------------- #
# Hardcoded strategy rules (deliberately not parameters -- no tweaking mid-trade)
# --------------------------------------------------------------------------- #
STOP_LOSS_PCT = Decimal("0.10")      # exit if price falls 10% below entry
TAKE_PROFIT_PCT = Decimal("0.30")    # exit if price rises 30% above entry (1:3)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _round_to_tick(price: Decimal) -> Decimal:
    """Round a price to the nearest valid tick ($0.01 if >= $1, else $0.0001)."""
    tick = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return price.quantize(tick, rounding=ROUND_HALF_UP)


def _fmt_price(price: float) -> str:
    """Show 2 decimals for prices >= $1, 4 decimals for sub-dollar prices."""
    return f"{price:,.2f}" if price >= 1 else f"{price:,.4f}"


# --------------------------------------------------------------------------- #
# Core calculation
# --------------------------------------------------------------------------- #
def calculate_bracket(account_balance: float, current_price: float, risk_pct: float = 0.01) -> dict:
    """
    Size a bracket order (entry + stop-loss + take-profit).

    Args:
        account_balance: total account value in dollars.
        current_price:   planned entry price per share.
        risk_pct:        fraction of the account we may lose if the stop hits
                         (0.01 = 1%).

    Returns:
        dict with account_balance, risk_pct, max_risk, entry, shares, capital,
        stop_loss, take_profit, loss_at_stop, gain_at_target, capped_by_cash.
    """
    # 1. Validate inputs -- bad numbers here would mean bad real-money orders.
    if account_balance <= 0:
        raise ValueError("account_balance must be positive")
    if current_price <= 0:
        raise ValueError("current_price must be positive")
    if not 0 < risk_pct <= 1:
        raise ValueError("risk_pct must be between 0 and 1 (e.g. 0.01 for 1%)")

    # Convert via str() so Decimal sees 1.5, not 1.4999999999999999778...
    balance = Decimal(str(account_balance))
    entry = Decimal(str(current_price))
    risk = Decimal(str(risk_pct))

    # 2. Maximum dollars we are allowed to lose on this trade.
    max_risk = balance * risk

    # 3. Stop-loss and take-profit prices (rounded to a valid tick).
    stop_loss = _round_to_tick(entry * (1 - STOP_LOSS_PCT))
    take_profit = _round_to_tick(entry * (1 + TAKE_PROFIT_PCT))

    # 4. Dollars lost per share if the stop is hit.
    risk_per_share = entry - stop_loss
    if risk_per_share <= 0:
        raise ValueError("price too low: stop-loss rounds to the entry price")

    # 5. Shares: max_risk / risk_per_share, rounded DOWN (no fractional shares).
    shares_by_risk = math.floor(max_risk / risk_per_share)

    # Safety cap: never buy more than the account can pay for in cash. At the
    # default 1% risk, capital is only ~10% of the account so this never
    # triggers, but a risk_pct above 10% would otherwise demand leverage.
    shares_by_cash = math.floor(balance / entry)
    shares = min(shares_by_risk, shares_by_cash)

    # 6. Totals for the receipt.
    capital = shares * entry
    loss_at_stop = shares * risk_per_share
    gain_at_target = shares * (take_profit - entry)

    return {
        "account_balance": float(balance),
        "risk_pct": float(risk),
        "max_risk": float(max_risk),
        "entry": float(entry),
        "shares": int(shares),
        "capital": float(capital),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "loss_at_stop": float(loss_at_stop),
        "gain_at_target": float(gain_at_target),
        "capped_by_cash": shares_by_cash < shares_by_risk,
    }


# --------------------------------------------------------------------------- #
# Terminal receipt
# --------------------------------------------------------------------------- #
def print_receipt(r: dict) -> None:
    """Print the result of calculate_bracket() as a structured receipt."""
    width = 46

    def row(label: str, value: str) -> None:
        print(f" {label:<20}: {value:>{width - 24}}")

    print("=" * width)
    print("  PROJECT SHIELD -- BRACKET ORDER RECEIPT".ljust(width))
    print("=" * width)
    row("Account Balance", f"${r['account_balance']:,.2f}")
    row(f"Max Risk ({r['risk_pct']:.1%})", f"${r['max_risk']:,.2f}")
    row("Entry Price", f"${_fmt_price(r['entry'])}")
    row("Shares to Buy", f"{r['shares']:,}")
    row("Capital Deployed", f"${r['capital']:,.2f}")
    print("-" * width)
    row(f"Stop Loss (-{STOP_LOSS_PCT:.0%})", f"${_fmt_price(r['stop_loss'])}")
    row(f"Take Profit (+{TAKE_PROFIT_PCT:.0%})", f"${_fmt_price(r['take_profit'])}")
    print("-" * width)
    row("Loss if stopped", f"-${r['loss_at_stop']:,.2f}")
    row("Gain if target hit", f"+${r['gain_at_target']:,.2f}")
    print("=" * width)

    # Flag the two situations where the user should look twice.
    if r["shares"] == 0:
        print(" WARNING: 0 shares -- risk budget too small for this price. NO TRADE.")
    elif r["capped_by_cash"]:
        print(" NOTE: position capped by available cash (risk limit not fully used).")


# --------------------------------------------------------------------------- #
# Test execution: $10,000 account, stock trading at $1.50
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    result = calculate_bracket(account_balance=10_000.00, current_price=1.50)
    print_receipt(result)
