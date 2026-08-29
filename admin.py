"""
Админ-команды бота. Работают ТОЛЬКО в личке и ТОЛЬКО для ADMIN_IDS.

  адмхелп                — список команд
  стата / статистика      — общая статистика
  стата [название/id]     — статистика беседы
  топ вся                 — глобальный топ (пагинация)
  топ [название/id]       — топ пользователей чата
  топ беседы / топ чаты   — топ бесед
  юзер [id/@username]     — статистика пользователя
"""

import html
import math

from telebot import types

import database
from config import bot, ADMIN_IDS, logger
from utils import (
    BoundedDict,
    build_clickable_name,
    rank_label,
    safe_answer_callback,
    safe_edit,
    safe_reply,
    safe_send,
)

PAGE_SIZE = 10

_pagination: BoundedDict = BoundedDict(500)


# =============================================================================
#                          ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def _is_admin_pm(m: types.Message) -> bool:
    return (m.chat.type == "private"
            and m.from_user is not None
            and m.from_user.id in ADMIN_IDS)


def _fmt_row_stats(messages, chars, stickers, photos, videos, voice, gifs, forwards) -> str:
    parts = [f"{messages} сообщений", f"{chars} символов"]
    for value, label in ((stickers, "стикеров"), (photos, "фото"), (videos, "видео"),
                         (voice, "голосовых"), (gifs, "gif"), (forwards, "пересланных")):
        if value:
            parts.append(f"{label} {value}")
    return ", ".join(parts)


def _total_pages(total: int) -> int:
    return max(1, math.ceil(total / PAGE_SIZE))


def _nav_keyboard(prefix: str, page: int, pages: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("◀️", callback_data=f"{prefix}_prev"),
        types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="top_noop"),
        types.InlineKeyboardButton("▶️", callback_data=f"{prefix}_next"),
    )
    return kb


def _shift_page(user_id: int, direction: int) -> int:
    state = _pagination.get(user_id, {})
    return (state.get("page", 0) + direction) % _total_pages(state.get("total", 0))


def admin_handler(fn):
    """Проверка прав + защита от необработанных исключений."""
    def wrapper(message: types.Message):
        if not _is_admin_pm(message):
            return
        if not database.db_enabled():
            safe_reply(message, "⚠️ База данных не настроена.")
            return
        try:
            fn(message)
        except Exception:
            logger.exception("Ошибка в админ-хендлере %s", fn.__name__)
            safe_reply(message, "⚠️ Ошибка при выполнении команды.")
    wrapper.__name__ = fn.__name__
    return wrapper


# =============================================================================
#                          ПОСТРОИТЕЛИ СТРАНИЦ
# =============================================================================

def _build_global_page(page: int):
    rows, total = database.get_global_top_page(page * PAGE_SIZE, PAGE_SIZE)
    pages = _total_pages(total)

    lines = [f"<b>🌍 Глобальный топ — стр. {page + 1}/{pages}</b>", ""]
    for i, row in enumerate(rows, start=page * PAGE_SIZE + 1):
        name = build_clickable_name(row[0], row[1], row[2])
        lines.append(f"{rank_label(i)} {name} ({html.escape(row[3] or '?')}) "
                     f"— {_fmt_row_stats(*row[4:])}")
    if not rows:
        lines.append("Нет данных.")

    return "\n".join(lines), _nav_keyboard("top_global", page, pages), total


def _build_chat_page(chat_id: int, chat_title: str, page: int):
    rows, total = database.get_chat_top_page(chat_id, page * PAGE_SIZE, PAGE_SIZE)
    pages = _total_pages(total)

    lines = [f"<b>💬 Топ «{html.escape(chat_title or str(chat_id))}» "
             f"— стр. {page + 1}/{pages}</b>", ""]
    for i, row in enumerate(rows, start=page * PAGE_SIZE + 1):
        name = build_clickable_name(row[0], row[1], row[2])
        lines.append(f"{rank_label(i)} {name} — {_fmt_row_stats(*row[3:])}")
    if not rows:
        lines.append("Нет данных.")

    return "\n".join(lines), _nav_keyboard("top_chat", page, pages), total


