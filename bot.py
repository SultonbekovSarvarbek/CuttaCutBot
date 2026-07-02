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
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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
    ChosenInlineResult,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputMediaVideo,
    InputTextMessageContent,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    User,
)

import premium
import stats
from downloader import (
    TMP_ROOT,
    add_watermark,
    cleanup,
    download_section,
    extract_audio,
    file_size_mb,
    get_video_info,
    make_gif,
    make_sticker,
    make_vertical,
    make_video_note,
)
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

# Telegram user id владельца бота — только ему доступна команда /stats.
# Свой id можно узнать, написав @userinfobot.
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

# Username поддержки (без @) — кнопка «Поддержка» ведёт в этот чат.
SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "s_sarvar").lstrip("@")

# URL локального Telegram Bot API server (если поднят) — для снятия лимита 50 МБ.
# Пусто = используется официальный api.telegram.org.
TELEGRAM_API_URL: str = os.getenv("TELEGRAM_API_URL", "")

# --- Премиум-подписка (Telegram Stars) ---
# Цена подписки в Stars за 30 дней.
PREMIUM_STARS: int = int(os.getenv("PREMIUM_STARS", "100"))
# Лимиты бесплатного тарифа.
FREE_CLIPS_PER_DAY: int = int(os.getenv("FREE_CLIPS_PER_DAY", "1"))
FREE_MAX_HEIGHT: int = int(os.getenv("FREE_MAX_HEIGHT", "480"))
FREE_MAX_CLIP_SECONDS: int = int(os.getenv("FREE_MAX_CLIP_SECONDS", str(5 * 60)))
# Текст водяного знака на видео бесплатного тарифа (пусто = @username бота).
WATERMARK_TEXT: str = os.getenv("WATERMARK_TEXT", "")
# Период подписки — ровно 30 дней (других значений Telegram не разрешает).
SUBSCRIPTION_PERIOD = 30 * 24 * 3600

# Доступные варианты качества для inline-кнопок.
QUALITY_OPTIONS = [360, 480, 720]

# Лимит Telegram на длину «кружочка» (video note), секунд.
NOTE_MAX_SECONDS = 60
# Лимит длины гифки (чтобы файлы оставались лёгкими), секунд.
GIF_MAX_SECONDS = 60
# Лимит Telegram на длину видео-стикера, секунд (берём начало отрезка).
STICKER_SECONDS = 3
# Качество исходника для кружочка и гифки.
NOTE_SOURCE_HEIGHT = 480
# Качество исходника для мобильного формата 9:16 (кадр режется по ширине,
# поэтому берём максимум, чтобы вертикальная полоса не была мыльной).
VERTICAL_SOURCE_HEIGHT = 720
# Качество клипа в inline-режиме (без выбора кнопками).
INLINE_HEIGHT = 480

# Username бота (заполняется при старте, нужен для inline-подсказок).
BOT_USERNAME = ""

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

# Пользователи, от которых ждём текст отзыва (нажали «Оставить отзыв»).
awaiting_feedback: set[int] = set()


@dataclass
class StoredAudio:
    """mp3, ожидающий нажатия кнопки «Скачать аудио» под видео."""

    path: Path
    title: str
    ts: float  # время создания — для чистки протухших файлов


# Папка для отложенных mp3 (живут до нажатия кнопки или до TTL).
AUDIO_DIR = TMP_ROOT / "audio"
# Сколько секунд храним аудио, если кнопку так и не нажали.
AUDIO_TTL_SECONDS = 6 * 3600

# Отложенные mp3 по токену из callback_data кнопки.
pending_audio: dict[str, StoredAudio] = {}


def _purge_old_audio() -> None:
    """Удаляет протухшие mp3 — вызывается лениво при каждом новом клипе."""
    now = time.time()
    for token, item in list(pending_audio.items()):
        if now - item.ts > AUDIO_TTL_SECONDS:
            item.path.unlink(missing_ok=True)
            pending_audio.pop(token, None)


# Ограничитель одновременных скачиваний (yt-dlp + ffmpeg — тяжёлые процессы).
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Отдельный пул слотов для подписчиков — им не приходится ждать в общей
# очереди за бесплатными пользователями.
premium_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


def watermark_for(premium_user: bool) -> str | None:
    """Текст водяного знака: None для подписчиков (и если подписать нечем)."""
    if premium_user:
        return None
    return WATERMARK_TEXT or (f"@{BOT_USERNAME}" if BOT_USERNAME else None)


