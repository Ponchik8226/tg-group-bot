"""
Telegram-бот для личного использования в группе (pyTelegramBotAPI).

Команды пользователей:
/ping, /id, /start, /help
/t, /т          — обычный таймер
/tr, /тр        — повторяющийся таймер (с лимитом срабатываний)
/mytimers       — список таймеров с сортировкой и обновлением
/del, /cancel   — удалить таймер
/к              — добавить reply-кнопку (видна только тебе)
/ук             — удалить кнопку
/кнопки         — список кнопок; /кнопки вкл/выкл — включить/выключить

Архитектура:
config.py   — объект bot, переменные окружения, логирование
database.py — вся работа с БД (таймеры, кнопки, статистика)
utils.py    — мелкие хелперы
admin.py    — все команды для администраторов
main.py     — этот файл: пользовательские хендлеры, middleware, Flask, запуск
"""

import atexit
import html
import os
import queue as _queue_module
import re
import sys
import threading
import time

from flask import Flask, request as flask_request
from telebot import types
from telebot.apihelper import ApiTelegramException

import admin
import database
from config import bot, logger, ADMIN_IDS, WEBHOOK_SECRET
from utils import (
    parse_duration,
    format_duration,
    build_mention,
    get_uptime_str,
    split_message,
)

# =============================================================================
# КОНСТАНТЫ
# =============================================================================

MAX_TIMERS_PER_USER    = 100              # максимум активных таймеров на юзера
MAX_TIMER_DURATION     = 365 * 24 * 3600  # максимальная длительность — 1 год
MIN_TIMER_DURATION     = 10               # минимальная длительность — 10 сек
MAX_DESCRIPTION_LENGTH = 200              # максимум символов в описании таймера

MAX_USER_BUTTONS       = 20              # максимум reply-кнопок на юзера
MAX_BUTTON_NAME_LENGTH = 50              # максимум символов в названии кнопки

# Лимит для повторяющихся таймеров:
# fires = min(365, 1_год // interval) → суммарный период ≤ 1 год
_ONE_YEAR = 365 * 24 * 3600


