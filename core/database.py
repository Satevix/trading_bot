"""
database.py — Capa de persistencia SATEVIS
Gestiona SQLite para todos los datos del bot y del dashboard.
"""
import sqlite3
import os
import hashlib
import secrets
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "satevis.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Mejor concurrencia
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Crea todas las tablas si no existen."""
    with get_conn() as conn:

        # ── Usuarios ─────────────────────────────────────────────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            username  TEXT UNIQUE NOT NULL,
            password  TEXT NOT NULL,          -- SHA256 hash
            created   TEXT DEFAULT (datetime('now'))
        )""")

        # ── Configuración del bot y API keys ─────────────────────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key       TEXT PRIMARY KEY,
            value     TEXT NOT NULL,
            updated   TEXT DEFAULT (datetime('now'))
        )""")

        # ── Trades ejecutados ─────────────────────────────────────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            binance_order_id TEXT,
            symbol          TEXT DEFAULT 'BTCUSDT',
            side            TEXT NOT NULL,        -- LONG / SHORT
            entry_price     REAL NOT NULL,
            exit_price      REAL,
            quantity        REAL NOT NULL,        -- BTC
            size_usdt       REAL NOT NULL,        -- valor posición
            sl_price        REAL,
            tp_price        REAL,
            liq_price       REAL,
            leverage        INTEGER DEFAULT 3,
            open_fee        REAL DEFAULT 0,
            close_fee       REAL DEFAULT 0,
            funding_cost    REAL DEFAULT 0,
            pnl_gross       REAL,                 -- antes de fees
            pnl_net         REAL,                 -- después de fees
            pnl_pct         REAL,
            result          TEXT,                 -- WIN / LOSS / LIQUIDATION
            close_reason    TEXT,                 -- TP / SL / LIQUIDATION / MANUAL / END
            signal_source   TEXT DEFAULT 'D_LOG_ACP',
            acp_angle       REAL,
            log_bias        INTEGER,
            opened_at       TEXT NOT NULL,
            closed_at       TEXT,
            duration_hours  REAL,
            capital_before  REAL,
            capital_after   REAL,
            notes           TEXT
        )""")

        # ── Señales generadas (incluyendo las no ejecutadas) ──────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT DEFAULT (datetime('now')),
            direction   INTEGER,               -- 1=LONG -1=SHORT 0=HOLD
            log_bias    INTEGER,
            acp_angle   REAL,
            macro_ok    INTEGER,
            slope_ok    INTEGER,
            executed    INTEGER DEFAULT 0,     -- 1 si generó trade
            reason_skip TEXT                   -- por qué no se ejecutó
        )""")

        # ── Historial de capital (equity curve real) ──────────────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS capital_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT DEFAULT (datetime('now')),
            balance     REAL NOT NULL,          -- saldo total USDT
            type        TEXT DEFAULT 'AUTO',    -- AUTO / DEPOSIT / WITHDRAWAL
            description TEXT
        )""")

        # ── Ingresos y egresos manuales ───────────────────────────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS capital_movements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT DEFAULT (datetime('now')),
            type        TEXT NOT NULL,          -- DEPOSIT / WITHDRAWAL
            amount      REAL NOT NULL,          -- positivo = ingreso
            description TEXT,
            balance_after REAL,
            registered_by TEXT DEFAULT 'user'
        )""")

        # ── Log de eventos del bot ────────────────────────────────
        conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT DEFAULT (datetime('now')),
            level   TEXT DEFAULT 'INFO',        -- INFO / WARNING / ERROR
            event   TEXT NOT NULL,
            detail  TEXT
        )""")

        # ── Configuración por defecto si es primera vez ───────────
        defaults = {
            "strategy":                   "D_LOG_ACP",
            "symbol":                     "BTCUSDT",
            "leverage":                   "3",
            "risk_pct":                   "1.0",
            "sl_pct":                     "1.5",
            "tp_pct":                     "3.0",
            "acp_threshold":              "0.04735",
            "sma_log_period":             "288",
            "ema_period":                 "144",
            "macro_ema":                  "200",
            "capital_initial":            "1000",
            "bot_status":                 "STOPPED",
            "testnet":                    "true",
            "binance_api_key":            "",
            "binance_secret":             "",
            # Telegram
            "telegram_token":             "",
            "telegram_chat_id":           "",
            "telegram_notify_filtered":   "false",
            "telegram_notify_errors":     "true",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v)
            )

        # Usuario admin por defecto (cambiar en primer login)
        default_pw = hash_password("satevis2024")
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)",
            ("admin", default_pw)
        )

    print("✅ Base de datos inicializada")


