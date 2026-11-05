import sqlite3, os, logging, json
from settings import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, 'messages_ledger.db')


def get_connection():
    """Создает подключение с таймаутом ожидания и авто-созданием папки"""
    os.makedirs(DATA_DIR, exist_ok=True)
    # timeout=20.0 ожидание освобождения файла до 20 секунд вместо краша
    return sqlite3.connect(DB_PATH, timeout=20.0)


def init_db():
    """Инициализация базы: основная таблица сообщений и таблица очереди синхронизации"""
    with get_connection() as conn:
        cursor = conn.cursor()
        # Включаем режим WAL (Write-Ahead Logging) для защиты от конфликтов потоков
        cursor.execute('PRAGMA journal_mode=WAL;')

        # Основная таблица сообщений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                user_id TEXT,
                date_str TEXT,
                meters INTEGER
            )
        ''')
        # Таблица очереди отложенной синхронизации с Google Таблицей
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS sync_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id INTEGER,
                        chat_id INTEGER,
                        user_id TEXT,
                        date_str TEXT,
                        delta INTEGER,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
        # Еженедельные уведомления: одна строка на тип уведомления и ISO-неделю
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS scheduled_posts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        kind TEXT NOT NULL,
                        week_key TEXT NOT NULL,
                        status TEXT DEFAULT 'pending',
                        rounds_done INTEGER DEFAULT 0,
                        send_attempts INTEGER DEFAULT 0,
                        next_attempt_at TEXT,
                        text TEXT,
                        sent_at TEXT,
                        UNIQUE(kind, week_key)
                    )
                ''')
        # Посты о местности и факты (topics), которые в них уже прозвучали
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS journey_posts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        week_key TEXT,
                        leg_index INTEGER,
                        lat REAL,
                        lon REAL,
                        text TEXT,
                        topics TEXT,
                        sources TEXT,
                        summarized INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
        journey_columns = {row[1] for row in cursor.execute('PRAGMA table_info(journey_posts)')}
        if 'sources' not in journey_columns:
            cursor.execute('ALTER TABLE journey_posts ADD COLUMN sources TEXT')
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS journey_memory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        summary TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
        cursor.execute('''
                    CREATE TABLE IF NOT EXISTS geo_cache (
                        cache_key TEXT PRIMARY KEY,
                        payload TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
        conn.commit()
    logging.info("База данных SQLite (сообщения, очередь синхронизации, уведомления) инициализирована.")


def save_message(message_id, chat_id, user_id, date_str, meters):
    """Выполняет запись сообщения в базу"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO messages (message_id, chat_id, user_id, date_str, meters)
            VALUES (?, ?, ?, ?, ?)
        ''', (message_id, chat_id, str(user_id), date_str, int(meters)))
        conn.commit()


def get_old_meters(message_id, chat_id):
    """Возвращает старое количество метров или None"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT meters FROM messages WHERE message_id = ? AND chat_id = ?',
                       (message_id, chat_id))
        row = cursor.fetchone()
        return row[0] if row else None


def get_workouts_count(user_id, date_str):
    """Считает количество тренировок пользователя за конкретную дату"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM messages WHERE user_id = ? AND date_str = ?',
                       (str(user_id), date_str))
        row = cursor.fetchone()
        return row[0] if row else 0


# ============================
# Функции для оффлайн-очереди
# ============================

def add_to_sync_queue(message_id, chat_id, user_id, date_str, delta):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO sync_queue (message_id, chat_id, user_id, date_str, delta, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
        ''', (message_id, chat_id, str(user_id), date_str, int(delta)))
        conn.commit()
    logging.info(f"Запись добавлена в очередь синхронизации: {delta}м для {user_id} на {date_str}")


def get_pending_syncs():
    """Получает все записи, ожидающие отправки в Google"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT id, message_id, chat_id, user_id, date_str, delta
        FROM sync_queue
        WHERE status = 'pending'
        ORDER BY id ASC
        ''')
        return cursor.fetchall()

def mark_as_synced(queue_id):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sync_queue SET status = 'synced' WHERE id = ?", (queue_id,))
        conn.commit()


# ============================
# Еженедельные уведомления
# ============================

SCHEDULED_POST_FIELDS = ('id', 'kind', 'week_key', 'status', 'rounds_done',
                         'send_attempts', 'next_attempt_at', 'text', 'sent_at')


def create_scheduled_post(kind, week_key, next_attempt_at):
    """Создает запись уведомления, если на эту неделю ее еще нет"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO scheduled_posts (kind, week_key, status, next_attempt_at)
            VALUES (?, ?, 'pending', ?)
        ''', (kind, week_key, next_attempt_at))
        conn.commit()
        return cursor.rowcount > 0


def get_due_scheduled_posts(now_str):
    """Уведомления в статусе pending, время попытки которых уже наступило"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT {", ".join(SCHEDULED_POST_FIELDS)}
            FROM scheduled_posts
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY id ASC
        ''', (now_str,))
        return [dict(zip(SCHEDULED_POST_FIELDS, row)) for row in cursor.fetchall()]


def update_scheduled_post(post_id, **fields):
    allowed = set(SCHEDULED_POST_FIELDS) - {'id', 'kind', 'week_key'}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Неизвестные поля scheduled_posts: {unknown}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE scheduled_posts SET {assignments} WHERE id = ?",
                       (*fields.values(), post_id))
        conn.commit()


def add_journey_post(week_key, leg_index, lat, lon, text, topics_json, sources_json="[]"):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO journey_posts (week_key, leg_index, lat, lon, text, topics, sources)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (week_key, leg_index, lat, lon, text, topics_json, sources_json))
        conn.commit()


def get_used_sources(last_posts=26):
    """Заголовки статей, на которые опирались последние посты о местности"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT sources FROM journey_posts ORDER BY id DESC LIMIT ?', (last_posts,))
        rows = cursor.fetchall()
    used = set()
    for (sources_json,) in rows:
        try:
            used.update(json.loads(sources_json or "[]"))
        except (json.JSONDecodeError, TypeError):
            continue
    return used


def get_unsummarized_journey_posts():
    """Возвращает список (id, topics_json, text) в порядке создания"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, topics, text FROM journey_posts
            WHERE summarized = 0
            ORDER BY id ASC
        ''')
        return cursor.fetchall()


def mark_journey_posts_summarized(post_ids):
    if not post_ids:
        return
    placeholders = ", ".join("?" for _ in post_ids)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE journey_posts SET summarized = 1 WHERE id IN ({placeholders})",
                       tuple(post_ids))
        conn.commit()


def get_latest_journey_memory():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT summary FROM journey_memory ORDER BY id DESC LIMIT 1')
        row = cursor.fetchone()
        return row[0] if row else ""


def save_journey_memory(summary):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO journey_memory (summary) VALUES (?)', (summary,))
        conn.commit()


def get_geo_cache(cache_key):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT payload FROM geo_cache WHERE cache_key = ?', (cache_key,))
        row = cursor.fetchone()
        return row[0] if row else None


def set_geo_cache(cache_key, payload):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO geo_cache (cache_key, payload, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (cache_key, payload))
        conn.commit()
