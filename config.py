"""
Общая конфигурация бота: объект bot, переменные окружения, логирование.

Этот модуль ничего не импортирует из других файлов проекта — на него
ссылаются database.py и main.py, чтобы не было циклических импортов.
"""

import logging
import os

import telebot
from telebot import apihelper


# --- Telegram-бот -------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Переменная окружения BOT_TOKEN не задана! "
        "Укажите токен бота в настройках Render → Environment."
    )

# Включаем поддержку middleware (нужно для учёта статистики в main.py).
# ENABLE_MIDDLEWARE должен быть установлен до создания TeleBot.
apihelper.ENABLE_MIDDLEWARE = True
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


# --- База данных ----------------------------------------------------------

# Строка подключения к Postgres (например, из Neon). Опционально.
DATABASE_URL = os.environ.get("DATABASE_URL")


# --- Администраторы --------------------------------------------------------

# Формат переменной окружения: ADMIN_IDS="123456789,987654321"
ADMIN_IDS = set()
for _raw_id in os.environ.get("ADMIN_IDS", "").split(","):
    _raw_id = _raw_id.strip()
    if _raw_id.isdigit():
        ADMIN_IDS.add(int(_raw_id))


# --- Безопасность вебхука ---------------------------------------------------

# Секретный токен для проверки запросов от Telegram.
# Задайте случайную строку в Render → Environment: WEBHOOK_SECRET
# Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


# --- Логирование ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("timer_bot")

if not WEBHOOK_SECRET:
    logger.warning(
        "WEBHOOK_SECRET не задан — webhook-эндпоинт не защищён "
        "от поддельных запросов. Задайте случайную строку "
        "в переменной окружения WEBHOOK_SECRET."
    )