def _build_chats_top_page(page: int):
    rows, total = database.get_chats_top_page(page * PAGE_SIZE, PAGE_SIZE)
    pages = _total_pages(total)

    lines = [f"<b>🏆 Топ бесед — стр. {page + 1}/{pages}</b>", ""]
    for i, row in enumerate(rows, start=page * PAGE_SIZE + 1):
        lines.append(f"{rank_label(i)} {html.escape(row[1] or 'Без названия')} "
                     f"— {_fmt_row_stats(*row[2:])}")
    if not rows:
        lines.append("Нет данных.")

    return "\n".join(lines), _nav_keyboard("top_chats", page, pages), total


def _build_chat_stats_text(chat_id: int, chat_title: str) -> str:
    row = database.get_chat_stats(chat_id)
    title_e = html.escape(chat_title or str(chat_id))
    if not row:
        return f"❌ Нет данных по чату «{title_e}»."

    participants, messages, chars, stickers, photos, videos, voice, gifs, forwards = row
    lines = [
        f"<b>📊 Статистика «{title_e}»</b>", "",
        f"👥 Участников в боте: {participants}",
        f"✉️ Сообщений: {messages}",
        f"🔠 Символов: {chars}",
        f"🎟 Стикеров: {stickers}",
        f"🖼 Фото: {photos}",
        f"🎬 Видео: {videos}",
        f"🎤 Голосовых: {voice}",
        f"🎞 GIF: {gifs}",
        f"↩️ Пересланных: {forwards}",
    ]

    top_rows, _ = database.get_chat_top_page(chat_id, 0, 5)
    if top_rows:
        lines += ["", "<b>🏆 Топ-5 участников</b>"]
        for i, top_row in enumerate(top_rows, start=1):
            name = build_clickable_name(top_row[0], top_row[1], top_row[2])
            lines.append(f"{rank_label(i)} {name} — {_fmt_row_stats(*top_row[3:])}")

    return "\n".join(lines)


def build_stats_report() -> str:
    total_users, _, totals = database.get_stats_overview()
    group_count, private_count = database.get_chats_count_by_type()

    lines = [
        "<b>📊 Общая статистика</b>", "",
        f"👤 Пользователей: {total_users}",
        f"💬 Бесед: {group_count}",
        f"📩 Личок: {private_count}",
        f"✉️ Сообщений: {totals['messages']}",
        f"🔠 Символов: {totals['chars']}",
        f"🎟 Стикеров: {totals['stickers']}",
        f"🖼 Фото: {totals['photos']}",
        f"🎬 Видео: {totals['videos']}",
        f"🎤 Голосовых: {totals['voice']}",
        f"🎞 GIF: {totals['gifs']}",
        f"↩️ Пересланных: {totals['forwards']}",
    ]

    top_rows = database.get_top_activity_groups(limit=5)
    if top_rows:
        lines += ["", "<b>🏆 Топ-5 активных (беседы)</b>"]
        for i, row in enumerate(top_rows, start=1):
            name = build_clickable_name(row[0], row[1], row[2])
            lines.append(f"{rank_label(i)} {name} — {html.escape(row[3] or 'Без названия')}: "
                         f"{_fmt_row_stats(*row[4:])}")

    return "\n".join(lines)


ADMIN_HELP_TEXT = (
    "<b>🔐 Команды для админа</b>\n\n"
    "Все команды работают только в личке бота.\n\n"
    "<b>стата</b> — общая статистика бота\n\n"
    "<b>стата [название или ID чата]</b> — статистика беседы\n"
    "  <code>стата Мой чат</code>, <code>стата -1001234567890</code>\n\n"
    "<b>топ вся</b> — глобальный топ по всем чатам (◀️ ▶️)\n\n"
    "<b>топ [название или ID чата]</b> — топ пользователей чата\n\n"
    "<b>топ беседы</b> / <b>топ чаты</b> — топ бесед (◀️ ▶️)\n\n"
    "<b>юзер [ID или @username]</b> — статистика пользователя\n\n"
    "<b>адмхелп</b> — это сообщение"
)


# =============================================================================
#                          ХЕНДЛЕРЫ
# =============================================================================

