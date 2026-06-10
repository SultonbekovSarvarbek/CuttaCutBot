"""Модуль скачивания отрезка YouTube-видео через yt-dlp + ffmpeg.

Всё работает асинхронно: yt-dlp вызывается через asyncio.create_subprocess_exec,
чтобы не блокировать event loop aiogram.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

# Папка, внутри которой создаются временные подпапки под каждый запрос.
TMP_ROOT = Path(__file__).parent / "tmp"

# Имя файла с cookies. Если такой файл лежит рядом с этим модулем — он
# автоматически передаётся в yt-dlp (обход "Sign in to confirm you're not a bot").
COOKIES_FILE = Path(__file__).parent / "cookies.txt"


@dataclass
class DownloadResult:
    """Результат скачивания.

    ok        — успешно ли скачалось;
    path      — путь к готовому mp4 (если ok);
    tmp_dir   — временная папка запроса (её нужно удалить после отправки);
    stderr    — текст ошибки от yt-dlp (если не ok);
    timed_out — True, если скачивание прервано по таймауту.
    """

    ok: bool
    path: Path | None
    tmp_dir: Path
    stderr: str = ""
    timed_out: bool = False


async def get_video_info(url: str) -> tuple[str | None, int | None]:
    """Узнаёт название и длину видео в секундах (без скачивания).

    Возвращает (title, duration); каждое поле может быть None, если
    выяснить не удалось (живой эфир, ошибка сети и т.п.) — в этом случае
    бот просто работает без названия / без проверки границ.
    """
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--print", "title",
        "--print", "duration",
    ]
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, None

    if proc.returncode != 0:
        return None, None

    lines = stdout_bytes.decode("utf-8", errors="replace").strip().splitlines()
    title = lines[0].strip() or None if lines else None
    try:
        duration = int(float(lines[1]))
    except (ValueError, IndexError):
        duration = None
    return title, duration


def _format_section(start: int, end: int) -> str:
    """Преобразует секунды в строку диапазона для --download-sections.

    Формат: "*HH:MM:SS-HH:MM:SS" (звёздочка = по времени, а не по главам).
    """

    def hhmmss(total: int) -> str:
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    return f"*{hhmmss(start)}-{hhmmss(end)}"


async def download_section(
    url: str,
    start: int,
    end: int,
    height: int,
    max_height: int,
    timeout: int = 600,
) -> DownloadResult:
    """Скачивает ТОЛЬКО заданный отрезок видео и склеивает в mp4.

    url        — ссылка на YouTube-видео;
    start, end — начало и конец отрезка в секундах;
    height     — выбранное пользователем качество (360/480/720);
    max_height — потолок качества из конфигурации;
    timeout    — максимум секунд на работу yt-dlp (защита от зависания).

    Возвращает DownloadResult. Каждый запрос работает в отдельной uuid-папке.
    """
    # Качество не может превышать общий потолок из конфигурации.
    height = min(height, max_height)

    # Уникальная папка под этот конкретный запрос.
    tmp_dir = TMP_ROOT / uuid.uuid4().hex
    tmp_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(tmp_dir / "clip.%(ext)s")

    # Формат: лучшее видео+аудио в пределах height, либо единый поток-фолбэк.
    fmt = (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]"
    )

    cmd: list[str] = [
        "yt-dlp",
        "--no-playlist",
        "--download-sections",
        _format_section(start, end),
        "--force-keyframes-at-cuts",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "-o",
        output_template,
    ]

    # Если рядом лежит cookies.txt — подкладываем его автоматически.
    if COOKIES_FILE.exists():
        cmd += ["--cookies", str(COOKIES_FILE)]

    cmd.append(url)

    # Запускаем yt-dlp как отдельный процесс, не блокируя event loop.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        cleanup(tmp_dir)
        return DownloadResult(
            ok=False,
            path=None,
            tmp_dir=tmp_dir,
            timed_out=True,
        )
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        # Ошибка скачивания — отдаём stderr наверх, папку убираем.
        cleanup(tmp_dir)
        return DownloadResult(ok=False, path=None, tmp_dir=tmp_dir, stderr=stderr)

    # Ищем итоговый mp4 в папке запроса.
    mp4_files = list(tmp_dir.glob("*.mp4"))
    if not mp4_files:
        # На всякий случай — вдруг ext оказался другим.
        any_files = [p for p in tmp_dir.iterdir() if p.is_file()]
        if not any_files:
            cleanup(tmp_dir)
            return DownloadResult(
                ok=False,
                path=None,
                tmp_dir=tmp_dir,
                stderr=stderr or "yt-dlp не создал выходной файл.",
            )
        return DownloadResult(ok=True, path=any_files[0], tmp_dir=tmp_dir, stderr=stderr)

    return DownloadResult(ok=True, path=mp4_files[0], tmp_dir=tmp_dir, stderr=stderr)


async def extract_audio(video_path: Path, timeout: int = 180) -> Path | None:
    """Извлекает аудиодорожку из готового клипа в mp3 (кладёт рядом).

    Возвращает путь к mp3 либо None, если извлечь не удалось — это не
    критично, видео к этому моменту уже отправлено.
    """
    audio_path = video_path.with_suffix(".mp3")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vn",                      # без видеопотока
        "-codec:a", "libmp3lame",
        "-q:a", "4",                # VBR ~165 кбит/с — достаточно для речи и музыки
        str(audio_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None

    if proc.returncode != 0 or not audio_path.exists():
        return None
    return audio_path


async def make_video_note(video_path: Path, timeout: int = 180) -> Path | None:
    """Перекодирует клип в квадратный «кружочек» Telegram (512×512).

    Кадр обрезается по центру до квадрата. Возвращает путь к готовому
    файлу либо None при ошибке.
    """
    note_path = video_path.with_name("note.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=512:512",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(note_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None

    if proc.returncode != 0 or not note_path.exists():
        return None
    return note_path


async def make_gif(video_path: Path, timeout: int = 120) -> Path | None:
    """Делает «гифку» для Telegram: тот же клип, но без звуковой дорожки.

    Telegram показывает mp4 без аудио, отправленный как animation,
    в виде зацикленной гифки. Видеопоток копируется без перекодирования,
    поэтому операция почти мгновенная.
    """
    gif_path = video_path.with_name("animation.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-an",                  # выбросить звук
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(gif_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None

    if proc.returncode != 0 or not gif_path.exists():
        return None
    return gif_path


def cleanup(tmp_dir: Path) -> None:
    """Удаляет временную папку запроса вместе со всем содержимым."""
    shutil.rmtree(tmp_dir, ignore_errors=True)


def file_size_mb(path: Path) -> float:
    """Возвращает размер файла в мегабайтах."""
    return os.path.getsize(path) / (1024 * 1024)
