import os
from dotenv import load_dotenv  # для локальной работы env var
from cryptography.fernet import Fernet

load_dotenv()

# --- TOKEN ---
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    print("CRITICAL ERROR: TOKEN не найден в переменных окружения!")

# ---  SPREADSHEET_ID ---
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
if not SPREADSHEET_ID:
    raise ValueError("CRITICAL ERROR: SPREADSHEET_ID не найден!")

# --- ENCRYPTION_KEY ---
ENCRYPTION_KEY = os.environ.get('ENCRYPTION_KEY')
if not ENCRYPTION_KEY:
    raise ValueError("CRITICAL ERROR: ENCRYPTION_KEY не найден!")
fernet = Fernet(ENCRYPTION_KEY.encode())

raw_admins = os.environ.get('ADMIN_IDS')
ADMIN_IDS = []
if raw_admins:
    # Разбиваем строку по запятой, убираем лишние пробелы и превращаем в int
    try:
        ADMIN_IDS = [int(admin_id.strip()) for admin_id in raw_admins.split(',')]
        print(f"Загружены права администратора для {len(ADMIN_IDS)} пользователей.")
    except ValueError:
        print("CRITICAL ERROR: Неверный формат ADMIN_IDS. Используйте цифры через запятую!")
else:
    print("WARNING: Переменная ADMIN_IDS не найдена. У бота нет администраторов!")

def encrypt_data(data: str) -> str:
    """Шифрует строку"""
    return fernet.encrypt(data.encode()).decode()


def decrypt_data(encrypted_data: str) -> str:
    """Расшифровывает строку"""
    try:
        return fernet.decrypt(encrypted_data.encode()).decode()
    except Exception:
        return "DECRYPTION_ERROR"


WORKSHEET_NAME = 'МетрыV2'
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
START_DATE = '13-01-2025'

# Папка для данных и бэкапов
DATA_DIR = '/data' if os.path.exists('/') else './data'
os.makedirs(DATA_DIR, exist_ok=True)
