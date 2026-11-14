"""Shared fixtures. NOTHING in this suite touches the network, Alpaca, EDGAR or Telegram."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# Dummy credentials so load_credentials() never reads a real .env by accident.
os.environ["SHIELD_AUTH_TOKEN"] = "test-shield-auth"
os.environ["ALPACA_API_KEY"] = "test-key"
os.environ["ALPACA_SECRET_KEY"] = "test-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["TELEGRAM_CHAT_ID"] = "1"


@pytest.fixture(autouse=True)
def temp_ledger(tmp_path, monkeypatch):
    """Every test gets its own empty SQLite ledger."""
    import ledger
    monkeypatch.setattr(ledger, "DB_PATH", str(tmp_path / "test_ledger.db"))
    ledger.init_db()
    return ledger


@pytest.fixture
def db_rows():
    """db_rows('audit_logs') -> list[dict]"""
    import ledger

    def _rows(table: str):
        with sqlite3.connect(ledger.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    return _rows


def article(days_ago: float = 1.0):
    return SimpleNamespace(created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))
