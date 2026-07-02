"""Премиум-подписка через Telegram Stars: хранилище подписчиков в SQLite.

Telegram сам списывает Stars и продлевает подписку каждые 30 дней, присылая
боту новый successful_payment, — бот просто сдвигает дату окончания.
Файл premium.db лежит рядом с ботом, записи редкие — синхронный sqlite3
event loop не мешает (та же логика, что в stats.py).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_DB = Path(__file__).parent / "premium.db"

_conn = sqlite3.connect(_DB)
_conn.execute(
    "CREATE TABLE IF NOT EXISTS subscriptions ("
    "  user_id      INTEGER PRIMARY KEY,"
    "  expires      INTEGER NOT NULL,"  # unix-время окончания подписки
    "  is_recurring INTEGER NOT NULL DEFAULT 0"
    ")"
)
_conn.commit()


def activate(user_id: int, expires: int, is_recurring: bool) -> None:
    """Включает или продлевает подписку.

    Новая оплата никогда не укорачивает уже оплаченный срок — берём
    максимум из старой и новой даты окончания.
    """
    row = _conn.execute(
        "SELECT expires FROM subscriptions WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row and row[0] > expires:
        expires = row[0]
    _conn.execute(
        "INSERT INTO subscriptions (user_id, expires, is_recurring) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "  expires = excluded.expires, is_recurring = excluded.is_recurring",
        (user_id, expires, int(is_recurring)),
    )
    _conn.commit()


def expires_at(user_id: int) -> int | None:
    """Unix-время окончания подписки (None, если подписки никогда не было)."""
    row = _conn.execute(
        "SELECT expires FROM subscriptions WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row else None


def is_premium(user_id: int) -> bool:
    """Активна ли подписка прямо сейчас."""
    exp = expires_at(user_id)
    return exp is not None and exp > int(time.time())
