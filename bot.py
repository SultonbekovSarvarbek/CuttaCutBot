"""Telegram-бот: вырезает отрезок из YouTube-видео по таймкодам.

Пользователь шлёт одним сообщением ссылку и диапазон времени, бот скачивает
ТОЛЬКО этот отрезок (через yt-dlp --download-sections) и присылает видеофайлом.

Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

# Подхватываем .env, если он есть (удобно для локального запуска).
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from downloader import cleanup, download_section, file_size_mb

# ---------------------------------------------------------------------------
# Конфигурация (через переменные окружения с дефолтами)
# ---------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
MAX_FILE_MB: int = int(os.getenv("MAX_FILE_MB", "50"))
MAX_HEIGHT: int = int(os.getenv("MAX_HEIGHT", "720"))
MAX_CLIP_SECONDS: int = int(os.getenv("MAX_CLIP_SECONDS", str(15 * 60)))
DOWNLOAD_TIMEOUT: int = int(os.getenv("DOWNLOAD_TIMEOUT", str(10 * 60)))
MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))

# URL локального Telegram Bot API server (если поднят) — для снятия лимита 50 МБ.
# Пусто = используется официальный api.telegram.org.
TELEGRAM_API_URL: str = os.getenv("TELEGRAM_API_URL", "")

# Доступные варианты качества для inline-кнопок.
QUALITY_OPTIONS = [360, 480, 720]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("clipbot")


# ---------------------------------------------------------------------------
# Состояние пользователя (in-memory)
# ---------------------------------------------------------------------------
@dataclass
class PendingRequest:
    """Запрос, ожидающий выбора качества."""

    url: str
    start: int
    end: int


# Незавершённые запросы (ждут нажатия кнопки качества).
pending: dict[int, PendingRequest] = {}

# Ограничитель одновременных скачиваний (yt-dlp + ffmpeg — тяжёлые процессы).
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ---------------------------------------------------------------------------
# Парсинг таймкодов и ссылки
# ---------------------------------------------------------------------------
# Один таймкод: SS, MM:SS или HH:MM:SS.
_TIME = r"\d{1,2}(?::\d{1,2}){0,2}"

# Общая регулярка: url + start + (пробел/дефис) + end.
_PATTERN = re.compile(
    r"(?P<url>https?://\S+)\s+"
    r"(?P<start>" + _TIME + r")"
    r"\s*[-\s]\s*"
    r"(?P<end>" + _TIME + r")",
)


def parse_timecode(value: str) -> int | None:
    """Преобразует таймкод (SS / MM:SS / HH:MM:SS) в секунды.

    Возвращает None, если минуты/секунды выходят за пределы 0–59
    (одиночное число трактуется как количество секунд и не ограничено).
    """
    parts = [int(p) for p in value.split(":")]
    if len(parts) > 1 and any(p > 59 for p in parts[1:]):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


@dataclass
class ParsedRequest:
    url: str
    start: int
    end: int


def parse_message(text: str) -> ParsedRequest | None:
    """Вытаскивает из сообщения ссылку и диапазон времени.

    Возвращает None, если формат не распознан.
    """
    match = _PATTERN.search(text.strip())
    if not match:
        return None

    url = match.group("url")
    start = parse_timecode(match.group("start"))
    end = parse_timecode(match.group("end"))
    if start is None or end is None:
        return None
    return ParsedRequest(url=url, start=start, end=end)


def format_seconds(total: int) -> str:
    """Секунды → читаемая строка H:MM:SS / M:SS."""
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Inline-клавиатура выбора качества
# ---------------------------------------------------------------------------
def quality_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора качества 360p / 480p / 720p.

    Если MAX_HEIGHT ниже всех стандартных вариантов — показываем одну
    кнопку с самим MAX_HEIGHT, чтобы клавиатура не оказалась пустой.
    """
    options = [q for q in QUALITY_OPTIONS if q <= MAX_HEIGHT] or [MAX_HEIGHT]
    buttons = [
        InlineKeyboardButton(text=f"{q}p", callback_data=f"quality:{q}")
        for q in options
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Инструкция с примерами формата."""
    text = (
        "👋 Привет! Я вырезаю фрагменты из YouTube-видео по таймкодам.\n\n"
        "Пришли одним сообщением <b>ссылку</b> и <b>диапазон времени</b>:\n\n"
        "<code>https://youtu.be/ID 11:50 15:00</code>\n"
        "<code>https://youtu.be/ID 11:50-15:00</code>\n"
        "<code>https://youtu.be/ID 0:11:50 0:15:00</code>\n\n"
        "Поддерживаю форматы времени: <b>SS</b>, <b>MM:SS</b>, <b>HH:MM:SS</b>.\n"
        f"Максимальная длина отрезка: <b>{format_seconds(MAX_CLIP_SECONDS)}</b>.\n\n"
        "После отправки выберешь качество кнопками."
    )
    await message.answer(text)


@dp.message(F.text)
async def handle_link(message: Message) -> None:
    """Принимает ссылку + таймкоды, валидирует, предлагает выбрать качество."""
    parsed = parse_message(message.text)
    if parsed is None:
        await message.answer(
            "❌ Не понял формат. Нужно: ссылка и диапазон времени.\n"
            "Например: <code>https://youtu.be/ID 11:50 15:00</code>\n"
            "Подробнее — /start"
        )
        return

    # Валидация диапазона.
    if parsed.end <= parsed.start:
        await message.answer("❌ Конец отрезка должен быть больше начала.")
        return

    duration = parsed.end - parsed.start
    if duration > MAX_CLIP_SECONDS:
        await message.answer(
            f"❌ Отрезок слишком длинный: {format_seconds(duration)}.\n"
            f"Максимум — {format_seconds(MAX_CLIP_SECONDS)}."
        )
        return

    # Сохраняем запрос и просим выбрать качество.
    pending[message.from_user.id] = PendingRequest(
        url=parsed.url, start=parsed.start, end=parsed.end
    )
    await message.answer(
        f"📐 Отрезок: <b>{format_seconds(parsed.start)} – "
        f"{format_seconds(parsed.end)}</b> "
        f"({format_seconds(duration)})\n\nВыбери качество:",
        reply_markup=quality_keyboard(),
    )


@dp.callback_query(F.data.startswith("quality:"))
async def handle_quality(callback: CallbackQuery) -> None:
    """Обрабатывает выбор качества и запускает скачивание."""
    user_id = callback.from_user.id
    height = int(callback.data.split(":", 1)[1])

    # pop, а не get: повторное нажатие кнопки не запустит второе скачивание,
    # а новый запрос пользователя, пришедший во время скачивания, не пострадает.
    request = pending.pop(user_id, None)
    if request is None:
        await callback.answer("Запрос устарел, пришли ссылку заново.", show_alert=True)
        return

    await callback.answer()
    status = callback.message

    # Если все слоты скачивания заняты — честно сообщаем про очередь.
    if download_semaphore.locked():
        await status.edit_text("⏳ Сейчас много запросов, жду свободный слот…")

    async with download_semaphore:
        # Редактируем одно и то же сообщение по мере прогресса.
        await status.edit_text(f"⏬ Качаю в {height}p…")

        logger.info(
            "Download start: user=%s url=%s %s-%s %sp",
            user_id, request.url, request.start, request.end, height,
        )

        result = await download_section(
            url=request.url,
            start=request.start,
            end=request.end,
            height=height,
            max_height=MAX_HEIGHT,
            timeout=DOWNLOAD_TIMEOUT,
        )

    if not result.ok:
        # Показываем последние ~500 символов stderr.
        tail = result.stderr[-500:] if result.stderr else "неизвестная ошибка"
        logger.error("Download failed: user=%s err=%s", user_id, result.stderr)
        await status.edit_text(
            f"❌ Не получилось скачать.\n<code>{_escape(tail)}</code>"
        )
        return

    assert result.path is not None

    # Проверка размера перед отправкой.
    size_mb = file_size_mb(result.path)
    if size_mb > MAX_FILE_MB:
        logger.warning(
            "File too big: user=%s size=%.1fMB limit=%sMB",
            user_id, size_mb, MAX_FILE_MB,
        )
        cleanup(result.tmp_dir)
        await status.edit_text(
            f"❌ Файл получился {size_mb:.0f} МБ — это больше лимита "
            f"{MAX_FILE_MB} МБ.\n"
            "Сократи отрезок или выбери качество пониже."
        )
        return

    await status.edit_text(f"📤 Отправляю… ({size_mb:.0f} МБ)")

    try:
        video = FSInputFile(result.path)
        await callback.message.answer_video(
            video,
            caption=(
                f"✂️ {format_seconds(request.start)}–"
                f"{format_seconds(request.end)} · {height}p"
            ),
        )
        await status.edit_text("✅ Готово!")
        logger.info("Sent OK: user=%s size=%.1fMB", user_id, size_mb)
    except Exception as exc:  # noqa: BLE001 — логируем любую ошибку отправки
        logger.exception("Send failed: user=%s", user_id)
        await status.edit_text(f"❌ Ошибка при отправке.\n<code>{_escape(str(exc))}</code>")
    finally:
        # Всегда чистим временную папку запроса.
        cleanup(result.tmp_dir)


def _escape(text: str) -> str:
    """Экранирует HTML-спецсимволы для вывода в <code>."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (переменная окружения).")

    default = DefaultBotProperties(parse_mode=ParseMode.HTML)

    # Если задан локальный Telegram Bot API server — используем его
    # (это нужно для отправки файлов больше 50 МБ).
    if TELEGRAM_API_URL:
        from aiogram.client.telegram import TelegramAPIServer

        session_server = TelegramAPIServer.from_base(TELEGRAM_API_URL)
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(api=session_server)
        bot = Bot(token=BOT_TOKEN, default=default, session=session)
    else:
        bot = Bot(token=BOT_TOKEN, default=default)

    logger.info(
        "Bot starting | MAX_FILE_MB=%s MAX_HEIGHT=%s MAX_CLIP=%ss "
        "TIMEOUT=%ss CONCURRENT=%s api=%s",
        MAX_FILE_MB, MAX_HEIGHT, MAX_CLIP_SECONDS,
        DOWNLOAD_TIMEOUT, MAX_CONCURRENT_DOWNLOADS, TELEGRAM_API_URL or "official",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
