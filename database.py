"""
database.py  —  SQLite helpers for Dan Data Plans Bot
"""
import os
import sqlite3
from datetime import datetime
from config import DB_PATH


# ─── Connection ───────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _dt_str(dt: datetime) -> str:
    """SQLite-friendly timestamp (same shape as CURRENT_TIMESTAMP).

    datetime.isoformat() emits a 'T' separator, which breaks lexicographic
    comparison against CURRENT_TIMESTAMP in SQL ('T' > ' ').
    """
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# Every start_date/end_date is written from datetime.now(), i.e. LOCAL time.
# SQLite's bare CURRENT_TIMESTAMP / datetime('now') are UTC, so comparing the
# two directly expires every plan one hour early on a UTC+1 machine. All
# end_date comparisons must therefore use _NOW.
#
# created_at is left on the UTC default: it is only ever bucketed against
# strftime(...,'now'), which is UTC too, so that stays self-consistent.
_NOW = "datetime('now','localtime')"


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

    # ── Additive migrations (safe on existing installs) ──
    _migrate(con)

    # Default settings (won't overwrite if already set)
    defaults = [
        ("bank_name",         "Opay"),
        ("account_number",    "9039287005"),
        ("account_name",      "AWSEOME CHUKWUEBUKA PATRICK"),
        ("wifi_password",     "NotSetYet"),
        ("router_name",       "DanRouter-5G"),
        ("router_ip",         "192.168.8.1"),
        ("support_handle",    "@rare_dan01"),
        ("admin_alert_hours", "12"),
        ("access_mode",       "per_user_ssid"),
        ("ssid_prefix",       "DanNet"),
    ]
    for k, v in defaults:
        con.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, v))

    # Legacy: the shared WiFi password became the router password.
    con.execute(
        "INSERT OR IGNORE INTO settings (key, value) "
        "SELECT 'router_password', value FROM settings WHERE key='wifi_password'"
    )

    # Earlier builds shipped placeholder payment details; replace them with the
    # real ones, but only where the value is still that placeholder. Anything
    # an admin typed themselves is left alone.
    con.execute(
        "UPDATE settings SET value=? WHERE key='bank_name' AND value=?",
        ("Opay", "Access Bank"),
    )
    con.execute(
        "UPDATE settings SET value=? WHERE key='account_number' AND value=?",
        ("9039287005", "0123456789"),
    )
    con.execute(
        "UPDATE settings SET value=? WHERE key='account_name' AND value=?",
        ("AWSEOME CHUKWUEBUKA PATRICK", "Dan Data Plans"),
    )

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


# Columns added after v1. Each is applied only when missing.
_ORDER_COLUMNS = (
    ("router_name",     "TEXT"),
    ("router_password", "TEXT"),
    ("revoked",         "INTEGER DEFAULT 0"),
    ("revoked_at",      "DATETIME"),
    ("admin_reminded",  "INTEGER DEFAULT 0"),
    ("network_name",    "TEXT"),
    ("access_password", "TEXT"),
    ("macs",            "TEXT"),
    ("devices",         "TEXT"),
    ("max_devices",     "INTEGER DEFAULT 2"),
    # Second admin alert, fired once the plan is actually used up. Kept separate
    # from admin_reminded so a pre-expiry warning does not swallow it.
    ("admin_exhausted", "INTEGER DEFAULT 0"),
)


def _migrate(con: sqlite3.Connection):
    existing = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
    for name, decl in _ORDER_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE orders ADD COLUMN {name} {decl}")
    con.commit()


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


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return default


# ─── Router credentials (shared by every subscriber) ──────────────────────────

def get_router_creds() -> dict:
    return {
        "name":     get_setting("router_name"),
        "ip":       get_setting("router_ip"),
        "password": get_setting("router_password"),
    }


def set_router_password(password: str):
    """Rotate the shared router password used by every subscriber."""
    set_setting("router_password", password)


def admin_alert_hours() -> int:
    return get_int_setting("admin_alert_hours", 12)


