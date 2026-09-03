import sqlite3, os
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
        conn.commit()
    print("✅ База данных SQLite (сообщения + очередь синхронизации) инициализирована.")


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
    print(f"Запись добавлена в очередь синхронизации: {delta}м для {user_id} на {date_str}")


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