def _calc_max_fires(interval_seconds: int) -> int:
    """Максимум срабатываний = 1 год суммарного периода, но не больше 365."""
    return max(1, min(365, _ONE_YEAR // interval_seconds))


def _check_callback_owner(call: types.CallbackQuery) -> bool:
    """
    Проверяет, что нажавший inline-кнопку — автор оригинальной команды.
    Бот всегда отвечает reply_to, поэтому call.message.reply_to_message
    указывает на сообщение вызвавшего команду.
    Если reply_to_message отсутствует (ЛС, редкие кейсы) — разрешаем.
    """
    original = getattr(call.message, "reply_to_message", None)
    if original is None:
        return True
    return call.from_user.id == original.from_user.id


# =============================================================================
# ХРАНИЛИЩЕ АКТИВНЫХ ТАЙМЕРОВ (В ПАМЯТИ)
# =============================================================================

# TIMERS: timer_id -> {chat_id, thread_id, user_id, user_mention, description,
#                      end_time, duration, timer_obj, is_recurring,
#                      interval_seconds, fires_remaining}
TIMERS: dict = {}

# USER_TIMERS: user_id -> set of timer_id
USER_TIMERS: dict = {}

_next_timer_id = 1
_timers_lock   = threading.Lock()

# Текущий режим сортировки /mytimers: user_id -> "id" | "time"


class _BoundedDict(dict):
    """Dict с ограничением размера — при переполнении удаляет самые старые записи."""

    __slots__ = ("_maxsize",)

    def __init__(self, maxsize: int = 500):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self._maxsize:
            oldest = next(iter(self))
            del self[oldest]
        super().__setitem__(key, value)


_sort_state: _BoundedDict = _BoundedDict(500)

# =============================================================================
# ЛОГИКА ТАЙМЕРОВ
# =============================================================================

def fire_timer(timer_id: int, missed: bool = False):
    """
    Срабатывает по таймеру.

    Идемпотентен — если таймер уже удалён, просто выходит.
    Порядок: отправить сообщение → обновить/удалить в БД.
    Повторяющийся: после отправки декрементирует счётчик.
      - Если остались ещё срабатывания → обновляет end_time и рестартует.
      - Если лимит исчерпан → удаляет из БД и уведомляет.
    """
    with _timers_lock:
        info = TIMERS.get(timer_id)
        if info is None:
            logger.info("Таймер #%s: уже был отменён, пропускаю.", timer_id)
            return
        is_recurring = info.get("is_recurring", False)
        # Для одноразовых таймеров — удаляем из TIMERS сразу.
        # Для повторяющихся — оставляем, чтобы /del мог их найти.
        if not is_recurring:
            TIMERS.pop(timer_id, None)
            USER_TIMERS.get(info["user_id"], set()).discard(timer_id)
        # Копируем info чтобы работать без лока
        info = dict(info)

    interval = info.get("interval_seconds", 0)

    logger.info(
        "Таймер #%s сработал (chat_id=%s, user_id=%s, missed=%s, recurring=%s).",
        timer_id, info["chat_id"], info["user_id"], missed, is_recurring,
    )

    # ---------- Текст уведомления ----------
    if is_recurring:
        text = f"🔁 {info['user_mention']}, повторяющийся таймер!"
    else:
        text = f"⏰ {info['user_mention']}, время вышло!"

    if info["description"]:
        text += f"\n📝 {html.escape(info['description'])}"
    if missed:
        text += "\n\n⚠️ Бот был выключен, когда таймер должен был сработать."

    # ---------- Отправка — 3 попытки ----------
    thread_id = info.get("thread_id")  # None для обычных чатов, int для топиков
    send_ok = False
    for attempt in range(1, 4):
        try:
            bot.send_message(
                info["chat_id"], text,
                message_thread_id=thread_id,
            )
            send_ok = True
            break
        except Exception as e:
            err_str = str(e).lower()
            # Бот кикнут или чат удалён — дальнейшие попытки бессмысленны
            if any(kw in err_str for kw in (
                "bot was kicked", "chat not found", "not a member",
                "bot is not a member", "user is deactivated",
            )):
                logger.warning(
                    "Таймер #%s: бот недоступен в чате %s (%s) — удаляю таймер.",
                    timer_id, info["chat_id"], e,
                )
                with _timers_lock:
                    TIMERS.pop(timer_id, None)
                    USER_TIMERS.get(info["user_id"], set()).discard(timer_id)
                database.delete_timer(timer_id)
                return
            if attempt < 3:
                logger.warning("Попытка %d/3 отправить таймер #%s не удалась...", attempt, timer_id)
                time.sleep(2)
            else:
                logger.exception("Не удалось отправить таймер #%s после 3 попыток.", timer_id)

    # ---------- Рестарт или завершение ----------
    if is_recurring and interval:
        now = time.time()
        old_end_time = info["end_time"]

        # Пропускаем все пропущенные интервалы чтобы не было лавины сообщений
        if old_end_time + interval < now:
            skipped = int((now - old_end_time) // interval)
        else:
            skipped = 0

        # Декрементируем fires_remaining с учётом пропущенных интервалов
        new_remaining = database.decrement_timer_fires(timer_id)
        # Если БД недоступна — считаем в памяти
        if new_remaining is None:
            new_remaining = max(0, info.get("fires_remaining", 0) - 1)
        # Вычитаем пропущенные интервалы из оставшихся
        new_remaining = max(0, new_remaining - skipped)

        if new_remaining > 0:
            # Продолжаем цикл. Якоримся на предыдущий end_time — не дрейфуем.
            new_end_time = old_end_time + interval * (skipped + 1)
            delay = max(1, new_end_time - now)
            database.update_timer_end_time(timer_id, new_end_time)

            new_timer_obj = threading.Timer(delay, fire_timer, args=(timer_id,))
            new_timer_obj.daemon = True

            with _timers_lock:
                # Проверяем, не был ли таймер отменён пока мы отправляли сообщение
                if timer_id not in TIMERS:
                    new_timer_obj.cancel()
                    logger.info("Таймер #%s был отменён во время срабатывания.", timer_id)
                    return
                TIMERS[timer_id] = {
                    **info,
                    "end_time":        new_end_time,
                    "duration":        interval,
                    "timer_obj":       new_timer_obj,
                    "fires_remaining": new_remaining,
                }
                USER_TIMERS.setdefault(info["user_id"], set()).add(timer_id)

            new_timer_obj.start()
            if skipped:
                logger.info(
                    "Повторяющийся таймер #%s: пропущено %s интервалов, "
                    "перезапланирован, осталось %s раз.",
                    timer_id, skipped, new_remaining,
                )
            else:
                logger.info(
                    "Повторяющийся таймер #%s перезапланирован, осталось %s раз.",
                    timer_id, new_remaining,
                )
        else:
            # Лимит исчерпан — удаляем
            with _timers_lock:
                TIMERS.pop(timer_id, None)
                USER_TIMERS.get(info["user_id"], set()).discard(timer_id)
            database.delete_timer(timer_id)
            logger.info("Повторяющийся таймер #%s завершён: лимит срабатываний исчерпан.", timer_id)
            try:
                bot.send_message(
                    info["chat_id"],
                    f"🔁 {info['user_mention']}, повторяющийся таймер #{timer_id} завершён — "
                    f"лимит срабатываний исчерпан.",
                    message_thread_id=thread_id,
                )
            except Exception:
                logger.exception("Не удалось отправить финальное сообщение таймера #%s.", timer_id)
    else:
        database.delete_timer(timer_id)


def _check_due_timers():
    """Поллер: проверяет TIMERS и срабатывает просроченные."""
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
    Запускает поллер (каждые 30 сек) как страховочную сеть.
    Основная точность — threading.Timer при создании/восстановлении.
    Оба пути безопасны: fire_timer идемпотентен.
    """
    def _loop():
        logger.info("Поллер таймеров запущен (каждые 30 сек).")
        while True:
            time.sleep(30)
            try:
                _check_due_timers()
            except Exception:
                logger.exception("Ошибка в поллере таймеров.")

    threading.Thread(target=_loop, daemon=True, name="timer-poller").start()


def create_timer(
    message: types.Message,
    duration_seconds: int,
    description: str,
    is_recurring: bool = False,
):
    """Создаёт таймер в БД и памяти."""
    global _next_timer_id

    user          = message.from_user
    first_name    = user.first_name or "Пользователь"
    mention       = build_mention(user.id, first_name)
    end_time      = time.time() + duration_seconds
    interval_secs = duration_seconds if is_recurring else 0
    fires_rem     = _calc_max_fires(duration_seconds) if is_recurring else 0
    # message_thread_id — ID топика в Forum-чатах, None для обычных чатов
    thread_id     = getattr(message, "message_thread_id", None)

    timer_id = None
    if database.db_enabled():
        timer_id = database.insert_timer(
            message.chat.id, user.id, first_name, description, end_time,
            is_recurring=is_recurring,
            interval_seconds=interval_secs,
            fires_remaining=fires_rem,
            thread_id=thread_id,
        )

    # Если БД недоступна или пул умер (insert вернул None) — локальный счётчик
    if timer_id is None:
        with _timers_lock:
            timer_id = _next_timer_id
            _next_timer_id += 1

    # Создаём Timer ДО захвата лока, стартуем ПОСЛЕ — чтобы не сработал
    # раньше, чем запись появится в TIMERS
    timer_obj = threading.Timer(duration_seconds, fire_timer, args=(timer_id,))
    timer_obj.daemon = True

    with _timers_lock:
        TIMERS[timer_id] = {
            "chat_id":          message.chat.id,
            "thread_id":        thread_id,
            "user_id":          user.id,
            "user_mention":     mention,
            "description":      description,
            "end_time":         end_time,
            "duration":         duration_seconds,
            "timer_obj":        timer_obj,
            "is_recurring":     is_recurring,
            "interval_seconds": interval_secs,
            "fires_remaining":  fires_rem,
        }
        USER_TIMERS.setdefault(user.id, set()).add(timer_id)

    timer_obj.start()

    logger.info(
        "Создан %s таймер #%s, %s сек (user=%s, chat=%s).",
        "повторяющийся" if is_recurring else "обычный",
        timer_id, duration_seconds, user.id, message.chat.id,
    )

    desc_part = f"\n📝 {html.escape(description)}" if description else ""
    if is_recurring:
        bot.reply_to(
            message,
            f"✅ Повторяющийся таймер #{timer_id} установлен.\n"
            f"↺ Будет срабатывать каждые {format_duration(duration_seconds)}.\n"
            f"📊 Максимум срабатываний: {fires_rem}"
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
            return f"❌ Таймер #{timer_id} не найден (сработал или уже удалён)."
        if info["user_id"] != user_id:
            return f"❌ Таймер #{timer_id} принадлежит другому пользователю."
        del TIMERS[timer_id]
        USER_TIMERS.get(user_id, set()).discard(timer_id)

    t = info.get("timer_obj")
    if t:
        t.cancel()
    database.delete_timer(timer_id)
    logger.info("Таймер #%s отменён пользователем %s.", timer_id, user_id)
    return f"🗑 Таймер #{timer_id} успешно удалён."


def restore_timers():
    """При старте восстанавливает таймеры из БД."""
    global _next_timer_id
    if not database.db_enabled():
        return

    rows = database.load_all_timers()
    if not rows:
        return

    # Инициализируем счётчик ID чтобы не было коллизий с ID из БД
    max_db_id = max(r[0] for r in rows)
    with _timers_lock:
        if _next_timer_id <= max_db_id:
            _next_timer_id = max_db_id + 1

    now = time.time()
    restored = missed = 0

    for (timer_id, chat_id, user_id, first_name, description,
         end_time, is_recurring, interval_seconds, fires_remaining,
         thread_id) in rows:

        mention   = build_mention(user_id, first_name)
        remaining = end_time - now

        timer_obj = None
        if remaining > 0:
            timer_obj = threading.Timer(remaining, fire_timer, args=(timer_id,))
            timer_obj.daemon = True

        with _timers_lock:
            TIMERS[timer_id] = {
                "chat_id":          chat_id,
                "thread_id":        thread_id,
                "user_id":          user_id,
                "user_mention":     mention,
                "description":      description,
                "end_time":         end_time,
                "duration":         max(0, int(remaining)),
                "timer_obj":        timer_obj,
                "is_recurring":     bool(is_recurring),
                "interval_seconds": int(interval_seconds),
                "fires_remaining":  int(fires_remaining),
            }
            USER_TIMERS.setdefault(user_id, set()).add(timer_id)

        if timer_obj:
            timer_obj.start()
            restored += 1
        else:
            missed += 1

    logger.info(
        "Восстановлено таймеров: %s активных, %s просроченных (сработают через ~30 сек).",
        restored, missed,
    )


# =============================================================================
# ОТОБРАЖЕНИЕ /mytimers
# =============================================================================

def _build_timers_message(user_id: int, sort_mode: str) -> tuple:
    """
    Строит текст + inline-клавиатуру для /mytimers.
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
                "fires_remaining":  TIMERS[tid].get("fires_remaining", 0),
            }
            for tid in USER_TIMERS.get(user_id, set())
            if tid in TIMERS
        ]

    if not snapshot:
        return "У вас нет активных таймеров.", None

    if sort_mode == "time":
        snapshot.sort(key=lambda t: t["end_time"])
    else:
        snapshot.sort(key=lambda t: t["id"])

    count = len(snapshot)
    lines = [f"<b>📑 Ваши активные таймеры ({count})</b>", ""]

    for info in snapshot:
        remaining = max(int(info["end_time"] - now), 0)
        icon      = "🔁" if info["is_recurring"] else "•"

        header = f"{icon} <b>#{info['id']}</b> · {format_duration(remaining)}"
        if info["is_recurring"] and info["interval_seconds"]:
            header += (
                f"  <i>↺ каждые {format_duration(info['interval_seconds'])}"
                f" · осталось {info['fires_remaining']} раз</i>"
            )
        lines.append(header)

        if info["description"]:
            lines.append(html.escape(info["description"]))
        lines.append("")

    lines.append("/del [ID] — удалить таймер")

    # Кнопки сортировки + обновление
    if sort_mode == "time":
        time_btn = types.InlineKeyboardButton("✅ По времени", callback_data="sort_timers:time")
        id_btn   = types.InlineKeyboardButton("По номеру",    callback_data="sort_timers:id")
    else:
        time_btn = types.InlineKeyboardButton("По времени",   callback_data="sort_timers:time")
        id_btn   = types.InlineKeyboardButton("✅ По номеру", callback_data="sort_timers:id")
    refresh_btn = types.InlineKeyboardButton("🔄 Обновить",  callback_data="sort_timers:refresh")

    markup = types.InlineKeyboardMarkup()
    markup.row(time_btn, id_btn)
    markup.row(refresh_btn)
    return "\n".join(lines), markup


def _show_my_timers(message: types.Message):
    user_id   = message.from_user.id
    sort_mode = _sort_state.get(user_id, "id")
    text, markup = _build_timers_message(user_id, sort_mode)

    if markup is None:
        bot.reply_to(message, text)
        return

    if len(text) <= 4000:
        bot.reply_to(message, text, reply_markup=markup)
    else:
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            if i == 0:
                bot.reply_to(message, chunk)
            else:
                bot.send_message(message.chat.id, chunk)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("sort_timers:"))
