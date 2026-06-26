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
                        forwards_count BIGINT NOT NULL DEFAULT 0,
                        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (user_id, chat_id)
                    )
                    """
                )

                # Добавляем колонку в уже существующую таблицу если её нет.
                # IF NOT EXISTS для ALTER COLUMN появился в Postgres 9.6+,
                # на Neon это поддерживается.
                cur.execute(
                    """
                    ALTER TABLE user_chat_stats
                    ADD COLUMN IF NOT EXISTS forwards_count BIGINT NOT NULL DEFAULT 0
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
        photos_count, videos_count, voice_count, gifs_count, forwards_count, last_seen_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
    ON CONFLICT (user_id, chat_id) DO UPDATE SET
        messages_count = user_chat_stats.messages_count + EXCLUDED.messages_count,
        chars_count = user_chat_stats.chars_count + EXCLUDED.chars_count,
        stickers_count = user_chat_stats.stickers_count + EXCLUDED.stickers_count,
        photos_count = user_chat_stats.photos_count + EXCLUDED.photos_count,
        videos_count = user_chat_stats.videos_count + EXCLUDED.videos_count,
        voice_count = user_chat_stats.voice_count + EXCLUDED.voice_count,
        gifs_count = user_chat_stats.gifs_count + EXCLUDED.gifs_count,
        forwards_count = user_chat_stats.forwards_count + EXCLUDED.forwards_count,
        last_seen_at = now()
"""


def record_message_stats(
    user_id, username, first_name, last_name,
    chat_id, chat_type, chat_title,
    messages, chars, stickers, photos, videos, voice, gifs, forwards,
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
                    (user_id, chat_id, messages, chars, stickers,
                     photos, videos, voice, gifs, forwards),
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
                        COALESCE(SUM(gifs_count), 0),
                        COALESCE(SUM(forwards_count), 0)
                    FROM user_chat_stats
                    """
                )
                (
                    messages, chars, stickers,
                    photos, videos, voice, gifs, forwards,
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
        "forwards": forwards,
    }
    return total_users, total_chats, totals


def get_top_activity(limit=10):
    """
    Возвращает топ записей (пользователь, чат) по количеству сообщений:
    (username, first_name, chat_title, messages, chars, stickers, photos, videos, voice, gifs, forwards)
    """
    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.username, u.first_name, c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count, s.gifs_count,
                           s.forwards_count
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


# =============================================================================
#              НОВЫЕ ЗАПРОСЫ ДЛЯ АДМИН-КОМАНД
# =============================================================================

def get_global_top_page(offset: int, limit: int = 10):
    """
    Глобальный топ по всем чатам с пагинацией.
    Возвращает (rows, total_count).
    rows: (user_id, username, first_name, chat_title, messages, chars,
           stickers, photos, videos, voice, gifs, forwards)
    """
    if not db_enabled() or _pool is None:
        return [], 0

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM user_chat_stats")
                total = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT u.user_id, u.username, u.first_name, c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN users u ON u.user_id = s.user_id
                    JOIN chats c ON c.chat_id = s.chat_id
                    ORDER BY s.messages_count DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                return cur.fetchall(), total
    finally:
        _put_conn(conn)


def find_chats_by_name(query: str):
    """
    Ищет чаты по частичному совпадению названия (регистронезависимо).
    Возвращает список (chat_id, title, chat_type).
    """
    if not db_enabled() or _pool is None:
        return []

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chat_id, title, chat_type
                    FROM chats
                    WHERE LOWER(title) LIKE LOWER(%s)
                    ORDER BY title
                    LIMIT 10
                    """,
                    (f"%{query}%",),
                )
                return cur.fetchall()
    finally:
        _put_conn(conn)


def get_chat_by_id(chat_id: int):
    """Возвращает (chat_id, title, chat_type) или None."""
    if not db_enabled() or _pool is None:
        return None

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT chat_id, title, chat_type FROM chats WHERE chat_id = %s",
                    (chat_id,),
                )
                return cur.fetchone()
    finally:
        _put_conn(conn)


def get_chat_top_page(chat_id: int, offset: int, limit: int = 10):
    """
    Топ пользователей в конкретном чате с пагинацией.
    Возвращает (rows, total_count).
    rows: (user_id, username, first_name, messages, chars,
           stickers, photos, videos, voice, gifs, forwards)
    """
    if not db_enabled() or _pool is None:
        return [], 0

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM user_chat_stats WHERE chat_id = %s",
                    (chat_id,),
                )
                total = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT u.user_id, u.username, u.first_name,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN users u ON u.user_id = s.user_id
                    WHERE s.chat_id = %s
                    ORDER BY s.messages_count DESC
                    LIMIT %s OFFSET %s
                    """,
                    (chat_id, limit, offset),
                )
                return cur.fetchall(), total
    finally:
        _put_conn(conn)


