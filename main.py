"""
Telegram-бот для личного использования в группе (pyTelegramBotAPI).

Команды: /ping /id /start /help /t /т /tr /тр /mytimers /del /cancel
         /к /ук /кнопки

Архитектура:
config.py   — bot, переменные окружения, логирование
database.py — вся работа с БД
utils.py    — хелперы + безопасная отправка в Telegram
admin.py    — команды для администраторов
main.py     — этот файл: пользовательские хендлеры, планировщик, Flask
"""

import hmac
import html
import math
import os
import queue as _queue_module
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps

from flask import Flask, request as flask_request
from telebot import types

import admin
import database
import utils
from config import bot, logger, WEBHOOK_SECRET
from utils import (
    BoundedDict,
    SendForbidden,
    build_mention,
    format_duration,
    get_uptime_str,
    parse_duration,
    safe_answer_callback,
    safe_edit,
    safe_reply,
    safe_send,
)

# =============================================================================
# КОНСТАНТЫ
# =============================================================================

MAX_TIMERS_PER_USER    = 100
MAX_TIMER_DURATION     = 365 * 24 * 3600
MIN_TIMER_DURATION     = 10
MAX_DESCRIPTION_LENGTH = 200
TIMERS_PAGE_SIZE       = 8

MAX_USER_BUTTONS       = 20
MAX_BUTTON_NAME_LENGTH = 50

# Названия, которые нельзя присваивать кнопке: иначе нажатие
# кнопки будет запускать команду бота.
_RESERVED_BUTTON_NAMES = {"кнопки", "таймеры", "удалить", "отмена"}

_ONE_YEAR = 365 * 24 * 3600


