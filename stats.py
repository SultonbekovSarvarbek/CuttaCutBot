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

# Отзывы и предложения пользователей (отдельная таблица — хранит текст).
_conn.execute(
    "CREATE TABLE IF NOT EXISTS feedback ("
    "  ts       INTEGER NOT NULL,"   # unix-время отзыва
    "  user_id  INTEGER NOT NULL,"
    "  username TEXT,"               # @username на момент отзыва (если был)
    "  text     TEXT    NOT NULL"
    ")"
)
_conn.commit()


def track(user_id: int, event: str) -> None:
    """Записывает событие."""
    _conn.execute(
        "INSERT INTO events (ts, user_id, event) VALUES (?, ?, ?)",
        (int(time.time()), user_id, event),
    )
    _conn.commit()


def save_feedback(user_id: int, username: str | None, text: str) -> None:
    """Сохраняет отзыв/предложение пользователя."""
    _conn.execute(
        "INSERT INTO feedback (ts, user_id, username, text) VALUES (?, ?, ?, ?)",
        (int(time.time()), user_id, username, text),
    )
    _conn.commit()


def recent_feedback(limit: int = 10) -> list[tuple[int, int, str | None, str]]:
    """Последние отзывы: список (ts, user_id, username, text), новые сверху."""
    rows = _conn.execute(
        "SELECT ts, user_id, username, text FROM feedback "
        "ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return rows


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
