"""
Lapisan akses database (SQLite).

Berisi definisi skema, migrasi otomatis untuk database lama,
dan helper koneksi.
"""

import os
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

DATABASE = os.path.join(BASE_DIR, "monitoring.db")

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def current_time():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_db():
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


# ---------------------------------------------------------
# Skema
# ---------------------------------------------------------

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS devices (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        location     TEXT NOT NULL,
        ip_address   TEXT NOT NULL UNIQUE,
        description  TEXT,
        status       TEXT DEFAULT 'UNKNOWN',
        latency      REAL,
        checked_at   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monitoring_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id   INTEGER NOT NULL,
        status      TEXT NOT NULL,
        latency     REAL,
        checked_at  TEXT NOT NULL,
        FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS path_traces (
        device_id         INTEGER PRIMARY KEY,
        hops_json         TEXT,
        bottleneck_hop    INTEGER,
        bottleneck_ip     TEXT,
        bottleneck_label  TEXT,
        bottleneck_delta  REAL,
        created_at        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_logs_device_time
    ON monitoring_logs(device_id, checked_at)
    """,
    # Pengaturan aplikasi (token bot Telegram, saklar notifikasi, dll.)
    """
    CREATE TABLE IF NOT EXISTS settings (
        key    TEXT PRIMARY KEY,
        value  TEXT
    )
    """,
    # Akun Telegram penerima notifikasi gangguan
    """
    CREATE TABLE IF NOT EXISTS telegram_accounts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        chat_id     TEXT NOT NULL UNIQUE,
        created_at  TEXT
    )
    """,
    # Daftar teknisi / support yang dapat dihubungi
    """
    CREATE TABLE IF NOT EXISTS technicians (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        contact     TEXT NOT NULL,
        created_at  TEXT
    )
    """,
]

# Kolom tambahan (ditambahkan otomatis pada database lama)
DEVICE_COLUMNS = {
    "device_type": "TEXT DEFAULT 'AP'",
    "building": "TEXT",
    "floor": "TEXT",
    "room": "TEXT",
    "parent_id": "INTEGER",
    "packet_loss": "REAL",
    "jitter": "REAL",
    "cause_code": "TEXT",
    "cause_title": "TEXT",
    "cause_detail": "TEXT",
    "cause_scope": "TEXT",
    "recommendations": "TEXT",
    "severity": "INTEGER DEFAULT 0",
}

LOG_COLUMNS = {
    "packet_loss": "REAL",
    "jitter": "REAL",
}


def _existing_columns(cursor, table):
    return {
        row[1]
        for row in cursor.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def _migrate(cursor, table, columns):
    existing = _existing_columns(cursor, table)

    for name, definition in columns.items():
        if name not in existing:
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )
            print(f"[MIGRASI] kolom {table}.{name} ditambahkan")


def init_database():
    connection = get_db()
    cursor = connection.cursor()

    for statement in SCHEMA:
        cursor.execute(statement)

    _migrate(cursor, "devices", DEVICE_COLUMNS)
    _migrate(cursor, "monitoring_logs", LOG_COLUMNS)

    connection.commit()

    total_users = cursor.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    if total_users == 0:
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, created_at)
            VALUES (?, ?, ?)
            """,
            (
                DEFAULT_ADMIN_USERNAME,
                generate_password_hash(DEFAULT_ADMIN_PASSWORD),
                current_time(),
            ),
        )
        connection.commit()

        print(
            "[SETUP] Akun admin default dibuat -> "
            f"{DEFAULT_ADMIN_USERNAME} / {DEFAULT_ADMIN_PASSWORD} "
            "(segera ganti password ini)."
        )

    connection.close()


def get_setting(key, default=None, connection=None):
    own = connection is None
    if own:
        connection = get_db()

    row = connection.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()

    if own:
        connection.close()

    return row["value"] if row and row["value"] is not None else default


def set_setting(key, value):
    connection = get_db()

    connection.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )

    connection.commit()
    connection.close()


def full_location(device):
    """Susun keterangan lokasi fisik yang mudah dibaca manusia."""

    parts = []

    for key in ("building", "floor", "room"):
        value = device[key] if key in device.keys() else None

        if value:
            parts.append(str(value).strip())

    if not parts:
        return device["location"]

    return f"{device['location']} - " + ", ".join(parts)
