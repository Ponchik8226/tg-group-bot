"""
Telegram-бот для личного использования в группе (pyTelegramBotAPI).

Команды пользователей:
/ping            — проверка отклика + аптайм
/t, /т           — установка таймера
/tr, /тр         — повторяющийся таймер (срабатывает снова и снова)
/mytimers        — список своих таймеров (с сортировкой)
/del, /cancel,
"удалить", "отмена" — удаление таймера
/start           — приветствие
/help            — список команд
/id              — ID чата, или ID пользователя/бота по реплаю

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

# TIMERS: timer_id -> {chat_id, user_id, user_mention, description, end_time,
#                      duration, timer_obj, is_recurring, interval_seconds}
TIMERS = {}

# USER_TIMERS: user_id -> множество timer_id пользователя
USER_TIMERS = {}

# Лимиты таймеров
MAX_TIMERS_PER_USER    = 100              # максимум активных таймеров
MAX_TIMER_DURATION     = 365 * 24 * 3600  # максимальная длительность — 1 год
MIN_TIMER_DURATION     = 10               # минимальная длительность — 10 секунд
MAX_DESCRIPTION_LENGTH = 200              # максимальная длина описания

_next_timer_id = 1
_timers_lock   = threading.Lock()

# Текущий режим сортировки /mytimers по user_id: "id" или "time"
_sort_state: dict[int, str] = {}

# =============================================================================
# ЛОГИКА ТАЙМЕРОВ
# =============================================================================

def fire_timer(timer_id: int, missed: bool = False):
    """
    Срабатывает по таймеру: тегает пользователя и шлёт описание.
    Если missed=True — таймер сработал, пока бот был выключен.

    Идемпотентен: если таймер уже удалён из TIMERS — просто выходим.

    Повторяющийся таймер: вместо удаления из БД обновляет end_time
    и создаёт новый threading.Timer на следующий цикл.

    Порядок: сначала отправить, потом обновить/удалить из БД.
    """
    with _timers_lock:
        info = TIMERS.pop(timer_id, None)
        if info is not None:
            USER_TIMERS.get(info["user_id"], set()).discard(timer_id)

    if info is None:
        logger.info("Таймер #%s сработал, но был отменён ранее.", timer_id)
        return

    is_recurring    = info.get("is_recurring", False)
    interval        = info.get("interval_seconds", 0)

    logger.info(
        "Таймер #%s сработал (chat_id=%s, user_id=%s, missed=%s, recurring=%s).",
        timer_id, info["chat_id"], info["user_id"], missed, is_recurring,
    )

    # Текст уведомления
    if is_recurring:
        text = f"🔁 {info['user_mention']}, повторяющийся таймер!"
    else:
        text = f"⏰ {info['user_mention']}, время вышло!"

    if info["description"]:
        text += f"\n📝 {html.escape(info['description'])}"
    if is_recurring and interval:
        text += f"\n↺ Следующий через {format_duration(interval)}"
    if missed:
        text += "\n\n⚠️ Бот был выключен, когда таймер должен был сработать."

    # Отправка — 3 попытки
    for attempt in range(1, 4):
        try:
            bot.send_message(info["chat_id"], text)
            break
        except Exception:
            if attempt < 3:
                logger.warning(
                    "Попытка %d/3 отправить таймер #%s не удалась, повтор через 2 сек...",
                    attempt, timer_id,
                )
                time.sleep(2)
            else:
                logger.exception(
                    "Не удалось отправить сообщение по таймеру #%s после 3 попыток.",
                    timer_id,
                )

    if is_recurring and interval:
        # Повторяющийся: обновляем время в БД и перезапускаем
        new_end_time  = time.time() + interval
        database.update_timer_end_time(timer_id, new_end_time)

        new_timer_obj = threading.Timer(interval, fire_timer, args=(timer_id,))
        new_timer_obj.daemon = True

        with _timers_lock:
            TIMERS[timer_id] = {
                **info,
                "end_time":   new_end_time,
                "duration":   interval,
                "timer_obj":  new_timer_obj,
            }
            USER_TIMERS.setdefault(info["user_id"], set()).add(timer_id)

        new_timer_obj.start()
        logger.info(
            "Повторяющийся таймер #%s перезапланирован через %s сек.",
            timer_id, interval,
        )
    else:
        # Обычный: удаляем из БД
        database.delete_timer(timer_id)


def _check_due_timers():
    """Проверяет TIMERS и срабатывает просроченные. Вызывается поллером."""
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
        logger.info("Планировщик таймеров запущен (интервал: 30 сек).")
        while True:
            time.sleep(30)
            try:
                _check_due_timers()
            except Exception:
                logger.exception("Ошибка в планировщике таймеров.")

    threading.Thread(target=_poller, daemon=True, name="timer-poller").start()


def create_timer(
    message: types.Message,
    duration_seconds: int,
    description: str,
    is_recurring: bool = False,
):
    """
    Создаёт таймер: сохраняет в БД и добавляет в память.

    Гибридный подход:
    - threading.Timer  — срабатывает точно в нужное время
    - поллер           — страховка на случай перезапуска бота
    Оба пути безопасны: fire_timer идемпотентен.
    """
    global _next_timer_id

    user          = message.from_user
    first_name    = user.first_name or "Пользователь"
    mention       = build_mention(user.id, first_name)
    end_time      = time.time() + duration_seconds
    interval_secs = duration_seconds if is_recurring else 0

    # ID из БД получаем ДО захвата лока (БД может быть медленной)
    if database.db_enabled():
        timer_id = database.insert_timer(
            message.chat.id, user.id, first_name, description, end_time,
            is_recurring=is_recurring, interval_seconds=interval_secs,
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
            "chat_id":          message.chat.id,
            "user_id":          user.id,
            "user_mention":     mention,
            "description":      description,
            "end_time":         end_time,
            "duration":         duration_seconds,
            "timer_obj":        timer_obj,
            "is_recurring":     is_recurring,
            "interval_seconds": interval_secs,
        }
        USER_TIMERS.setdefault(user.id, set()).add(timer_id)

    timer_obj.start()

    logger.info(
        "Создан %s таймер #%s на %s сек (user_id=%s, chat_id=%s).",
        "повторяющийся" if is_recurring else "обычный",
        timer_id, duration_seconds, user.id, message.chat.id,
    )

    desc_part = f"\n📝 {html.escape(description)}" if description else ""
    if is_recurring:
        bot.reply_to(
            message,
            f"✅ Повторяющийся таймер #{timer_id} установлен.\n"
            f"↺ Будет срабатывать каждые {format_duration(duration_seconds)}."
            f"{desc_part}",
        )
    else:
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
            return f"❌ Таймер #{timer_id} принадлежит другому пользователю."
        del TIMERS[timer_id]
        USER_TIMERS.get(user_id, set()).discard(timer_id)

    timer_obj = info.get("timer_obj")
    if timer_obj is not None:
        timer_obj.cancel()

    database.delete_timer(timer_id)
    logger.info("Таймер #%s отменён пользователем %s.", timer_id, user_id)
    return f"🗑 Таймер #{timer_id} успешно удалён."


def restore_timers():
    """
    При старте восстанавливает таймеры из базы.

    Просроченные — поллер подберёт через ≤30 сек.
    Повторяющиеся просроченные — тоже подберёт и сразу перезапланирует.
    """
    if not database.db_enabled():
        return

    rows = database.load_all_timers()
    if not rows:
        return

    now = time.time()
    restored = missed = 0

    for (timer_id, chat_id, user_id, first_name,
         description, end_time, is_recurring, interval_seconds) in rows:

        mention   = build_mention(user_id, first_name)
        remaining = end_time - now

        if remaining > 0:
            timer_obj = threading.Timer(remaining, fire_timer, args=(timer_id,))
            timer_obj.daemon = True
        else:
            timer_obj = None

        with _timers_lock:
            TIMERS[timer_id] = {
                "chat_id":          chat_id,
                "user_id":          user_id,
                "user_mention":     mention,
                "description":      description,
                "end_time":         end_time,
                "duration":         max(0, int(remaining)),
                "timer_obj":        timer_obj,
                "is_recurring":     bool(is_recurring),
                "interval_seconds": int(interval_seconds),
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


# =============================================================================
# ОТОБРАЖЕНИЕ ТАЙМЕРОВ (/mytimers)
# =============================================================================

def _build_timers_message(user_id: int, sort_mode: str) -> tuple:
    """
    Строит текст и inline-клавиатуру для /mytimers.
    sort_mode: "id" — по порядку создания, "time" — сначала ближайшие.
    Возвращает (text, markup). Если таймеров нет — markup=None.
    """
    now = time.time()

    with _timers_lock:
        snapshot = [
            {
                "id":               tid,
                "end_time":         TIMERS[tid]["end_time"],
                "description":      TIMERS[tid]["description"],
                "is_recurring":     TIMERS[tid].get("is_recurring", False),
                "interval_seconds": TIMERS[tid].get("interval_seconds", 0),
            }
            for tid in USER_TIMERS.get(user_id, set())
            if tid in TIMERS
        ]

    if not snapshot:
        return "У вас нет активных таймеров.", None

    # Сортировка
    if sort_mode == "time":
        snapshot.sort(key=lambda t: t["end_time"])
    else:
        snapshot.sort(key=lambda t: t["id"])

    count = len(snapshot)
    lines = [f"<b>📑 Ваши активные таймеры ({count})</b>", ""]

    for info in snapshot:
        remaining = max(int(info["end_time"] - now), 0)
        icon      = "🔁" if info["is_recurring"] else "⏱"

        header = f"{icon} <b>#{info['id']}</b> · {format_duration(remaining)}"
        if info["is_recurring"] and info["interval_seconds"]:
            header += f"  <i>↺ каждые {format_duration(info['interval_seconds'])}</i>"
        lines.append(header)

        if info["description"]:
            lines.append(html.escape(info["description"]))

        lines.append("")  # пустая строка между таймерами

    lines.append("/del [ID] — удалить таймер")

    # Inline-кнопки сортировки
    if sort_mode == "time":
        time_btn = types.InlineKeyboardButton("✅ По времени", callback_data="sort_timers:time")
        id_btn   = types.InlineKeyboardButton("🔢 По ID",      callback_data="sort_timers:id")
    else:
        time_btn = types.InlineKeyboardButton("⏱ По времени", callback_data="sort_timers:time")
        id_btn   = types.InlineKeyboardButton("✅ По ID",      callback_data="sort_timers:id")

    markup = types.InlineKeyboardMarkup()
    markup.row(time_btn, id_btn)

    return "\n".join(lines), markup


def _show_my_timers(message: types.Message):
    user_id   = message.from_user.id
    sort_mode = _sort_state.get(user_id, "id")
    text, markup = _build_timers_message(user_id, sort_mode)

    if markup is None:
        # Нет таймеров — без клавиатуры
        bot.reply_to(message, text)
        return

    if len(text) <= 4000:
        # Обычный случай: одно сообщение с кнопками
        bot.reply_to(message, text, reply_markup=markup)
    else:
        # Очень много таймеров / длинные описания — режем на части без кнопок
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            if i == 0:
                bot.reply_to(message, chunk)
            else:
                bot.send_message(message.chat.id, chunk)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sort_timers:"))
def handle_sort_timers(call: types.CallbackQuery):
    """Обрабатывает нажатия кнопок сортировки в /mytimers."""
    sort_mode = call.data.split(":")[1]  # "time" или "id"
    user_id   = call.from_user.id
    _sort_state[user_id] = sort_mode

    text, markup = _build_timers_message(user_id, sort_mode)

    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except Exception:
        pass  # "message is not modified" — нормально, если список не изменился

    bot.answer_callback_query(call.id)


# =============================================================================
# ПАРСИНГ И ОБРАБОТКА КОМАНД ТАЙМЕРА
# =============================================================================

def _send_timer_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Не удалось распознать команду таймера.\n\n"
        "Формат: <code>/т [время] [описание]</code>\n"
        "Время: буквы д/ч/м/с или d/h/m/s, например:\n"
        "<code>/т 1д5ч30с проверить код</code>\n"
        "<code>/т 10с</code> (описание не обязательно)\n"
        f"Минимальное время — {MIN_TIMER_DURATION} сек, "
        f"максимум описания — {MAX_DESCRIPTION_LENGTH} символов.",
    )


def _send_recurring_timer_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Не удалось распознать команду.\n\n"
        "Формат: <code>/тр [интервал] [описание]</code>\n"
        "Пример: <code>/тр 1д проверить почту</code>\n\n"
        "Повторяющийся таймер срабатывает снова и снова с заданным "
        "интервалом, пока не удалишь его через /del.",
    )


def _validate_timer_args(
    message: types.Message,
    time_part: str,
    description: str,
) -> int | None:
    """
    Проверяет время и описание. Возвращает duration_seconds или None
    (и сам отвечает на message с причиной ошибки).
    """
    duration_seconds = parse_duration(time_part)
    if duration_seconds is None:
        return None

    if duration_seconds < MIN_TIMER_DURATION:
        bot.reply_to(
            message,
            f"⚠️ Минимальная длительность — {MIN_TIMER_DURATION} секунд.",
        )
        return None

    if duration_seconds > MAX_TIMER_DURATION:
        bot.reply_to(message, "⚠️ Максимальная длительность — 1 год.")
        return None

    if len(description) > MAX_DESCRIPTION_LENGTH:
        bot.reply_to(
            message,
            f"⚠️ Описание слишком длинное ({len(description)} симв.). "
            f"Максимум — {MAX_DESCRIPTION_LENGTH} символов. Сократите и попробуйте снова.",
        )
        return None

    with _timers_lock:
        count = len(USER_TIMERS.get(message.from_user.id, set()))
    if count >= MAX_TIMERS_PER_USER:
        bot.reply_to(
            message,
            f"⚠️ У вас уже {count} активных таймеров (максимум {MAX_TIMERS_PER_USER}). "
            "Удалите старые через /mytimers.",
        )
        return None

    return duration_seconds


def _process_timer_request(message: types.Message, args_text: str):
    """Разбирает аргументы и создаёт обычный таймер."""
    args_text = args_text.strip()
    if not args_text:
        _send_timer_usage_hint(message)
        return

    parts       = args_text.split(maxsplit=1)
    time_part   = parts[0]
    description = parts[1].strip() if len(parts) > 1 else ""

    duration = _validate_timer_args(message, time_part, description)
    if duration is None:
        if parse_duration(time_part) is None:
            _send_timer_usage_hint(message)
        return

    create_timer(message, duration, description, is_recurring=False)


def _process_recurring_timer_request(message: types.Message, args_text: str):
    """Разбирает аргументы и создаёт повторяющийся таймер."""
    args_text = args_text.strip()
    if not args_text:
        _send_recurring_timer_usage_hint(message)
        return

    parts       = args_text.split(maxsplit=1)
    time_part   = parts[0]
    description = parts[1].strip() if len(parts) > 1 else ""

    duration = _validate_timer_args(message, time_part, description)
    if duration is None:
        if parse_duration(time_part) is None:
            _send_recurring_timer_usage_hint(message)
        return

    create_timer(message, duration, description, is_recurring=True)


def _send_cancel_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Укажите ID таймера для удаления.\n"
        "Формат: <code>/del [ID]</code> или <code>удалить [ID]</code>\n"
        "Посмотреть ID — /mytimers.",
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

    bot.reply_to(message, cancel_timer(int(timer_id_str), message.from_user.id))


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
        message.forward_origin      is not None
        or message.forward_from     is not None
        or message.forward_from_chat is not None
        or message.forward_sender_name is not None
    )

    if is_forward:
        database.record_message_stats(
            user_id=user.id, username=user.username,
            first_name=user.first_name, last_name=user.last_name,
            chat_id=chat.id, chat_type=chat.type, chat_title=chat_title,
            messages=0, chars=0, stickers=0, photos=0,
            videos=0, voice=0, gifs=0, forwards=1,
        )
        return

    content_type = message.content_type
    is_sticker   = content_type == "sticker"
    messages     = 0 if is_sticker else 1
    chars = stickers = photos = videos = voice = gifs = 0

    if content_type == "text":
        chars = len(message.text or "")
    elif content_type == "sticker":
        stickers = 1
    elif content_type == "photo":
        photos = 1
        chars  = len(message.caption or "")
    elif content_type == "video":
        videos = 1
        chars  = len(message.caption or "")
    elif content_type in ("voice", "video_note"):
        voice = 1
    elif content_type == "animation":
        gifs  = 1
        chars = len(message.caption or "")
    elif message.caption:
        chars = len(message.caption)

    database.record_message_stats(
        user_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
        chat_id=chat.id, chat_type=chat.type, chat_title=chat_title,
        messages=messages, chars=chars, stickers=stickers, photos=photos,
        videos=videos, voice=voice, gifs=gifs, forwards=0,
    )


# =============================================================================
# MIDDLEWARE: УЧЁТ СТАТИСТИКИ
# =============================================================================

@bot.middleware_handler(update_types=["message"])
def stats_middleware(bot_instance, message):
    """
    Срабатывает на каждое сообщение во всех чатах.
    Запись в БД — в отдельном потоке, чтобы не задерживать ответ бота.
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

