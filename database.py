"""
Всё, что связано с базой данных (PostgreSQL).

Этот модуль не зависит от telebot — принимает и возвращает только простые
типы (числа, строки, кортежи), чтобы main.py занимался Telegram-логикой,
а этот файл — только хранением данных.

Если DATABASE_URL не задан (или не установлен psycopg2), все функции
работают как no-op — бот продолжает работать только в памяти.

Подключения к БД управляются через пул (ThreadedConnectionPool):
  - при старте создаётся 1 соединение, максимум 5 одновременных
  - каждая функция берёт соединение из пула и возвращает обратно
  - это исключает накладные расходы на установку нового TCP-соединения
    при каждом запросе и делает работу с БД быстрее и стабильнее
"""

import time

from config import DATABASE_URL, logger

try:
    import psycopg2
    from psycopg2 import pool as psycopg2_pool
except ImportError:
    psycopg2 = None
    psycopg2_pool = None


# =============================================================================
#                          ПУЛ СОЕДИНЕНИЙ
# =============================================================================

# Глобальный пул: инициализируется один раз в init_db().
# min=1 — одно соединение всегда держится открытым (нет cold start на Neon).
# max=5 — не более 5 одновременных соединений (хватает для фоновых потоков
#          статистики + основного потока polling + таймеров).
_pool = None


def db_enabled() -> bool:
    """True, если задан DATABASE_URL и установлен psycopg2."""
    return bool(DATABASE_URL) and psycopg2 is not None


def _init_pool():
    """
    Создаёт пул соединений. Если Neon ещё "спит" после паузы —
    повторяет попытку до 5 раз с паузой 3 секунды.
    """
    global _pool

    for attempt in range(1, 6):
        try:
            _pool = psycopg2_pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=5,
                dsn=DATABASE_URL,
            )
            logger.info("Пул соединений с БД создан.")
            return
        except Exception as e:
            logger.warning(
                "Попытка %s/5 подключиться к БД не удалась: %s. "
                "Повтор через 3 секунды...", attempt, e,
            )
            time.sleep(3)

    logger.error(
        "Не удалось подключиться к БД после 5 попыток. "
        "Бот продолжит работу без базы данных."
    )


def _get_conn():
    """Берёт соединение из пула."""
    return _pool.getconn()


def _put_conn(conn):
    """Возвращает соединение в пул."""
    _pool.putconn(conn)


# =============================================================================
#                          ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ
# =============================================================================

def init_db():
    """Инициализирует пул и создаёт все необходимые таблицы."""
    if not db_enabled():
        logger.warning(
            "DATABASE_URL не задан — таймеры и статистика не будут "
            "сохраняться между перезапусками."
        )
        return

    _init_pool()

    if _pool is None:
        return

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS timers (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT NOT NULL,
                        user_id BIGINT NOT NULL,
                        user_first_name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        end_time DOUBLE PRECISION NOT NULL
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id BIGINT PRIMARY KEY,
                        chat_type TEXT NOT NULL,
                        title TEXT
                    )
                    """
                )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_chat_stats (
                        user_id BIGINT NOT NULL REFERENCES users(user_id),
                        chat_id BIGINT NOT NULL REFERENCES chats(chat_id),
                        messages_count BIGINT NOT NULL DEFAULT 0,
                        chars_count BIGINT NOT NULL DEFAULT 0,
                        stickers_count BIGINT NOT NULL DEFAULT 0,
                        photos_count BIGINT NOT NULL DEFAULT 0,
                        videos_count BIGINT NOT NULL DEFAULT 0,
                        voice_count BIGINT NOT NULL DEFAULT 0,
                        gifs_count BIGINT NOT NULL DEFAULT 0,
                        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (user_id, chat_id)
                    )
                    """
                )
    finally:
        _put_conn(conn)


# =============================================================================
#                          ТАЙМЕРЫ
# =============================================================================

def insert_timer(chat_id, user_id, first_name, description, end_time):
    """Сохраняет таймер в базу и возвращает его ID (или None без БД)."""
    if not db_enabled() or _pool is None:
        return None

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO timers (chat_id, user_id, user_first_name, description, end_time) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (chat_id, user_id, first_name, description, end_time),
                )
                return cur.fetchone()[0]
    finally:
        _put_conn(conn)


