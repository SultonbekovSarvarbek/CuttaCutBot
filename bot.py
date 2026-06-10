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
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from downloader import cleanup, download_section, file_size_mb
from i18n import CHOOSE_LANGUAGE, LANGUAGES, load_langs, save_langs, t

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

# Ссылки, ожидающие таймкодов (пользователь прислал только URL).
pending_url: dict[int, str] = {}

# Ограничитель одновременных скачиваний (yt-dlp + ffmpeg — тяжёлые процессы).
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Выбранный язык каждого пользователя (подгружается с диска при старте).
user_lang: dict[int, str] = load_langs()


def get_lang(user: User) -> str:
    """Язык пользователя: сохранённый выбор, иначе — язык клиента Telegram."""
    saved = user_lang.get(user.id)
    if saved:
        return saved
    code = (user.language_code or "")[:2].lower()
    return code if code in LANGUAGES else "en"


# ---------------------------------------------------------------------------
# Парсинг таймкодов и ссылки
# ---------------------------------------------------------------------------
# Один таймкод: SS, MM:SS или HH:MM:SS. Разделитель — двоеточие или точка
# (точку проще набирать с телефона: 1.17 = 1:17).
_TIME = r"\d{1,2}(?:[:.]\d{1,2}){0,2}"

# Диапазон: start + (пробел/дефис/тире) + end.
_RANGE = (
    r"(?P<start>" + _TIME + r")"
    r"\s*[-–—\s]\s*"
    r"(?P<end>" + _TIME + r")"
)

# Всё одним сообщением: url + диапазон.
_PATTERN = re.compile(r"(?P<url>https?://\S+)\s+" + _RANGE)

# Только ссылка (для двухшагового ввода).
_URL_RE = re.compile(r"https?://\S+")

# Только диапазон времени (ответ на просьбу прислать таймкоды).
_RANGE_RE = re.compile(_RANGE)


def parse_timecode(value: str) -> int | None:
    """Преобразует таймкод (SS / MM:SS / HH:MM:SS) в секунды.

    Разделитель — «:» или «.». Возвращает None, если минуты/секунды
    выходят за пределы 0–59 (одиночное число трактуется как количество
    секунд и не ограничено).
    """
    parts = [int(p) for p in re.split(r"[:.]", value)]
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


def language_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора языка."""
    buttons = [
        InlineKeyboardButton(text=name, callback_data=f"lang:{code}")
        for code, name in LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def start_text(lang: str) -> str:
    """Текст инструкции /start на нужном языке."""
    return t(lang, "start", max_clip=format_seconds(MAX_CLIP_SECONDS))


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Первый запуск — выбор языка, дальше — инструкция."""
    if message.from_user.id not in user_lang:
        await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())
        return
    await message.answer(start_text(get_lang(message.from_user)))


@dp.message(Command("lang"))
async def cmd_lang(message: Message) -> None:
    """Смена языка."""
    await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())


@dp.callback_query(F.data.startswith("lang:"))
async def handle_lang(callback: CallbackQuery) -> None:
    """Сохраняет выбранный язык и показывает инструкцию."""
    code = callback.data.split(":", 1)[1]
    if code not in LANGUAGES:
        await callback.answer()
        return

    user_lang[callback.from_user.id] = code
    save_langs(user_lang)
    await callback.answer()
    await callback.message.edit_text(t(code, "lang_saved", name=LANGUAGES[code]))
    await callback.message.answer(start_text(code))


async def ask_quality(
    message: Message, lang: str, url: str, start: int, end: int
) -> bool:
    """Валидирует диапазон и предлагает выбрать качество.

    Возвращает True, если запрос принят (показана клавиатура качества).
    """
    if end <= start:
        await message.answer(t(lang, "end_before_start"))
        return False

    duration = end - start
    if duration > MAX_CLIP_SECONDS:
        await message.answer(
            t(
                lang,
                "too_long",
                duration=format_seconds(duration),
                max_clip=format_seconds(MAX_CLIP_SECONDS),
            )
        )
        return False

    pending[message.from_user.id] = PendingRequest(url=url, start=start, end=end)
    await message.answer(
        t(
            lang,
            "choose_quality",
            start=format_seconds(start),
            end=format_seconds(end),
            duration=format_seconds(duration),
        ),
        reply_markup=quality_keyboard(),
    )
    return True


