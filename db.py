import sqlite3, os
from settings import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, 'messages_ledger.db')


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id INTEGER PRIMARY KEY,
                chat_id INTEGER,
                user_id TEXT,
                date_str TEXT,
                meters INTEGER
            )
        ''')
        conn.commit()


def save_message(message_id, chat_id, user_id, date_str, meters):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO messages (message_id, chat_id, user_id, date_str, meters)
            VALUES (?, ?, ?, ?, ?)''',
                       (message_id, chat_id, str(user_id), date_str, int(meters)))
        conn.commit()


def get_old_meters(message_id, chat_id):
    """Возвращает старое количество метров или None, если сообщения нет в базе"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT meters FROM messages WHERE message_id = ? AND chat_id = ?',
                       (message_id, chat_id))
        row = cursor.fetchone()
        return row[0] if row else None
