"""
Общие хелперы: парсинг и форматирование времени, упоминания, аптайм,
разбивка длинных сообщений и безопасная отправка в Telegram.

safe_send / safe_reply / safe_edit — единая точка отправки сообщений:
они переживают сетевые сбои, соблюдают rate limit (429) и отличают
временные ошибки от фатальных (бот заблокирован, выкинут из чата).
"""

import html
import re
import time

from telebot.apihelper import ApiTelegramException

from config import bot, logger


START_TIME = time.time()

MAX_MESSAGE_LENGTH = 4000  # запас к лимиту Telegram в 4096

# "1д5ч30м10с" / "1d5h30m10s". Латинская "c" тоже принимается: визуально
# неотличима от кириллической "с", пользователи путают их постоянно.
_TIME_PATTERN = re.compile(
    r"^(?:(\d+)[дd])?(?:(\d+)[чh])?(?:(\d+)[мm])?(?:(\d+)[сsc])?$",
    re.IGNORECASE,
)

_EMOJI_DIGITS = {
    0: "0️⃣", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣",
    5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟",
}


# =============================================================================
# ОГРАНИЧЕННЫЙ СЛОВАРЬ
# =============================================================================

class BoundedDict(dict):
    """Dict с ограничением размера — при переполнении удаляет самые старые записи."""

    __slots__ = ("_maxsize",)

    def __init__(self, maxsize: int = 500):
        super().__init__()
        self._maxsize = maxsize

    def __setitem__(self, key, value):
        if key not in self and len(self) >= self._maxsize:
            del self[next(iter(self))]
        super().__setitem__(key, value)


# =============================================================================
# ВРЕМЯ И ФОРМАТИРОВАНИЕ
# =============================================================================

def rank_label(n: int) -> str:
    """1 → 1️⃣, 10 → 🔟, 23 → 2️⃣3️⃣."""
    if n in _EMOJI_DIGITS:
        return _EMOJI_DIGITS[n]
    return "".join(_EMOJI_DIGITS[int(d)] for d in str(n))


def parse_duration(time_str: str):
    """Парсит "1д5ч30м10с" в секунды. None — если строка некорректна."""
    match = _TIME_PATTERN.match(time_str.strip())
    if not match:
        return None

    days, hours, minutes, seconds = (
        int(group) if group else 0 for group in match.groups()
    )
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def format_duration(seconds: int) -> str:
    """Секунды → "1д 5ч 30м 10с"."""
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
    return f'<a href="tg://user?id={user_id}">{html.escape(first_name or "Пользователь")}</a>'


def build_clickable_name(user_id: int, username, first_name) -> str:
    """Кликабельное имя для топов: @username, иначе имя."""
    display = html.escape(f"@{username}") if username else html.escape(first_name or "Без имени")
    return f'<a href="tg://user?id={user_id}">{display}</a>'


def get_uptime_str() -> str:
    return format_duration(int(time.time() - START_TIME))


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH):
    """Делит текст на части по границам строк; слишком длинную строку режет."""
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

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


def truncate_html(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Обрезает по границе строки — чтобы не разорвать HTML-тег посередине."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 2]
    nl = cut.rfind("\n")
    return (cut[:nl] if nl > 0 else cut) + "\n…"


# =============================================================================
# БЕЗОПАСНАЯ ОТПРАВКА В TELEGRAM
# =============================================================================

class SendForbidden(Exception):
    """Отправка в этот чат невозможна навсегда: кик, блокировка, чат удалён."""


_FATAL_SEND_ERRORS = (
    "bot was kicked",
    "bot was blocked",
    "blocked by the user",
    "chat not found",
    "not a member",
    "bot is not a member",
    "user is deactivated",
    "have no rights",
    "not enough rights",
    "chat_write_forbidden",
    "chat_admin_required",
    "topic_closed",
    "topic_deleted",
    "peer_id_invalid",
    "group chat was upgraded",
)

_RETRY_REPLY_ERRORS = (
    "message to be replied not found",
    "reply message not found",
    "message to reply not found",
)


def _is_fatal(description: str, error_code) -> bool:
    return error_code in (400, 403) and any(k in description for k in _FATAL_SEND_ERRORS)


def _retry_after(exc: ApiTelegramException, default: int = 5) -> int:
    try:
        return int(exc.result_json["parameters"]["retry_after"])
    except Exception:
        return default


def _send_once(chat_id, text, attempts=3, **kwargs):
    """Одна логическая отправка с ретраями. None — временно не удалось."""
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except ApiTelegramException as e:
            desc = (e.description or "").lower()
            if e.error_code == 429:
                wait = _retry_after(e)
                logger.warning("Rate limit для chat_id=%s, пауза %s сек.", chat_id, wait)
                time.sleep(wait + 1)
                continue
            if any(k in desc for k in _RETRY_REPLY_ERRORS):
                # Сообщение, на которое отвечаем, удалили — шлём без реплая
                kwargs.pop("reply_to_message_id", None)
                continue
            if _is_fatal(desc, e.error_code):
                raise SendForbidden(desc)
            logger.warning(
                "Telegram API (попытка %d/%d, chat=%s): %s", attempt, attempts, chat_id, e
            )
        except Exception:
            logger.exception(
                "Сетевая ошибка при отправке (попытка %d/%d, chat=%s)",
                attempt, attempts, chat_id,
            )
        if attempt < attempts:
            time.sleep(delay)
            delay *= 2
    return None


def safe_send(chat_id, text, **kwargs):
    """
    Отправляет сообщение, при необходимости разбивая на части.
    Возвращает последнее отправленное сообщение или None.
    Бросает SendForbidden, если чат недоступен окончательно.
    """
    chunks = split_message(text)
    markup = kwargs.pop("reply_markup", None)
    last = None
    for i, chunk in enumerate(chunks):
        extra = dict(kwargs)
        if i > 0:
            extra.pop("reply_to_message_id", None)
        if i == len(chunks) - 1 and markup is not None:
            extra["reply_markup"] = markup
        last = _send_once(chat_id, chunk, **extra)
    return last


def safe_reply(message, text, **kwargs):
    """Ответ на сообщение с сохранением топика форума."""
    return safe_send(
        message.chat.id,
        text,
        reply_to_message_id=message.message_id,
        message_thread_id=getattr(message, "message_thread_id", None),
        **kwargs,
    )


def safe_edit(text, chat_id, message_id, **kwargs) -> bool:
    """Редактирует сообщение. True — успех. "not modified" не считается ошибкой."""
    try:
        bot.edit_message_text(
            truncate_html(text),
            chat_id=chat_id,
            message_id=message_id,
            parse_mode="HTML",
            **kwargs,
        )
        return True
    except ApiTelegramException as e:
        desc = (e.description or "").lower()
        if "message is not modified" in desc:
            return True
        if e.error_code == 429:
            time.sleep(_retry_after(e) + 1)
            return safe_edit(text, chat_id, message_id, **kwargs)
        logger.warning("Не удалось отредактировать сообщение %s: %s", message_id, e)
    except Exception:
        logger.exception("Сетевая ошибка при редактировании сообщения %s", message_id)
    return False


def safe_answer_callback(call_id, text=None, show_alert=False):
    try:
        bot.answer_callback_query(call_id, text, show_alert=show_alert)
    except Exception:
        logger.debug("Не удалось ответить на callback %s", call_id)
