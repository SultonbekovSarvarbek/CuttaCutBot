"""Распознавание музыки в клипе через Shazam (библиотека shazamio).

Необязательная фича: если shazamio не установлена или Shazam недоступен,
бот продолжает работать без распознавания — все ошибки гасятся здесь.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("clipbot.music")

try:
    from shazamio import Shazam
except ImportError:  # библиотека не установлена — фича просто выключена
    Shazam = None
    logger.warning("shazamio не установлена — распознавание музыки отключено")


@dataclass
class MusicMatch:
    """Найденный трек: название, исполнитель и ссылка на shazam.com."""

    title: str
    artist: str
    url: str


async def recognize_music(audio_path: Path, timeout: int = 45) -> MusicMatch | None:
    """Распознаёт музыку в аудиофайле. None — не распознано или ошибка.

    Shazam'у достаточно фрагмента, поэтому отправляем mp3 клипа как есть.
    """
    if Shazam is None:
        return None

    shazam = Shazam()
    # В старых версиях shazamio метод назывался recognize_song.
    recognize = getattr(shazam, "recognize", None) or shazam.recognize_song
    try:
        result = await asyncio.wait_for(recognize(str(audio_path)), timeout)
    except Exception as exc:  # noqa: BLE001 — сеть, формат, таймаут: не критично
        logger.warning("Music recognition failed: %s", exc)
        return None

    track = (result or {}).get("track") or {}
    title = (track.get("title") or "").strip()
    if not title:
        return None
    return MusicMatch(
        title=title,
        artist=(track.get("subtitle") or "").strip(),
        url=track.get("url") or "",
    )