def register():
    def _find_chats(query: str):
        if query.lstrip("-").isdigit():
            chat = database.get_chat_by_id(int(query))
            return [chat] if chat else []
        return database.find_chats_by_name(query)

    def _ask_which_chat(message, chats, query, cb_prefix):
        lines = [f"Найдено несколько чатов по запросу «{html.escape(query)}»:\n"]
        keyboard = types.InlineKeyboardMarkup()
        for chat_id, chat_title, _ in chats:
            title = chat_title or str(chat_id)
            lines.append(f"• {html.escape(title)}")
            # Подпись кнопки — не HTML, экранировать её нельзя
            keyboard.add(types.InlineKeyboardButton(
                title[:64], callback_data=f"{cb_prefix}{chat_id}"))
        safe_reply(message, "\n".join(lines) + "\n\nВыберите чат:", reply_markup=keyboard)

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower() == "адмхелп")
    @admin_handler
    def handle_admin_help(message):
        safe_reply(message, ADMIN_HELP_TEXT)

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower() in ("стата", "статистика"))
    @admin_handler
    def handle_stats(message):
        safe_reply(message, build_stats_report())

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower().startswith(("стата ", "статистика ")))
    @admin_handler
    def handle_stats_chat(message):
        text = message.text.strip()
        query = text.split(maxsplit=1)[1].strip()
        if not query:
            safe_reply(message, "Укажите название или ID чата.")
            return

        chats = _find_chats(query)
        if not chats:
            safe_reply(message, f"❌ Чат «{html.escape(query)}» не найден.")
        elif len(chats) == 1:
            safe_reply(message, _build_chat_stats_text(chats[0][0], chats[0][1]))
        else:
            _ask_which_chat(message, chats, query, "stats_select_")

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower() == "топ вся")
    @admin_handler
    def handle_top_global(message):
        text, keyboard, total = _build_global_page(0)
        sent = safe_send(message.chat.id, text, reply_markup=keyboard)
        if sent:
            _pagination[message.from_user.id] = {
                "mode": "global", "chat_id": None, "chat_title": "",
                "page": 0, "total": total, "message_id": sent.message_id,
            }

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower() in ("топ беседы", "топ чаты"))
    @admin_handler
    def handle_top_chats(message):
        text, keyboard, total = _build_chats_top_page(0)
        sent = safe_send(message.chat.id, text, reply_markup=keyboard)
        if sent:
            _pagination[message.from_user.id] = {
                "mode": "chats_top", "chat_id": None, "chat_title": "",
                "page": 0, "total": total, "message_id": sent.message_id,
            }

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower().startswith("топ ")
                         and (m.text or "").strip().lower()
                         not in ("топ вся", "топ беседы", "топ чаты"))
    @admin_handler
    def handle_top_chat(message):
        query = message.text.strip()[4:].strip()
        if not query:
            safe_reply(message, "Укажите название или ID чата.")
            return

        chats = _find_chats(query)
        if not chats:
            safe_reply(message, f"❌ Чат «{html.escape(query)}» не найден.")
        elif len(chats) == 1:
            _send_chat_top(message.chat.id, message.from_user.id, chats[0][0], chats[0][1])
        else:
            _ask_which_chat(message, chats, query, "top_select_")

    @bot.message_handler(func=lambda m: _is_admin_pm(m)
                         and (m.text or "").strip().lower().startswith("юзер "))
    @admin_handler
    def handle_user_stats(message):
        query = message.text.strip()[5:].strip()
        if not query:
            safe_reply(message, "Укажите ID или @username.")
            return

        user_row = (database.get_user_by_id(int(query))
                    if query.lstrip("-").isdigit()
                    else database.get_user_by_username(query.lstrip("@")))
        if not user_row:
            safe_reply(message, f"❌ Пользователь «{html.escape(query)}» не найден.")
            return

        user_id, username, first_name, last_name, registered_at, last_seen_at = user_row
        chat_stats = database.get_user_stats_all_chats(user_id)
        full_name = html.escape(" ".join(p for p in (first_name or "", last_name or "") if p) or "—")

        lines = [
            f"<b>👤 Пользователь: {build_clickable_name(user_id, username, first_name)}</b>", "",
            f"🆔 ID: <code>{user_id}</code>",
            f"📛 Имя: {full_name}",
            f"🔖 Username: @{html.escape(username)}" if username else "🔖 Username: —",
            f"📅 Зарегистрирован: {registered_at.strftime('%d.%m.%Y %H:%M')}",
            f"🕐 Последняя активность: {last_seen_at.strftime('%d.%m.%Y %H:%M')}",
        ]

        if chat_stats:
            lines += [
                "", "<b>📊 Итого по всем чатам:</b>",
                f"✉️ Сообщений: {sum(r[1] for r in chat_stats)}",
                f"🔠 Символов: {sum(r[2] for r in chat_stats)}",
                "", "<b>📋 По чатам:</b>",
            ]
            for row in chat_stats:
                lines.append(f"• {html.escape(row[0] or '?')}: {_fmt_row_stats(*row[1:])}")
        else:
            lines.append("\nСтатистика не найдена.")

        safe_send(message.chat.id, "\n".join(lines))

    # --- Пагинация ---

    @bot.callback_query_handler(
        func=lambda c: (c.data or "").startswith(("top_", "stats_")))
    def handle_callbacks(call: types.CallbackQuery):
        # Защита в глубину: кнопки живут только в личке админа,
        # но проверить права всё равно стоит.
        if call.from_user.id not in ADMIN_IDS:
            safe_answer_callback(call.id, "⛔ Недостаточно прав.")
            return
        try:
            _dispatch_callback(call)
        except Exception:
            logger.exception("Ошибка в админ-callback.")
            safe_answer_callback(call.id, "⚠️ Ошибка.")

    def _dispatch_callback(call: types.CallbackQuery):
        user_id = call.from_user.id
        data = call.data

        if data == "top_noop":
            safe_answer_callback(call.id)
            return

        if data.startswith("stats_select_"):
            chat = database.get_chat_by_id(int(data.replace("stats_select_", "")))
            if not chat:
                safe_answer_callback(call.id, "Чат не найден.")
                return
            safe_edit(_build_chat_stats_text(chat[0], chat[1]),
                      call.message.chat.id, call.message.message_id)
            safe_answer_callback(call.id)
            return

        if data.startswith("top_select_"):
            chat = database.get_chat_by_id(int(data.replace("top_select_", "")))
            if not chat:
                safe_answer_callback(call.id, "Чат не найден.")
                return
            text, keyboard, total = _build_chat_page(chat[0], chat[1], 0)
            _pagination[user_id] = {
                "mode": "chat", "chat_id": chat[0], "chat_title": chat[1],
                "page": 0, "total": total, "message_id": call.message.message_id,
            }
            safe_edit(text, call.message.chat.id, call.message.message_id,
                      reply_markup=keyboard)
            safe_answer_callback(call.id)
            return

        modes = {
            "top_global": ("global", "«топ вся»"),
            "top_chat":   ("chat",   "«топ [чат]»"),
            "top_chats":  ("chats_top", "«топ беседы»"),
        }
        for prefix, (mode, hint) in modes.items():
            if data in (f"{prefix}_prev", f"{prefix}_next"):
                state = _pagination.get(user_id)
                if (not state or state.get("mode") != mode
                        or state.get("message_id") != call.message.message_id):
                    safe_answer_callback(call.id, f"Начните заново: напишите {hint}")
                    return

                page = _shift_page(user_id, 1 if data.endswith("_next") else -1)
                if mode == "global":
                    text, keyboard, total = _build_global_page(page)
                elif mode == "chats_top":
                    text, keyboard, total = _build_chats_top_page(page)
                else:
                    text, keyboard, total = _build_chat_page(
                        state["chat_id"], state["chat_title"], page)

                state["page"], state["total"] = page, total
                safe_edit(text, call.message.chat.id, call.message.message_id,
                          reply_markup=keyboard)
                safe_answer_callback(call.id)
                return

    def _send_chat_top(target_chat_id: int, user_id: int, chat_id: int, chat_title: str):
        text, keyboard, total = _build_chat_page(chat_id, chat_title, 0)
        sent = safe_send(target_chat_id, text, reply_markup=keyboard)
        if sent:
            _pagination[user_id] = {
                "mode": "chat", "chat_id": chat_id, "chat_title": chat_title,
                "page": 0, "total": total, "message_id": sent.message_id,
            }
