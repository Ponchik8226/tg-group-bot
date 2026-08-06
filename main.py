"""
Telegram-бот для личного использования в группе (pyTelegramBotAPI).

Команды пользователей:
/ping        — проверка отклика + аптайм
/t, /т       — установка таймера
/mytimers    — список своих таймеров
/del, /cancel,
"удалить", "отмена" — удаление таймера
/start       — приветствие
/help        — список команд
/id          — ID чата, или ID пользователя/бота по реплаю

Архитектура:
config.py   — объект bot, переменные окружения, логирование
database.py — вся работа с БД (таймеры, статистика)
utils.py    — мелкие хелперы
admin.py    — все команды для администраторов
main.py     — этот файл: пользовательские хендлеры, middleware, Flask, запуск

ВАЖНО: для учёта статистики во всех чатах нужно отключить Privacy Mode
бота через @BotFather (Bot Settings -> Group Privacy -> Turn off).
"""

import html
import os
import re
import threading
import time

from flask import Flask, request as flask_request
from telebot import types

import admin
import database
from config import bot, logger, ADMIN_IDS
from utils import (
    parse_duration,
    format_duration,
    build_mention,
    get_uptime_str,
    split_message,
    build_stats_report,
)

# =============================================================================
# ХРАНИЛИЩЕ АКТИВНЫХ ТАЙМЕРОВ (В ПАМЯТИ)
# =============================================================================

# TIMERS: timer_id -> {chat_id, user_id, user_mention, description, end_time, duration}
TIMERS = {}

# USER_TIMERS: user_id -> множество timer_id пользователя
USER_TIMERS = {}

# Лимиты таймеров
MAX_TIMERS_PER_USER = 100              # максимум активных таймеров на одного пользователя
MAX_TIMER_DURATION = 365 * 24 * 3600  # максимальная длительность — 1 год в секундах
MIN_TIMER_DURATION = 10                # минимальная длительность — 10 секунд

_next_timer_id = 1
_timers_lock = threading.Lock()

# =============================================================================
# ЛОГИКА ТАЙМЕРОВ
# =============================================================================

def fire_timer(timer_id: int, missed: bool = False):
    """
    Срабатывает по таймеру: тегает пользователя и шлёт описание.
    Если missed=True — таймер сработал, пока бот был выключен.

    Идемпотентен: если таймер уже удалён из TIMERS (сработал через
    threading.Timer, а поллер тоже попытался его запустить) — просто выходим.

    Порядок: сначала отправить сообщение, потом удалить из БД.
    Это исключает ситуацию «удалили из БД, но сообщение не дошло».
    Если send_message упадёт — таймер останется в БД и сработает
    повторно после перезапуска бота.
    """
    with _timers_lock:
        info = TIMERS.pop(timer_id, None)
        if info is not None:
            USER_TIMERS.get(info["user_id"], set()).discard(timer_id)

    if info is None:
        logger.info("Таймер #%s сработал, но был отменён ранее.", timer_id)
        return

    logger.info(
        "Таймер #%s сработал (chat_id=%s, user_id=%s, missed=%s).",
        timer_id, info["chat_id"], info["user_id"], missed,
    )

    text = f"⏰ {info['user_mention']}, время вышло!"
    if info["description"]:
        text += f"\n📝 {html.escape(info['description'])}"
    if missed:
        text += "\n\n⚠️ Бот был выключен, когда таймер должен был сработать."

    # Сначала отправляем — 3 попытки с паузой
    sent = False
    for attempt in range(1, 4):
        try:
            bot.send_message(info["chat_id"], text)
            sent = True
            break
        except Exception:
            if attempt < 3:
                logger.warning(
                    "Попытка %d/3 отправить сообщение таймера #%s не удалась, повтор через 2 сек...",
                    attempt, timer_id,
                )
                time.sleep(2)
            else:
                logger.exception(
                    "Не удалось отправить сообщение по таймеру #%s после 3 попыток.",
                    timer_id,
                )

    # Удаляем из БД только после попытки отправки.
    # Если отправка провалилась — таймер всё равно удаляем, чтобы не
    # спамить при следующем перезапуске. Логи помогут разобраться.
    database.delete_timer(timer_id)