def _calc_max_fires(interval_seconds: int) -> int:
    """Максимум срабатываний: не дольше года и не больше 365 раз."""
    return max(1, min(365, _ONE_YEAR // interval_seconds))


# =============================================================================
# ХРАНИЛИЩЕ АКТИВНЫХ ТАЙМЕРОВ
# =============================================================================

# TIMERS: timer_id -> {chat_id, thread_id, user_id, user_mention, description,
#                      end_time, duration, is_recurring, interval_seconds,
#                      fires_remaining, firing, missed}
TIMERS: dict = {}
USER_TIMERS: dict = {}          # user_id -> set(timer_id)

# Один Condition вместо Lock: планировщик спит на нём и просыпается,
# когда появляется таймер с более ранним временем.
_timers_cv = threading.Condition(threading.RLock())

_next_local_id = 1              # для таймеров, которые не удалось записать в БД
_fire_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="timer-fire")

_sort_state: BoundedDict = BoundedDict(500)   # user_id -> {"sort": str, "page": int}


def _forget_timer(timer_id: int, user_id: int):
    """Убирает таймер из памяти, не оставляя пустых множеств."""
    with _timers_cv:
        TIMERS.pop(timer_id, None)
        owned = USER_TIMERS.get(user_id)
        if owned is not None:
            owned.discard(timer_id)
            if not owned:
                USER_TIMERS.pop(user_id, None)


# =============================================================================
# ПЛАНИРОВЩИК ТАЙМЕРОВ
# =============================================================================

def _scheduler_loop():
    """
    Один поток на все таймеры вместо threading.Timer на каждый.
    Просыпается к ближайшему сроку (но не реже раза в 30 сек) и отдаёт
    сработавшие таймеры в пул потоков, чтобы медленная отправка
    не задерживала остальные.
    """
    logger.info("Планировщик таймеров запущен.")
    while True:
        try:
            with _timers_cv:
                now = time.time()
                due = [
                    tid for tid, info in TIMERS.items()
                    if not info.get("firing") and info["end_time"] <= now
                ]
                for tid in due:
                    TIMERS[tid]["firing"] = True   # защита от двойного срабатывания

                if not due:
                    pending = [i["end_time"] for i in TIMERS.values() if not i.get("firing")]
                    wait = (min(pending) - now) if pending else 30.0
                    _timers_cv.wait(max(0.2, min(wait, 30.0)))

            for tid in due:
                _fire_pool.submit(_safe_fire, tid)
        except Exception:
            logger.exception("Ошибка в планировщике таймеров.")
            time.sleep(1)


def _safe_fire(timer_id: int):
    try:
        fire_timer(timer_id)
    except Exception:
        logger.exception("Ошибка при срабатывании таймера #%s", timer_id)
        # Не оставляем таймер вечно в состоянии "срабатывает",
        # но и не даём ему уйти в цикл мгновенных повторов.
        with _timers_cv:
            info = TIMERS.get(timer_id)
            if info is not None:
                info["firing"] = False
                info["end_time"] = time.time() + 60


def fire_timer(timer_id: int):
    """Срабатывание таймера. Идемпотентен: повторный вызов ничего не делает."""
    with _timers_cv:
        info = TIMERS.get(timer_id)
        if info is None:
            return
        is_recurring = bool(info.get("is_recurring"))
        missed = bool(info.pop("missed", False))
        if not is_recurring:
            TIMERS.pop(timer_id, None)
            owned = USER_TIMERS.get(info["user_id"])
            if owned is not None:
                owned.discard(timer_id)
                if not owned:
                    USER_TIMERS.pop(info["user_id"], None)
        snapshot = dict(info)

    interval  = snapshot.get("interval_seconds", 0)
    chat_id   = snapshot["chat_id"]
    thread_id = snapshot.get("thread_id")
    user_id   = snapshot["user_id"]

    logger.info(
        "Таймер #%s сработал (chat=%s, user=%s, missed=%s, recurring=%s).",
        timer_id, chat_id, user_id, missed, is_recurring,
    )

    if is_recurring:
        text = f"🔁 {snapshot['user_mention']}, повторяющийся таймер!"
    else:
        text = f"⏰ {snapshot['user_mention']}, время вышло!"
    if snapshot["description"]:
        text += f"\n📝 {html.escape(snapshot['description'])}"
    if missed:
        text += "\n\n⚠️ Бот был выключен, когда таймер должен был сработать."

    try:
        safe_send(chat_id, text, message_thread_id=thread_id)
    except SendForbidden as e:
        logger.warning("Таймер #%s: чат %s недоступен (%s) — удаляю таймер.",
                       timer_id, chat_id, e)
        _forget_timer(timer_id, user_id)
        database.delete_timer(timer_id)
        return

    if not (is_recurring and interval):
        database.delete_timer(timer_id)
        return

    # ---------- Перепланирование повторяющегося таймера ----------
    now = time.time()
    old_end = snapshot["end_time"]
    # Пропущенные интервалы (бот лежал) не превращаем в лавину сообщений
    skipped = int((now - old_end) // interval) if old_end + interval < now else 0

    new_remaining = database.decrement_timer_fires(timer_id)
    if new_remaining is None:
        new_remaining = max(0, snapshot.get("fires_remaining", 0) - 1)
    new_remaining = max(0, new_remaining - skipped)

    if new_remaining > 0:
        new_end = old_end + interval * (skipped + 1)
        database.update_timer_end_time(timer_id, new_end)
        with _timers_cv:
            if timer_id not in TIMERS:
                logger.info("Таймер #%s отменён во время срабатывания.", timer_id)
                return
            TIMERS[timer_id].update({
                "end_time":        new_end,
                "duration":        interval,
                "fires_remaining": new_remaining,
                "firing":          False,
            })
            _timers_cv.notify_all()
        logger.info("Таймер #%s перезапланирован (пропущено %s, осталось %s).",
                    timer_id, skipped, new_remaining)
    else:
        _forget_timer(timer_id, user_id)
        database.delete_timer(timer_id)
        logger.info("Повторяющийся таймер #%s завершён: лимит исчерпан.", timer_id)
        try:
            safe_send(
                chat_id,
                f"🔁 {snapshot['user_mention']}, повторяющийся таймер #{timer_id} "
                f"завершён — лимит срабатываний исчерпан.",
                message_thread_id=thread_id,
            )
        except SendForbidden:
            pass


def create_timer(message: types.Message, duration_seconds: int,
                 description: str, is_recurring: bool = False):
    global _next_local_id

    user       = message.from_user
    first_name = user.first_name or "Пользователь"
    end_time   = time.time() + duration_seconds
    interval   = duration_seconds if is_recurring else 0
    fires_rem  = _calc_max_fires(duration_seconds) if is_recurring else 0
    thread_id  = getattr(message, "message_thread_id", None)

    timer_id = database.insert_timer(
        message.chat.id, user.id, first_name, description, end_time,
        is_recurring=is_recurring, interval_seconds=interval,
        fires_remaining=fires_rem, thread_id=thread_id,
    )

    # БД недоступна — локальный ID делаем отрицательным, чтобы он никогда
    # не столкнулся с SERIAL-идентификатором из базы.
    if timer_id is None:
        with _timers_cv:
            timer_id = -_next_local_id
            _next_local_id += 1

    with _timers_cv:
        TIMERS[timer_id] = {
            "chat_id":          message.chat.id,
            "thread_id":        thread_id,
            "user_id":          user.id,
            "user_mention":     build_mention(user.id, first_name),
            "description":      description,
            "end_time":         end_time,
            "duration":         duration_seconds,
            "is_recurring":     is_recurring,
            "interval_seconds": interval,
            "fires_remaining":  fires_rem,
            "firing":           False,
        }
        USER_TIMERS.setdefault(user.id, set()).add(timer_id)
        _timers_cv.notify_all()

    logger.info("Создан %s таймер #%s на %s сек (user=%s, chat=%s).",
                "повторяющийся" if is_recurring else "обычный",
                timer_id, duration_seconds, user.id, message.chat.id)

    label     = f"#{timer_id}" if timer_id > 0 else "(без сохранения в базе)"
    desc_part = f"\n📝 {html.escape(description)}" if description else ""
    if is_recurring:
        safe_reply(
            message,
            f"✅ Повторяющийся таймер {label} установлен.\n"
            f"↺ Будет срабатывать каждые {format_duration(duration_seconds)}.\n"
            f"📊 Максимум срабатываний: {fires_rem}{desc_part}",
        )
    else:
        safe_reply(
            message,
            f"✅ Таймер {label} установлен на "
            f"{format_duration(duration_seconds)}.{desc_part}",
        )


def cancel_timer(timer_id: int, user_id: int) -> str:
    with _timers_cv:
        info = TIMERS.get(timer_id)
        if info is None:
            return f"❌ Таймер #{timer_id} не найден (сработал или уже удалён)."
        if info["user_id"] != user_id:
            return f"❌ Таймер #{timer_id} принадлежит другому пользователю."
        TIMERS.pop(timer_id, None)
        owned = USER_TIMERS.get(user_id)
        if owned is not None:
            owned.discard(timer_id)
            if not owned:
                USER_TIMERS.pop(user_id, None)
        _timers_cv.notify_all()

    database.delete_timer(timer_id)
    logger.info("Таймер #%s отменён пользователем %s.", timer_id, user_id)
    return f"🗑 Таймер #{timer_id} успешно удалён."


def restore_timers():
    """Восстанавливает таймеры из БД при старте."""
    rows = database.load_all_timers()
    if not rows:
        return

    now = time.time()
    restored = overdue = 0

    for (timer_id, chat_id, user_id, first_name, description, end_time,
         is_recurring, interval_seconds, fires_remaining, thread_id) in rows:

        is_overdue = end_time <= now
        with _timers_cv:
            TIMERS[timer_id] = {
                "chat_id":          chat_id,
                "thread_id":        thread_id,
                "user_id":          user_id,
                "user_mention":     build_mention(user_id, first_name),
                "description":      description,
                "end_time":         end_time,
                "duration":         max(0, int(end_time - now)),
                "is_recurring":     bool(is_recurring),
                "interval_seconds": int(interval_seconds),
                "fires_remaining":  int(fires_remaining),
                "firing":           False,
                "missed":           is_overdue,
            }
            USER_TIMERS.setdefault(user_id, set()).add(timer_id)

        if is_overdue:
            overdue += 1
        else:
            restored += 1

    logger.info("Восстановлено таймеров: %s активных, %s просроченных.",
                restored, overdue)


# =============================================================================
# /mytimers
# =============================================================================

def _timers_snapshot(user_id: int, sort_mode: str) -> list:
    with _timers_cv:
        items = [
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
    items.sort(key=(lambda t: t["end_time"]) if sort_mode == "time" else (lambda t: t["id"]))
    return items


def _build_timers_message(user_id: int, sort_mode: str, page: int):
    """(text, markup, page). Постранично — иначе не влезает в лимит Telegram."""
    items = _timers_snapshot(user_id, sort_mode)
    if not items:
        return "У вас нет активных таймеров.", None, 0

    pages = max(1, math.ceil(len(items) / TIMERS_PAGE_SIZE))
    page = page % pages
    chunk = items[page * TIMERS_PAGE_SIZE:(page + 1) * TIMERS_PAGE_SIZE]

    now = time.time()
    lines = [f"<b>📑 Ваши активные таймеры ({len(items)}) — стр. {page + 1}/{pages}</b>", ""]

    for info in chunk:
        remaining = max(int(info["end_time"] - now), 0)
        icon = "🔁" if info["is_recurring"] else "•"
        header = f"{icon} <b>#{info['id']}</b> · {format_duration(remaining)}"
        if info["is_recurring"] and info["interval_seconds"]:
            header += (f"  <i>↺ каждые {format_duration(info['interval_seconds'])}"
                       f" · осталось {info['fires_remaining']} раз</i>")
        lines.append(header)
        if info["description"]:
            lines.append(html.escape(info["description"]))
        lines.append("")

    lines.append("/del [ID] — удалить таймер")

    def cb(action):
        return f"tm:{user_id}:{action}"

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton(
            "✅ По времени" if sort_mode == "time" else "По времени", callback_data=cb("time")),
        types.InlineKeyboardButton(
            "✅ По номеру" if sort_mode != "time" else "По номеру", callback_data=cb("id")),
    )
    if pages > 1:
        markup.row(
            types.InlineKeyboardButton("◀️", callback_data=cb("prev")),
            types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=cb("noop")),
            types.InlineKeyboardButton("▶️", callback_data=cb("next")),
        )
    markup.row(types.InlineKeyboardButton("🔄 Обновить", callback_data=cb("refresh")))
    return "\n".join(lines), markup, page


def _show_my_timers(message: types.Message):
    user_id = message.from_user.id
    state = _sort_state.get(user_id) or {"sort": "id", "page": 0}
    text, markup, page = _build_timers_message(user_id, state["sort"], state["page"])
    _sort_state[user_id] = {"sort": state["sort"], "page": page}
    safe_reply(message, text, reply_markup=markup)


# =============================================================================
# CALLBACK-ХЕЛПЕРЫ
# =============================================================================

def _parse_callback(call: types.CallbackQuery):
    """"prefix:owner_id:action" -> (prefix, owner_id, action) или None."""
    parts = (call.data or "").split(":")
    if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
        return None
    return parts[0], int(parts[1]), parts[2]


def callback_handler(fn):
    """Проверяет владельца кнопки и глушит исключения."""
    @wraps(fn)
    def wrapper(call: types.CallbackQuery):
        parsed = _parse_callback(call)
        if parsed is None:
            safe_answer_callback(call.id)
            return
        _, owner_id, action = parsed
        # Владелец зашит в саму кнопку: работает даже если исходную
        # команду удалили из чата.
        if call.from_user.id != owner_id:
            safe_answer_callback(call.id, "⛔ Это не твоя кнопка.")
            return
        try:
            fn(call, action)
        except SendForbidden:
            safe_answer_callback(call.id, "⛔ Бот не может писать в этот чат.")
        except Exception:
            logger.exception("Ошибка в callback-хендлере %s", fn.__name__)
            safe_answer_callback(call.id, "⚠️ Что-то пошло не так.")
    return wrapper


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("tm:"))
@callback_handler
def handle_timers_callback(call: types.CallbackQuery, action: str):
    if action == "noop":
        safe_answer_callback(call.id)
        return

    user_id = call.from_user.id
    state = _sort_state.get(user_id) or {"sort": "id", "page": 0}

    if action in ("id", "time"):
        state = {"sort": action, "page": 0}
    elif action == "prev":
        state = {"sort": state["sort"], "page": state["page"] - 1}
    elif action == "next":
        state = {"sort": state["sort"], "page": state["page"] + 1}

    text, markup, page = _build_timers_message(user_id, state["sort"], state["page"])
    _sort_state[user_id] = {"sort": state["sort"], "page": page}
    safe_edit(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    safe_answer_callback(call.id)


# =============================================================================
# ПАРСИНГ КОМАНД ТАЙМЕРА
# =============================================================================

_TIMER_USAGE = (
    "⚠️ Не удалось распознать команду.\n\n"
    "Формат: <code>/т [время] [описание]</code>\n"
    "Время: д/ч/м/с или d/h/m/s, например:\n"
    "  <code>/т 1д5ч30с купить продукты</code>\n"
    "  <code>/т 10с</code>\n"
    f"Минимум: {MIN_TIMER_DURATION} сек · Описание: до {MAX_DESCRIPTION_LENGTH} символов."
)

_RECURRING_USAGE = (
    "⚠️ Не удалось распознать команду.\n\n"
    "Формат: <code>/тр [интервал] [описание]</code>\n"
    "Пример: <code>/тр 1д проверить почту</code>\n\n"
    "Таймер срабатывает снова и снова, пока не удалишь его через /del."
)


def _validate_timer_args(message, time_part, description):
    """(duration, error_text). duration=None → отправить error_text."""
    duration = parse_duration(time_part)
    if duration is None:
        return None, None                        # None → показать формат команды
    if duration < MIN_TIMER_DURATION:
        return None, f"⚠️ Минимальная длительность — {MIN_TIMER_DURATION} секунд."
    if duration > MAX_TIMER_DURATION:
        return None, "⚠️ Максимальная длительность — 1 год."
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return None, (f"⚠️ Описание слишком длинное ({len(description)} симв.). "
                      f"Максимум — {MAX_DESCRIPTION_LENGTH} символов.")

    with _timers_cv:
        count = len(USER_TIMERS.get(message.from_user.id, set()))
    if count >= MAX_TIMERS_PER_USER:
        return None, (f"⚠️ У вас уже {count} таймеров (максимум {MAX_TIMERS_PER_USER}). "
                      "Удалите лишние через /mytimers.")
    return duration, None


def _process_timer_request(message, args_text, recurring: bool):
    usage = _RECURRING_USAGE if recurring else _TIMER_USAGE
    args_text = args_text.strip()
    if not args_text:
        safe_reply(message, usage)
        return

    parts = args_text.split(maxsplit=1)
    description = parts[1].strip() if len(parts) > 1 else ""
    duration, error = _validate_timer_args(message, parts[0], description)
    if duration is None:
        safe_reply(message, error or usage)
        return
    create_timer(message, duration, description, is_recurring=recurring)


def _process_cancel_request(message, args_text):
    args_text = args_text.strip()
    id_str = args_text.split()[0].lstrip("#") if args_text else ""
    if not id_str.isdigit():
        safe_reply(
            message,
            "⚠️ Укажите ID таймера.\n"
            "Формат: <code>/del [ID]</code>\n"
            "Список ID — /mytimers.",
        )
        return
    safe_reply(message, cancel_timer(int(id_str), message.from_user.id))


# =============================================================================
# ПОЛЬЗОВАТЕЛЬСКИЕ REPLY-КНОПКИ
# =============================================================================

def _build_user_keyboard(buttons: list):
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


def _parse_button_names(raw: str) -> list:
    if ";" in raw:
        parts = raw.split(";")
    elif "\n" in raw:
        parts = raw.split("\n")
    else:
        parts = [raw]

    seen, result = set(), []
    for p in parts:
        name = p.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


_BUTTON_USAGE = (
    "⚠️ Укажите название кнопки.\n\n"
    "<b>Одна кнопка:</b>\n"
    "<code>/к Проверить почту</code>\n\n"
    "<b>Несколько через «;»:</b>\n"
    "<code>/к Почта; Задачи; Позвонить маме</code>\n\n"
    f"Максимум кнопок: {MAX_USER_BUTTONS} · Название: до {MAX_BUTTON_NAME_LENGTH} символов.\n"
    "Список кнопок — /кнопки"
)


def _require_db(message) -> bool:
    if not database.db_enabled():
        safe_reply(message, "⚠️ База данных не настроена — кнопки недоступны.")
        return False
    return True


def _render_buttons_list(user_id: int):
    """Единый рендер списка кнопок — используется и командой, и callback'ом."""
    buttons = database.get_user_buttons(user_id)
    if not buttons:
        return "У вас нет кнопок.\nДобавьте: <code>/к [название]</code>", None, buttons

    active_count = sum(1 for _, _, a in buttons if a)
    lines = [f"<b>📌 Ваши кнопки ({active_count}/{len(buttons)})</b>", ""]
    for i, (_, name, is_active) in enumerate(buttons, 1):
        lines.append(f"{i}. {html.escape(name)}" + ("" if is_active else " <i>(выкл)</i>"))
    lines.append("\nДля удаления: <code>/ук [номер или название]</code>")
    lines.append("Для добавления: <code>/к [название]</code>")

    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Включить", callback_data=f"btn:{user_id}:on"),
        types.InlineKeyboardButton("❌ Выключить", callback_data=f"btn:{user_id}:off"),
    )
    return "\n".join(lines), markup, buttons


