import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

DB_PATH = "shield_ledger.db"

def init_db():
    """Initializes the SQLite tables for audit events and executions."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        
        # Table 1: Regulatory Audits
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                timeframe TEXT NOT NULL
            )
        """)
        
        # Table 2: Executed Trades
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_executions (
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

def log_audit(ticker: str, status: str, reason: str, timeframe: str):
    """Records the outcome of an SEC regulatory audit."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO audit_logs (timestamp, ticker, status, reason, timeframe)
            VALUES (?, ?, ?, ?, ?)
        """, (datetime.now(timezone.utc).isoformat(), ticker, status, reason, timeframe))
        conn.commit()

def log_execution(order_id: str, ticker: str, shares: int, entry: Decimal, stop: Decimal, target: Decimal, risk: Decimal, timeframe: str):
    """Records an executed parent bracket trade."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trade_executions (
                timestamp, order_id, ticker, shares, entry_price, stop_loss, take_profit, max_risk_usd, timeframe
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            str(order_id),
            ticker,
            int(shares),
            float(entry),
            float(stop),
            float(target),
            float(risk),
            timeframe
        ))
        conn.commit()

if __name__ == "__main__":
    init_db()
    print("[+] Database shield_ledger.db initialized.")
