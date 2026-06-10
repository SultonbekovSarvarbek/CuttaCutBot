"""Переводы текстов бота: русский, английский, узбекский.

Выбранный язык каждого пользователя хранится в user_langs.json рядом
с ботом — простое key-value хранилище, переживает перезапуск.
"""

from __future__ import annotations

import json
from pathlib import Path

# Файл с выбором языка пользователей: {"123456": "ru", ...}
_STORE = Path(__file__).parent / "user_langs.json"

# Язык по умолчанию, если код языка из Telegram нам неизвестен.
FALLBACK_LANG = "en"

# Доступные языки: код → подпись на кнопке.
LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "uz": "🇺🇿 O'zbekcha",
}

# Приглашение выбрать язык — одно на всех, показывается до выбора.
CHOOSE_LANGUAGE = "🌐 Выбери язык / Choose a language / Tilni tanlang:"

TEXTS: dict[str, dict[str, str]] = {
    "start": {
        "ru": (
            "👋 Привет! Я вырезаю фрагменты из YouTube-видео по таймкодам.\n\n"
            "Просто пришли <b>ссылку</b> — я спрошу таймкоды.\n"
            "Или всё одним сообщением:\n"
            "<code>https://youtu.be/ID 1:17 1:25</code>\n\n"
            "Время: <b>SS</b>, <b>MM:SS</b> или <b>HH:MM:SS</b> — "
            "через <code>:</code> или <code>.</code> "
            "(<code>1.17</code> = <code>1:17</code>).\n"
            "Максимальная длина отрезка: <b>{max_clip}</b>.\n\n"
            "Дальше выберешь качество кнопками.\n\n"
            "Сменить язык: /lang"
        ),
        "en": (
            "👋 Hi! I cut clips from YouTube videos by timecodes.\n\n"
            "Just send a <b>link</b> — I'll ask for the timecodes.\n"
            "Or everything in one message:\n"
            "<code>https://youtu.be/ID 1:17 1:25</code>\n\n"
            "Time: <b>SS</b>, <b>MM:SS</b> or <b>HH:MM:SS</b> — "
            "with <code>:</code> or <code>.</code> "
            "(<code>1.17</code> = <code>1:17</code>).\n"
            "Maximum clip length: <b>{max_clip}</b>.\n\n"
            "Then you'll pick the quality with the buttons.\n\n"
            "Change language: /lang"
        ),
        "uz": (
            "👋 Salom! Men YouTube videolaridan taym-kodlar bo'yicha "
            "parchalar kesib beraman.\n\n"
            "Shunchaki <b>havola</b> yuboring — taym-kodlarni o'zim so'rayman.\n"
            "Yoki hammasini bitta xabarda:\n"
            "<code>https://youtu.be/ID 1:17 1:25</code>\n\n"
            "Vaqt: <b>SS</b>, <b>MM:SS</b> yoki <b>HH:MM:SS</b> — "
            "<code>:</code> yoki <code>.</code> bilan "
            "(<code>1.17</code> = <code>1:17</code>).\n"
            "Parchaning maksimal uzunligi: <b>{max_clip}</b>.\n\n"
            "Keyin sifatni tugmalar orqali tanlaysiz.\n\n"
            "Tilni o'zgartirish: /lang"
        ),
    },
    "link_received": {
        "ru": (
            "🔗 Ссылка получена!\n"
            "Теперь пришли диапазон времени, например:\n"
            "<code>1:17 1:25</code> или <code>1.17-1.25</code>"
        ),
        "en": (
            "🔗 Got the link!\n"
            "Now send the time range, for example:\n"
            "<code>1:17 1:25</code> or <code>1.17-1.25</code>"
        ),
        "uz": (
            "🔗 Havola qabul qilindi!\n"
            "Endi vaqt oralig'ini yuboring, masalan:\n"
            "<code>1:17 1:25</code> yoki <code>1.17-1.25</code>"
        ),
    },
    "lang_saved": {
        "ru": "✅ Язык сохранён: {name}",
        "en": "✅ Language set: {name}",
        "uz": "✅ Til saqlandi: {name}",
    },
    "bad_format": {
        "ru": (
            "❌ Не понял. Пришли ссылку на видео — я спрошу таймкоды.\n"
            "Или всё одним сообщением: "
            "<code>https://youtu.be/ID 1:17 1:25</code>\n"
            "Подробнее — /start"
        ),
        "en": (
            "❌ I didn't get that. Send a video link — I'll ask for the timecodes.\n"
            "Or everything in one message: "
            "<code>https://youtu.be/ID 1:17 1:25</code>\n"
            "More info — /start"
        ),
        "uz": (
            "❌ Tushunmadim. Video havolasini yuboring — taym-kodlarni "
            "o'zim so'rayman.\n"
            "Yoki hammasini bitta xabarda: "
            "<code>https://youtu.be/ID 1:17 1:25</code>\n"
            "Batafsil — /start"
        ),
    },
    "end_before_start": {
        "ru": "❌ Конец отрезка должен быть больше начала.",
        "en": "❌ The end of the clip must be after the start.",
        "uz": "❌ Parchaning oxiri boshidan keyin bo'lishi kerak.",
    },
    "too_long": {
        "ru": "❌ Отрезок слишком длинный: {duration}.\nМаксимум — {max_clip}.",
        "en": "❌ The clip is too long: {duration}.\nThe maximum is {max_clip}.",
        "uz": "❌ Parcha juda uzun: {duration}.\nMaksimum — {max_clip}.",
    },
    "choose_quality": {
        "ru": "📐 Отрезок: <b>{start} – {end}</b> ({duration})\n\nВыбери качество:",
        "en": "📐 Clip: <b>{start} – {end}</b> ({duration})\n\nChoose the quality:",
        "uz": "📐 Parcha: <b>{start} – {end}</b> ({duration})\n\nSifatni tanlang:",
    },
    "beyond_video": {
        "ru": (
            "❌ Отрезок начинается за концом видео — его длина всего "
            "<b>{length}</b>.\nПришли ссылку и таймкоды заново."
        ),
        "en": (
            "❌ The clip starts after the video ends — it is only "
            "<b>{length}</b> long.\nSend the link and timecodes again."
        ),
        "uz": (
            "❌ Parcha video tugaganidan keyin boshlanadi — video bor-yo'g'i "
            "<b>{length}</b>.\nHavola va taym-kodlarni qaytadan yuboring."
        ),
    },
    "stale_request": {
        "ru": "Запрос устарел, пришли ссылку заново.",
        "en": "This request has expired, send the link again.",
        "uz": "So'rov eskirgan, havolani qaytadan yuboring.",
    },
    "queued": {
        "ru": "⏳ Сейчас много запросов, жду свободный слот…",
        "en": "⏳ Many requests right now, waiting for a free slot…",
        "uz": "⏳ Hozir so'rovlar ko'p, bo'sh o'rin kutyapman…",
    },
    "downloading": {
        "ru": "⏬ Качаю в {height}p…",
        "en": "⏬ Downloading in {height}p…",
        "uz": "⏬ {height}p sifatda yuklab olyapman…",
    },
    "download_failed": {
        "ru": "❌ Не получилось скачать.\n<code>{error}</code>",
        "en": "❌ Download failed.\n<code>{error}</code>",
        "uz": "❌ Yuklab olib bo'lmadi.\n<code>{error}</code>",
    },
    "download_timeout": {
        "ru": "Скачивание не уложилось в {timeout} с и было прервано.",
        "en": "The download didn't finish within {timeout} s and was aborted.",
        "uz": "Yuklab olish {timeout} soniyada tugamadi va to'xtatildi.",
    },
    "unknown_error": {
        "ru": "неизвестная ошибка",
        "en": "unknown error",
        "uz": "noma'lum xatolik",
    },
    "too_big": {
        "ru": (
            "❌ Файл получился {size} МБ — это больше лимита {limit} МБ.\n"
            "Сократи отрезок или выбери качество пониже."
        ),
        "en": (
            "❌ The file is {size} MB — over the {limit} MB limit.\n"
            "Make the clip shorter or pick a lower quality."
        ),
        "uz": (
            "❌ Fayl {size} MB chiqdi — bu {limit} MB limitdan katta.\n"
            "Parchani qisqartiring yoki pastroq sifat tanlang."
        ),
    },
    "uploading": {
        "ru": "📤 Отправляю… ({size} МБ)",
        "en": "📤 Uploading… ({size} MB)",
        "uz": "📤 Yuboryapman… ({size} MB)",
    },
    "note_button": {
        "ru": "🔵 Кружочек",
        "en": "🔵 Video note",
        "uz": "🔵 Doira video",
    },
    "making_note": {
        "ru": "🔵 Делаю кружочек…",
        "en": "🔵 Making the video note…",
        "uz": "🔵 Doira video tayyorlayapman…",
    },
    "note_too_long": {
        "ru": "❌ Кружочек может быть не длиннее 60 секунд.",
        "en": "❌ A video note can be at most 60 seconds long.",
        "uz": "❌ Doira video ko'pi bilan 60 soniya bo'lishi mumkin.",
    },
    "extracting_audio": {
        "ru": "🎵 Извлекаю аудио…",
        "en": "🎵 Extracting the audio…",
        "uz": "🎵 Audioni ajratyapman…",
    },
    "done": {
        "ru": "✅ Готово!",
        "en": "✅ Done!",
        "uz": "✅ Tayyor!",
    },
    "send_failed": {
        "ru": "❌ Ошибка при отправке.\n<code>{error}</code>",
        "en": "❌ Failed to send.\n<code>{error}</code>",
        "uz": "❌ Yuborishda xatolik.\n<code>{error}</code>",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    """Возвращает перевод по ключу с подстановкой параметров."""
    variants = TEXTS[key]
    text = variants.get(lang) or variants[FALLBACK_LANG]
    return text.format(**kwargs) if kwargs else text


def load_langs() -> dict[int, str]:
    """Читает сохранённые языки пользователей (пустой словарь, если файла нет)."""
    try:
        raw = json.loads(_STORE.read_text(encoding="utf-8"))
        return {int(user_id): lang for user_id, lang in raw.items()}
    except (FileNotFoundError, ValueError):
        return {}


def save_langs(langs: dict[int, str]) -> None:
    """Сохраняет языки пользователей на диск."""
    _STORE.write_text(
        json.dumps({str(k): v for k, v in langs.items()}),
        encoding="utf-8",
    )