@dp.message(F.text)
async def handle_link(message: Message) -> None:
    """Принимает ссылку и таймкоды — вместе или по очереди.

    Поддерживаются три варианта:
    1) ссылка + диапазон одним сообщением;
    2) только ссылка — бот попросит таймкоды следующим сообщением;
    3) только диапазон — если ссылка была прислана ранее.
    """
    lang = get_lang(message.from_user)
    user_id = message.from_user.id
    text = message.text.strip()

    # Вариант 1: всё одним сообщением.
    parsed = parse_message(text)
    if parsed is not None:
        pending_url.pop(user_id, None)
        await ask_quality(message, lang, parsed.url, parsed.start, parsed.end)
        return

    # Вариант 2: только ссылка — запоминаем и просим таймкоды.
    url_match = _URL_RE.search(text)
    if url_match:
        pending_url[user_id] = url_match.group(0)
        await message.answer(t(lang, "link_received"))
        return

    # Вариант 3: только таймкоды — если ссылка уже ждёт.
    range_match = _RANGE_RE.fullmatch(text)
    if range_match:
        url = pending_url.get(user_id)
        if url is not None:
            start = parse_timecode(range_match.group("start"))
            end = parse_timecode(range_match.group("end"))
            if start is None or end is None:
                await message.answer(t(lang, "bad_format"))
                return
            # Ссылку освобождаем только после успешного принятия запроса,
            # чтобы при ошибке можно было просто прислать таймкоды заново.
            if await ask_quality(message, lang, url, start, end):
                pending_url.pop(user_id, None)
            return

    await message.answer(t(lang, "bad_format"))


@dp.callback_query(F.data.startswith("quality:"))
async def handle_quality(callback: CallbackQuery) -> None:
    """Обрабатывает выбор качества и запускает скачивание."""
    user_id = callback.from_user.id
    lang = get_lang(callback.from_user)
    height = int(callback.data.split(":", 1)[1])

    # pop, а не get: повторное нажатие кнопки не запустит второе скачивание,
    # а новый запрос пользователя, пришедший во время скачивания, не пострадает.
    request = pending.pop(user_id, None)
    if request is None:
        await callback.answer(t(lang, "stale_request"), show_alert=True)
        return

    await callback.answer()
    status = callback.message

    # Если все слоты скачивания заняты — честно сообщаем про очередь.
    if download_semaphore.locked():
        await status.edit_text(t(lang, "queued"))

    async with download_semaphore:
        # Редактируем одно и то же сообщение по мере прогресса.
        await status.edit_text(t(lang, "downloading", height=height))

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
        # Таймаут показываем переведённой фразой, остальное — хвостом stderr.
        if result.timed_out:
            tail = t(lang, "download_timeout", timeout=DOWNLOAD_TIMEOUT)
        else:
            tail = result.stderr[-500:] if result.stderr else t(lang, "unknown_error")
        logger.error("Download failed: user=%s err=%s", user_id, result.stderr)
        await status.edit_text(t(lang, "download_failed", error=_escape(tail)))
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
            t(lang, "too_big", size=f"{size_mb:.0f}", limit=MAX_FILE_MB)
        )
        return

    await status.edit_text(t(lang, "uploading", size=f"{size_mb:.0f}"))

    try:
        video = FSInputFile(result.path)
        await callback.message.answer_video(
            video,
            caption=(
                f"✂️ {format_seconds(request.start)}–"
                f"{format_seconds(request.end)} · {height}p"
            ),
        )
        await status.edit_text(t(lang, "done"))
        logger.info("Sent OK: user=%s size=%.1fMB", user_id, size_mb)
    except Exception as exc:  # noqa: BLE001 — логируем любую ошибку отправки
        logger.exception("Send failed: user=%s", user_id)
        await status.edit_text(t(lang, "send_failed", error=_escape(str(exc))))
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