def handle_sort_timers(call: types.CallbackQuery):
    """Сортировка и обновление /mytimers через inline-кнопки."""
    if not _check_callback_owner(call):
        bot.answer_callback_query(call.id, "⛔ Это не твой список таймеров.", show_alert=False)
        return

    action  = call.data.split(":")[1]
    user_id = call.from_user.id

    if action == "refresh":
        sort_mode = _sort_state.get(user_id, "id")
    else:
        sort_mode = action
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
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            logger.warning("Ошибка edit_message (sort_timers): %s", e)

    bot.answer_callback_query(call.id)


# =============================================================================
# ПАРСИНГ КОМАНД ТАЙМЕРА
# =============================================================================

def _send_timer_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Не удалось распознать команду.\n\n"
        "Формат: <code>/т [время] [описание]</code>\n"
        "Время: д/ч/м/с или d/h/m/s, например:\n"
        "  <code>/т 1д5ч30с купить продукты</code>\n"
        "  <code>/т 10с</code>\n"
        f"Минимум: {MIN_TIMER_DURATION} сек · Описание: до {MAX_DESCRIPTION_LENGTH} символов.",
    )


def _send_recurring_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Не удалось распознать команду.\n\n"
        "Формат: <code>/тр [интервал] [описание]</code>\n"
        "Пример: <code>/тр 1д проверить почту</code>\n\n"
        "Таймер срабатывает снова и снова пока не удалишь через /del.\n"
        "Суммарный период — не более 1 года.",
    )


