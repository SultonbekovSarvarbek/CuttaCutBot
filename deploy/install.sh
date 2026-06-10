#!/usr/bin/env bash
# Устанавливает systemd-юнит бота из ТЕКУЩЕЙ папки проекта.
#
# Использование (на сервере, из корня проекта):
#   sudo bash deploy/install.sh
#
# Скрипт сам подставит путь к проекту и пользователя, под которым ты
# работаешь, включит автозапуск и сразу стартует бота.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"
SERVICE=/etc/systemd/system/clipbot.service

if [[ $EUID -ne 0 ]]; then
    echo "Нужны права root. Запусти: sudo bash deploy/install.sh"
    exit 1
fi

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
    echo "Не найден venv. Сначала выполни в корне проекта:"
    echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "Не найден .env. Сначала выполни:"
    echo "  cp .env.example .env   # и впиши BOT_TOKEN"
    exit 1
fi

if ! command -v ffmpeg >/dev/null; then
    echo "Не найден ffmpeg. Сначала выполни: sudo apt install -y ffmpeg"
    exit 1
fi

cat > "$SERVICE" <<EOF
[Unit]
Description=YouTube Clip Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$(id -gn "$RUN_USER")
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/bot.py
Restart=always
RestartSec=5
Environment=PATH=$PROJECT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now clipbot
sleep 2
systemctl --no-pager --full status clipbot || true

echo
echo "Готово! Бот запущен и будет стартовать сам после перезагрузки."
echo "Логи в реальном времени:  journalctl -u clipbot -f"
echo "Перезапуск после git pull: sudo systemctl restart clipbot"