def _process_add_button(message, raw: str):
    if not _require_db(message):
        return

    raw = raw.strip()
    user_id = message.from_user.id
    names = _parse_button_names(raw)
    if not names:
        safe_reply(message, _BUTTON_USAGE)
        return

    too_long = [n for n in names if len(n) > MAX_BUTTON_NAME_LENGTH]
    if too_long:
        safe_reply(message, f"⚠️ Слишком длинные названия "
                            f"({MAX_BUTTON_NAME_LENGTH} симв. макс.): "
                            + ", ".join(f"«{html.escape(n)}»" for n in too_long))
        return

    bad = [n for n in names
           if n.startswith("/") or n.lower() in _RESERVED_BUTTON_NAMES]
    if bad:
        safe_reply(message, "⚠️ Эти названия зарезервированы под команды бота: "
                            + ", ".join(f"«{html.escape(n)}»" for n in bad))
        return

    buttons = database.get_user_buttons(user_id)
    existing = {n.lower() for _, n, _ in buttons}
    free_slots = MAX_USER_BUTTONS - len(buttons)

    duplicates = [n for n in names if n.lower() in existing]
    new_names  = [n for n in names if n.lower() not in existing]

    if not new_names:
        safe_reply(message, "⚠️ Все эти кнопки уже есть: "
                            + ", ".join(f"«{html.escape(n)}»" for n in duplicates))
        return

    if len(new_names) > free_slots:
        safe_reply(message, f"⚠️ Можно добавить ещё {free_slots} кнопок "
                            f"(максимум {MAX_USER_BUTTONS}), а вы добавляете "
                            f"{len(new_names)}. Сократите список.")
        return

    added, failed = [], []
    for name in new_names:
        (added if database.add_user_button(user_id, name) is not None else failed).append(name)

    lines = []
    if added:
        lines.append("✅ Добавлено: " + ", ".join(f"«{html.escape(n)}»" for n in added))
    if duplicates:
        lines.append("⚠️ Уже были: " + ", ".join(f"«{html.escape(n)}»" for n in duplicates))
    if failed:
        lines.append("❌ Не удалось добавить: "
                     + ", ".join(f"«{html.escape(n)}»" for n in failed))

    safe_reply(message, "\n".join(lines),
               reply_markup=_build_user_keyboard(database.get_user_buttons(user_id)))