_BOT_USERNAME = ""  # заполняется при старте через bot.get_me()

HELP_TEXT = (
    "<b>🤖 Команды бота</b>\n\n"

    "<b>⏱ Обычный таймер</b>\n"
    "<code>/t [время] [описание]</code>  или  <code>/т [время] [описание]</code>\n"
    "Время: <code>д/ч/м/с</code> или <code>d/h/m/s</code>, можно комбинировать.\n"
    f"Мин. {MIN_TIMER_DURATION} сек · Макс. описание {MAX_DESCRIPTION_LENGTH} симв.\n"
    "Примеры:\n"
    "  <code>/т 1д5ч30с купить продукты</code>\n"
    "  <code>/t 2h30m buy groceries</code>\n\n"

    "<b>🔁 Повторяющийся таймер</b>\n"
    "<code>/tr [интервал] [описание]</code>  или  <code>/тр [интервал] [описание]</code>\n"
    "Срабатывает снова и снова с заданным интервалом до тех пор, "
    "пока не удалишь через /del.\n"
    "Пример:\n"
    "  <code>/тр 1д проверить почту</code>\n\n"

    "<b>📑 Мои таймеры</b>\n"
    "<code>/mytimers</code>\n"
    "Список с остатком времени. Кнопки внизу — сортировка по времени или по ID.\n\n"

    "<b>🗑 Удалить таймер</b>\n"
    "<code>/del [ID]</code>\n"
    "Синонимы: <code>/cancel</code>, <code>удалить</code>, <code>отмена</code>\n"
    "Пример: <code>/del 3</code>\n\n"

    "🏓 <code>/ping</code> — отклик + аптайм\n\n"

    "🆔 <code>/id</code>\n"
    "В чате — ID чата. Реплаем на сообщение — ID пользователя/бота. "
    "В личке — ваш ID.\n\n"
)


