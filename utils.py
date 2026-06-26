"""
Общие хелперы, используемые разными частями бота: парсинг и форматирование
времени, упоминания пользователей, аптайм бота, разбивка длинных сообщений.
"""

import html
import re
import time


START_TIME = time.time()

# "1д5ч30м10с" или "1d5h30m10s" -> компоненты, все группы опциональны
_TIME_PATTERN = re.compile(
    r"^(?:(\d+)[дd])?(?:(\d+)[чh])?(?:(\d+)[мm])?(?:(\d+)[сs])?$",
    re.IGNORECASE,
)

# Эмодзи-цифры для топов (1-10, дальше в формате "11." и т.д.)
_EMOJI_DIGITS = {
    1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
    6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟",
}


def rank_label(n: int) -> str:
    """Возвращает эмодзи-цифру для позиции n (1-10) или 'N.' для остальных."""
    return _EMOJI_DIGITS.get(n, f"{n}.")


def parse_duration(time_str: str):
    """Парсит "1д5ч30м10с" и т.п. в секунды. None — если строка некорректна."""
    match = _TIME_PATTERN.match(time_str.strip())
    if not match:
        return None

    days, hours, minutes, seconds = (
        int(group) if group else 0 for group in match.groups()
    )
    total_seconds = days * 86400 + hours * 3600 + minutes * 60 + seconds

    if total_seconds <= 0:
        return None

    return total_seconds


def format_duration(seconds: int) -> str:
    """
    Преобразует количество секунд в человекочитаемую строку,
    например: "1д 5ч 30м 10с".
    """
    seconds = int(seconds)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    if secs or not parts:
        parts.append(f"{secs}с")

    return " ".join(parts)


def build_mention(user_id: int, first_name: str) -> str:
    """HTML-ссылка tg://user?id=... — тегает пользователя даже без username."""
    display_name = html.escape(first_name or "Пользователь")
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'


def build_clickable_name(user_id: int, username: str | None, first_name: str | None) -> str:
    """
    Кликабельное имя пользователя для топов.
    Если есть username — показываем @username как ссылку.
    Если нет — показываем first_name как ссылку через tg://user?id=.
    В любом случае ссылка кликабельна и открывает профиль.
    """
    if username:
        display = html.escape(f"@{username}")
    else:
        display = html.escape(first_name or "Без имени")
    return f'<a href="tg://user?id={user_id}">{display}</a>'


def get_uptime_str() -> str:
    """Аптайм бота с момента запуска, например "1д 5ч 30м 10с"."""
    uptime_seconds = int(time.time() - START_TIME)
    days, remainder = divmod(uptime_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes:
        parts.append(f"{minutes}м")
    parts.append(f"{seconds}с")

    return " ".join(parts)


def split_message(text: str, limit: int = 4000):
    """Делит длинный текст на части не длиннее limit символов по границам строк."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def build_stats_report() -> str:
    """Формирует текст отчёта /стата: сводка + топ-5 по активности (только из бесед)."""
    import database  # импорт здесь чтобы избежать циклических зависимостей

    total_users, _, totals = database.get_stats_overview()
    group_count, private_count = database.get_chats_count_by_type()
    # Топ-5 только из групп (chat_type != 'private')
    top_rows = database.get_top_activity_groups(limit=5)

    lines = [
        "<b>📊 Общая статистика</b>",
        "",
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

    if top_rows:
        lines.append("")
        lines.append("<b>🏆 Топ-5 активных (беседы)</b>")
        for i, row in enumerate(top_rows, start=1):
            user_id, username, first_name, chat_title, \
                messages, chars, stickers, photos, videos, voice, gifs, forwards = row

            name = build_clickable_name(user_id, username, first_name)
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
            if forwards:
                extra_parts.append(f"пересланных {forwards}")
            extra = f" ({', '.join(extra_parts)})" if extra_parts else ""

            lines.append(
                f"{rank_label(i)} {name} — {chat_label}: "
                f"{messages} сообщений, {chars} символов{extra}"
            )

    return "\n".join(lines)