# ─── Per-subscriber access profiles ───────────────────────────────────────────
# An MTN 5G ODU has no per-user accounts: customers pick an SSID and type one
# WPA2 password. So "per-user password" is emulated with one network per
# subscriber (access_mode='per_user_ssid'), falling back to a single shared
# network gated by MAC whitelist (access_mode='shared_mac').

def access_mode() -> str:
    mode = get_setting("access_mode")
    return mode if mode in ("per_user_ssid", "shared_mac") else "per_user_ssid"


def ssid_prefix() -> str:
    prefix = (get_setting("ssid_prefix") or "DanNet").strip()
    # SSIDs are capped at 32 chars and look awful with spaces
    return "".join(c for c in prefix if not c.isspace())[:20] or "DanNet"


def set_access_profile(order_id: int, network_name: str, access_password: str):
    con = _conn()
    con.execute(
        "UPDATE orders SET network_name=?, access_password=? WHERE id=?",
        (network_name, access_password, order_id),
    )
    con.commit()
    con.close()


def rotate_access_password(order_id: int, access_password: str):
    con = _conn()
    con.execute(
        "UPDATE orders SET access_password=? WHERE id=?", (access_password, order_id)
    )
    con.commit()
    con.close()


def set_macs(order_id: int, macs: str):
    """Store a comma-separated MAC whitelist for one subscriber."""
    con = _conn()
    con.execute("UPDATE orders SET macs=? WHERE id=?", (macs, order_id))
    con.commit()
    con.close()


def set_devices(order_id: int, devices: str):
    """Store the device names a subscriber says they'll connect from."""
    con = _conn()
    con.execute("UPDATE orders SET devices=? WHERE id=?", (devices, order_id))
    con.commit()
    con.close()


def set_max_devices(order_id: int, max_devices: int):
    con = _conn()
    con.execute(
        "UPDATE orders SET max_devices=? WHERE id=?", (max_devices, order_id)
    )
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


def approve_order(
    order_id: int,
    token: str,
    start_date: datetime,
    end_date: datetime,
    network_name: str,
    access_password: str,
    house_network: str,
    house_password: str,
):
    con = _conn()
    con.execute(
        """UPDATE orders
           SET status='active', token=?, start_date=?, end_date=?,
               network_name=?, access_password=?,
               router_name=?, router_password=?,
               revoked=0, revoked_at=NULL,
               reminded=0, admin_reminded=0, admin_exhausted=0
           WHERE id=?""",
        (token, _dt_str(start_date), _dt_str(end_date),
         network_name, access_password, house_network, house_password, order_id),
    )
    con.commit()
    con.close()


def reject_order(order_id: int):
    con = _conn()
    con.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
    con.commit()
    con.close()


def revoke_subscription(order_id: int):
    """Pull a subscriber off the plan immediately."""
    con = _conn()
    con.execute(
        """UPDATE orders
           SET status='revoked', revoked=1, revoked_at=?
           WHERE id=?""",
        (_dt_str(datetime.now()), order_id),
    )
    con.commit()
    con.close()


