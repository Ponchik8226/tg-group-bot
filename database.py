"""
Всё, что связано с базой данных (PostgreSQL).

Модуль не зависит от telebot — принимает и возвращает только простые типы.
Если DATABASE_URL не задан (или нет psycopg2), все функции работают как
no-op и бот продолжает жить в памяти.

Соединения берутся из пула. Число одновременных соединений ограничено
семафором — getconn() у psycopg2 при исчерпании пула не ждёт, а сразу
бросает PoolError, поэтому очередь мы держим сами.

_run() никогда не пробрасывает исключение наружу: при недоступной БД
возвращается значение по умолчанию, чтобы бот отвечал пользователю.
"""

import threading
import time

from config import DATABASE_URL, logger

try:
    import psycopg2
    from psycopg2 import pool as psycopg2_pool
    from psycopg2.extras import execute_values
except ImportError:
    psycopg2 = None
    psycopg2_pool = None
    execute_values = None

# =============================================================================
# ПУЛ СОЕДИНЕНИЙ
# =============================================================================

MAX_CONNECTIONS = 10

_pool = None
_pool_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


def db_enabled() -> bool:
    return bool(DATABASE_URL) and psycopg2 is not None


def _ready() -> bool:
    return db_enabled() and _pool is not None


def _init_pool():
    global _pool
    for attempt in range(1, 6):
        try:
            _pool = psycopg2_pool.ThreadedConnectionPool(
                minconn=1, maxconn=MAX_CONNECTIONS, dsn=DATABASE_URL,
            )
            logger.info("Пул соединений с БД создан (max=%s).", MAX_CONNECTIONS)
            return
        except Exception as e:
            logger.warning(
                "Попытка %s/5 подключиться к БД не удалась: %s. Повтор через 3 сек...",
                attempt, e,
            )
            time.sleep(3)

    logger.error("Не удалось подключиться к БД. Бот продолжит работу без базы.")


def _put_conn(conn, close: bool = False):
    if _pool is None or conn is None:
        return
    try:
        _pool.putconn(conn, close=close)
    except Exception:
        logger.debug("Не удалось вернуть соединение в пул.")


# =============================================================================
# RECONNECT-ХЕЛПЕР
# =============================================================================

def _run(fn, default=None, retry: bool = True):
    """
    Выполняет fn(conn) с переподключением при обрыве соединения.

    retry=False — для неидемпотентных запросов (инкремент/декремент):
    повтор после разрыва может применить изменение дважды.
    """
    if not _ready():
        return default

    attempts = 3 if retry else 1
    last_exc = None

    with _pool_slots:  # ждём свободный слот вместо PoolError
        for attempt in range(1, attempts + 1):
            conn = None
            try:
                conn = _pool.getconn()
                result = fn(conn)
                _put_conn(conn)
                return result
            except (psycopg2.OperationalError,
                    psycopg2.InterfaceError,
                    psycopg2_pool.PoolError) as e:
                last_exc = e
                logger.warning(
                    "Соединение с БД прервано (попытка %d/%d): %s",
                    attempt, attempts, e,
                )
                _put_conn(conn, close=True)
                if attempt < attempts:
                    time.sleep(1)
            except Exception:
                _put_conn(conn)
                logger.exception("Ошибка запроса к БД.")
                return default

    logger.error("Запрос к БД не выполнен: %s", last_exc)
    return default


# =============================================================================
# ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ
# =============================================================================