def _process_remove_button(message, raw: str):
    if not _require_db(message):
        return

    raw = raw.strip()
    user_id = message.from_user.id

    if not raw:
        safe_reply(
            message,
            "⚠️ Укажите номер или название кнопки.\n\n"
            "<code>/ук 1</code>  ·  <code>/ук Проверить почту</code>\n"
            "<code>/ук 1 3 5</code>  ·  <code>/ук Почта; Задачи</code>\n"
            "<code>/ук все</code> — удалить все\n\n"
            "Список кнопок — /кнопки",
        )
        return

    buttons = database.get_user_buttons(user_id)
    if not buttons:
        safe_reply(message, "У вас нет кнопок.")
        return

    if raw.lower() == "все":
        count = database.remove_all_user_buttons(user_id)
        safe_reply(message, f"🗑 Удалено всех кнопок: {count}.",
                   reply_markup=types.ReplyKeyboardRemove(selective=True))
        return

    tokens = ([t.strip() for t in raw.split(";") if t.strip()]
              if ";" in raw else raw.split())

    to_delete_ids, to_delete_names, not_found = [], [], []
    for token in tokens:
        match = None
        if token.lstrip("#").isdigit():
            idx = int(token.lstrip("#")) - 1
            if 0 <= idx < len(buttons):
                match = (buttons[idx][0], buttons[idx][1])
        else:
            match = next(((bid, bname) for bid, bname, _ in buttons
                          if bname.lower() == token.lower()), None)

        if match is None:
            not_found.append(token)
        elif match[0] not in to_delete_ids:
            to_delete_ids.append(match[0])
            to_delete_names.append(match[1])

    lines = []
    if to_delete_ids:
        database.remove_user_buttons(to_delete_ids, user_id)
        lines.append("🗑 Удалено: "
                     + ", ".join(f"«{html.escape(n)}»" for n in to_delete_names))
    if not_found:
        lines.append("⚠️ Не найдено: " + ", ".join(html.escape(t) for t in not_found))

    remaining = database.get_user_buttons(user_id)
    if not remaining:
        lines.append("Все кнопки удалены.")

    safe_reply(message, "\n".join(lines), reply_markup=_build_user_keyboard(remaining))


