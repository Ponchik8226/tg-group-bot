"""
Telegram-бот для личного использования в группе (pyTelegramBotAPI).

Команды:
  /ping                              — проверка отклика + аптайм
  /t, /timer, "тайм", "таймер"       — установка таймера
  /mytimers, "таймеры"              — список своих таймеров
  /del, /del_timer, /cancel,
  "удалить", "отмена"                — удаление таймера
  /start, /help                      — справка
  /myid                              — показать свой Telegram ID
  "стата", "статистика"              — статистика (только админ, только ЛС)

Архитектура:
  config.py   — объект bot, переменные окружения, логирование
  database.py — вся работа с БД (таймеры, статистика)
  utils.py    — мелкие хелперы
  main.py     — этот файл: все хендлеры, middleware, Flask, запуск бота

ВАЖНО: для учёта статистики во всех чатах нужно отключить Privacy Mode
бота через @BotFather (Bot Settings -> Group Privacy -> Turn off).
"""

import html
import os
import re
import threading
import time

from flask import Flask
from telebot import types

import database
from config import bot, logger, ADMIN_IDS
from utils import (
    parse_duration,
    format_duration,
    build_mention,
    get_uptime_str,
    split_message,
)


# =============================================================================
#                          ХРАНИЛИЩЕ АКТИВНЫХ ТАЙМЕРОВ (В ПАМЯТИ)
# =============================================================================

# TIMERS: timer_id -> {chat_id, user_id, user_mention, description,
#                       end_time, duration, timer_obj}
TIMERS = {}

# USER_TIMERS: user_id -> множество timer_id пользователя
USER_TIMERS = {}

_next_timer_id = 1
_timers_lock = threading.Lock()


# =============================================================================
#                          ЛОГИКА ТАЙМЕРОВ
# =============================================================================

def fire_timer(timer_id: int, missed: bool = False):
    """
    Срабатывает по таймеру: тегает пользователя и шлёт описание.
    Если missed=True — таймер сработал, пока бот был выключен.
    """
    with _timers_lock:
        info = TIMERS.pop(timer_id, None)
        if info is not None:
            USER_TIMERS.get(info["user_id"], set()).discard(timer_id)

    if info is None:
        logger.info("Таймер #%s сработал, но был отменён ранее.", timer_id)
        return

    database.delete_timer(timer_id)

    logger.info(
        "Таймер #%s сработал (chat_id=%s, user_id=%s, missed=%s).",
        timer_id, info["chat_id"], info["user_id"], missed,
    )

    text = f"⏰ {info['user_mention']}, время вышло!"
    if info["description"]:
        text += f"\n📝 {html.escape(info['description'])}"
    if missed:
        text += "\n\n⚠️ Бот был выключен, когда таймер должен был сработать."

    try:
        bot.send_message(info["chat_id"], text)
    except Exception:
        logger.exception("Не удалось отправить сообщение по таймеру #%s", timer_id)


def create_timer(message: types.Message, duration_seconds: int, description: str):
    """Создаёт таймер: сохраняет в БД (если настроена) и запускает threading.Timer."""
    global _next_timer_id

    user = message.from_user
    first_name = user.first_name or "Пользователь"
    mention = build_mention(user.id, first_name)
    end_time = time.time() + duration_seconds

    with _timers_lock:
        if database.db_enabled():
            timer_id = database.insert_timer(
                message.chat.id, user.id, first_name, description, end_time
            )
        else:
            timer_id = _next_timer_id
            _next_timer_id += 1

        timer_obj = threading.Timer(duration_seconds, fire_timer, args=(timer_id,))
        timer_obj.daemon = True

        TIMERS[timer_id] = {
            "chat_id": message.chat.id,
            "user_id": user.id,
            "user_mention": mention,
            "description": description,
            "end_time": end_time,
            "duration": duration_seconds,
            "timer_obj": timer_obj,
        }
        USER_TIMERS.setdefault(user.id, set()).add(timer_id)

        timer_obj.start()

    logger.info(
        "Создан таймер #%s на %s сек (user_id=%s, chat_id=%s).",
        timer_id, duration_seconds, user.id, message.chat.id,
    )

    desc_part = f"\n📝 Описание: {html.escape(description)}" if description else ""
    bot.reply_to(
        message,
        f"✅ Таймер #{timer_id} установлен на {format_duration(duration_seconds)}."
        f"{desc_part}",
    )