# ── Helpers de usuario ────────────────────────────────────────────
def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def verify_user(username: str, password: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT password FROM users WHERE username=?", (username,)
        ).fetchone()
    return row is not None and row["password"] == hash_password(password)

def change_password(username: str, new_pw: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password=? WHERE username=?",
            (hash_password(new_pw), username)
        )


# ── Config ────────────────────────────────────────────────────────
def get_config(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM config WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default

def set_config(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value, updated) VALUES (?, ?, datetime('now'))",
            (key, str(value))
        )

def get_all_config() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM config").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Trades ────────────────────────────────────────────────────────
def insert_trade(data: dict) -> int:
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" * len(data))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO trades ({cols}) VALUES ({placeholders})",
            list(data.values())
        )
    return cur.lastrowid

def close_trade(trade_id: int, data: dict):
    sets = ", ".join(f"{k}=?" for k in data)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE trades SET {sets} WHERE id=?",
            list(data.values()) + [trade_id]
        )

def get_open_trade():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None

def get_trades(limit=200, offset=0) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    return [dict(r) for r in rows]

def get_trade_stats() -> dict:
    with get_conn() as conn:
        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN result='LIQUIDATION' THEN 1 ELSE 0 END) as liquidations,
                SUM(CASE WHEN closed_at IS NOT NULL THEN pnl_gross ELSE 0 END) as total_pnl_gross,
                SUM(CASE WHEN closed_at IS NOT NULL THEN pnl_net ELSE 0 END) as total_pnl_net,
                SUM(open_fee + close_fee + funding_cost) as total_fees,
                AVG(CASE WHEN result='WIN' THEN pnl_pct END) as avg_win_pct,
                AVG(CASE WHEN result='LOSS' THEN pnl_pct END) as avg_loss_pct,
                MAX(pnl_pct) as best_trade,
                MIN(pnl_pct) as worst_trade
            FROM trades
            WHERE closed_at IS NOT NULL
        """).fetchone()
    return dict(stats) if stats else {}


# ── Capital ───────────────────────────────────────────────────────
def record_capital(balance: float, type_: str = "AUTO", desc: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO capital_history (balance, type, description) VALUES (?, ?, ?)",
            (balance, type_, desc)
        )

def get_capital_history(limit=500) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM capital_history ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def add_capital_movement(type_: str, amount: float, desc: str, balance_after: float):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO capital_movements
               (type, amount, description, balance_after)
               VALUES (?, ?, ?, ?)""",
            (type_, amount, desc, balance_after)
        )
    record_capital(balance_after, type_, desc)

def get_capital_movements(limit=100) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM capital_movements ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Signals ───────────────────────────────────────────────────────
def insert_signal(data: dict):
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" * len(data))
    with get_conn() as conn:
        conn.execute(
            f"INSERT INTO signals ({cols}) VALUES ({placeholders})",
            list(data.values())
        )

def get_recent_signals(limit=50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Bot events ────────────────────────────────────────────────────
def log_event(event: str, detail: str = "", level: str = "INFO"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bot_events (level, event, detail) VALUES (?, ?, ?)",
            (level, event, detail)
        )

def get_events(limit=100) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bot_events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