def _toggle_all_buttons(message, is_active: bool):
    if not _require_db(message):
        return
    user_id = message.from_user.id
    if not database.get_user_buttons(user_id):
        safe_reply(message, "У вас нет кнопок.")
        return

    database.set_all_buttons_active(user_id, is_active)
    buttons = database.get_user_buttons(user_id)
    safe_reply(message,
               "✅ Все кнопки включены." if is_active else "❌ Все кнопки выключены.",
               reply_markup=_build_user_keyboard(buttons))


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("btn:"))
@callback_handler
def handle_buttons_toggle(call: types.CallbackQuery, action: str):
    user_id = call.from_user.id
    is_active = (action == "on")

    database.set_all_buttons_active(user_id, is_active)
    text, markup, buttons = _render_buttons_list(user_id)

    # Отдельное сообщение нужно, чтобы обновить саму reply-клавиатуру.
    original = getattr(call.message, "reply_to_message", None)
    safe_send(
        call.message.chat.id,
        "✅ Все кнопки включены." if is_active else "❌ Все кнопки выключены.",
        reply_to_message_id=original.message_id if original else None,
        message_thread_id=getattr(call.message, "message_thread_id", None),
        reply_markup=_build_user_keyboard(buttons),
    )
    safe_edit(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    safe_answer_callback(call.id)


# =============================================================================
# СТАТИСТИКА
# =============================================================================

_stats_queue: _queue_module.Queue = _queue_module.Queue(maxsize=5000)


def _extract_stats(message: types.Message):
    """Message -> (user_key, user_row, chat_key, chat_row, counters) или None."""
    user = message.from_user
    if user is None or user.is_bot:
        return None

    chat = message.chat
    chat_title = (f"ЛС: {user.first_name or user.username or user.id}"
                  if chat.type == "private" else (chat.title or str(chat.id)))

    is_forward = any((
        getattr(message, "forward_origin", None),
        getattr(message, "forward_from", None),
        getattr(message, "forward_from_chat", None),
        getattr(message, "forward_sender_name", None),
    ))

    # [messages, chars, stickers, photos, videos, voice, gifs, forwards]
    counters = [0] * 8
    if is_forward:
        counters[7] = 1
    else:
        ct = message.content_type
        if ct != "sticker":
            counters[0] = 1
        if ct == "text":
            counters[1] = len(message.text or "")
        elif ct == "sticker":
            counters[2] = 1
        elif ct == "photo":
            counters[3] = 1; counters[1] = len(message.caption or "")
        elif ct == "video":
            counters[4] = 1; counters[1] = len(message.caption or "")
        elif ct in ("voice", "video_note"):
            counters[5] = 1
        elif ct == "animation":
            counters[6] = 1; counters[1] = len(message.caption or "")
        elif message.caption:
            counters[1] = len(message.caption)

    return (
        user.id, (user.username, user.first_name, user.last_name),
        chat.id, (chat.type, chat_title),
        counters,
    )


def _flush_stats(batch: list):
    """Схлопывает пачку в один INSERT ... ON CONFLICT на каждую таблицу."""
    users, chats, stats = {}, {}, {}
    for user_id, user_row, chat_id, chat_row, counters in batch:
        users[user_id] = user_row
        chats[chat_id] = chat_row
        acc = stats.setdefault((user_id, chat_id), [0] * 8)
        for i, v in enumerate(counters):
            acc[i] += v
    database.record_message_stats_bulk(users, chats, stats)


def _start_stats_worker():
    def _worker():
        logger.info("Воркер статистики запущен.")
        while True:
            batch = [_stats_queue.get()]
            time.sleep(0.5)      # даём накопиться пачке — вместо 3 запросов на сообщение
            try:
                while len(batch) < 500:
                    batch.append(_stats_queue.get_nowait())
            except _queue_module.Empty:
                pass

            try:
                _flush_stats(batch)
            except Exception:
                logger.exception("Ошибка при записи статистики.")
            for _ in batch:
                _stats_queue.task_done()

    threading.Thread(target=_worker, daemon=True, name="stats-worker").start()


@bot.middleware_handler(update_types=["message"])
def stats_middleware(bot_instance, message):
    if not database.db_enabled():
        return
    try:
        item = _extract_stats(message)
        if item is not None:
            _stats_queue.put_nowait(item)
    except _queue_module.Full:
        logger.warning("Очередь статистики переполнена, сообщение пропущено.")
    except Exception:
        logger.exception("Ошибка в middleware статистики.")


# =============================================================================
# СПРАВКА
# =============================================================================

_HELP_MAIN = "📖 <b>Справка</b>\n\nВыбери раздел:"

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
    "Число срабатываний ограничено: не более 365 раз и не дольше года — "
    "точное количество бот покажет при создании.\n\n"
    "  <code>/тр 8ч пить воду</code>\n"
    "  <code>/тр 1д проверить почту</code>\n\n"
    "〔 Список 〕\n<code>/mytimers</code>\n\n"
    "〔 Удалить 〕\n"
    "<code>/del [ID]</code>  ·  <code>/cancel</code>  ·  <code>удалить 3</code>"
)