def init_db():
    if not db_enabled():
        logger.warning(
            "DATABASE_URL не задан — таймеры, кнопки и статистика "
            "не сохраняются между перезапусками."
        )
        return

    _init_pool()
    if _pool is None:
        return

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS timers (
                        id                SERIAL PRIMARY KEY,
                        chat_id           BIGINT NOT NULL,
                        user_id           BIGINT NOT NULL,
                        user_first_name   TEXT NOT NULL,
                        description       TEXT NOT NULL DEFAULT '',
                        end_time          DOUBLE PRECISION NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id         BIGINT PRIMARY KEY,
                        username        TEXT,
                        first_name      TEXT,
                        last_name       TEXT,
                        registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
                        last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id     BIGINT PRIMARY KEY,
                        chat_type   TEXT NOT NULL,
                        title       TEXT
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_chat_stats (
                        user_id         BIGINT NOT NULL REFERENCES users(user_id),
                        chat_id         BIGINT NOT NULL REFERENCES chats(chat_id),
                        messages_count  BIGINT NOT NULL DEFAULT 0,
                        chars_count     BIGINT NOT NULL DEFAULT 0,
                        stickers_count  BIGINT NOT NULL DEFAULT 0,
                        photos_count    BIGINT NOT NULL DEFAULT 0,
                        videos_count    BIGINT NOT NULL DEFAULT 0,
                        voice_count     BIGINT NOT NULL DEFAULT 0,
                        gifs_count      BIGINT NOT NULL DEFAULT 0,
                        forwards_count  BIGINT NOT NULL DEFAULT 0,
                        last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                        PRIMARY KEY (user_id, chat_id)
                    )
                """)
                cur.execute("""
                    ALTER TABLE user_chat_stats
                    ADD COLUMN IF NOT EXISTS forwards_count BIGINT NOT NULL DEFAULT 0
                """)
                cur.execute("""
                    ALTER TABLE timers
                    ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN NOT NULL DEFAULT FALSE
                """)
                cur.execute("""
                    ALTER TABLE timers
                    ADD COLUMN IF NOT EXISTS interval_seconds BIGINT NOT NULL DEFAULT 0
                """)
                cur.execute("""
                    ALTER TABLE timers
                    ADD COLUMN IF NOT EXISTS fires_remaining INT NOT NULL DEFAULT 0
                """)
                cur.execute("""
                    ALTER TABLE timers
                    ADD COLUMN IF NOT EXISTS thread_id BIGINT
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_buttons (
                        id          SERIAL PRIMARY KEY,
                        user_id     BIGINT NOT NULL,
                        name        TEXT NOT NULL,
                        is_active   BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                        UNIQUE (user_id, name)
                    )
                """)
                cur.execute("""
                    ALTER TABLE user_buttons
                    ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ucs_chat_id "
                            "ON user_chat_stats (chat_id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ucs_messages "
                            "ON user_chat_stats (messages_count DESC)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_ub_user_id "
                            "ON user_buttons (user_id)")

    _run(_fn)


# =============================================================================
# ТАЙМЕРЫ
# =============================================================================

def insert_timer(chat_id, user_id, first_name, description, end_time,
                 is_recurring=False, interval_seconds=0,
                 fires_remaining=0, thread_id=None):
    """Сохраняет таймер и возвращает его ID (None — если БД недоступна)."""

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO timers "
                    "(chat_id, user_id, user_first_name, description, end_time, "
                    " is_recurring, interval_seconds, fires_remaining, thread_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (chat_id, user_id, first_name, description, end_time,
                     is_recurring, interval_seconds, fires_remaining, thread_id),
                )
                return cur.fetchone()[0]

    return _run(_fn)


def delete_timer(timer_id):
    if timer_id is None or timer_id < 0:
        return  # локальный (не сохранённый в БД) таймер

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM timers WHERE id = %s", (timer_id,))

    _run(_fn)


def load_all_timers():
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, chat_id, user_id, user_first_name, description, "
                    "       end_time, is_recurring, interval_seconds, "
                    "       fires_remaining, thread_id "
                    "FROM timers"
                )
                return cur.fetchall()

    return _run(_fn, default=[])


def decrement_timer_fires(timer_id: int):
    """
    Атомарно уменьшает fires_remaining на 1 и возвращает новое значение.
    None — считать в памяти (БД недоступна или строки нет).
    Без ретраев: повтор мог бы уменьшить счётчик дважды.
    """
    if timer_id < 0:
        return None

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE timers SET fires_remaining = fires_remaining - 1 "
                    "WHERE id = %s RETURNING fires_remaining",
                    (timer_id,),
                )
                row = cur.fetchone()
                return row[0] if row else None

    return _run(_fn, retry=False)


def update_timer_end_time(timer_id: int, new_end_time: float):
    if timer_id < 0:
        return

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE timers SET end_time = %s WHERE id = %s",
                            (new_end_time, timer_id))

    _run(_fn)


# =============================================================================
# ПОЛЬЗОВАТЕЛЬСКИЕ КНОПКИ
# =============================================================================

def get_user_buttons(user_id: int) -> list:
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, is_active FROM user_buttons "
                    "WHERE user_id = %s ORDER BY id",
                    (user_id,),
                )
                return cur.fetchall()

    return _run(_fn, default=[])


def add_user_button(user_id: int, name: str):
    """id новой кнопки, или None если дубликат / БД недоступна."""

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO user_buttons (user_id, name) VALUES (%s, %s) "
                    "ON CONFLICT (user_id, name) DO NOTHING RETURNING id",
                    (user_id, name),
                )
                row = cur.fetchone()
                return row[0] if row else None

    return _run(_fn)


def remove_user_buttons(button_ids: list, user_id: int) -> int:
    if not button_ids:
        return 0

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_buttons WHERE id = ANY(%s) AND user_id = %s",
                    (list(button_ids), user_id),
                )
                return cur.rowcount

    return _run(_fn, default=0)