def cancel_timer(timer_id: int, user_id: int) -> str:
    """Отменяет таймер по ID, если он принадлежит user_id."""
    with _timers_lock:
        info = TIMERS.get(timer_id)

        if info is None:
            return f"❌ Таймер #{timer_id} не найден (возможно, он уже сработал или удалён)."

        if info["user_id"] != user_id:
            return f"❌ Таймер #{timer_id} принадлежит другому пользователю — отменить его может только автор."

        info["timer_obj"].cancel()
        del TIMERS[timer_id]
        USER_TIMERS.get(user_id, set()).discard(timer_id)

    database.delete_timer(timer_id)

    logger.info("Таймер #%s отменён пользователем %s.", timer_id, user_id)
    return f"🗑 Таймер #{timer_id} успешно удалён."


def restore_timers():
    """При старте восстанавливает таймеры из базы (если она настроена)."""
    if not database.db_enabled():
        return

    rows = database.load_all_timers()
    now = time.time()
    restored = 0
    fired = 0

    for timer_id, chat_id, user_id, first_name, description, end_time in rows:
        mention = build_mention(user_id, first_name)
        remaining = end_time - now

        if remaining <= 0:
            # Таймер должен был сработать, пока бот был выключен — шлём сразу
            with _timers_lock:
                TIMERS[timer_id] = {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "user_mention": mention,
                    "description": description,
                    "end_time": end_time,
                    "duration": 0,
                    "timer_obj": None,
                }
                USER_TIMERS.setdefault(user_id, set()).add(timer_id)
            fire_timer(timer_id, missed=True)
            fired += 1
            continue

        timer_obj = threading.Timer(remaining, fire_timer, args=(timer_id,))
        timer_obj.daemon = True

        with _timers_lock:
            TIMERS[timer_id] = {
                "chat_id": chat_id,
                "user_id": user_id,
                "user_mention": mention,
                "description": description,
                "end_time": end_time,
                "duration": int(remaining),
                "timer_obj": timer_obj,
            }
            USER_TIMERS.setdefault(user_id, set()).add(timer_id)

        timer_obj.start()
        restored += 1

    if restored or fired:
        logger.info(
            "Восстановлено таймеров: %s, сработало во время простоя: %s.",
            restored, fired,
        )


# --- Подсказки и парсинг команд таймера --------------------------------------

def _send_timer_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Не удалось распознать команду таймера.\n\n"
        "Используйте формат: <code>[команда] [время] [описание]</code>\n"
        "Время задаётся буквами д/ч/м/с или d/h/m/s, например:\n"
        "<code>/t 1д5ч30с проверить код</code>\n"
        "<code>/t 1d5h30s check code</code>\n"
        "<code>/t 10с</code> (описание не обязательно)",
    )


def _process_timer_request(message: types.Message, args_text: str):
    args_text = args_text.strip()
    if not args_text:
        _send_timer_usage_hint(message)
        return

    parts = args_text.split(maxsplit=1)
    time_part = parts[0]
    description = parts[1].strip() if len(parts) > 1 else ""

    duration_seconds = parse_duration(time_part)
    if duration_seconds is None:
        _send_timer_usage_hint(message)
        return

    create_timer(message, duration_seconds, description)