_HELP_BUTTONS = (
    "📌 <b>Быстрые кнопки</b>\n\n"
    "Персональные кнопки в панели ввода. Видны только тебе.\n\n"
    "〔 Добавить 〕\n<code>/к [название]</code>\n"
    "  <code>/к Почта; Задачи; Позвонить</code>  — сразу несколько\n\n"
    "〔 Удалить 〕\n<code>/ук [номер или название]</code>\n"
    "  <code>/ук 2</code>  ·  <code>/ук Почта</code>  ·  <code>/ук все</code>\n\n"
    "〔 Включить / выключить 〕\n<code>/кнопки вкл</code>  ·  <code>/кнопки выкл</code>\n\n"
    "〔 Список 〕\n<code>/кнопки</code>  ·  <code>/buttons</code>\n\n"
    f"Максимум {MAX_USER_BUTTONS} кнопок · Название до {MAX_BUTTON_NAME_LENGTH} символов."
)

_HELP_MISC = (
    "💡 <b>Прочее</b>\n\n"
    "〔 /ping 〕\nВремя отклика до Telegram и аптайм бота.\n\n"
    "〔 /id 〕\nБез реплая — ID чата. Реплаем — ID пользователя."
)

_HELP_SECTIONS = {"main": _HELP_MAIN, "timers": _HELP_TIMERS,
                  "buttons": _HELP_BUTTONS, "misc": _HELP_MISC}


