"""Простая статистика использования бота в SQLite.

События пишутся в stats.db рядом с ботом. Файл маленький, записи редкие,
поэтому синхронный sqlite3 не мешает event loop.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DB = Path(__file__).parent / "stats.db"

_conn = sqlite3.connect(_DB)
_conn.execute(
    "CREATE TABLE IF NOT EXISTS events ("
    "  ts      INTEGER NOT NULL,"   # unix-время события
    "  user_id INTEGER NOT NULL,"
    "  event   TEXT    NOT NULL"    # start / request / download_ok / ...
    ")"
)
_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)")
_conn.commit()


def track(user_id: int, event: str) -> None:
    """Записывает событие."""
    _conn.execute(
        "INSERT INTO events (ts, user_id, event) VALUES (?, ?, ?)",
        (int(time.time()), user_id, event),
    )
    _conn.commit()


def _count(query: str, *args) -> int:
    return _conn.execute(query, args).fetchone()[0]


def summary() -> dict[str, int]:
    """Сводка для /stats: пользователи и клипы за всё время / 7 дней / 24 часа."""
    now = int(time.time())
    day = now - 24 * 3600
    week = now - 7 * 24 * 3600

    return {
        "users_total": _count("SELECT COUNT(DISTINCT user_id) FROM events"),
        "users_week": _count(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?", week
        ),
        "users_day": _count(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= ?", day
        ),
        "clips_total": _count(
            "SELECT COUNT(*) FROM events WHERE event = 'download_ok'"
        ),
        "clips_week": _count(
            "SELECT COUNT(*) FROM events WHERE event = 'download_ok' AND ts >= ?",
            week,
        ),
        "clips_day": _count(
            "SELECT COUNT(*) FROM events WHERE event = 'download_ok' AND ts >= ?",
            day,
        ),
        "requests_total": _count(
            "SELECT COUNT(*) FROM events WHERE event = 'request'"
        ),
        "fails_total": _count(
            "SELECT COUNT(*) FROM events WHERE event = 'download_fail'"
        ),
    }