def _show_my_timers(message: types.Message):
    user_id = message.from_user.id
    now = time.time()

    with _timers_lock:
        timer_ids = sorted(USER_TIMERS.get(user_id, set()))
        timers_info = [
            (tid, TIMERS[tid]["end_time"], TIMERS[tid]["description"])
            for tid in timer_ids
            if tid in TIMERS
        ]

    if not timers_info:
        bot.reply_to(message, "У вас нет активных таймеров.")
        return

    lines = ["<b>📑 Ваши активные таймеры:</b>"]
    for tid, end_time, description in timers_info:
        remaining = max(int(end_time - now), 0)
        desc_part = f" — {html.escape(description)}" if description else ""
        lines.append(f"• #{tid}: осталось {format_duration(remaining)}{desc_part}")

    lines.append("\nДля удаления используйте: <code>/del [ID]</code>")
    bot.reply_to(message, "\n".join(lines))


def _send_cancel_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Укажите ID таймера для удаления.\n"
        "Формат: <code>/del [ID]</code> или <code>удалить [ID]</code>\n"
        "Посмотреть свои ID можно командой /mytimers.",
    )


def _process_cancel_request(message: types.Message, args_text: str):
    args_text = args_text.strip()
    if not args_text:
        _send_cancel_usage_hint(message)
        return

    timer_id_str = args_text.split(maxsplit=1)[0].lstrip("#")

    if not timer_id_str.isdigit():
        _send_cancel_usage_hint(message)
        return

    timer_id = int(timer_id_str)
    result_text = cancel_timer(timer_id, message.from_user.id)
    bot.reply_to(message, result_text)


# =============================================================================
#                          СТАТИСТИКА: УЧЁТ И ОТЧЁТ
# =============================================================================

def track_message_stats(message: types.Message):
    """Извлекает данные из сообщения и передаёт их в database.record_message_stats."""
    if not database.db_enabled():
        return

    user = message.from_user
    if user is None or user.is_bot:
        return

    chat = message.chat
    if chat.type == "private":
        chat_title = f"ЛС: {user.first_name or user.username or user.id}"
    else:
        chat_title = chat.title or str(chat.id)

    content_type = message.content_type
    chars = stickers = photos = videos = voice = gifs = 0

    if content_type == "text":
        chars = len(message.text or "")
    elif content_type == "sticker":
        stickers = 1
    elif content_type == "photo":
        photos = 1
        chars = len(message.caption or "")
    elif content_type == "video":
        videos = 1
        chars = len(message.caption or "")
    elif content_type in ("voice", "video_note"):
        voice = 1
    elif content_type == "animation":
        gifs = 1
        chars = len(message.caption or "")
    elif message.caption:
        chars = len(message.caption)

    database.record_message_stats(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        chat_id=chat.id,
        chat_type=chat.type,
        chat_title=chat_title,
        chars=chars,
        stickers=stickers,
        photos=photos,
        videos=videos,
        voice=voice,
        gifs=gifs,
    )


def build_stats_report() -> str:
    """Формирует текст отчёта /стата: сводка + топ-10 по активности."""
    total_users, total_chats, totals = database.get_stats_overview()
    top_rows = database.get_top_activity(limit=10)

    lines = [
        "<b>📊 Общая статистика</b>",
        "",
        f"👤 Пользователей: {total_users}",
        f"💬 Чатов: {total_chats}",
        f"✉️ Сообщений: {totals['messages']}",
        f"🔠 Символов: {totals['chars']}",
        f"🎟 Стикеров: {totals['stickers']}",
        f"🖼 Фото: {totals['photos']}",
        f"🎬 Видео: {totals['videos']}",
        f"🎤 Голосовых: {totals['voice']}",
        f"🎞 GIF: {totals['gifs']}",
    ]

    if top_rows:
        lines.append("")
        lines.append("<b>🏆 Топ-10 по активности</b>")
        for i, row in enumerate(top_rows, start=1):
            (username, first_name, chat_title,
             messages, chars, stickers, photos, videos, voice, gifs) = row

            display_name = f"@{username}" if username else (first_name or "Без имени")
            display_name = html.escape(display_name)
            chat_label = html.escape(chat_title or "Без названия")

            extra_parts = []
            if stickers:
                extra_parts.append(f"стикеры {stickers}")
            if photos:
                extra_parts.append(f"фото {photos}")
            if videos:
                extra_parts.append(f"видео {videos}")
            if voice:
                extra_parts.append(f"голосовые {voice}")
            if gifs:
                extra_parts.append(f"gif {gifs}")
            extra = f" ({', '.join(extra_parts)})" if extra_parts else ""

            lines.append(
                f"{i}. {display_name} — {chat_label}: "
                f"{messages} сообщ., {chars} симв.{extra}"
            )

    return "\n".join(lines)


