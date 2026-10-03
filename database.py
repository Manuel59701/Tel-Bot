"""
database.py  —  SQLite helpers for Dan Data Plans Bot
"""
import os
import sqlite3
from datetime import datetime
from config import DB_PATH


# ─── Connection ───────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _dt_str(dt: datetime) -> str:
    """SQLite-friendly timestamp (same shape as CURRENT_TIMESTAMP).

    datetime.isoformat() emits a 'T' separator, which breaks lexicographic
    comparison against CURRENT_TIMESTAMP in SQL ('T' > ' ').
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS orders (
            id            INTEGER  PRIMARY KEY AUTOINCREMENT,
            telegram_id   INTEGER  NOT NULL,
            plan_type     TEXT     NOT NULL,
            proof_file_id TEXT,
            status        TEXT     NOT NULL DEFAULT 'pending',
            token         TEXT,
            start_date    DATETIME,
            end_date      DATETIME,
            amount        INTEGER  NOT NULL,
            reminded      INTEGER  DEFAULT 0,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    # Default settings (won't overwrite if already set)
    defaults = [
        ("bank_name",       "Access Bank"),
        ("account_number",  "0123456789"),
        ("account_name",    "Dan Data Plans"),
        ("wifi_password",   "NotSetYet"),
    ]
    for k, v in defaults:
        con.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))

    # Migrate legacy ISO timestamps ('T' separator) to SQLite format
    con.execute(
        "UPDATE orders SET start_date=REPLACE(start_date,'T',' ') "
        "WHERE start_date LIKE '%T%'"
    )
    con.execute(
        "UPDATE orders SET end_date=REPLACE(end_date,'T',' ') "
        "WHERE end_date LIKE '%T%'"
    )
    con.commit()
    con.close()


# ─── Settings ─────────────────────────────────────────────────────────────────

def get_setting(key: str) -> str:
    con = _conn()
    row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    con.close()
    return row["value"] if row else ""


def set_setting(key: str, value: str):
    con = _conn()
    con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    con.commit()
    con.close()


# ─── Users ────────────────────────────────────────────────────────────────────

def upsert_user(telegram_id: int, username: str | None, first_name: str):
    con = _conn()
    con.execute(
        """INSERT INTO users (telegram_id, username, first_name) VALUES (?,?,?)
           ON CONFLICT(telegram_id) DO UPDATE SET
               username   = excluded.username,
               first_name = excluded.first_name""",
        (telegram_id, username, first_name),
    )
    con.commit()
    con.close()


# ─── Orders ───────────────────────────────────────────────────────────────────

def create_order(telegram_id: int, plan_type: str, proof_file_id: str, amount: int) -> int:
    con = _conn()
    cur = con.execute(
        "INSERT INTO orders (telegram_id, plan_type, proof_file_id, amount) VALUES (?,?,?,?)",
        (telegram_id, plan_type, proof_file_id, amount),
    )
    order_id = cur.lastrowid
    con.commit()
    con.close()
    return order_id


def get_order(order_id: int) -> dict | None:
    con = _conn()
    row = con.execute(
        """SELECT o.*, u.username, u.first_name
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.id = ?""",
        (order_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def approve_order(order_id: int, token: str, start_date: datetime, end_date: datetime):
    con = _conn()
    con.execute(
"UPDATE orders SET status='active', token=?, start_date=?, end_date=? WHERE id=?",
        (token, _dt_str(start_date), _dt_str(end_date), order_id),
    )
    con.commit()
    con.close()


def reject_order(order_id: int):
    con = _conn()
    con.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
    con.commit()
    con.close()


def get_active_subscription(telegram_id: int) -> dict | None:
    con = _conn()
    row = con.execute(
        """SELECT * FROM orders
           WHERE telegram_id=? AND status='active' AND end_date > CURRENT_TIMESTAMP
           ORDER BY end_date DESC LIMIT 1""",
        (telegram_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_all_active_subscriptions() -> list[dict]:
    con = _conn()
    rows = con.execute(
        """SELECT o.*, u.first_name, u.username
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.status='active' AND o.end_date > CURRENT_TIMESTAMP"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_pending_orders() -> list[dict]:
    con = _conn()
    rows = con.execute(
        """SELECT o.*, u.first_name, u.username
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.status='pending'
           ORDER BY o.created_at DESC"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def has_pending_order(telegram_id: int) -> bool:
    con = _conn()
    row = con.execute(
        "SELECT id FROM orders WHERE telegram_id=? AND status='pending' LIMIT 1",
        (telegram_id,),
    ).fetchone()
    con.close()
    return row is not None


# ─── Expiry & Reminders ───────────────────────────────────────────────────────

def expire_old_subscriptions() -> list[dict]:
    """Mark expired active orders and return them for notification."""
    con = _conn()
    rows = con.execute(
        """SELECT o.*, u.first_name, u.username
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.status='active' AND o.end_date <= CURRENT_TIMESTAMP"""
    ).fetchall()
    expired = [dict(r) for r in rows]
    if expired:
        con.execute(
            "UPDATE orders SET status='expired' WHERE status='active' AND end_date <= CURRENT_TIMESTAMP"
        )
        con.commit()
    con.close()
    return expired


def get_expiring_soon(hours: int = 24) -> list[dict]:
    """Return active orders expiring within `hours` hours that haven't been reminded yet."""
    con = _conn()
    rows = con.execute(
        f"""SELECT o.*, u.first_name, u.username
            FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
            WHERE o.status='active'
              AND o.end_date > CURRENT_TIMESTAMP
              AND o.end_date <= datetime('now', '+{hours} hours')
              AND o.reminded = 0"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def mark_reminded(order_id: int):
    con = _conn()
    con.execute("UPDATE orders SET reminded=1 WHERE id=?", (order_id,))
    con.commit()
    con.close()


# ─── Stats ────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    con = _conn()
    active  = con.execute("SELECT COUNT(*) FROM orders WHERE status='active' AND end_date > CURRENT_TIMESTAMP").fetchone()[0]
    pending = con.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
    users   = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total   = con.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('active','expired')").fetchone()[0]
    monthly = con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('active','expired') AND strftime('%Y-%m', created_at) = strftime('%Y-%m','now')"
    ).fetchone()[0]
    con.close()
    return {
        "active":           active,
        "pending":          pending,
        "total_users":      users,
        "total_earnings":   total,
        "monthly_earnings": monthly,
    }