def _check_due_timers():
    """
    Проверяет все активные таймеры и срабатывает те, у которых время вышло.
    Вызывается планировщиком каждые 30 секунд.
    """
    now = time.time()
    with _timers_lock:
        due = [tid for tid, info in TIMERS.items() if info["end_time"] <= now]

    for tid in due:
        try:
            fire_timer(tid)
        except Exception:
            logger.exception("Ошибка при срабатывании таймера #%s", tid)


def _start_timer_poller():
    """
    Запускает фоновый поток-поллер — страховочная сеть для таймеров.

    Основная точность обеспечивается threading.Timer (создаётся при
    каждом новом таймере и при восстановлении из БД после рестарта).
    Поллер нужен как запасной путь: если threading.Timer умер (рестарт
    Render в середине длинного таймера), поллер подхватит через ≤30 сек.

    Поскольку fire_timer идемпотентен, двойной вызов (Timer + поллер)
    безопасен: второй просто найдёт пустое место и тихо выйдет.
    """
    def _poller():
        logger.info("Планировщик таймеров запущен (интервал проверки: 30 сек).")
        while True:
            time.sleep(30)
            try:
                _check_due_timers()
            except Exception:
                logger.exception("Ошибка в планировщике таймеров.")

    thread = threading.Thread(target=_poller, daemon=True, name="timer-poller")
    thread.start()


def create_timer(message: types.Message, duration_seconds: int, description: str):
    """Создаёт таймер: сохраняет в БД и добавляет в память.

    Гибридный подход:
    - threading.Timer  — срабатывает точно в нужное время
    - поллер           — страховка на случай перезапуска бота
    Оба пути безопасны: fire_timer идемпотентен.
    """
    global _next_timer_id

    user = message.from_user
    first_name = user.first_name or "Пользователь"
    mention = build_mention(user.id, first_name)
    end_time = time.time() + duration_seconds

    # Получаем ID из БД до захвата лока (БД может быть медленной)
    if database.db_enabled():
        timer_id = database.insert_timer(
            message.chat.id, user.id, first_name, description, end_time
        )
    else:
        with _timers_lock:
            timer_id = _next_timer_id
            _next_timer_id += 1

    # Создаём объект таймера, но не стартуем — чтобы не сработал
    # раньше, чем запись появится в TIMERS
    timer_obj = threading.Timer(duration_seconds, fire_timer, args=(timer_id,))
    timer_obj.daemon = True

    with _timers_lock:
        TIMERS[timer_id] = {
            "chat_id": message.chat.id,
            "user_id": user.id,
            "user_mention": mention,
            "description": description,
            "end_time": end_time,
            "duration": duration_seconds,
            "timer_obj": timer_obj,   # нужен для точной отмены через .cancel()
        }
        USER_TIMERS.setdefault(user.id, set()).add(timer_id)

    timer_obj.start()  # стартуем только после попадания в TIMERS

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
        del TIMERS[timer_id]
        USER_TIMERS.get(user_id, set()).discard(timer_id)

    # Отменяем threading.Timer если он ещё не сработал
    timer_obj = info.get("timer_obj")
    if timer_obj is not None:
        timer_obj.cancel()

    database.delete_timer(timer_id)
    logger.info("Таймер #%s отменён пользователем %s.", timer_id, user_id)
    return f"🗑 Таймер #{timer_id} успешно удалён."


def restore_timers():
    """
    При старте восстанавливает таймеры из базы (если она настроена).

    Просроченные таймеры добавляются в TIMERS с прошедшим end_time —
    поллер подберёт их на первой же проверке (~30 сек после старта)
    и отправит с пометкой «опоздание».
    """
    if not database.db_enabled():
        return

    rows = database.load_all_timers()
    if not rows:
        return

    now = time.time()
    restored = 0
    missed = 0

    for timer_id, chat_id, user_id, first_name, description, end_time in rows:
        mention = build_mention(user_id, first_name)
        remaining = end_time - now

        if remaining > 0:
            # Таймер ещё не истёк — создаём threading.Timer с остатком времени
            timer_obj = threading.Timer(remaining, fire_timer, args=(timer_id,))
            timer_obj.daemon = True
        else:
            # Таймер просрочен — поллер подберёт на следующей проверке (~30 сек)
            timer_obj = None

        with _timers_lock:
            TIMERS[timer_id] = {
                "chat_id": chat_id,
                "user_id": user_id,
                "user_mention": mention,
                "description": description,
                "end_time": end_time,
                "duration": max(0, int(remaining)),
                "timer_obj": timer_obj,
            }
            USER_TIMERS.setdefault(user_id, set()).add(timer_id)

        if timer_obj is not None:
            timer_obj.start()
            restored += 1
        else:
            missed += 1

    logger.info(
        "Восстановлено таймеров: %s активных, %s просроченных (сработают через ~30 сек).",
        restored, missed,
    )