# =============================================================================
#                          MIDDLEWARE: УЧЁТ СТАТИСТИКИ
# =============================================================================

@bot.middleware_handler(update_types=["message"])
def stats_middleware(bot_instance, message):
    """
    Срабатывает на каждое сообщение во всех чатах.
    Запись в БД выполняется в отдельном потоке, чтобы не задерживать
    ответ бота на текущую команду.
    """
    def _track():
        try:
            track_message_stats(message)
        except Exception:
            logger.exception("Ошибка при записи статистики сообщения.")

    threading.Thread(target=_track, daemon=True).start()


# =============================================================================
#                          ОБРАБОТЧИКИ КОМАНД
# =============================================================================

HELP_TEXT = (
    "<b>🤖 Команды бота</b>\n\n"

    "<b>⏰ Таймер</b>\n"
    "<code>/t [время] [описание]</code>\n"
    "Синонимы: <code>/timer</code>, <code>таймер</code>, <code>тайм</code>\n"
    "Время: <code>д/ч/м/с</code> или <code>d/h/m/s</code> "
    "(дни/часы/минуты/секунды), можно комбинировать.\n"
    "Описание необязательно. По срабатыванию бот напишет в чат и упомянет вас.\n"
    "Примеры:\n"
    "  <code>/t 1д5ч30с купить продукты</code>\n"
    "  <code>/t 2h30m buy groceries</code>\n"
    "  <code>/t 5м вытащить мясо с морозильника</code>\n\n"

    "<b>📑 Мои таймеры</b>\n"
    "<code>/mytimers</code> или <code>таймеры</code>\n"
    "Список активных таймеров с ID и оставшимся временем.\n\n"

    "<b>🗑 Удалить таймер</b>\n"
    "<code>/del [ID]</code>\n"
    "Синонимы: <code>/cancel</code>, <code>удалить</code>, <code>отмена</code>\n"
    "ID берётся из <code>/mytimers</code>.\n"
    "Пример: <code>/del 3</code>\n\n"

    "🏓 <code>/ping</code>\n"
    "Проверка отклика бота + аптайм.\n\n"

    "🆔 <code>/myid</code>\n"
    "Показать свой Telegram ID.\n\n"
)


@bot.message_handler(commands=["start", "help"])
def handle_help(message: types.Message):
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["myid"])
def handle_myid(message: types.Message):
    """Показывает Telegram ID пользователя — удобно для настройки ADMIN_IDS."""
    bot.reply_to(message, f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>")


@bot.message_handler(commands=["ping"])
def handle_ping(message: types.Message):
    """Замеряет время round-trip к Telegram API и показывает Uptime."""
    start = time.perf_counter()
    sent = bot.send_message(message.chat.id, "🏓 Pong!")
    elapsed_ms = (time.perf_counter() - start) * 1000

    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=(
            f"🏓 Pong!\n"
            f"Ping: <code>{elapsed_ms:.3f}</code> ms\n"
            f"Uptime: {get_uptime_str()}"
        ),
    )


@bot.message_handler(commands=["t", "timer"])
def handle_timer_slash(message: types.Message):
    parts = message.text.split(maxsplit=1)
    args_text = parts[1] if len(parts) > 1 else ""
    _process_timer_request(message, args_text)