def _validate_timer_args(
    message: types.Message,
    time_part: str,
    description: str,
) -> int | None:
    """
    Проверяет время и описание. Возвращает duration_seconds или None
    (сам отправляет сообщение с причиной ошибки).
    """
    duration = parse_duration(time_part)
    if duration is None:
        return None

    if duration < MIN_TIMER_DURATION:
        bot.reply_to(message, f"⚠️ Минимальная длительность — {MIN_TIMER_DURATION} секунд.")
        return None

    if duration > MAX_TIMER_DURATION:
        bot.reply_to(message, "⚠️ Максимальная длительность — 1 год.")
        return None

    if len(description) > MAX_DESCRIPTION_LENGTH:
        bot.reply_to(
            message,
            f"⚠️ Описание слишком длинное ({len(description)} симв.). "
            f"Максимум — {MAX_DESCRIPTION_LENGTH} символов.",
        )
        return None

    with _timers_lock:
        count = len(USER_TIMERS.get(message.from_user.id, set()))
    if count >= MAX_TIMERS_PER_USER:
        bot.reply_to(
            message,
            f"⚠️ У вас уже {count} таймеров (максимум {MAX_TIMERS_PER_USER}). "
            "Удалите лишние через /mytimers.",
        )
        return None

    return duration


def _process_timer_request(message: types.Message, args_text: str):
    args_text = args_text.strip()
    if not args_text:
        _send_timer_usage_hint(message)
        return
    parts       = args_text.split(maxsplit=1)
    description = parts[1].strip() if len(parts) > 1 else ""
    duration    = _validate_timer_args(message, parts[0], description)
    if duration is None:
        if parse_duration(parts[0]) is None:
            _send_timer_usage_hint(message)
        return
    create_timer(message, duration, description, is_recurring=False)


def _process_recurring_request(message: types.Message, args_text: str):
    args_text = args_text.strip()
    if not args_text:
        _send_recurring_usage_hint(message)
        return
    parts       = args_text.split(maxsplit=1)
    description = parts[1].strip() if len(parts) > 1 else ""
    duration    = _validate_timer_args(message, parts[0], description)
    if duration is None:
        if parse_duration(parts[0]) is None:
            _send_recurring_usage_hint(message)
        return
    create_timer(message, duration, description, is_recurring=True)


def _send_cancel_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Укажите ID таймера.\n"
        "Формат: <code>/del [ID]</code>\n"
        "Список ID — /mytimers.",
    )


def _process_cancel_request(message: types.Message, args_text: str):
    args_text = args_text.strip()
    if not args_text:
        _send_cancel_usage_hint(message)
        return
    id_str = args_text.split()[0].lstrip("#")
    if not id_str.isdigit():
        _send_cancel_usage_hint(message)
        return
    bot.reply_to(message, cancel_timer(int(id_str), message.from_user.id))


# =============================================================================
# ПОЛЬЗОВАТЕЛЬСКИЕ REPLY-КНОПКИ (/к, /ук, /кнопки)
# =============================================================================

def _build_user_keyboard(buttons: list):
    """
    Строит ReplyKeyboardMarkup из активных кнопок (3 в ряд, selective=True).
    buttons: [(id, name, is_active), ...]
    Если активных кнопок нет — возвращает ReplyKeyboardRemove.
    """
    active = [name for _, name, is_active in buttons if is_active]
    if not active:
        return types.ReplyKeyboardRemove(selective=True)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, selective=True)
    row = []
    for name in active:
        row.append(types.KeyboardButton(name))
        if len(row) == 3:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    return markup


def _parse_button_names(raw: str) -> list[str]:
    """
    Парсит строку с названиями кнопок.
    Приоритет: разделитель ";" → перенос строки → вся строка целиком.
    Возвращает список непустых уникальных названий (порядок сохраняется).
    """
    if ";" in raw:
        parts = raw.split(";")
    elif "\n" in raw:
        parts = raw.split("\n")
    else:
        parts = [raw]

    seen = set()
    result = []
    for p in parts:
        name = p.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


def _send_button_usage_hint(message: types.Message):
    bot.reply_to(
        message,
        "⚠️ Укажите название кнопки.\n\n"
        "<b>Одна кнопка:</b>\n"
        "<code>/к Проверить почту</code>\n\n"
        "<b>Несколько кнопок через «;»:</b>\n"
        "<code>/к Почта; Задачи; Позвонить маме</code>\n\n"
        "<b>Несколько кнопок с новой строки:</b>\n"
        "<code>/к Почта\nЗадачи\nПозвонить маме</code>\n\n"
        f"Максимум кнопок: {MAX_USER_BUTTONS} · Название: до {MAX_BUTTON_NAME_LENGTH} символов.\n"
        "Список кнопок — /кнопки",
    )