def _is_for_me(message: types.Message) -> bool:
    """Команда адресована нашему боту (или без @mention в группе)."""
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
        "Список команд — /help",
    )


@bot.message_handler(commands=["help"])
def handle_help(message: types.Message):
    if not _is_for_me(message):
        return
    bot.reply_to(message, HELP_TEXT)


@bot.message_handler(commands=["id"])
def handle_id(message: types.Message):
    """
    Без реплая в группе  — ID чата.
    Без реплая в личке   — Telegram ID пользователя.
    С реплаем            — ID того, на кого ответили.
    """
    if message.reply_to_message:
        target    = message.reply_to_message.from_user
        name      = html.escape(target.first_name or target.username or str(target.id))
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
    """Round-trip до Telegram API + аптайм."""
    start = time.perf_counter()
    sent  = bot.send_message(message.chat.id, "🏓 Pong!")
    ms    = (time.perf_counter() - start) * 1000
    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=(
            f"🏓 Pong!\n"
            f"Ping: <code>{ms:.3f}</code> ms\n"
            f"Uptime: {get_uptime_str()}"
        ),
    )


@bot.message_handler(commands=["t", "т"])
def handle_timer_slash(message: types.Message):
    parts = message.text.split(maxsplit=1)
    _process_timer_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(commands=["tr", "тр"])