def get_user_by_id(user_id: int):
    """
    Возвращает полную информацию о пользователе:
    (user_id, username, first_name, last_name, registered_at, last_seen_at)
    или None если не найден.
    """
    if not db_enabled() or _pool is None:
        return None

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, username, first_name, last_name,
                           registered_at, last_seen_at
                    FROM users WHERE user_id = %s
                    """,
                    (user_id,),
                )
                return cur.fetchone()
    finally:
        _put_conn(conn)


def get_user_by_username(username: str):
    """
    Ищет пользователя по username (без @, регистронезависимо).
    Возвращает (user_id, username, first_name, last_name,
                registered_at, last_seen_at) или None.
    """
    if not db_enabled() or _pool is None:
        return None

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, username, first_name, last_name,
                           registered_at, last_seen_at
                    FROM users WHERE LOWER(username) = LOWER(%s)
                    """,
                    (username,),
                )
                return cur.fetchone()
    finally:
        _put_conn(conn)


def get_user_stats_all_chats(user_id: int):
    """
    Возвращает статистику пользователя по всем чатам:
    список (chat_title, messages, chars, stickers, photos, videos, voice, gifs, forwards)
    отсортированный по сообщениям.
    """
    if not db_enabled() or _pool is None:
        return []

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN chats c ON c.chat_id = s.chat_id
                    WHERE s.user_id = %s
                    ORDER BY s.messages_count DESC
                    """,
                    (user_id,),
                )
                return cur.fetchall()
    finally:
        _put_conn(conn)


def get_stats_overview():
    """Возвращает (total_users, total_chats, totals_dict) с суммарными счётчиками."""
    if not db_enabled() or _pool is None:
        return 0, 0, {k: 0 for k in
                      ("messages", "chars", "stickers", "photos",
                       "videos", "voice", "gifs", "forwards")}

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
                        COALESCE(SUM(gifs_count), 0),
                        COALESCE(SUM(forwards_count), 0)
                    FROM user_chat_stats
                    """
                )
                (
                    messages, chars, stickers,
                    photos, videos, voice, gifs, forwards,
                ) = cur.fetchone()
    finally:
        _put_conn(conn)

    totals = {
        "messages": messages, "chars": chars, "stickers": stickers,
        "photos": photos, "videos": videos, "voice": voice,
        "gifs": gifs, "forwards": forwards,
    }
    return total_users, total_chats, totals


def get_chat_stats(chat_id: int):
    """
    Суммарная статистика конкретного чата по всем пользователям.
    Возвращает (participants, messages, chars, stickers, photos,
                videos, voice, gifs, forwards) или None если чат не найден.
    """
    if not db_enabled() or _pool is None:
        return None

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(DISTINCT s.user_id),
                        COALESCE(SUM(s.messages_count), 0),
                        COALESCE(SUM(s.chars_count), 0),
                        COALESCE(SUM(s.stickers_count), 0),
                        COALESCE(SUM(s.photos_count), 0),
                        COALESCE(SUM(s.videos_count), 0),
                        COALESCE(SUM(s.voice_count), 0),
                        COALESCE(SUM(s.gifs_count), 0),
                        COALESCE(SUM(s.forwards_count), 0)
                    FROM user_chat_stats s
                    WHERE s.chat_id = %s
                    """,
                    (chat_id,),
                )
                return cur.fetchone()
    finally:
        _put_conn(conn)


def get_chats_top_page(offset: int, limit: int = 10):
    """
    Топ бесед (без ЛС) по суммарному количеству сообщений с пагинацией.
    Возвращает (rows, total_count).
    rows: (chat_id, title, total_messages, chars, stickers,
           photos, videos, voice, gifs, forwards)
    """
    if not db_enabled() or _pool is None:
        return [], 0

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT c.chat_id)
                    FROM chats c
                    JOIN user_chat_stats s ON s.chat_id = c.chat_id
                    WHERE c.chat_type != 'private'
                    """
                )
                total = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT
                        c.chat_id,
                        c.title,
                        COALESCE(SUM(s.messages_count), 0) AS total_messages,
                        COALESCE(SUM(s.chars_count), 0),
                        COALESCE(SUM(s.stickers_count), 0),
                        COALESCE(SUM(s.photos_count), 0),
                        COALESCE(SUM(s.videos_count), 0),
                        COALESCE(SUM(s.voice_count), 0),
                        COALESCE(SUM(s.gifs_count), 0),
                        COALESCE(SUM(s.forwards_count), 0)
                    FROM chats c
                    JOIN user_chat_stats s ON s.chat_id = c.chat_id
                    WHERE c.chat_type != 'private'
                    GROUP BY c.chat_id, c.title
                    ORDER BY total_messages DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset),
                )
                rows = cur.fetchall()
        return rows, total
    finally:
        _put_conn(conn)


def get_chats_count_by_type():
    """
    Возвращает (group_count, private_count) — количество бесед и личок.
    """
    if not db_enabled() or _pool is None:
        return 0, 0

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE chat_type != 'private'),
                        COUNT(*) FILTER (WHERE chat_type = 'private')
                    FROM chats
                    """
                )
                return cur.fetchone()
    finally:
        _put_conn(conn)


def get_top_activity_groups(limit: int = 5):
    """
    Топ пользователей только из групповых чатов (без ЛС).
    Возвращает (user_id, username, first_name, chat_title, messages, chars,
                stickers, photos, videos, voice, gifs, forwards)
    """
    if not db_enabled() or _pool is None:
        return []

    conn = _get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.user_id, u.username, u.first_name, c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN users u ON u.user_id = s.user_id
                    JOIN chats c ON c.chat_id = s.chat_id
                    WHERE c.chat_type != 'private'
                    ORDER BY s.messages_count DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()
    finally:
        _put_conn(conn)