def _process_add_button(message: types.Message, raw: str):
    raw     = raw.strip()
    user_id = message.from_user.id

    if not raw:
        _send_button_usage_hint(message)
        return

    names = _parse_button_names(raw)

    if not names:
        _send_button_usage_hint(message)
        return

    # Валидация длины
    too_long = [n for n in names if len(n) > MAX_BUTTON_NAME_LENGTH]
    if too_long:
        joined = ", ".join(f"«{html.escape(n)}»" for n in too_long)
        bot.reply_to(
            message,
            f"⚠️ Слишком длинные названия ({MAX_BUTTON_NAME_LENGTH} симв. макс.): {joined}",
        )
        return

    buttons = database.get_user_buttons(user_id)
    existing_names = {n.lower() for _, n, _ in buttons}
    free_slots = MAX_USER_BUTTONS - len(buttons)

    # Дубликаты (уже есть в базе)
    duplicates = [n for n in names if n.lower() in existing_names]
    # Новые
    new_names = [n for n in names if n.lower() not in existing_names]

    if not new_names:
        dupes_str = ", ".join(f"«{html.escape(n)}»" for n in duplicates)
        bot.reply_to(message, f"⚠️ Все эти кнопки уже есть: {dupes_str}")
        return

    if len(new_names) > free_slots:
        bot.reply_to(
            message,
            f"⚠️ Можно добавить ещё {free_slots} кнопок (максимум {MAX_USER_BUTTONS}), "
            f"а вы пытаетесь добавить {len(new_names)}. Сократите список.",
        )
        return

    added = []
    failed = []
    for name in new_names:
        result = database.add_user_button(user_id, name)
        if result is not None:
            added.append(name)
        else:
            failed.append(name)

    buttons = database.get_user_buttons(user_id)

    lines = []
    if added:
        lines.append("✅ Добавлено: " + ", ".join(f"«{html.escape(n)}»" for n in added))
    if duplicates:
        lines.append("⚠️ Уже были: " + ", ".join(f"«{html.escape(n)}»" for n in duplicates))
    if failed:
        lines.append("❌ Ошибка: " + ", ".join(f"«{html.escape(n)}»" for n in failed))

    bot.reply_to(
        message,
        "\n".join(lines),
        reply_markup=_build_user_keyboard(buttons),
    )


def _process_remove_button(message: types.Message, raw: str):
    raw     = raw.strip()
    user_id = message.from_user.id

    if not raw:
        bot.reply_to(
            message,
            "⚠️ Укажите номер или название кнопки.\n\n"
            "<b>Одна кнопка:</b>\n"
            "<code>/ук 1</code>  или  <code>/ук Проверить почту</code>\n\n"
            "<b>Несколько через пробел или «;»:</b>\n"
            "<code>/ук 1 3 5</code>  или  <code>/ук Почта; Задачи</code>\n\n"
            "<b>Удалить все:</b>\n"
            "<code>/ук все</code>\n\n"
            "Список кнопок — /кнопки",
        )
        return

    buttons = database.get_user_buttons(user_id)

    if not buttons:
        bot.reply_to(message, "У вас нет кнопок.")
        return

    # Удалить все
    if raw.strip().lower() == "все":
        count = database.remove_all_user_buttons(user_id)
        bot.reply_to(
            message,
            f"🗑 Удалено всех кнопок: {count}.",
            reply_markup=types.ReplyKeyboardRemove(selective=True),
        )
        return

    # Парсим идентификаторы: сначала пробуем ";" как разделитель, иначе пробел
    if ";" in raw:
        tokens = [t.strip() for t in raw.split(";") if t.strip()]
    else:
        tokens = raw.split()

    to_delete_ids   = []
    to_delete_names = []
    not_found       = []

    for token in tokens:
        # По номеру (1-индексированный)
        if token.lstrip("#").isdigit():
            idx = int(token.lstrip("#")) - 1
            if 0 <= idx < len(buttons):
                bid, bname, _ = buttons[idx]
                if bid not in to_delete_ids:
                    to_delete_ids.append(bid)
                    to_delete_names.append(bname)
            else:
                not_found.append(token)
        else:
            # По названию (без учёта регистра)
            match = next(
                ((bid, bname) for bid, bname, _ in buttons if bname.lower() == token.lower()),
                None,
            )
            if match:
                bid, bname = match
                if bid not in to_delete_ids:
                    to_delete_ids.append(bid)
                    to_delete_names.append(bname)
            else:
                not_found.append(token)

    lines = []

    if to_delete_ids:
        database.remove_user_buttons(to_delete_ids, user_id)
        deleted_str = ", ".join(f"«{html.escape(n)}»" for n in to_delete_names)
        lines.append(f"🗑 Удалено: {deleted_str}")

    if not_found:
        nf_str = ", ".join(html.escape(t) for t in not_found)
        lines.append(f"⚠️ Не найдено: {nf_str}")

    remaining = database.get_user_buttons(user_id)
    if not remaining:
        lines.append("Все кнопки удалены.")

    bot.reply_to(
        message,
        "\n".join(lines),
        reply_markup=_build_user_keyboard(remaining),
    )