def remove_all_user_buttons(user_id: int) -> int:
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_buttons WHERE user_id = %s", (user_id,))
                return cur.rowcount

    return _run(_fn, default=0)


def set_all_buttons_active(user_id: int, is_active: bool) -> int:
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE user_buttons SET is_active = %s WHERE user_id = %s",
                    (is_active, user_id),
                )
                return cur.rowcount

    return _run(_fn, default=0)


# =============================================================================
# СТАТИСТИКА
# =============================================================================

_BULK_USERS_SQL = """
    INSERT INTO users (user_id, username, first_name, last_name, registered_at, last_seen_at)
    VALUES %s
    ON CONFLICT (user_id) DO UPDATE SET
        username     = EXCLUDED.username,
        first_name   = EXCLUDED.first_name,
        last_name    = EXCLUDED.last_name,
        last_seen_at = now()
"""

_BULK_CHATS_SQL = """
    INSERT INTO chats (chat_id, chat_type, title)
    VALUES %s
    ON CONFLICT (chat_id) DO UPDATE SET
        chat_type = EXCLUDED.chat_type,
        title     = EXCLUDED.title
"""

_BULK_STATS_SQL = """
    INSERT INTO user_chat_stats (
        user_id, chat_id, messages_count, chars_count, stickers_count,
        photos_count, videos_count, voice_count, gifs_count, forwards_count, last_seen_at
    )
    VALUES %s
    ON CONFLICT (user_id, chat_id) DO UPDATE SET
        messages_count = user_chat_stats.messages_count + EXCLUDED.messages_count,
        chars_count    = user_chat_stats.chars_count    + EXCLUDED.chars_count,
        stickers_count = user_chat_stats.stickers_count + EXCLUDED.stickers_count,
        photos_count   = user_chat_stats.photos_count   + EXCLUDED.photos_count,
        videos_count   = user_chat_stats.videos_count   + EXCLUDED.videos_count,
        voice_count    = user_chat_stats.voice_count    + EXCLUDED.voice_count,
        gifs_count     = user_chat_stats.gifs_count     + EXCLUDED.gifs_count,
        forwards_count = user_chat_stats.forwards_count + EXCLUDED.forwards_count,
        last_seen_at   = now()
"""


def record_message_stats_bulk(users: dict, chats: dict, stats: dict):
    """
    Пишет пачку сообщений одной транзакцией.

    users: {user_id: (username, first_name, last_name)}
    chats: {chat_id: (chat_type, title)}
    stats: {(user_id, chat_id): [messages, chars, stickers, photos,
                                 videos, voice, gifs, forwards]}

    Ключи уже уникальны — иначе Postgres ругается на повторное
    обновление одной строки внутри ON CONFLICT DO UPDATE.
    """
    if not stats or execute_values is None:
        return

    user_rows = [(uid, *data) for uid, data in users.items()]
    chat_rows = [(cid, *data) for cid, data in chats.items()]
    stat_rows = [(uid, cid, *counters) for (uid, cid), counters in stats.items()]

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                execute_values(cur, _BULK_USERS_SQL, user_rows,
                               template="(%s, %s, %s, %s, now(), now())")
                execute_values(cur, _BULK_CHATS_SQL, chat_rows)
                execute_values(cur, _BULK_STATS_SQL, stat_rows,
                               template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())")

    _run(_fn)


# =============================================================================
# ЗАПРОСЫ ДЛЯ АДМИН-КОМАНД
# =============================================================================

def get_global_top_page(offset: int, limit: int = 10):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM user_chat_stats")
                total = cur.fetchone()[0]
                cur.execute("""
                    SELECT u.user_id, u.username, u.first_name, c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN users u ON u.user_id = s.user_id
                    JOIN chats c ON c.chat_id = s.chat_id
                    ORDER BY s.messages_count DESC
                    LIMIT %s OFFSET %s
                """, (limit, offset))
                return cur.fetchall(), total

    return _run(_fn, default=([], 0))


def find_chats_by_name(query: str):
    safe_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT chat_id, title, chat_type
                    FROM chats
                    WHERE LOWER(title) LIKE LOWER(%s) ESCAPE '\\'
                    ORDER BY title
                    LIMIT 10
                """, (f"%{safe_query}%",))
                return cur.fetchall()

    return _run(_fn, default=[])


def get_chat_by_id(chat_id: int):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id, title, chat_type FROM chats WHERE chat_id = %s",
                            (chat_id,))
                return cur.fetchone()

    return _run(_fn)


def get_chat_top_page(chat_id: int, offset: int, limit: int = 10):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM user_chat_stats WHERE chat_id = %s",
                            (chat_id,))
                total = cur.fetchone()[0]
                cur.execute("""
                    SELECT u.user_id, u.username, u.first_name,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN users u ON u.user_id = s.user_id
                    WHERE s.chat_id = %s
                    ORDER BY s.messages_count DESC
                    LIMIT %s OFFSET %s
                """, (chat_id, limit, offset))
                return cur.fetchall(), total

    return _run(_fn, default=([], 0))


def get_user_by_id(user_id: int):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, username, first_name, last_name,
                           registered_at, last_seen_at
                    FROM users WHERE user_id = %s
                """, (user_id,))
                return cur.fetchone()

    return _run(_fn)


