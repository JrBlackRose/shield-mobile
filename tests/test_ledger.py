import sqlite3
from decimal import Decimal

import pytest

import ledger


def test_init_is_idempotent(db_rows):
    ledger.init_db()
    ledger.init_db()
    assert db_rows("audit_logs") == []


def test_log_audit_row(db_rows):
    ledger.log_audit("AAPL", "REJECTED", "Dilution Trap", "SWING")
    (row,) = db_rows("audit_logs")
    assert (row["ticker"], row["status"], row["reason"], row["timeframe"]) == ("AAPL", "REJECTED", "Dilution Trap", "SWING")
    assert row["timestamp"]


def test_log_execution_row_defaults_to_open(db_rows):
    ledger.log_execution("oid-1", "AAPL", 28, Decimal("341.74"), Decimal("307.57"),
                         Decimal("444.26"), Decimal("956.76"), "SWING")
    (row,) = db_rows("trade_executions")
    assert row["shares"] == 28 and row["status"] == "OPEN"
    assert row["entry_price"] == pytest.approx(341.74)


def test_duplicate_order_id_is_refused():
    args = ("oid-1", "AAPL", 1, 1.0, 0.9, 1.3, 0.1, "SWING")
    ledger.log_execution(*args)
    with pytest.raises(sqlite3.IntegrityError):
        ledger.log_execution(*args)
