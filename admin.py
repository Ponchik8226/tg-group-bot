"""
Админ-команды бота. Все команды работают ТОЛЬКО в личке бота
и ТОЛЬКО для пользователей из ADMIN_IDS.

Команды:
  адмхелп                      — список всех админ-команд
  стата / статистика            — общая статистика бота
  стата [название/id]           — статистика конкретной беседы
  топ вся                       — глобальный топ по всем чатам (пагинация ◀️▶️)
  топ [название/id]             — топ пользователей в конкретном чате (пагинация ◀️▶️)
  топ беседы / топ чаты         — топ бесед по активности (пагинация ◀️▶️)
  юзер [id/@username]           — детальная статистика пользователя
"""

import html
import math

from telebot import types

import database
from config import bot, logger, ADMIN_IDS
from utils import split_message, build_stats_report, build_clickable_name, rank_label


# =============================================================================
#                          ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

PAGE_SIZE = 10


def _is_admin_in_pm(message: types.Message) -> bool:
    """Возвращает True если сообщение из лички и отправитель — админ."""
    return (
        message.chat.type == "private"
        and message.from_user.id in ADMIN_IDS
    )


def _fmt_user(user_id: int, username, first_name) -> str:
    """Кликабельное имя пользователя через build_clickable_name из utils."""
    return build_clickable_name(user_id, username, first_name)


def _fmt_row_stats(messages, chars, stickers, photos, videos, voice, gifs, forwards) -> str:
    """Форматирует счётчики в читаемую строку без странных точек-запятых."""
    parts = [f"{messages} сообщений", f"{chars} символов"]
    if stickers:
        parts.append(f"стикеров {stickers}")
    if photos:
        parts.append(f"фото {photos}")
    if videos:
        parts.append(f"видео {videos}")
    if voice:
        parts.append(f"голосовых {voice}")
    if gifs:
        parts.append(f"gif {gifs}")
    if forwards:
        parts.append(f"пересланных {forwards}")
    return ", ".join(parts)


# =============================================================================
#                          СОСТОЯНИЕ ПАГИНАЦИИ (в памяти)
# =============================================================================

# {user_id: {"mode": "global"|"chat"|"chats_top", "chat_id": int|None,
#             "chat_title": str, "page": int, "total": int}}
_pagination = {}


def _total_pages(total: int) -> int:
    return max(1, math.ceil(total / PAGE_SIZE))


def _get_page(user_id: int, direction: int) -> int:
    """Кольцевая навигация: +1 вперёд, -1 назад."""
    state = _pagination.get(user_id, {})
    current = state.get("page", 0)
    total = _total_pages(state.get("total", 0))
    return (current + direction) % total


# =============================================================================
#                          ПОСТРОИТЕЛИ СТРАНИЦ
# =============================================================================

def _build_global_page(page: int):
    """Страница глобального топа. Возвращает (text, keyboard, total)."""
    offset = page * PAGE_SIZE
    rows, total = database.get_global_top_page(offset, PAGE_SIZE)
    pages = _total_pages(total)

    lines = [f"<b>🌍 Глобальный топ — стр. {page + 1}/{pages}</b>", ""]
    for i, row in enumerate(rows, start=offset + 1):
        user_id, username, first_name, chat_title = row[0], row[1], row[2], row[3]
        stats = row[4:]
        name = _fmt_user(user_id, username, first_name)
        chat = html.escape(chat_title or "?")
        lines.append(f"{rank_label(i)} {name} ({chat}) — {_fmt_row_stats(*stats)}")

    if not rows:
        lines.append("Нет данных.")

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("◀️", callback_data="top_global_prev"),
        types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="top_noop"),
        types.InlineKeyboardButton("▶️", callback_data="top_global_next"),
    )
    return "\n".join(lines), keyboard, total