def _help_markup(user_id: int, section: str) -> types.InlineKeyboardMarkup:
    m = types.InlineKeyboardMarkup()
    if section == "main":
        m.row(
            types.InlineKeyboardButton("⏰ Таймеры", callback_data=f"help:{user_id}:timers"),
            types.InlineKeyboardButton("📌 Кнопки",  callback_data=f"help:{user_id}:buttons"),
            types.InlineKeyboardButton("💡 Прочее",  callback_data=f"help:{user_id}:misc"),
        )
    else:
        m.row(types.InlineKeyboardButton("← Назад", callback_data=f"help:{user_id}:main"))
    return m


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("help:"))
@callback_handler
def handle_help_callback(call: types.CallbackQuery, section: str):
    if section not in _HELP_SECTIONS:
        safe_answer_callback(call.id)
        return
    safe_edit(_HELP_SECTIONS[section], call.message.chat.id, call.message.message_id,
              reply_markup=_help_markup(call.from_user.id, section))
    safe_answer_callback(call.id)


# =============================================================================
# ОБРАБОТЧИКИ КОМАНД
# =============================================================================

_BOT_USERNAME = ""


def _is_for_me(message: types.Message) -> bool:
    """
    Команда адресована этому боту?
    Смотрим только на саму команду: "@" в тексте (например, в описании
    таймера) не должен приводить к игнорированию команды.
    """
    text = (message.text or "").strip()
    if not text.startswith("/"):
        return True
    first = text.split(maxsplit=1)[0]
    if "@" not in first or not _BOT_USERNAME:
        return True
    return first.split("@", 1)[1].lower() == _BOT_USERNAME.lower()


def user_command(fn):
    """
    Общая обвязка хендлеров: отсекает сообщения без автора (автофорварды
    из каналов), чужие команды с @username и не даёт исключению остаться
    без ответа пользователю.
    """
    @wraps(fn)
    def wrapper(message: types.Message):
        if message.from_user is None or not _is_for_me(message):
            return
        try:
            fn(message)
        except SendForbidden as e:
            logger.warning("Нет доступа к чату %s: %s", message.chat.id, e)
        except Exception:
            logger.exception("Ошибка в хендлере %s", fn.__name__)
            try:
                safe_reply(message, "⚠️ Что-то пошло не так. Попробуйте ещё раз.")
            except Exception:
                pass
    return wrapper


@bot.message_handler(commands=["start"])
@user_command
def handle_start(message):
    safe_reply(message, "👋 Привет! Я бот для напоминаний и статистики чата.\n\n"
                        "Список команд — /help")


@bot.message_handler(commands=["help"])
@user_command
def handle_help(message):
    safe_reply(message, _HELP_MAIN,
               reply_markup=_help_markup(message.from_user.id, "main"))


@bot.message_handler(commands=["id"])
@user_command
def handle_id(message):
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if target is None:
            safe_reply(message, "❌ У этого сообщения нет автора (автофорвард из канала).")
            return
        name = html.escape(target.first_name or target.username or str(target.id))
        label = " (бот)" if target.is_bot else ""
        safe_reply(message, f"🆔 ID <b>{name}</b>{label}: <code>{target.id}</code>")
    elif message.chat.type == "private":
        safe_reply(message, f"🆔 Ваш Telegram ID: <code>{message.from_user.id}</code>")
    else:
        safe_reply(message, f"🆔 ID этого чата: <code>{message.chat.id}</code>")