def _show_user_buttons(message: types.Message):
    """Показывает список кнопок с inline-кнопками Вкл/Выкл всех."""
    user_id = message.from_user.id
    buttons = database.get_user_buttons(user_id)

    if not buttons:
        bot.reply_to(
            message,
            "У вас нет кнопок.\n"
            "Добавьте: <code>/к [название]</code>",
        )
        return

    active_count = sum(1 for _, _, is_active in buttons if is_active)
    total        = len(buttons)

    lines = [f"<b>📌 Ваши кнопки ({active_count}/{total})</b>", ""]
    for i, (_, name, is_active) in enumerate(buttons, 1):
        status = "" if is_active else " <i>(выкл)</i>"
        lines.append(f"{i}. {html.escape(name)}{status}")
    lines.append("\nДля удаления: <code>/ук [номер или название]</code>")
    lines.append("Для добавления: <code>/к [название]</code>")

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Включить", callback_data="buttons_toggle:on"),
        types.InlineKeyboardButton("❌ Выключить", callback_data="buttons_toggle:off"),
    )

    bot.reply_to(
        message,
        "\n".join(lines),
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("buttons_toggle:"))
def handle_buttons_toggle(call: types.CallbackQuery):
    """Включает или выключает все кнопки пользователя."""
    if not _check_callback_owner(call):
        bot.answer_callback_query(call.id, "⛔ Это не твой список кнопок.", show_alert=False)
        return

    action    = call.data.split(":")[1]
    user_id   = call.from_user.id
    is_active = (action == "on")

    database.set_all_buttons_active(user_id, is_active)
    buttons = database.get_user_buttons(user_id)

    # Обновляем reply-клавиатуру.
    # reply_to_message_id нужен чтобы selective=True корректно
    # адресовал клавиатуру конкретному пользователю в группе.
    original     = getattr(call.message, "reply_to_message", None)
    reply_msg_id = original.message_id if original else None
    status_text  = "✅ Все кнопки включены." if is_active else "❌ Все кнопки выключены."
    try:
        bot.send_message(
            call.message.chat.id,
            status_text,
            reply_to_message_id=reply_msg_id,
            reply_markup=_build_user_keyboard(buttons),
        )
    except ApiTelegramException as e:
        logger.warning("Ошибка send_message (buttons_toggle): %s", e)

    # Обновляем текст и инлайн-кнопки в исходном сообщении-списке
    active_count = sum(1 for _, _, a in buttons if a)
    total = len(buttons)
    lines = [f"<b>📌 Ваши кнопки ({active_count}/{total})</b>", ""]
    for i, (_, name, btn_active) in enumerate(buttons, 1):
        status = "" if btn_active else " <i>(выкл)</i>"
        lines.append(f"{i}. {html.escape(name)}{status}")
    lines.append("\nДля удаления: <code>/ук [номер или название]</code>")
    lines.append("Для добавления: <code>/к [название]</code>")

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Включить", callback_data="buttons_toggle:on"),
        types.InlineKeyboardButton("❌ Выключить", callback_data="buttons_toggle:off"),
    )

    try:
        bot.edit_message_text(
            "\n".join(lines),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML",
        )
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            logger.warning("Ошибка edit_message (buttons_toggle): %s", e)

    bot.answer_callback_query(call.id)


# =============================================================================
# СТАТИСТИКА: УЧЁТ СООБЩЕНИЙ
# =============================================================================