@bot.message_handler(
    func=lambda m: bool(re.match(r"^(тайм|таймер)\b", (m.text or ""), re.IGNORECASE))
)
def handle_timer_text(message: types.Message):
    parts = message.text.split(maxsplit=1)
    args_text = parts[1] if len(parts) > 1 else ""
    _process_timer_request(message, args_text)


@bot.message_handler(commands=["mytimers"])
def handle_my_timers_command(message: types.Message):
    _show_my_timers(message)


@bot.message_handler(
    func=lambda m: bool(re.match(r"^таймеры\b", (m.text or ""), re.IGNORECASE))
)
def handle_my_timers_text(message: types.Message):
    _show_my_timers(message)


@bot.message_handler(commands=["del", "del_timer", "cancel"])
def handle_cancel_slash(message: types.Message):
    parts = message.text.split(maxsplit=1)
    args_text = parts[1] if len(parts) > 1 else ""
    _process_cancel_request(message, args_text)


@bot.message_handler(
    func=lambda m: bool(re.match(r"^(удалить|отмена)\b", (m.text or ""), re.IGNORECASE))
)
def handle_cancel_text(message: types.Message):
    parts = message.text.split(maxsplit=1)
    args_text = parts[1] if len(parts) > 1 else ""
    _process_cancel_request(message, args_text)


@bot.message_handler(
    func=lambda m: bool(re.match(r"^/?(стата|статистика)\b", (m.text or ""), re.IGNORECASE))
)
def handle_stats(message: types.Message):
    """Статистика — только в личке бота и только для админов из ADMIN_IDS."""
    if message.chat.type != "private":
        return
    if message.from_user.id not in ADMIN_IDS:
        return

    if not database.db_enabled():
        bot.reply_to(message, "⚠️ База данных не настроена — статистика недоступна.")
        return

    report = build_stats_report()
    for chunk in split_message(report):
        bot.send_message(message.chat.id, chunk)


# =============================================================================
#                  ВЕБ-СЕРВЕР (WEBHOOK + HEALTHCHECK)
# =============================================================================

web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    """UptimeRobot пингует этот адрес, чтобы Render не засыпал."""
    return "Bot is running!"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    """
    Telegram присылает сюда POST-запрос при каждом новом сообщении.
    Передаём данные боту для обработки.
    """
    import flask
    if flask.request.headers.get("content-type") == "application/json":
        json_update = flask.request.get_data(as_text=True)
        update = types.Update.de_json(json_update)
        bot.process_new_updates([update])
        return "ok", 200
    return "bad request", 400


# =============================================================================
#                          ТОЧКА ВХОДА / ЗАПУСК БОТА
# =============================================================================

def main():
    logger.info("Бот запускается...")

    database.init_db()
    restore_timers()

    # Адрес на Render, куда Telegram будет слать обновления.
    # Render всегда даёт HTTPS, что обязательно для webhook.
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if not webhook_url:
        # Если WEBHOOK_URL не задан — fallback на polling (удобно для
        # локального тестирования в VS Code без ngrok).
        logger.warning(
            "WEBHOOK_URL не задан — запускаю polling (только для локального теста)."
        )
        while True:
            try:
                logger.info("Запуск polling...")
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception:
                logger.exception("Бот упал с ошибкой, перезапуск через 5 секунд...")
                time.sleep(5)
        return

    # Снимаем старый webhook (если был) и регистрируем новый.
    # drop_pending_updates=True — игнорируем сообщения накопившиеся
    # пока бот был выключен (аналог skip_pending в polling).
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(
        url=f"{webhook_url}/webhook",
        drop_pending_updates=True,
    )
    logger.info("Webhook зарегистрирован: %s/webhook", webhook_url)

    port = int(os.environ.get("PORT", 10000))
    logger.info("Запуск Flask на порту %s...", port)

    # Flask теперь основной процесс — он принимает запросы от Telegram.
    # threaded=True чтобы несколько запросов обрабатывались параллельно.
    web_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()