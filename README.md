# ClipBot — Telegram-бот для нарезки YouTube

Бот скачивает **только нужный отрезок** YouTube-видео по таймкодам (а не всё
видео) и присылает его обратно видеофайлом.

## Как пользоваться

Отправь боту одним сообщением ссылку и диапазон времени:

```
https://youtu.be/ID 11:50 15:00
https://youtu.be/ID 11:50-15:00
https://youtu.be/ID 0:11:50 0:15:00
```

Поддерживаются форматы времени: `SS`, `MM:SS`, `HH:MM:SS`.
Разделитель между началом и концом — пробел или дефис.

После отправки бот предложит выбрать качество (360p / 480p / 720p).

---

## Установка

### 1. Системные зависимости

Нужен **ffmpeg** (yt-dlp использует его для нарезки и склейки):

```bash
sudo apt update
sudo apt install -y ffmpeg
```

Проверка:

```bash
ffmpeg -version
```

### 2. Виртуальное окружение и зависимости

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Токен бота

1. Открой в Telegram [@BotFather](https://t.me/BotFather).
2. Команда `/newbot`, придумай имя и username.
3. BotFather пришлёт токен вида `123456:ABC-DEF...`.

Скопируй пример конфигурации и впиши токен:

```bash
cp .env.example .env
# отредактируй .env — впиши BOT_TOKEN
```

### 4. Запуск

```bash
python bot.py
```

---

## Конфигурация (`.env`)

| Переменная         | По умолчанию | Описание                                              |
|--------------------|--------------|-------------------------------------------------------|
| `BOT_TOKEN`        | —            | Токен от @BotFather (обязательно)                     |
| `MAX_FILE_MB`      | `50`         | Лимит размера отправляемого файла в МБ                |
| `MAX_HEIGHT`       | `720`        | Потолок качества видео по высоте (px)                 |
| `MAX_CLIP_SECONDS` | `900`        | Максимальная длина отрезка в секундах (15 минут)      |
| `DOWNLOAD_TIMEOUT` | `600`        | Максимум секунд на работу yt-dlp (защита от зависания)|
| `MAX_CONCURRENT_DOWNLOADS` | `2`  | Сколько скачиваний может идти одновременно            |
| `TELEGRAM_API_URL` | пусто        | URL локального Bot API server (см. ниже)              |

---

## Cookies (обход «Sign in to confirm you're not a bot»)

YouTube иногда требует подтверждения, что вы не бот. Решается передачей cookies
из браузера в yt-dlp.

1. Установи расширение для экспорта cookies в формате Netscape
   (например, «Get cookies.txt LOCALLY»).
2. Зайди на youtube.com под своим аккаунтом, экспортируй cookies.
3. Сохрани файл как `cookies.txt` **рядом с `bot.py`**.

Бот сам обнаружит `cookies.txt` и добавит флаг `--cookies cookies.txt`.
Никакой дополнительной настройки не нужно.

> ⚠️ Не коммить `cookies.txt` в git — он уже в `.gitignore`.

---

## Автозапуск через systemd

В папке `deploy/` лежит готовый юнит `clipbot.service`.

```bash
# Скопируй проект в /opt/clipbot (или поправь пути в юните)
sudo cp deploy/clipbot.service /etc/systemd/system/clipbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now clipbot
sudo systemctl status clipbot
journalctl -u clipbot -f   # смотреть логи
```

Перед этим создай пользователя и положи `.env` рядом:

```bash
sudo useradd -r -s /usr/sbin/nologin clipbot
sudo mkdir -p /opt/clipbot
# скопируй файлы проекта, создай venv, положи .env
```

---

## Как снять лимит 50 МБ (локальный Telegram Bot API server)

Официальный Bot API не даёт боту отправлять файлы больше **50 МБ**. Чтобы
поднять лимит до **2000 МБ (2 ГБ)**, нужно поднять собственный
[Telegram Bot API server](https://github.com/tdlib/telegram-bot-api).

### 1. Получи `api_id` и `api_hash`

Зайди на <https://my.telegram.org> → **API development tools** → создай
приложение. Получишь `api_id` и `api_hash`.

### 2. Подними сервер

Через Docker (проще всего):

```bash
docker run -d --name telegram-bot-api \
  -p 8081:8081 \
  -e TELEGRAM_API_ID=ВАШ_API_ID \
  -e TELEGRAM_API_HASH=ВАШ_API_HASH \
  -e TELEGRAM_LOCAL=1 \
  aiogram/telegram-bot-api:latest
```

Либо собери `telegram-bot-api` из исходников по инструкции в репозитории tdlib.

### 3. Настрой бота

В `.env`:

```bash
TELEGRAM_API_URL=http://localhost:8081
MAX_FILE_MB=2000
```

Бот через aiogram `TelegramAPIServer` будет ходить в твой локальный сервер,
и лимит вырастет до 2 ГБ.

> Важно: после переключения на локальный сервер бот «привязывается» к нему.
> Чтобы вернуться на официальный, может понадобиться вызвать `logOut` на
> старом сервере. Подробности — в документации telegram-bot-api.