# --- Подсказки и парсинг команд таймера --------------------------------------

def _send_timer_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Не удалось распознать команду таймера.\n\n"
        "Используйте формат: <code>[команда] [время] [описание]</code>\n"
        "Время задаётся буквами д/ч/м/с или d/h/m/s, например:\n"
        "<code>/t 1д5ч30с проверить код</code>\n"
        "<code>/т 10с</code> (описание не обязательно)\n"
        f"Минимальное время — {MIN_TIMER_DURATION} секунд.",
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

    # Проверка минимальной длительности
    if duration_seconds < MIN_TIMER_DURATION:
        bot.reply_to(
            message,
            f"⚠️ Минимальная длительность таймера — {MIN_TIMER_DURATION} секунд.",
        )
        return

    # Проверка максимальной длительности
    if duration_seconds > MAX_TIMER_DURATION:
        bot.reply_to(
            message,
            "⚠️ Максимальная длительность таймера — 1 год. Укажите меньшее время.",
        )
        return

    # Проверка лимита активных таймеров у пользователя
    with _timers_lock:
        user_timer_count = len(USER_TIMERS.get(message.from_user.id, set()))
    if user_timer_count >= MAX_TIMERS_PER_USER:
        bot.reply_to(
            message,
            f"⚠️ У вас уже {user_timer_count} активных таймеров (максимум {MAX_TIMERS_PER_USER}). "
            f"Удалите старые через /mytimers, прежде чем создавать новые.",
        )
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
# СТАТИСТИКА: УЧЁТ И ОТЧЁТ
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

    is_forward = (
        message.forward_origin is not None
        or message.forward_from is not None
        or message.forward_from_chat is not None
        or message.forward_sender_name is not None
    )

    if is_forward:
        database.record_message_stats(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            chat_id=chat.id,
            chat_type=chat.type,
            chat_title=chat_title,
            messages=0, chars=0, stickers=0, photos=0,
            videos=0, voice=0, gifs=0, forwards=1,
        )
        return

    content_type = message.content_type
    is_sticker = content_type == "sticker"
    messages = 0 if is_sticker else 1
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
        messages=messages,
        chars=chars,
        stickers=stickers,
        photos=photos,
        videos=videos,
        voice=voice,
        gifs=gifs,
        forwards=0,
    )


# =============================================================================
# MIDDLEWARE: УЧЁТ СТАТИСТИКИ
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
# ОБРАБОТЧИКИ КОМАНД
# =============================================================================

# Username бота заполняется при старте в main() через bot.get_me()
_BOT_USERNAME = ""

HELP_TEXT = (
    "<b>🤖 Команды бота</b>\n\n"
    "<b>⏰ Таймер</b>\n"
    "<code>/t [время] [описание]</code> или <code>/т [время] [описание]</code>\n"
    "Время: <code>д/ч/м/с</code> или <code>d/h/m/s</code> "
    "(дни/часы/минуты/секунды), можно комбинировать.\n"
    "Описание необязательно. По срабатыванию бот напишет в чат и упомянет вас.\n"
    f"Минимальное время — {MIN_TIMER_DURATION} секунд.\n"
    "Примеры:\n"
    " <code>/t 1д5ч30с купить продукты</code>\n"
    " <code>/t 2h30m buy groceries</code>\n"
    " <code>/т 5м вытащить мясо с морозильника</code>\n\n"
    "<b>📑 Мои таймеры</b>\n"
    "<code>/mytimers</code>\n"
    "Список активных таймеров с ID и оставшимся временем.\n\n"
    "<b>🗑 Удалить таймер</b>\n"
    "<code>/del [ID]</code>\n"
    "Синонимы: <code>/cancel</code>, <code>удалить</code>, <code>отмена</code>\n"
    "ID берётся из <code>/mytimers</code>.\n"
    "Пример: <code>/del 3</code>\n\n"
    "🏓 <code>/ping</code>\n"
    "Проверка отклика бота + аптайм.\n\n"
    "🆔 <code>/id</code>\n"
    "В чате — показывает ID чата.\n"
    "Реплаем на сообщение — показывает ID пользователя или бота.\n"
    "В личке — показывает ваш Telegram ID.\n\n"
)


