"""
Общая конфигурация бота: логирование, объект bot, переменные окружения.

Этот модуль ничего не импортирует из других файлов проекта — на него
ссылаются все остальные, чтобы не было циклических импортов.
"""

import logging
import os

import telebot
from telebot import apihelper


# --- Логирование (настраивается первым: нужно для обработчика ошибок) -------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("timer_bot")


# --- Telegram-бот -----------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Переменная окружения BOT_TOKEN не задана! "
        "Укажите токен бота в настройках Render → Environment."
    )


class _BotExceptionHandler(telebot.ExceptionHandler):
    """
    Ловит всё, что не поймали сами хендлеры.
    Без него необработанное исключение может уронить поток обработки
    апдейтов, и бот молча перестаёт отвечать.
    """

    def handle(self, exception):
        logger.exception("Необработанное исключение в telebot", exc_info=exception)
        return True


# Включаем поддержку middleware (нужно для учёта статистики в main.py).
# ENABLE_MIDDLEWARE должен быть установлен до создания TeleBot.
apihelper.ENABLE_MIDDLEWARE = True

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML",
    exception_handler=_BotExceptionHandler(),
)


# --- База данных ------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")


# --- Администраторы ---------------------------------------------------------

# Формат переменной окружения: ADMIN_IDS="123456789,987654321"
ADMIN_IDS = set()
for _raw_id in os.environ.get("ADMIN_IDS", "").split(","):
    _raw_id = _raw_id.strip()
    if _raw_id.isdigit():
        ADMIN_IDS.add(int(_raw_id))

if not ADMIN_IDS:
    logger.warning("ADMIN_IDS не задан — админ-команды недоступны никому.")


# --- Безопасность вебхука ---------------------------------------------------

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

if not WEBHOOK_SECRET:
    logger.warning(
        "WEBHOOK_SECRET не задан — webhook-эндпоинт не защищён "
        "от поддельных запросов. Задайте случайную строку "
        "в переменной окружения WEBHOOK_SECRET."
    )