def handle_recurring_timer_slash(message: types.Message):
    parts = message.text.split(maxsplit=1)
    _process_recurring_timer_request(message, parts[1] if len(parts) > 1 else "")


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
    _process_cancel_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(
    func=lambda m: bool(re.match(r"^(удалить|отмена)\b", (m.text or ""), re.IGNORECASE))
)
def handle_cancel_text(message: types.Message):
    parts = message.text.split(maxsplit=1)
    _process_cancel_request(message, parts[1] if len(parts) > 1 else "")


# =============================================================================
# ВЕБ-СЕРВЕР (WEBHOOK + HEALTHCHECK)
# =============================================================================

web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    """UptimeRobot пингует сюда каждые 5 мин, чтобы Render не засыпал."""
    return "Bot is running!"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    """Telegram присылает сюда обновления (сообщения, callback_query и т.д.)."""
    if flask_request.headers.get("content-type") == "application/json":
        update = types.Update.de_json(flask_request.get_data(as_text=True))
        bot.process_new_updates([update])
        return "ok", 200
    return "bad request", 400


# =============================================================================
# ТОЧКА ВХОДА / ЗАПУСК БОТА
# =============================================================================

def main():
    global _BOT_USERNAME

    logger.info("Бот запускается...")

    try:
        me = bot.get_me()
        _BOT_USERNAME = me.username or ""
        logger.info("Бот: @%s (id=%s)", _BOT_USERNAME, me.id)
    except Exception:
        logger.exception("Не удалось получить информацию о боте.")

    database.init_db()
    restore_timers()
    _start_timer_poller()

    admin.register(_BOT_USERNAME)

    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if not webhook_url:
        logger.warning("WEBHOOK_URL не задан — запускаю polling (только локально).")
        while True:
            try:
                logger.info("Запуск polling...")
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception:
                logger.exception("Polling упал, перезапуск через 5 сек...")
                time.sleep(5)
        return

    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(
        url=f"{webhook_url}/webhook",
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "channel_post", "callback_query"],
    )
    logger.info("Webhook: %s/webhook", webhook_url)

    port = int(os.environ.get("PORT", 10000))
    logger.info("Flask на порту %s...", port)
    web_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