def _is_for_me(message: types.Message) -> bool:
    """
    Проверяет что слэш-команда адресована нашему боту (или вообще без @).
    Нужно чтобы не отвечать на /start@другой_бот в группах.
    """
    text = message.text or ""
    if "@" not in text:
        return True
    return f"@{_BOT_USERNAME}".lower() in text.lower()


@bot.message_handler(commands=["start"])
def handle_start(message: types.Message):
    if not _is_for_me(message):
        return
    bot.reply_to(
        message,
        "👋 Привет! Я бот для напоминаний и статистики чата.\n\n"
        "Чтобы увидеть список команд — напишите /help",
    )


@bot.message_handler(commands=["help"])
def handle_help(message: types.Message):
    if not _is_for_me(message):
        return
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["id"])
def handle_id(message: types.Message):
    """
    Без реплая в группе  — показывает ID текущего чата.
    Без реплая в личке   — показывает Telegram ID пользователя.
    С реплаем на кого-то — показывает ID того пользователя или бота.
    """
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        name = html.escape(target.first_name or target.username or str(target.id))
        bot_label = " (бот)" if target.is_bot else ""
        bot.reply_to(
            message,
            f"🆔 ID <b>{name}</b>{bot_label}: <code>{target.id}</code>",
        )
    elif message.chat.type == "private":
        bot.reply_to(
            message,
            f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>",
        )
    else:
        bot.reply_to(
            message,
            f"🆔 ID этого чата: <code>{message.chat.id}</code>",
        )


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


# /t и /т — единственные команды для таймера
@bot.message_handler(commands=["t", "т"])
def handle_timer_slash(message: types.Message):
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


# =============================================================================
# ВЕБ-СЕРВЕР (WEBHOOK + HEALTHCHECK)
# =============================================================================

web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    """UptimeRobot пингует этот адрес, чтобы Render не засыпал."""
    return "Bot is running!"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    """
    Telegram присылает сюда POST-запрос при каждом новом сообщении
    или нажатии на inline-кнопку (callback_query).
    """
    if flask_request.headers.get("content-type") == "application/json":
        json_update = flask_request.get_data(as_text=True)
        update = types.Update.de_json(json_update)
        bot.process_new_updates([update])
        return "ok", 200
    return "bad request", 400


# =============================================================================
# ТОЧКА ВХОДА / ЗАПУСК БОТА
# =============================================================================

def main():
    global _BOT_USERNAME

    logger.info("Бот запускается...")

    # Получаем username бота — нужен для фильтрации чужих команд в группах
    try:
        me = bot.get_me()
        _BOT_USERNAME = me.username or ""
        logger.info("Бот: @%s (id=%s)", _BOT_USERNAME, me.id)
    except Exception:
        logger.exception("Не удалось получить информацию о боте.")

    database.init_db()
    restore_timers()

    # Запускаем поллер таймеров (проверка каждые 30 сек)
    _start_timer_poller()

    # Регистрируем хендлеры админ-команд
    admin.register(_BOT_USERNAME)

    # Адрес на Render, куда Telegram будет слать обновления.
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if not webhook_url:
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

    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(
        url=f"{webhook_url}/webhook",
        drop_pending_updates=True,
        # callback_query — для inline-кнопок пагинации в топах
        allowed_updates=["message", "edited_message", "channel_post", "callback_query"],
    )
    logger.info("Webhook зарегистрирован: %s/webhook", webhook_url)

    port = int(os.environ.get("PORT", 10000))
    logger.info("Запуск Flask на порту %s...", port)
    web_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