def get_subscription(order_id: int) -> dict | None:
    """Fetch any order (any status) with its user info."""
    con = _conn()
    row = con.execute(
        """SELECT o.*, u.username, u.first_name
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.id = ?""",
        (order_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_latest_for_user(telegram_id: int) -> dict | None:
    """Most recent non-pending order for a user (for support lookups)."""
    con = _conn()
    row = con.execute(
        """SELECT * FROM orders
           WHERE telegram_id=? AND status IN ('active','expired','revoked')
           ORDER BY id DESC LIMIT 1""",
        (telegram_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_active_subscription(telegram_id: int) -> dict | None:
    con = _conn()
    row = con.execute(
        f"""SELECT * FROM orders
           WHERE telegram_id=? AND status='active' AND end_date > {_NOW}
           ORDER BY end_date DESC LIMIT 1""",
        (telegram_id,),
    ).fetchone()
    con.close()
    return dict(row) if row else None


def get_all_active_subscriptions() -> list[dict]:
    con = _conn()
    rows = con.execute(
        f"""SELECT o.*, u.first_name, u.username
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.status='active' AND o.end_date > {_NOW}"""
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
        f"""SELECT o.*, u.first_name, u.username
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.status='active' AND o.end_date <= {_NOW}"""
    ).fetchall()
    expired = [dict(r) for r in rows]
    if expired:
        con.execute(
            f"UPDATE orders SET status='expired' WHERE status='active' AND end_date <= {_NOW}"
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
              AND o.end_date > {_NOW}
              AND o.end_date <= datetime('now','localtime','+{int(hours)} hours')
              AND o.reminded = 0"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def mark_reminded(order_id: int):
    con = _conn()
    con.execute("UPDATE orders SET reminded=1 WHERE id=?", (order_id,))
    con.commit()
    con.close()


def mark_admin_reminded(order_id: int, kind: str = "soon"):
    """Record that one of the two admin alerts has been delivered.

    They are separate flags on purpose: warning '12h left' must not stop the
    'data exhausted' alert from firing later.
    """
    column = "admin_exhausted" if kind == "exhausted" else "admin_reminded"
    con = _conn()
    con.execute(f"UPDATE orders SET {column}=1 WHERE id=?", (order_id,))
    con.commit()
    con.close()


def reset_expiry_flags(order_id: int):
    """Clear every reminder flag so a renewed plan alerts from scratch."""
    con = _conn()
    con.execute(
        """UPDATE orders SET reminded=0, admin_reminded=0, admin_exhausted=0
           WHERE id=?""",
        (order_id,),
    )
    con.commit()
    con.close()


def get_running_out(hours: int = 0) -> list[dict]:
    """Orders the admin still has to act on, each tagged with `alert_kind`.

    Two independent alerts per plan:
      * 'soon'      -> active, expiring inside `hours`, pre-expiry notice not sent
      * 'exhausted' -> past its end date and not yet revoked, regardless of
                       whether expire_old_subscriptions() already flipped the
                       status to 'expired'

    The exhausted branch deliberately spans both statuses: only the admin can
    rotate the credentials on the router, so an order must not drop out of the
    queue just because its timer ran out.
    """
    con = _conn()
    rows = con.execute(
        f"""SELECT o.*, u.first_name, u.username,
                   CASE WHEN o.end_date <= {_NOW}
                        THEN 'exhausted' ELSE 'soon' END AS alert_kind
            FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
            WHERE (o.status='active'
                   AND o.admin_reminded = 0
                   AND o.end_date > {_NOW}
                   AND o.end_date <= datetime('now','localtime','+{int(hours)} hours'))
               OR (o.end_date <= {_NOW}
                   AND o.status IN ('active','expired')
                   AND o.revoked = 0
                   AND o.admin_exhausted = 0)
            ORDER BY o.end_date ASC"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_exhausted_orders() -> list[dict]:
    """Plans that ran out but have not been closed out by the admin yet."""
    con = _conn()
    rows = con.execute(
        """SELECT o.*, u.first_name, u.username
           FROM orders o JOIN users u ON o.telegram_id = u.telegram_id
           WHERE o.status='expired' AND o.revoked = 0
           ORDER BY o.end_date ASC"""
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ─── Stats ────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    con = _conn()
    active  = con.execute(f"SELECT COUNT(*) FROM orders WHERE status='active' AND end_date > {_NOW}").fetchone()[0]
    pending = con.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
    users   = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    revoked = con.execute("SELECT COUNT(*) FROM orders WHERE status='revoked'").fetchone()[0]
    running_out = con.execute(
        f"SELECT COUNT(*) FROM orders WHERE status='active' AND end_date <= {_NOW}"
    ).fetchone()[0]
    total   = con.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('active','expired','revoked')").fetchone()[0]
    monthly = con.execute(
        "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status IN ('active','expired','revoked') AND strftime('%Y-%m', created_at) = strftime('%Y-%m','now')"
    ).fetchone()[0]
    con.close()
    return {
        "active":           active,
        "pending":          pending,
        "revoked":          revoked,
        "running_out":      running_out,
        "total_users":      users,
        "total_earnings":   total,
        "monthly_earnings": monthly,
    }