def free_max_clip() -> int:
    """Максимальная длина отрезка на бесплатном тарифе."""
    return min(FREE_MAX_CLIP_SECONDS, MAX_CLIP_SECONDS)

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
def quality_keyboard(
    lang: str, allow_note: bool, premium_user: bool
) -> InlineKeyboardMarkup:
    """Кнопки выбора качества 360p / 480p / 720p (+ «кружочек» для коротких).

    Качества выше FREE_MAX_HEIGHT для бесплатного тарифа показываются
    с замком — нажатие ведёт на предложение Премиума.
    Если MAX_HEIGHT ниже всех стандартных вариантов — показываем одну
    кнопку с самим MAX_HEIGHT, чтобы клавиатура не оказалась пустой.
    """
    options = [q for q in QUALITY_OPTIONS if q <= MAX_HEIGHT] or [MAX_HEIGHT]
    quality_row = []
    for q in options:
        if premium_user or q <= FREE_MAX_HEIGHT:
            quality_row.append(
                InlineKeyboardButton(text=f"{q}p", callback_data=f"quality:{q}")
            )
        else:
            quality_row.append(
                InlineKeyboardButton(text=f"🔒 {q}p", callback_data="locked")
            )
    rows = [quality_row]
    if allow_note:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(lang, "note_button"), callback_data="quality:note"
                ),
                InlineKeyboardButton(
                    text=t(lang, "gif_button"), callback_data="quality:gif"
                ),
            ]
        )
    # Мобильный формат 9:16 доступен для любого отрезка.
    rows.append(
        [
            InlineKeyboardButton(
                text=t(lang, "vertical_button"), callback_data="quality:vertical"
            )
        ]
    )
    # Стикер и «только аудио» доступны для любого отрезка.
    rows.append(
        [
            InlineKeyboardButton(
                text=t(lang, "sticker_button"), callback_data="quality:sticker"
            ),
            InlineKeyboardButton(
                text=t(lang, "audio_button"), callback_data="quality:audio"
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора языка."""
    buttons = [
        InlineKeyboardButton(text=name, callback_data=f"lang:{code}")
        for code, name in LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def feedback_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Кнопки «Оставить отзыв» и «Поддержка» под инструкцией /start.

    «Поддержка» — обычная ссылка на чат с человеком из SUPPORT_USERNAME.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=t(lang, "premium_button"), callback_data="premium"
            )
        ],
        [
            InlineKeyboardButton(
                text=t(lang, "feedback_button"), callback_data="feedback"
            )
        ],
    ]
    if SUPPORT_USERNAME:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(lang, "support_button"),
                    url=f"https://t.me/{SUPPORT_USERNAME}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def premium_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Одна кнопка «Премиум» — под сообщениями про лимиты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "premium_button"), callback_data="premium"
                )
            ]
        ]
    )


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
    stats.track(message.from_user.id, "start")
    # /start всегда отменяет ожидание отзыва.
    awaiting_feedback.discard(message.from_user.id)
    if message.from_user.id not in user_lang:
        await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())
        return
    lang = get_lang(message.from_user)
    await message.answer(start_text(lang), reply_markup=feedback_keyboard(lang))


@dp.message(Command("lang"))
async def cmd_lang(message: Message) -> None:
    """Смена языка."""
    await message.answer(CHOOSE_LANGUAGE, reply_markup=language_keyboard())


@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Статистика использования — только для владельца бота (ADMIN_ID)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return  # для остальных команда просто молчит

    s = stats.summary()
    await message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        "👥 Пользователи:\n"
        f"  за 24 часа: <b>{s['users_day']}</b>\n"
        f"  за 7 дней: <b>{s['users_week']}</b>\n"
        f"  всего: <b>{s['users_total']}</b>\n\n"
        "🎬 Готовые клипы:\n"
        f"  за 24 часа: <b>{s['clips_day']}</b>\n"
        f"  за 7 дней: <b>{s['clips_week']}</b>\n"
        f"  всего: <b>{s['clips_total']}</b>\n\n"
        f"📨 Запросов всего: <b>{s['requests_total']}</b>\n"
        f"❌ Ошибок скачивания: <b>{s['fails_total']}</b>"
    )


@dp.message(Command("feedback"))
async def cmd_feedback(message: Message) -> None:
    """Команда «Оставить отзыв»: переводит пользователя в режим ввода отзыва."""
    lang = get_lang(message.from_user)
    awaiting_feedback.add(message.from_user.id)
    await message.answer(t(lang, "feedback_prompt"))


@dp.message(Command("feedbacks"))
async def cmd_feedbacks(message: Message) -> None:
    """Последние отзывы пользователей — только для владельца бота (ADMIN_ID)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return  # для остальных команда просто молчит

    rows = stats.recent_feedback(limit=15)
    if not rows:
        await message.answer("Отзывов пока нет.")
        return

    lines = ["💬 <b>Последние отзывы</b>\n"]
    for ts, user_id, username, text in rows:
        when = time.strftime("%d.%m %H:%M", time.localtime(ts))
        who = f"@{username}" if username else f"id{user_id}"
        lines.append(f"<b>{who}</b> · {when}\n{_escape(text)}\n")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@dp.callback_query(F.data == "feedback")
async def handle_feedback_button(callback: CallbackQuery) -> None:
    """Кнопка «Оставить отзыв»: просим написать отзыв следующим сообщением."""
    lang = get_lang(callback.from_user)
    awaiting_feedback.add(callback.from_user.id)
    await callback.answer()
    await callback.message.answer(t(lang, "feedback_prompt"))


# ---------------------------------------------------------------------------
# Премиум-подписка (Telegram Stars)
# ---------------------------------------------------------------------------
async def send_premium_pitch(message: Message, lang: str, user_id: int) -> None:
    """Показывает статус подписки либо предложение оформить Премиум.

    Подписка оформляется по инвойс-ссылке: только они поддерживают
    автопродление (Telegram сам списывает Stars раз в 30 дней).
    """
    exp = premium.expires_at(user_id)
    if exp and exp > time.time():
        date = time.strftime("%d.%m.%Y", time.localtime(exp))
        await message.answer(t(lang, "premium_status", date=date))
        return

    link = await message.bot.create_invoice_link(
        title=t(lang, "premium_invoice_title"),
        description=t(lang, "premium_invoice_desc", max_height=MAX_HEIGHT),
        payload="premium",
        currency="XTR",
        prices=[LabeledPrice(label="Premium", amount=PREMIUM_STARS)],
        subscription_period=SUBSCRIPTION_PERIOD,
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "premium_subscribe_button", stars=PREMIUM_STARS),
                    url=link,
                )
            ]
        ]
    )
    await message.answer(
        t(
            lang,
            "premium_pitch",
            stars=PREMIUM_STARS,
            free_clips=FREE_CLIPS_PER_DAY,
            free_height=FREE_MAX_HEIGHT,
            free_max=format_seconds(free_max_clip()),
            max_height=MAX_HEIGHT,
            max_clip=format_seconds(MAX_CLIP_SECONDS),
        ),
        reply_markup=kb,
    )