def _build_chat_page(chat_id: int, chat_title: str, page: int):
    """Страница топа по конкретному чату. Возвращает (text, keyboard, total)."""
    offset = page * PAGE_SIZE
    rows, total = database.get_chat_top_page(chat_id, offset, PAGE_SIZE)
    pages = _total_pages(total)

    title_escaped = html.escape(chat_title or str(chat_id))
    lines = [f"<b>💬 Топ «{title_escaped}» — стр. {page + 1}/{pages}</b>", ""]
    for i, row in enumerate(rows, start=offset + 1):
        user_id, username, first_name = row[0], row[1], row[2]
        stats = row[3:]
        name = _fmt_user(user_id, username, first_name)
        lines.append(f"{rank_label(i)} {name} — {_fmt_row_stats(*stats)}")

    if not rows:
        lines.append("Нет данных.")

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("◀️", callback_data="top_chat_prev"),
        types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="top_noop"),
        types.InlineKeyboardButton("▶️", callback_data="top_chat_next"),
    )
    return "\n".join(lines), keyboard, total


def _build_chats_top_page(page: int):
    """Страница топа бесед. Возвращает (text, keyboard, total)."""
    offset = page * PAGE_SIZE
    rows, total = database.get_chats_top_page(offset, PAGE_SIZE)
    pages = _total_pages(total)

    lines = [f"<b>🏆 Топ бесед — стр. {page + 1}/{pages}</b>", ""]
    for i, row in enumerate(rows, start=offset + 1):
        _, title, messages, chars, stickers, photos, videos, voice, gifs, forwards = row
        title_e = html.escape(title or "Без названия")
        stats = _fmt_row_stats(messages, chars, stickers, photos, videos, voice, gifs, forwards)
        lines.append(f"{rank_label(i)} {title_e} — {stats}")

    if not rows:
        lines.append("Нет данных.")

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton("◀️", callback_data="top_chats_prev"),
        types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="top_noop"),
        types.InlineKeyboardButton("▶️", callback_data="top_chats_next"),
    )
    return "\n".join(lines), keyboard, total


# =============================================================================
#                          ТЕКСТЫ
# =============================================================================

ADMIN_HELP_TEXT = (
    "<b>🔐 Команды для админа</b>\n\n"
    "Все команды работают только в личке бота.\n\n"

    "<b>стата</b> — общая статистика бота\n\n"

    "<b>стата [название или ID чата]</b> — статистика конкретной беседы\n"
    "  Примеры: <code>стата Мой чат</code>, <code>стата -1001234567890</code>\n\n"

    "<b>топ вся</b> — глобальный топ по всем чатам\n"
    "  листается кнопками ◀️ ▶️\n\n"

    "<b>топ [название или ID чата]</b> — топ пользователей в конкретном чате\n"
    "  Примеры: <code>топ Мой чат</code>, <code>топ -1001234567890</code>\n\n"

    "<b>топ беседы</b> / <b>топ чаты</b> — топ бесед по активности (без ЛС)\n"
    "  листается кнопками ◀️ ▶️\n\n"

    "<b>юзер [ID или @username]</b> — детальная статистика пользователя\n"
    "  Примеры: <code>юзер 123456789</code>, <code>юзер @nickname</code>\n\n"

    "<b>адмхелп</b> — это сообщение"
)


# =============================================================================
#                          ХЕНДЛЕРЫ
# =============================================================================