def track_message_stats(message: types.Message):
    if not database.db_enabled():
        return

    user = message.from_user
    if user is None or user.is_bot:
        return

    chat = message.chat
    chat_title = (
        f"ЛС: {user.first_name or user.username or user.id}"
        if chat.type == "private"
        else (chat.title or str(chat.id))
    )

    is_forward = (
        message.forward_origin       is not None
        or message.forward_from      is not None
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

    ct       = message.content_type
    messages = 0 if ct == "sticker" else 1
    chars = stickers = photos = videos = voice = gifs = 0

    if ct == "text":           chars    = len(message.text or "")
    elif ct == "sticker":      stickers = 1
    elif ct == "photo":        photos   = 1; chars = len(message.caption or "")
    elif ct == "video":        videos   = 1; chars = len(message.caption or "")
    elif ct in ("voice","video_note"): voice = 1
    elif ct == "animation":    gifs     = 1; chars = len(message.caption or "")
    elif message.caption:      chars    = len(message.caption)

    database.record_message_stats(
        user_id=user.id, username=user.username,
        first_name=user.first_name, last_name=user.last_name,
        chat_id=chat.id, chat_type=chat.type, chat_title=chat_title,
        messages=messages, chars=chars, stickers=stickers, photos=photos,
        videos=videos, voice=voice, gifs=gifs, forwards=0,
    )


# Очередь и воркер: один фоновый поток вместо нового на каждое сообщение.
# maxsize=2000 — при переполнении просто пропускаем, не блокируем бот.
_stats_queue: _queue_module.Queue = _queue_module.Queue(maxsize=2000)


def _start_stats_worker():
    """Запускает единственный фоновый поток для записи статистики."""
    def _worker():
        logger.info("Воркер статистики запущен.")
        while True:
            msg = _stats_queue.get()
            batch = [msg]
            # Забираем все ожидающие сообщения из очереди (батчинг)
            try:
                while len(batch) < 100:
                    batch.append(_stats_queue.get_nowait())
            except _queue_module.Empty:
                pass

            for m in batch:
                try:
                    track_message_stats(m)
                except Exception:
                    logger.exception("Ошибка при записи статистики сообщения.")

            for _ in batch:
                _stats_queue.task_done()

    threading.Thread(target=_worker, daemon=True, name="stats-worker").start()


@bot.middleware_handler(update_types=["message"])
def stats_middleware(bot_instance, message):
    try:
        _stats_queue.put_nowait(message)
    except _queue_module.Full:
        logger.warning("Очередь статистики переполнена, сообщение пропущено.")


# =============================================================================
# ИНТЕРАКТИВНЫЙ /help
# =============================================================================

_HELP_MAIN = (
    "📖 <b>Справка</b>\n\n"
    "Выбери раздел:"
)

_HELP_TIMERS = (
    "⏰ <b>Таймеры</b>\n\n"

    "Поставь таймер — бот пришлёт напоминание когда время выйдет.\n\n"

    "〔 Одноразовый 〕\n"
    "<code>/т [время] [описание]</code>  ·  <code>/t ...</code>\n"
    "Время пишется буквами: <code>д ч м с</code> или <code>d h m s</code>\n\n"
    "  <code>/т 30м</code>  — через 30 минут\n"
    "  <code>/т 2ч купить молоко</code>  — с подписью\n"
    "  <code>/т 1д5ч30с</code>  — комбинация\n\n"

    "〔 Повторяющийся 〕\n"
    "<code>/тр [интервал] [описание]</code>  ·  <code>/tr ...</code>\n"
    "Срабатывает снова и снова с заданным интервалом.\n"
    "Автоматически завершается через 1 год.\n\n"
    "  <code>/тр 8ч пить воду</code>\n"
    "  <code>/тр 1д проверить почту</code>\n\n"

    "〔 Список 〕\n"
    "<code>/mytimers</code>  — все активные таймеры\n\n"

    "〔 Удалить 〕\n"
    "<code>/del [ID]</code>  ·  <code>/cancel</code>  ·  <code>удалить</code>  ·  <code>отмена</code>\n"
    "  <code>/del 3</code>  — удалить таймер #3"
)

_HELP_BUTTONS = (
    "📌 <b>Быстрые кнопки</b>\n\n"

    "Персональные кнопки в панели ввода. "
    "Видны только тебе — другие участники чата их не видят.\n\n"

    "〔 Добавить 〕\n"
    "<code>/к [название]</code>\n"
    "  <code>/к Проверить почту</code>\n"
    "  <code>/к Почта; Задачи; Позвонить</code>  — сразу несколько через «;»\n\n"

    "〔 Удалить 〕\n"
    "<code>/ук [номер или название]</code>\n"
    "  <code>/ук 2</code>  ·  <code>/ук Почта</code>  ·  <code>/ук 1 3 5</code>  ·  <code>/ук все</code>\n\n"

    "〔 Включить / выключить 〕\n"
    "<code>/кнопки вкл</code>  ·  <code>/кнопки выкл</code>\n"
    "Или нажать кнопки прямо в списке /кнопки\n\n"

    "〔 Список кнопок 〕\n"
    "<code>/кнопки</code>  ·  <code>/buttons</code>  ·  <code>Кнопки</code>\n\n"

    f"Максимум {MAX_USER_BUTTONS} кнопок · Название до {MAX_BUTTON_NAME_LENGTH} символов."
)

_HELP_MISC = (
    "💡 <b>Прочее</b>\n\n"

    "〔 /ping 〕\n"
    "Показывает время отклика до Telegram и сколько бот работает без перерыва.\n\n"

    "〔 /id 〕\n"
    "Без реплая — ID этого чата (или твой ID в личке).\n"
    "Реплаем на сообщение — ID того пользователя или бота."
)


def _help_markup_main() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup()
    m.row(
        types.InlineKeyboardButton("⏰ Таймеры", callback_data="help:timers"),
        types.InlineKeyboardButton("📌 Кнопки",  callback_data="help:buttons"),
        types.InlineKeyboardButton("💡 Прочее",  callback_data="help:misc"),
    )
    return m


def _help_markup_back() -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup()
    m.row(types.InlineKeyboardButton("← Назад", callback_data="help:main"))
    return m


_HELP_SECTIONS = {
    "main":    (_HELP_MAIN,    _help_markup_main),
    "timers":  (_HELP_TIMERS,  _help_markup_back),
    "buttons": (_HELP_BUTTONS, _help_markup_back),
    "misc":    (_HELP_MISC,    _help_markup_back),
}


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("help:"))
def handle_help_callback(call: types.CallbackQuery):
    if not _check_callback_owner(call):
        bot.answer_callback_query(call.id, "⛔ Это не твой /help.", show_alert=False)
        return

    section = call.data.split(":")[1]
    if section not in _HELP_SECTIONS:
        bot.answer_callback_query(call.id)
        return

    text, markup_fn = _HELP_SECTIONS[section]
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup_fn(),
            parse_mode="HTML",
        )
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            logger.warning("Ошибка edit_message (help): %s", e)
    bot.answer_callback_query(call.id)


# =============================================================================
# ОБРАБОТЧИКИ КОМАНД
# =============================================================================

_BOT_USERNAME = ""


def _is_for_me(message: types.Message) -> bool:
    """Проверяет, адресована ли команда этому боту (по @username)."""
    if not _BOT_USERNAME:
        return True  # fallback: не удалось получить username бота
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
    bot.reply_to(message, _HELP_MAIN, reply_markup=_help_markup_main())


@bot.message_handler(commands=["id"])
def handle_id(message: types.Message):
    if not _is_for_me(message):
        return
    if message.reply_to_message:
        t = message.reply_to_message.from_user
        if t is None:
            # Автофорвард из канала — нет пользователя
            bot.reply_to(message, "❌ У этого сообщения нет автора (автофорвард из канала).")
            return
        name      = html.escape(t.first_name or t.username or str(t.id))
        bot_label = " (бот)" if t.is_bot else ""
        bot.reply_to(message, f"🆔 ID <b>{name}</b>{bot_label}: <code>{t.id}</code>")
    elif message.chat.type == "private":
        bot.reply_to(message, f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>")
    else:
        bot.reply_to(message, f"🆔 ID этого чата: <code>{message.chat.id}</code>")


@bot.message_handler(commands=["ping"])
def handle_ping(message: types.Message):
    if not _is_for_me(message):
        return
    start = time.perf_counter()
    sent  = bot.send_message(message.chat.id, "🏓 Pong!")
    ms    = (time.perf_counter() - start) * 1000
    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=sent.message_id,
        text=f"🏓 Pong!\nPing: <code>{ms:.3f}</code> ms\nUptime: {get_uptime_str()}",
    )


@bot.message_handler(commands=["t", "т"])
def handle_timer(message: types.Message):
    if not _is_for_me(message):
        return
    parts = message.text.split(maxsplit=1)
    _process_timer_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(commands=["tr", "тр"])