@bot.message_handler(commands=["ping"])
@user_command
def handle_ping(message):
    start = time.perf_counter()
    sent = safe_send(message.chat.id, "🏓 Pong!",
                     message_thread_id=getattr(message, "message_thread_id", None))
    if sent is None:
        return
    ms = (time.perf_counter() - start) * 1000
    safe_edit(f"🏓 Pong!\nPing: <code>{ms:.0f}</code> ms\nUptime: {get_uptime_str()}",
              message.chat.id, sent.message_id)


@bot.message_handler(commands=["t", "т"])
@user_command
def handle_timer(message):
    parts = message.text.split(maxsplit=1)
    _process_timer_request(message, parts[1] if len(parts) > 1 else "", recurring=False)


@bot.message_handler(commands=["tr", "тр"])
@user_command
def handle_recurring_timer(message):
    parts = message.text.split(maxsplit=1)
    _process_timer_request(message, parts[1] if len(parts) > 1 else "", recurring=True)


@bot.message_handler(commands=["mytimers"])
@user_command
def handle_mytimers(message):
    _show_my_timers(message)


@bot.message_handler(func=lambda m: bool(re.match(r"^таймеры\b", m.text or "", re.IGNORECASE)))
@user_command
def handle_mytimers_text(message):
    _show_my_timers(message)


@bot.message_handler(commands=["del", "del_timer", "cancel"])
@user_command
def handle_cancel(message):
    parts = message.text.split(maxsplit=1)
    _process_cancel_request(message, parts[1] if len(parts) > 1 else "")


@bot.message_handler(
    func=lambda m: bool(re.match(r"^(удалить|отмена)\s+#?\d+\s*$", m.text or "", re.IGNORECASE))
)
@user_command
def handle_cancel_text(message):
    _process_cancel_request(message, message.text.split(maxsplit=1)[1])


@bot.message_handler(commands=["к"])
@user_command
def handle_add_button(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        _process_add_button(message, parts[1])
    else:
        safe_reply(message, _BUTTON_USAGE)


@bot.message_handler(commands=["ук"])
@user_command
def handle_remove_button(message):
    parts = message.text.split(maxsplit=1)
    _process_remove_button(message, parts[1] if len(parts) > 1 else "")


def _handle_buttons_command(message):
    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else ""
    if arg in ("вкл", "on"):
        _toggle_all_buttons(message, True)
    elif arg in ("выкл", "off"):
        _toggle_all_buttons(message, False)
    else:
        if not _require_db(message):
            return
        text, markup, _ = _render_buttons_list(message.from_user.id)
        safe_reply(message, text, reply_markup=markup)


@bot.message_handler(commands=["кнопки", "buttons"])
@user_command
def handle_show_buttons(message):
    _handle_buttons_command(message)


@bot.message_handler(func=lambda m: bool(re.match(r"^кнопки\b", m.text or "", re.IGNORECASE)))
@user_command
def handle_show_buttons_text(message):
    _handle_buttons_command(message)


# =============================================================================
# ВЕБ-СЕРВЕР
# =============================================================================

web_app = Flask(__name__)


@web_app.route("/")
def health_check():
    return "Bot is running!"


@web_app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        incoming = flask_request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(incoming, WEBHOOK_SECRET):
            logger.warning("Webhook: отклонён запрос с неверным секретным токеном.")
            return "forbidden", 403

    if not flask_request.is_json:
        return "bad request", 400

    # Всегда отвечаем 200: на 5xx Telegram будет присылать тот же
    # апдейт снова и снова.
    try:
        update = types.Update.de_json(flask_request.get_data(as_text=True))
        bot.process_new_updates([update])
    except Exception:
        logger.exception("Ошибка обработки апдейта.")
    return "ok", 200


# =============================================================================
# ТОЧКА ВХОДА
# =============================================================================

_initialized = False
_init_lock = threading.Lock()


def _init_all():
    global _initialized, _BOT_USERNAME
    with _init_lock:
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
    threading.Thread(target=_scheduler_loop, daemon=True, name="timer-scheduler").start()
    _start_stats_worker()
    admin.register()

    # Render останавливает сервис через SIGTERM; без обработчика
    # процесс умирает молча, не успев ничего записать в лог.
    signal.signal(signal.SIGTERM, lambda *_: (logger.info("SIGTERM — завершение."),
                                              sys.exit(0)))


def _setup_webhook() -> bool:
    webhook_url = os.environ.get("WEBHOOK_URL", "").rstrip("/")
    if not webhook_url:
        return False

    bot.remove_webhook()
    time.sleep(1)

    kwargs = dict(
        url=f"{webhook_url}/webhook",
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )
    if WEBHOOK_SECRET:
        kwargs["secret_token"] = WEBHOOK_SECRET

    bot.set_webhook(**kwargs)
    logger.info("Webhook: %s/webhook", webhook_url)
    return True


def create_app():
    """
    Фабрика Flask-приложения для Gunicorn:
        gunicorn "main:create_app()" --bind 0.0.0.0:$PORT --workers 1 --threads 4
    ВАЖНО: строго 1 worker — таймеры живут в памяти процесса.
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

    port = int(os.environ.get("PORT", 10000))
    logger.info("Flask dev-сервер на порту %s (для прода — gunicorn).", port)
    web_app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