def register(bot_username: str):
    """Регистрирует все хендлеры админ-команд."""

    @bot.message_handler(
        func=lambda m: (m.text or "").strip().lower() == "адмхелп"
    )
    def handle_admin_help(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        bot.send_message(message.chat.id, ADMIN_HELP_TEXT)

    # --- стата (без аргументов) ---
    @bot.message_handler(
        func=lambda m: (m.text or "").strip().lower() in ("стата", "статистика")
    )
    def handle_stats(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        if not database.db_enabled():
            bot.reply_to(message, "⚠️ База данных не настроена.")
            return
        report = build_stats_report()
        for chunk in split_message(report):
            bot.send_message(message.chat.id, chunk)

    # --- стата [чат] ---
    @bot.message_handler(
        func=lambda m: bool(
            (m.text or "").strip().lower().startswith(("стата ", "статистика "))
        )
    )
    def handle_stats_chat(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        if not database.db_enabled():
            bot.reply_to(message, "⚠️ База данных не настроена.")
            return

        # Убираем команду, берём аргумент
        text = message.text.strip()
        if text.lower().startswith("статистика "):
            query = text[11:].strip()
        else:
            query = text[6:].strip()

        if not query:
            bot.reply_to(
                message,
                "Укажите название или ID чата.\n"
                "Пример: <code>стата Мой чат</code>",
            )
            return

        # Ищем чат
        if query.lstrip("-").isdigit():
            chat = database.get_chat_by_id(int(query))
            if not chat:
                bot.reply_to(message, f"❌ Чат с ID <code>{query}</code> не найден.")
                return
            chats = [chat]
        else:
            chats = database.find_chats_by_name(query)
            if not chats:
                bot.reply_to(message, f"❌ Чаты с названием «{html.escape(query)}» не найдены.")
                return

        if len(chats) == 1:
            chat_id, chat_title, _ = chats[0]
            _send_chat_stats(message.chat.id, chat_id, chat_title)
            return

        # Несколько совпадений — предлагаем выбор
        lines = [f"Найдено несколько чатов по запросу «{html.escape(query)}»:\n"]
        keyboard = types.InlineKeyboardMarkup()
        for chat_id, chat_title, _ in chats:
            label = html.escape(chat_title or str(chat_id))
            lines.append(f"• {label}")
            keyboard.add(
                types.InlineKeyboardButton(
                    label, callback_data=f"stats_select_{chat_id}"
                )
            )
        bot.send_message(
            message.chat.id,
            "\n".join(lines) + "\n\nВыберите чат:",
            reply_markup=keyboard,
        )

    # --- топ вся ---
    @bot.message_handler(
        func=lambda m: (m.text or "").strip().lower() == "топ вся"
    )
    def handle_top_global(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        if not database.db_enabled():
            bot.reply_to(message, "⚠️ База данных не настроена.")
            return
        page = 0
        text, keyboard, total = _build_global_page(page)
        _pagination[message.from_user.id] = {
            "mode": "global", "chat_id": None,
            "chat_title": "", "page": page, "total": total,
        }
        bot.send_message(message.chat.id, text, reply_markup=keyboard)

    # --- топ беседы / топ чаты ---
    @bot.message_handler(
        func=lambda m: (m.text or "").strip().lower() in ("топ беседы", "топ чаты")
    )
    def handle_top_chats(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        if not database.db_enabled():
            bot.reply_to(message, "⚠️ База данных не настроена.")
            return
        page = 0
        text, keyboard, total = _build_chats_top_page(page)
        _pagination[message.from_user.id] = {
            "mode": "chats_top", "chat_id": None,
            "chat_title": "", "page": page, "total": total,
        }
        bot.send_message(message.chat.id, text, reply_markup=keyboard)

    # --- топ [чат] ---
    @bot.message_handler(
        func=lambda m: bool(
            (m.text or "").strip().lower().startswith("топ ")
            and (m.text or "").strip().lower() not in ("топ вся", "топ беседы", "топ чаты")
        )
    )
    def handle_top_chat(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        if not database.db_enabled():
            bot.reply_to(message, "⚠️ База данных не настроена.")
            return

        query = message.text.strip()[4:].strip()
        if not query:
            bot.reply_to(
                message,
                "Укажите название или ID чата.\n"
                "Пример: <code>топ Мой чат</code>",
            )
            return

        if query.lstrip("-").isdigit():
            chat = database.get_chat_by_id(int(query))
            if not chat:
                bot.reply_to(message, f"❌ Чат с ID <code>{query}</code> не найден.")
                return
            chats = [chat]
        else:
            chats = database.find_chats_by_name(query)
            if not chats:
                bot.reply_to(message, f"❌ Чаты с названием «{html.escape(query)}» не найдены.")
                return

        if len(chats) == 1:
            chat_id, chat_title, _ = chats[0]
            _send_chat_top(message.chat.id, message.from_user.id, chat_id, chat_title)
            return

        lines = [f"Найдено несколько чатов по запросу «{html.escape(query)}»:\n"]
        keyboard = types.InlineKeyboardMarkup()
        for chat_id, chat_title, _ in chats:
            label = html.escape(chat_title or str(chat_id))
            lines.append(f"• {label}")
            keyboard.add(
                types.InlineKeyboardButton(
                    label, callback_data=f"top_select_{chat_id}"
                )
            )
        bot.send_message(
            message.chat.id,
            "\n".join(lines) + "\n\nВыберите чат:",
            reply_markup=keyboard,
        )

    # --- юзер [id/@username] ---
    @bot.message_handler(
        func=lambda m: bool((m.text or "").strip().lower().startswith("юзер "))
    )
    def handle_user_stats(message: types.Message):
        if not _is_admin_in_pm(message):
            return
        if not database.db_enabled():
            bot.reply_to(message, "⚠️ База данных не настроена.")
            return

        query = message.text.strip()[5:].strip()
        if not query:
            bot.reply_to(
                message,
                "Укажите ID или @username.\n"
                "Примеры: <code>юзер 123456789</code>, <code>юзер @nickname</code>",
            )
            return

        if query.lstrip("-").isdigit():
            user_row = database.get_user_by_id(int(query))
        else:
            user_row = database.get_user_by_username(query.lstrip("@"))

        if not user_row:
            bot.reply_to(message, f"❌ Пользователь «{html.escape(query)}» не найден в базе.")
            return

        user_id, username, first_name, last_name, registered_at, last_seen_at = user_row
        chat_stats = database.get_user_stats_all_chats(user_id)

        display = build_clickable_name(user_id, username, first_name)
        full_name_parts = [first_name or "", last_name or ""]
        full_name = html.escape(" ".join(p for p in full_name_parts if p).strip() or "—")

        lines = [
            f"<b>👤 Пользователь: {display}</b>",
            "",
            f"🆔 ID: <code>{user_id}</code>",
            f"📛 Имя: {full_name}",
            f"🔖 Username: @{html.escape(username)}" if username else "🔖 Username: —",
            f"📅 Зарегистрирован: {registered_at.strftime('%d.%m.%Y %H:%M')}",
            f"🕐 Последняя активность: {last_seen_at.strftime('%d.%m.%Y %H:%M')}",
        ]

        if chat_stats:
            total_msgs = sum(r[1] for r in chat_stats)
            total_chars = sum(r[2] for r in chat_stats)
            lines += [
                "",
                "<b>📊 Итого по всем чатам:</b>",
                f"✉️ Сообщений: {total_msgs}",
                f"🔠 Символов: {total_chars}",
                "",
                "<b>📋 По чатам:</b>",
            ]
            for row in chat_stats:
                chat_title = html.escape(row[0] or "?")
                stats_str = _fmt_row_stats(*row[1:])
                lines.append(f"• {chat_title}: {stats_str}")
        else:
            lines.append("\nСтатистика не найдена.")

        for chunk in split_message("\n".join(lines)):
            bot.send_message(message.chat.id, chunk)

    # --- Callback-хендлеры для пагинации ---

    @bot.callback_query_handler(func=lambda c: c.data.startswith("top_") or c.data.startswith("stats_"))
    def handle_callbacks(call: types.CallbackQuery):
        user_id = call.from_user.id

        if call.data == "top_noop":
            bot.answer_callback_query(call.id)
            return

        # Выбор чата для стата [чат]
        if call.data.startswith("stats_select_"):
            chat_id = int(call.data.replace("stats_select_", ""))
            chat = database.get_chat_by_id(chat_id)
            if not chat:
                bot.answer_callback_query(call.id, "Чат не найден.")
                return
            _, chat_title, _ = chat
            bot.edit_message_text(
                _build_chat_stats_text(chat_id, chat_title),
                call.message.chat.id,
                call.message.message_id,
            )
            bot.answer_callback_query(call.id)
            return

        # Выбор чата для топ [чат]
        if call.data.startswith("top_select_"):
            chat_id = int(call.data.replace("top_select_", ""))
            chat = database.get_chat_by_id(chat_id)
            if not chat:
                bot.answer_callback_query(call.id, "Чат не найден.")
                return
            _, chat_title, _ = chat
            page = 0
            text, keyboard, total = _build_chat_page(chat_id, chat_title, page)
            _pagination[user_id] = {
                "mode": "chat", "chat_id": chat_id,
                "chat_title": chat_title, "page": page, "total": total,
            }
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=keyboard,
            )
            bot.answer_callback_query(call.id)
            return

        # Глобальный топ
        if call.data in ("top_global_prev", "top_global_next"):
            state = _pagination.get(user_id)
            if not state or state.get("mode") != "global":
                bot.answer_callback_query(call.id, "Начните заново: напишите «топ вся»")
                return
            direction = 1 if call.data == "top_global_next" else -1
            page = _get_page(user_id, direction)
            text, keyboard, total = _build_global_page(page)
            state["page"] = page
            state["total"] = total
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=keyboard,
            )
            bot.answer_callback_query(call.id)
            return

        # Топ по чату
        if call.data in ("top_chat_prev", "top_chat_next"):
            state = _pagination.get(user_id)
            if not state or state.get("mode") != "chat":
                bot.answer_callback_query(call.id, "Начните заново: напишите «топ [чат]»")
                return
            direction = 1 if call.data == "top_chat_next" else -1
            page = _get_page(user_id, direction)
            chat_id = state["chat_id"]
            chat_title = state["chat_title"]
            text, keyboard, total = _build_chat_page(chat_id, chat_title, page)
            state["page"] = page
            state["total"] = total
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=keyboard,
            )
            bot.answer_callback_query(call.id)
            return

        # Топ бесед
        if call.data in ("top_chats_prev", "top_chats_next"):
            state = _pagination.get(user_id)
            if not state or state.get("mode") != "chats_top":
                bot.answer_callback_query(call.id, "Начните заново: напишите «топ беседы»")
                return
            direction = 1 if call.data == "top_chats_next" else -1
            page = _get_page(user_id, direction)
            text, keyboard, total = _build_chats_top_page(page)
            state["page"] = page
            state["total"] = total
            bot.edit_message_text(
                text, call.message.chat.id, call.message.message_id,
                reply_markup=keyboard,
            )
            bot.answer_callback_query(call.id)
            return

    # --- Вспомогательные функции (определены внутри register чтобы видеть bot) ---

    def _send_chat_top(chat_id_to_send: int, user_id: int, chat_id: int, chat_title: str):
        page = 0
        text, keyboard, total = _build_chat_page(chat_id, chat_title, page)
        _pagination[user_id] = {
            "mode": "chat", "chat_id": chat_id,
            "chat_title": chat_title, "page": page, "total": total,
        }
        bot.send_message(chat_id_to_send, text, reply_markup=keyboard)

    def _send_chat_stats(chat_id_to_send: int, chat_id: int, chat_title: str):
        text = _build_chat_stats_text(chat_id, chat_title)
        bot.send_message(chat_id_to_send, text)


def _build_chat_stats_text(chat_id: int, chat_title: str) -> str:
    """Формирует текст статистики конкретного чата + топ-5 участников."""
    row = database.get_chat_stats(chat_id)
    if not row:
        return f"❌ Нет данных по чату «{html.escape(chat_title or str(chat_id))}»."

    participants, messages, chars, stickers, photos, videos, voice, gifs, forwards = row
    title_e = html.escape(chat_title or str(chat_id))

    lines = [
        f"<b>📊 Статистика «{title_e}»</b>",
        "",
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

    # Топ-5 участников этой беседы
    top_rows, _ = database.get_chat_top_page(chat_id, 0, 5)
    if top_rows:
        lines.append("")
        lines.append("<b>🏆 Топ-5 участников</b>")
        for i, top_row in enumerate(top_rows, start=1):
            user_id, username, first_name = top_row[0], top_row[1], top_row[2]
            stats = top_row[3:]
            name = build_clickable_name(user_id, username, first_name)
            lines.append(f"{rank_label(i)} {name} — {_fmt_row_stats(*stats)}")

    return "\n".join(lines)