def handle_recurring_timer(message: types.Message):
    if not _is_for_me(message):
        return
    parts = message.text.split(maxsplit=1)
    _process_recurring_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(commands=["mytimers"])
def handle_mytimers(message: types.Message):
    if not _is_for_me(message):
        return
    _show_my_timers(message)


@bot.message_handler(
    func=lambda m: bool(re.match(r"^таймеры\b", (m.text or ""), re.IGNORECASE))
)
def handle_mytimers_text(message: types.Message):
    if not _is_for_me(message):
        return
    _show_my_timers(message)


@bot.message_handler(commands=["del", "del_timer", "cancel"])
def handle_cancel(message: types.Message):
    if not _is_for_me(message):
        return
    parts = message.text.split(maxsplit=1)
    _process_cancel_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(
    func=lambda m: bool(re.match(r"^(удалить|отмена)\s+#?\d+\s*$", (m.text or ""), re.IGNORECASE))
)
def handle_cancel_text(message: types.Message):
    parts = message.text.split(maxsplit=1)
    _process_cancel_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(commands=["к"])
def handle_add_button(message: types.Message):
    if not _is_for_me(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1:
        _process_add_button(message, parts[1])
    else:
        _send_button_usage_hint(message)


@bot.message_handler(commands=["ук"])
def handle_remove_button(message: types.Message):
    if not _is_for_me(message):
        return
    parts = message.text.split(maxsplit=1)
    _process_remove_button(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(commands=["кнопки", "buttons"])
def handle_show_buttons(message: types.Message):
    if not _is_for_me(message):
        return
    parts = message.text.split(maxsplit=1)
    arg   = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("вкл", "on"):
        _toggle_all_buttons(message, True)
    elif arg in ("выкл", "off"):
        _toggle_all_buttons(message, False)
    else:
        _show_user_buttons(message)


@bot.message_handler(
    func=lambda m: bool(re.match(r"^кнопки\b", (m.text or ""), re.IGNORECASE))
)
def handle_show_buttons_text(message: types.Message):
    """Синоним "Кнопки" без слэша — поддерживает вкл/выкл."""
    if not _is_for_me(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    arg   = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("вкл", "on"):
        _toggle_all_buttons(message, True)
    elif arg in ("выкл", "off"):
        _toggle_all_buttons(message, False)
    else:
        _show_user_buttons(message)


def _toggle_all_buttons(message: types.Message, is_active: bool):
    """Включает или выключает все кнопки пользователя через команду."""
    user_id = message.from_user.id
    buttons = database.get_user_buttons(user_id)

    if not buttons:
        bot.reply_to(message, "У вас нет кнопок.")
        return

    database.set_all_buttons_active(user_id, is_active)
    buttons = database.get_user_buttons(user_id)

    text = "✅ Все кнопки включены." if is_active else "❌ Все кнопки выключены."
    bot.reply_to(message, text, reply_markup=_build_user_keyboard(buttons))


# =============================================================================
# ВЕБ-СЕРВЕР
# =============================================================================

web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    return "Bot is running!"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    # Проверяем секретный токен (защита от поддельных запросов)
    if WEBHOOK_SECRET:
        incoming = flask_request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != WEBHOOK_SECRET:
            logger.warning("Webhook: отклонён запрос с неверным секретным токеном.")
            return "forbidden", 403

    if flask_request.is_json:
        update = types.Update.de_json(flask_request.get_data(as_text=True))
        bot.process_new_updates([update])
        return "ok", 200
    return "bad request", 400


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

_initialized = False


def _init_all():
    """Инициализация бота: БД, таймеры, воркеры, админ-хендлеры."""
    global _initialized, _BOT_USERNAME
    if _initialized:
        return
    _initialized = True

    logger.info("Бот запускается...")

    try:
        me = bot.get_me()
        _BOT_USERNAME = me.username or ""
        logger.info("Бот: @%s (id=%s)", _BOT_USERNAME, me.id)
    except Exception:
        logger.critical("Не удалось получить информацию о боте — завершение.")
        sys.exit(1)

    database.init_db()
    restore_timers()
    _start_timer_poller()
    _start_stats_worker()
    admin.register()
    _register_shutdown()


def _setup_webhook() -> bool:
    """Устанавливает webhook если задан WEBHOOK_URL. Возвращает True если установлен."""
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if not webhook_url:
        return False

    bot.remove_webhook()
    time.sleep(1)

    set_webhook_kwargs = dict(
        url=f"{webhook_url}/webhook",
        drop_pending_updates=True,
        # Только нужные типы — убираем edited_message и channel_post
        allowed_updates=["message", "callback_query"],
    )
    if WEBHOOK_SECRET:
        set_webhook_kwargs["secret_token"] = WEBHOOK_SECRET

    bot.set_webhook(**set_webhook_kwargs)
    logger.info("Webhook: %s/webhook", webhook_url)
    return True


def _register_shutdown():
    """Регистрирует graceful shutdown: отменяет все активные таймеры при завершении."""
    def _shutdown():
        logger.info("Graceful shutdown: отмена %d активных таймеров...", len(TIMERS))
        with _timers_lock:
            for info in TIMERS.values():
                t = info.get("timer_obj")
                if t:
                    t.cancel()

    atexit.register(_shutdown)


def create_app():
    """
    Фабрика Flask-приложения для Gunicorn.

    Использование:
        gunicorn "main:create_app()" --bind 0.0.0.0:$PORT --workers 1 --threads 4

    ВАЖНО: используйте строго 1 worker — таймеры хранятся в памяти процесса.
    """
    _init_all()
    _setup_webhook()
    return web_app


def main():
    _init_all()

    if not _setup_webhook():
        logger.warning("WEBHOOK_URL не задан — polling (только локально).")
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
            except Exception:
                logger.exception("Polling упал, перезапуск через 5 сек...")
                time.sleep(5)
        return

    port = int(os.environ.get("PORT", 10000))
    logger.info("Flask dev-сервер на порту %s (для прода — gunicorn)", port)
    web_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