@dp.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    """Команда /premium: статус подписки или предложение оформить."""
    lang = get_lang(message.from_user)
    await send_premium_pitch(message, lang, message.from_user.id)


@dp.callback_query(F.data == "premium")
async def handle_premium_button(callback: CallbackQuery) -> None:
    """Кнопка «Премиум» (стартовый экран, сообщения про лимиты)."""
    lang = get_lang(callback.from_user)
    await callback.answer()
    await send_premium_pitch(callback.message, lang, callback.from_user.id)


@dp.callback_query(F.data == "locked")
async def handle_locked(callback: CallbackQuery) -> None:
    """Нажатие на 🔒-качество: объясняем и предлагаем Премиум."""
    lang = get_lang(callback.from_user)
    await callback.answer(
        t(lang, "quality_locked", free_height=FREE_MAX_HEIGHT), show_alert=True
    )
    await send_premium_pitch(callback.message, lang, callback.from_user.id)


@dp.pre_checkout_query()
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram спрашивает подтверждение перед списанием Stars."""
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def handle_payment(message: Message) -> None:
    """Оплата прошла (первая или автопродление) — включаем подписку."""
    user_id = message.from_user.id
    lang = get_lang(message.from_user)
    payment = message.successful_payment

    # У подписочных платежей Telegram присылает дату окончания сам;
    # на всякий случай fallback — 30 дней от момента оплаты.
    exp_dt = payment.subscription_expiration_date
    expires = (
        int(exp_dt.timestamp()) if exp_dt
        else int(time.time()) + SUBSCRIPTION_PERIOD
    )
    premium.activate(user_id, expires, bool(payment.is_recurring))
    stats.track(user_id, "premium_paid")
    logger.info(
        "Premium paid: user=%s stars=%s until=%s",
        user_id, payment.total_amount, expires,
    )

    date = time.strftime("%d.%m.%Y", time.localtime(expires))
    await message.answer(t(lang, "premium_thanks", date=date))

    if ADMIN_ID:
        who = (
            f"@{message.from_user.username}"
            if message.from_user.username
            else f"id{user_id}"
        )
        try:
            await message.bot.send_message(
                ADMIN_ID,
                f"⭐ <b>Оплата подписки</b>: {who}, "
                f"{payment.total_amount} Stars, до {date}",
            )
        except Exception:  # noqa: BLE001 — не мешаем пользователю
            logger.exception("Failed to notify admin about payment")


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
    await callback.message.answer(start_text(code), reply_markup=feedback_keyboard(code))


async def ask_quality(
    message: Message, lang: str, url: str, start: int, end: int
) -> bool:
    """Валидирует диапазон и предлагает выбрать качество.

    Возвращает True, если запрос принят (показана клавиатура качества).
    """
    if end <= start:
        await message.answer(t(lang, "end_before_start"))
        return False

    user_id = message.from_user.id
    premium_user = premium.is_premium(user_id)

    duration = end - start
    max_clip = MAX_CLIP_SECONDS if premium_user else free_max_clip()
    if duration > max_clip:
        # Отрезок влез бы в премиум-лимит — подсказываем про подписку.
        if not premium_user and duration <= MAX_CLIP_SECONDS:
            await message.answer(
                t(
                    lang,
                    "too_long_free",
                    duration=format_seconds(duration),
                    max_clip=format_seconds(max_clip),
                    max_premium=format_seconds(MAX_CLIP_SECONDS),
                ),
                reply_markup=premium_keyboard(lang),
            )
        else:
            await message.answer(
                t(
                    lang,
                    "too_long",
                    duration=format_seconds(duration),
                    max_clip=format_seconds(max_clip),
                )
            )
        return False

    # Дневной лимит бесплатного тарифа.
    if not premium_user and stats.clips_today(user_id) >= FREE_CLIPS_PER_DAY:
        await message.answer(
            t(lang, "limit_reached", limit=FREE_CLIPS_PER_DAY),
            reply_markup=premium_keyboard(lang),
        )
        return False

    pending[message.from_user.id] = PendingRequest(url=url, start=start, end=end)
    logger.info(
        "Request: user=%s %s %s-%s",
        message.from_user.id,
        url,
        format_seconds(start),
        format_seconds(end),
    )
    stats.track(message.from_user.id, "request")
    await message.answer(
        t(
            lang,
            "choose_quality",
            start=format_seconds(start),
            end=format_seconds(end),
            duration=format_seconds(duration),
        ),
        reply_markup=quality_keyboard(
            lang,
            allow_note=duration <= NOTE_MAX_SECONDS,
            premium_user=premium_user,
        ),
    )
    return True


async def save_user_feedback(message: Message, lang: str) -> None:
    """Сохраняет отзыв пользователя и пересылает его владельцу бота."""
    user = message.from_user
    text = message.text.strip()
    if not text:
        await message.answer(t(lang, "feedback_empty"))
        return

    awaiting_feedback.discard(user.id)
    stats.save_feedback(user.id, user.username, text)
    logger.info("Feedback: user=%s %r", user.id, text[:200])

    # Пересылаем владельцу в реальном времени (если ADMIN_ID задан).
    if ADMIN_ID:
        who = f"@{user.username}" if user.username else f"id{user.id}"
        name = _escape(user.full_name or "")
        try:
            await message.bot.send_message(
                ADMIN_ID,
                f"💬 <b>Новый отзыв</b> от {who} ({name})\n\n{_escape(text)}",
                disable_web_page_preview=True,
            )
        except Exception:  # noqa: BLE001 — не мешаем пользователю, просто логируем
            logger.exception("Failed to forward feedback to admin")

    await message.answer(t(lang, "feedback_thanks"))


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

    # Режим отзыва: текущее сообщение — это отзыв, а не запрос на клип.
    if user_id in awaiting_feedback:
        await save_user_feedback(message, lang)
        return

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
    choice = callback.data.split(":", 1)[1]
    is_note = choice == "note"
    is_gif = choice == "gif"
    is_sticker = choice == "sticker"
    is_audio = choice == "audio"
    is_vertical = choice == "vertical"
    premium_user = premium.is_premium(user_id)
    if is_note or is_gif or is_sticker or is_audio:
        height = NOTE_SOURCE_HEIGHT
    elif is_vertical:
        height = VERTICAL_SOURCE_HEIGHT
    else:
        height = int(choice)
        # Страховка от устаревшей клавиатуры (подписка кончилась,
        # а кнопка 720p ещё видна) — молча спускаем к лимиту тарифа.
        if not premium_user:
            height = min(height, FREE_MAX_HEIGHT)

    # pop, а не get: повторное нажатие кнопки не запустит второе скачивание,
    # а новый запрос пользователя, пришедший во время скачивания, не пострадает.
    request = pending.pop(user_id, None)
    if request is None:
        await callback.answer(t(lang, "stale_request"), show_alert=True)
        return

    # Кнопки «кружочек»/«GIF» показываются только для коротких отрезков, но
    # запрос мог смениться, пока на экране была старая клавиатура — перепроверяем.
    if (is_note or is_gif) and request.end - request.start > NOTE_MAX_SECONDS:
        pending[user_id] = request  # вернуть, выбор качества ещё актуален
        await callback.answer(
            t(lang, "note_too_long" if is_note else "gif_too_long"),
            show_alert=True,
        )
        return

    # Дневной лимит бесплатного тарифа (перепроверка: клавиатура могла
    # висеть на экране, пока лимит уже исчерпался другими клипами).
    if not premium_user and stats.clips_today(user_id) >= FREE_CLIPS_PER_DAY:
        await callback.answer()
        await callback.message.edit_text(
            t(lang, "limit_reached", limit=FREE_CLIPS_PER_DAY),
            reply_markup=premium_keyboard(lang),
        )
        return

    # В стикер идут только первые 3 секунды — не качаем лишнего.
    if is_sticker:
        request.end = min(request.end, request.start + STICKER_SECONDS)

    await callback.answer()
    status = callback.message

    # Подписчики идут через отдельный пул слотов — без общей очереди.
    semaphore = premium_semaphore if premium_user else download_semaphore

    # Если все слоты скачивания заняты — честно сообщаем про очередь.
    if semaphore.locked():
        await status.edit_text(t(lang, "queued"))

    async with semaphore:
        # Редактируем одно и то же сообщение по мере прогресса.
        await status.edit_text(
            t(lang, "downloading_audio") if is_audio
            else t(lang, "downloading", height=height)
        )

        # Название пойдёт в подписи, длина — для проверки границ отрезка,
        # чтобы ffmpeg не падал на диапазоне за концом ролика.
        title, video_len = await get_video_info(request.url)
        if video_len:
            if request.start >= video_len:
                await status.edit_text(
                    t(lang, "beyond_video", length=format_seconds(video_len))
                )
                return
            if request.end > video_len:
                request.end = video_len

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
            audio_only=is_audio,
        )

    if not result.ok:
        # Таймаут показываем переведённой фразой, остальное — хвостом stderr.
        if result.timed_out:
            tail = t(lang, "download_timeout", timeout=DOWNLOAD_TIMEOUT)
        else:
            tail = result.stderr[-500:] if result.stderr else t(lang, "unknown_error")
        logger.error("Download failed: user=%s err=%s", user_id, result.stderr)
        stats.track(user_id, "download_fail")
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

    if not is_audio:
        await status.edit_text(t(lang, "uploading", size=f"{size_mb:.0f}"))

    try:
        clip_range = (
            f"{format_seconds(request.start)}–{format_seconds(request.end)}"
        )
        # В подписи — только название видео (если его удалось узнать).
        title_caption = f"<b>{_escape(title[:200])}</b>" if title else None

        # Режим «только аудио»: конвертируем скачанную дорожку в mp3
        # и присылаем без видео.
        if is_audio:
            await status.edit_text(t(lang, "extracting_audio"))
            audio_path = await extract_audio(result.path)
            if audio_path is None:
                logger.error("Audio convert failed: user=%s", user_id)
                await status.edit_text(
                    t(lang, "download_failed", error=t(lang, "unknown_error"))
                )
                stats.track(user_id, "download_fail")
                return
            await callback.message.answer_audio(
                FSInputFile(
                    audio_path,
                    filename=f"{_safe_filename(title) or 'clip'}.mp3",
                ),
                caption=f"🎵 {title_caption}" if title_caption else None,
                title=title or clip_range,
            )
            await status.edit_text(t(lang, "done"))
            stats.track(user_id, "download_ok")
            logger.info("Audio sent OK: user=%s", user_id)
            return

        # Режим стикера: WEBM/VP9 из первых секунд отрезка, без подписи
        # (Telegram не позволяет прикладывать подпись к стикеру).
        if is_sticker:
            await status.edit_text(t(lang, "making_sticker"))
            sticker_path = await make_sticker(
                result.path, max_seconds=STICKER_SECONDS
            )
            if sticker_path is None:
                logger.error("Sticker failed: user=%s", user_id)
                await status.edit_text(
                    t(lang, "download_failed", error=t(lang, "unknown_error"))
                )
                stats.track(user_id, "download_fail")
                return
            await callback.message.answer_sticker(FSInputFile(sticker_path))
            await status.edit_text(t(lang, "done"))
            stats.track(user_id, "download_ok")
            logger.info("Sticker sent OK: user=%s", user_id)
            return

        # Режим кружочка: квадратное видео без подписи, аудио не прикладываем.
        if is_note:
            await status.edit_text(t(lang, "making_note"))
            note_path = await make_video_note(result.path)
            if note_path is None or file_size_mb(note_path) > MAX_FILE_MB:
                logger.error("Video note failed: user=%s", user_id)
                await status.edit_text(
                    t(lang, "download_failed", error=t(lang, "unknown_error"))
                )
                stats.track(user_id, "download_fail")
                return
            await callback.message.answer_video_note(FSInputFile(note_path))
            await status.edit_text(t(lang, "done"))
            stats.track(user_id, "download_ok")
            logger.info("Note sent OK: user=%s", user_id)
            return

        # Режим гифки: тот же клип без звука, Telegram покажет как GIF.
        if is_gif:
            await status.edit_text(t(lang, "making_gif"))
            gif_path = await make_gif(result.path)
            if gif_path is None or file_size_mb(gif_path) > MAX_FILE_MB:
                logger.error("GIF failed: user=%s", user_id)
                await status.edit_text(
                    t(lang, "download_failed", error=t(lang, "unknown_error"))
                )
                stats.track(user_id, "download_fail")
                return
            await callback.message.answer_animation(
                FSInputFile(
                    gif_path, filename=f"{_safe_filename(title) or 'clip'}.mp4"
                ),
                caption=f"🎞 {title_caption}" if title_caption else None,
            )
            await status.edit_text(t(lang, "done"))
            stats.track(user_id, "download_ok")
            logger.info("GIF sent OK: user=%s", user_id)
            return

        # Мобильный формат: режем кадр по центру до вертикального 9:16,
        # дальше клип идёт обычным путём (аудио-кнопка, распознавание музыки).
        if is_vertical:
            await status.edit_text(t(lang, "making_vertical"))
            vertical_path = await make_vertical(
                result.path, watermark=watermark_for(premium_user)
            )
            if vertical_path is None or file_size_mb(vertical_path) > MAX_FILE_MB:
                logger.error("Vertical failed: user=%s", user_id)
                await status.edit_text(
                    t(lang, "download_failed", error=t(lang, "unknown_error"))
                )
                stats.track(user_id, "download_fail")
                return
            result.path = vertical_path

        # Бесплатный тариф: полупрозрачный водяной знак в углу кадра.
        elif watermark_for(premium_user):
            await status.edit_text(t(lang, "adding_watermark"))
            marked_path = await add_watermark(
                result.path, watermark_for(premium_user)
            )
            if marked_path is not None:
                result.path = marked_path
            else:
                # Не критично: отправим без знака, просто логируем.
                logger.warning("Watermark failed: user=%s", user_id)

        # mp3 извлекаем заранее: он нужен и для кнопки «Скачать аудио»,
        # и для распознавания музыки. Сам файл отправляем только по кнопке.
        await status.edit_text(t(lang, "extracting_audio"))
        audio_path = await extract_audio(result.path)
        if audio_path is None:
            # Не критично: видео отправим без кнопки, просто логируем.
            logger.warning("Audio extraction failed: user=%s", user_id)

        audio_kb = None
        if audio_path and file_size_mb(audio_path) <= MAX_FILE_MB:
            # Переносим mp3 из tmp-папки запроса (её удалит cleanup)
            # в долгоживущую папку — до нажатия кнопки или до TTL.
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            stored_path = AUDIO_DIR / f"{token}.mp3"
            shutil.move(str(audio_path), stored_path)
            audio_path = stored_path
            pending_audio[token] = StoredAudio(
                path=stored_path, title=title or clip_range, ts=time.time()
            )
            _purge_old_audio()
            audio_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t(lang, "audio_button"),
                            callback_data=f"audio:{token}",
                        )
                    ]
                ]
            )

        video = FSInputFile(result.path)
        await callback.message.answer_video(
            video,
            caption=f"✂️ {title_caption}" if title_caption else None,
            reply_markup=audio_kb,
        )

        await status.edit_text(t(lang, "done"))
        stats.track(user_id, "download_ok")
        logger.info("Sent OK: user=%s size=%.1fMB", user_id, size_mb)
    except Exception as exc:  # noqa: BLE001 — логируем любую ошибку отправки
        logger.exception("Send failed: user=%s", user_id)
        await status.edit_text(t(lang, "send_failed", error=_escape(str(exc))))
    finally:
        # Всегда чистим временную папку запроса.
        cleanup(result.tmp_dir)


@dp.callback_query(F.data.startswith("audio:"))
async def handle_audio(callback: CallbackQuery) -> None:
    """Кнопка «Скачать аудио» под видео: присылает отложенный mp3."""
    lang = get_lang(callback.from_user)
    token = callback.data.split(":", 1)[1]

    # pop: повторное нажатие не отправит файл дважды.
    stored = pending_audio.pop(token, None)
    if stored is None or not stored.path.exists():
        await callback.answer(t(lang, "audio_gone"), show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.answer_audio(
            FSInputFile(
                stored.path,
                filename=f"{_safe_filename(stored.title) or 'clip'}.mp3",
            ),
            caption=f"🎵 <b>{_escape(stored.title[:200])}</b>",
            title=stored.title,
        )
    except Exception:  # noqa: BLE001 — вернуть токен, чтобы можно было повторить
        logger.exception("Audio send failed: token=%s", token)
        pending_audio[token] = stored
        return
    finally:
        if token not in pending_audio:
            stored.path.unlink(missing_ok=True)

    # Убираем кнопку у видео — аудио уже отправлено.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — не критично
        pass


# ---------------------------------------------------------------------------
# Inline-режим: @bot ссылка 1:17 1:25 в любом чате
# ---------------------------------------------------------------------------
@dp.inline_query()
async def handle_inline(query: InlineQuery) -> None:
    """Мгновенный ответ на inline-запрос: карточка «Вырезать» или подсказка."""
    lang = get_lang(query.from_user)
    parsed = parse_message(query.query or "")

    max_clip = (
        MAX_CLIP_SECONDS
        if premium.is_premium(query.from_user.id)
        else free_max_clip()
    )
    valid = (
        parsed is not None
        and parsed.end > parsed.start
        and parsed.end - parsed.start <= max_clip
    )
    if not valid:
        help_result = InlineQueryResultArticle(
            id="help",
            title=t(lang, "inline_help_title"),
            description=t(lang, "inline_help_desc"),
            input_message_content=InputTextMessageContent(
                message_text=t(lang, "inline_help_msg", bot=BOT_USERNAME),
            ),
        )
        await query.answer([help_result], cache_time=5, is_personal=True)
        return

    clip_range = f"{format_seconds(parsed.start)}–{format_seconds(parsed.end)}"
    # Клавиатура обязательна: без неё Telegram не пришлёт inline_message_id,
    # и заглушку будет нечем заменить на готовое видео.
    placeholder_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏳", callback_data="noop")]]
    )
    cut_result = InlineQueryResultArticle(
        id="clip",
        title=t(lang, "inline_cut_title", range=clip_range),
        description=t(lang, "inline_cut_desc"),
        input_message_content=InputTextMessageContent(
            message_text=t(lang, "inline_preparing", range=clip_range),
        ),
        reply_markup=placeholder_kb,
    )
    await query.answer([cut_result], cache_time=5, is_personal=True)


@dp.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    """Кнопка-заглушка «⏳» в inline-сообщении — просто гасим спиннер."""
    await callback.answer()


@dp.chosen_inline_result()
async def handle_chosen(chosen: ChosenInlineResult, bot: Bot) -> None:
    """Пользователь отправил inline-карточку: качаем клип и подменяем заглушку."""
    imid = chosen.inline_message_id
    if chosen.result_id != "clip" or not imid:
        return

    lang = get_lang(chosen.from_user)
    user_id = chosen.from_user.id
    parsed = parse_message(chosen.query)
    if parsed is None:
        return

    stats.track(user_id, "request")
    logger.info("Inline request: user=%s query=%r", user_id, chosen.query)
    request = PendingRequest(url=parsed.url, start=parsed.start, end=parsed.end)

    async def fail(text: str) -> None:
        try:
            await bot.edit_message_text(inline_message_id=imid, text=text)
        except Exception:  # noqa: BLE001
            logger.exception("Inline edit failed: user=%s", user_id)

    premium_user = premium.is_premium(user_id)

    # Дневной лимит бесплатного тарифа действует и в inline-режиме.
    if not premium_user and stats.clips_today(user_id) >= FREE_CLIPS_PER_DAY:
        await fail(t(lang, "limit_reached", limit=FREE_CLIPS_PER_DAY))
        return

    semaphore = premium_semaphore if premium_user else download_semaphore
    async with semaphore:
        title, video_len = await get_video_info(request.url)
        if video_len:
            if request.start >= video_len:
                await fail(t(lang, "beyond_video", length=format_seconds(video_len)))
                return
            if request.end > video_len:
                request.end = video_len

        result = await download_section(
            url=request.url,
            start=request.start,
            end=request.end,
            height=INLINE_HEIGHT,
            max_height=MAX_HEIGHT,
            timeout=DOWNLOAD_TIMEOUT,
        )

    if not result.ok:
        stats.track(user_id, "download_fail")
        if result.timed_out:
            err = t(lang, "download_timeout", timeout=DOWNLOAD_TIMEOUT)
        else:
            err = result.stderr[-200:] if result.stderr else t(lang, "unknown_error")
        await fail(t(lang, "download_failed", error=_escape(err)))
        return

    try:
        # Бесплатный тариф: водяной знак и на inline-клипах.
        wm = watermark_for(premium_user)
        if wm:
            marked_path = await add_watermark(result.path, wm)
            if marked_path is not None:
                result.path = marked_path
            else:
                logger.warning("Inline watermark failed: user=%s", user_id)

        size_mb = file_size_mb(result.path)
        if size_mb > MAX_FILE_MB:
            await fail(t(lang, "too_big", size=f"{size_mb:.0f}", limit=MAX_FILE_MB))
            return

        title_caption = f"<b>{_escape(title[:200])}</b>" if title else None
        caption = f"✂️ {title_caption}" if title_caption else None

        # Inline-сообщение можно заменить видео только по file_id, поэтому
        # сначала тихо загружаем файл в личку автора запроса и сразу удаляем.
        try:
            upload = await bot.send_video(
                chat_id=user_id,
                video=FSInputFile(result.path),
                caption=caption,
                disable_notification=True,
            )
        except Exception:  # noqa: BLE001 — почти всегда «бот не запущен»
            logger.warning("Inline upload failed: user=%s (no PM?)", user_id)
            await fail(t(lang, "inline_need_start", bot=BOT_USERNAME))
            return

        try:
            await bot.delete_message(chat_id=user_id, message_id=upload.message_id)
        except Exception:  # noqa: BLE001 — не критично, файл уже загружен
            pass

        await bot.edit_message_media(
            inline_message_id=imid,
            media=InputMediaVideo(media=upload.video.file_id, caption=caption),
        )
        stats.track(user_id, "download_ok")
        logger.info("Inline sent OK: user=%s size=%.1fMB", user_id, size_mb)
    except Exception:  # noqa: BLE001
        logger.exception("Inline failed: user=%s", user_id)
        await fail(t(lang, "send_failed", error=t(lang, "unknown_error")))
    finally:
        cleanup(result.tmp_dir)


def _escape(text: str) -> str:
    """Экранирует HTML-спецсимволы для вывода в <code>."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _safe_filename(title: str | None) -> str:
    """Превращает название видео в безопасное имя файла."""
    if not title:
        return ""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    return cleaned[:60]


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (переменная окружения).")

    # После рестарта токены кнопок теряются — чистим осиротевшие mp3.
    shutil.rmtree(AUDIO_DIR, ignore_errors=True)

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

    # Username нужен для подсказок в inline-режиме.
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""

    logger.info(
        "Bot starting | @%s MAX_FILE_MB=%s MAX_HEIGHT=%s MAX_CLIP=%ss "
        "TIMEOUT=%ss CONCURRENT=%s api=%s",
        BOT_USERNAME, MAX_FILE_MB, MAX_HEIGHT, MAX_CLIP_SECONDS,
        DOWNLOAD_TIMEOUT, MAX_CONCURRENT_DOWNLOADS, TELEGRAM_API_URL or "official",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