def delete_timer(timer_id):
    """Удаляет таймер из базы."""
    if not db_enabled() or _pool is None:
        return

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM timers WHERE id = %s", (timer_id,))
    finally:
        _put_conn(conn)


def load_all_timers():
    """Возвращает все сохранённые таймеры: (id, chat_id, user_id, first_name, description, end_time)."""
    if not db_enabled() or _pool is None:
        return []

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, chat_id, user_id, user_first_name, description, end_time "
                    "FROM timers"
                )
                return cur.fetchall()
    finally:
        _put_conn(conn)


# =============================================================================
#                          СТАТИСТИКА
# =============================================================================

_UPSERT_USER_SQL = """
    INSERT INTO users (user_id, username, first_name, last_name, registered_at, last_seen_at)
    VALUES (%s, %s, %s, %s, now(), now())
    ON CONFLICT (user_id) DO UPDATE SET
        username = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name,
        last_seen_at = now()
"""

_UPSERT_CHAT_SQL = """
    INSERT INTO chats (chat_id, chat_type, title)
    VALUES (%s, %s, %s)
    ON CONFLICT (chat_id) DO UPDATE SET
        chat_type = EXCLUDED.chat_type,
        title = EXCLUDED.title
"""

_UPSERT_STATS_SQL = """
    INSERT INTO user_chat_stats (
        user_id, chat_id, messages_count, chars_count, stickers_count,
        photos_count, videos_count, voice_count, gifs_count, last_seen_at
    )
    VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (user_id, chat_id) DO UPDATE SET
        messages_count = user_chat_stats.messages_count + 1,
        chars_count = user_chat_stats.chars_count + EXCLUDED.chars_count,
        stickers_count = user_chat_stats.stickers_count + EXCLUDED.stickers_count,
        photos_count = user_chat_stats.photos_count + EXCLUDED.photos_count,
        videos_count = user_chat_stats.videos_count + EXCLUDED.videos_count,
        voice_count = user_chat_stats.voice_count + EXCLUDED.voice_count,
        gifs_count = user_chat_stats.gifs_count + EXCLUDED.gifs_count,
        last_seen_at = now()
"""


def record_message_stats(
    user_id, username, first_name, last_name,
    chat_id, chat_type, chat_title,
    chars, stickers, photos, videos, voice, gifs,
):
    """Обновляет данные пользователя, чата и счётчики по одному сообщению."""
    if not db_enabled() or _pool is None:
        return

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_UPSERT_USER_SQL, (user_id, username, first_name, last_name))
                cur.execute(_UPSERT_CHAT_SQL, (chat_id, chat_type, chat_title))
                cur.execute(
                    _UPSERT_STATS_SQL,
                    (user_id, chat_id, chars, stickers, photos, videos, voice, gifs),
                )
    finally:
        _put_conn(conn)


def get_stats_overview():
    """Возвращает (total_users, total_chats, totals_dict) с суммарными счётчиками."""
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                total_users = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM chats")
                total_chats = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT
                        COALESCE(SUM(messages_count), 0),
                        COALESCE(SUM(chars_count), 0),
                        COALESCE(SUM(stickers_count), 0),
                        COALESCE(SUM(photos_count), 0),
                        COALESCE(SUM(videos_count), 0),
                        COALESCE(SUM(voice_count), 0),
                        COALESCE(SUM(gifs_count), 0)
                    FROM user_chat_stats
                    """
                )
                (
                    messages, chars, stickers,
                    photos, videos, voice, gifs,
                ) = cur.fetchone()
    finally:
        _put_conn(conn)

    totals = {
        "messages": messages,
        "chars": chars,
        "stickers": stickers,
        "photos": photos,
        "videos": videos,
        "voice": voice,
        "gifs": gifs,
    }
    return total_users, total_chats, totals


def get_top_activity(limit=10):
    """
    Возвращает топ записей (пользователь, чат) по количеству сообщений:
    (username, first_name, chat_title, messages, chars, stickers, photos, videos, voice, gifs)
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.username, u.first_name, c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count, s.gifs_count
                    FROM user_chat_stats s
                    JOIN users u ON u.user_id = s.user_id
                    JOIN chats c ON c.chat_id = s.chat_id
                    ORDER BY s.messages_count DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()
    finally:
        _put_conn(conn)