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


def test_log_execution_records_client_order_id(db_rows):
    ledger.log_execution(
        "oid-cid", "AAPL", 1, Decimal("100"), Decimal("90"), Decimal("130"),
        Decimal("10"), "SWING", client_order_id="shield-abc123",
    )
    (row,) = db_rows("trade_executions")
    assert row["client_order_id"] == "shield-abc123"


def test_init_migrates_legacy_trade_executions_table(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE trade_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                order_id TEXT NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                shares INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                max_risk_usd REAL NOT NULL,
                timeframe TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN'
            )
        """)
        conn.commit()

    monkeypatch.setattr(ledger, "DB_PATH", str(db_path))
    ledger.init_db()

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(trade_executions)")]
    assert "client_order_id" in columns

    ledger.log_execution(
        "oid-legacy", "AAPL", 1, Decimal("100"), Decimal("90"), Decimal("130"),
        Decimal("10"), "SWING", client_order_id="shield-legacy",
    )
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT client_order_id FROM trade_executions WHERE order_id = 'oid-legacy'"
        ).fetchone()[0] == "shield-legacy"