def get_user_by_username(username: str):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id, username, first_name, last_name,
                           registered_at, last_seen_at
                    FROM users WHERE LOWER(username) = LOWER(%s)
                """, (username,))
                return cur.fetchone()

    return _run(_fn)


def get_user_stats_all_chats(user_id: int):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT c.title,
                           s.messages_count, s.chars_count, s.stickers_count,
                           s.photos_count, s.videos_count, s.voice_count,
                           s.gifs_count, s.forwards_count
                    FROM user_chat_stats s
                    JOIN chats c ON c.chat_id = s.chat_id
                    WHERE s.user_id = %s
                    ORDER BY s.messages_count DESC
                """, (user_id,))
                return cur.fetchall()

    return _run(_fn, default=[])


def get_chats_count_by_type():
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE chat_type != 'private'),
                        COUNT(*) FILTER (WHERE chat_type = 'private')
                    FROM chats
                """)
                return cur.fetchone()

    return _run(_fn, default=(0, 0))


def get_top_activity_groups(limit: int = 5):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
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
                """, (limit,))
                return cur.fetchall()

    return _run(_fn, default=[])


def get_chats_top_page(offset: int, limit: int = 10):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(DISTINCT s.chat_id)
                    FROM user_chat_stats s
                    JOIN chats c ON c.chat_id = s.chat_id
                    WHERE c.chat_type != 'private'
                """)
                total = cur.fetchone()[0]
                cur.execute("""
                    SELECT c.chat_id, c.title,
                           SUM(s.messages_count) AS messages,
                           SUM(s.chars_count)    AS chars,
                           SUM(s.stickers_count) AS stickers,
                           SUM(s.photos_count)   AS photos,
                           SUM(s.videos_count)   AS videos,
                           SUM(s.voice_count)    AS voice,
                           SUM(s.gifs_count)     AS gifs,
                           SUM(s.forwards_count) AS forwards
                    FROM user_chat_stats s
                    JOIN chats c ON c.chat_id = s.chat_id
                    WHERE c.chat_type != 'private'
                    GROUP BY c.chat_id, c.title
                    ORDER BY messages DESC
                    LIMIT %s OFFSET %s
                """, (limit, offset))
                return cur.fetchall(), total

    return _run(_fn, default=([], 0))


def get_chat_stats(chat_id: int):
    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(DISTINCT user_id),
                        COALESCE(SUM(messages_count), 0),
                        COALESCE(SUM(chars_count),    0),
                        COALESCE(SUM(stickers_count), 0),
                        COALESCE(SUM(photos_count),   0),
                        COALESCE(SUM(videos_count),   0),
                        COALESCE(SUM(voice_count),    0),
                        COALESCE(SUM(gifs_count),     0),
                        COALESCE(SUM(forwards_count), 0)
                    FROM user_chat_stats
                    WHERE chat_id = %s
                """, (chat_id,))
                row = cur.fetchone()
                return None if row is None or row[0] == 0 else row

    return _run(_fn)


def get_stats_overview():
    empty = {k: 0 for k in ("messages", "chars", "stickers", "photos",
                            "videos", "voice", "gifs", "forwards")}

    def _fn(conn):
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                total_users = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM chats")
                total_chats = cur.fetchone()[0]
                cur.execute("""
                    SELECT
                        COALESCE(SUM(messages_count), 0),
                        COALESCE(SUM(chars_count),    0),
                        COALESCE(SUM(stickers_count), 0),
                        COALESCE(SUM(photos_count),   0),
                        COALESCE(SUM(videos_count),   0),
                        COALESCE(SUM(voice_count),    0),
                        COALESCE(SUM(gifs_count),     0),
                        COALESCE(SUM(forwards_count), 0)
                    FROM user_chat_stats
                """)
                messages, chars, stickers, photos, videos, voice, gifs, forwards = cur.fetchone()
                return total_users, total_chats, {
                    "messages": messages, "chars": chars, "stickers": stickers,
                    "photos": photos, "videos": videos, "voice": voice,
                    "gifs": gifs, "forwards": forwards,
                }

    return _run(_fn, default=(0, 0, empty))
