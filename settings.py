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



def _optional_int(name):
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        print(f"CRITICAL ERROR: {name} должен быть целым числом!")
        return None


# --- Еженедельные уведомления ---
NOTIFY_CHAT_ID = _optional_int('NOTIFY_CHAT_ID')
NOTIFY_THREAD_ID = _optional_int('NOTIFY_THREAD_ID')  # ID темы в группе с темами
if NOTIFY_CHAT_ID is None:
    print("WARNING: NOTIFY_CHAT_ID не задан. Еженедельные уведомления отключены!")

# --- LLM (любой провайдер с OpenAI-совместимым /v1/chat/completions) ---
LLM_TOKEN = os.environ.get('LLM_TOKEN')
LLM_URL = os.environ.get('LLM_URL')
LLM_MODELS = [model.strip() for model in os.environ.get('LLM_MODELS', '').split(',') if model.strip()]
_missing_llm_vars = [name for name, value in (('LLM_TOKEN', LLM_TOKEN), ('LLM_URL', LLM_URL),
                                              ('LLM_MODELS', LLM_MODELS)) if not value]
if _missing_llm_vars:
    print(f"WARNING: не заданы {', '.join(_missing_llm_vars)}. "
          "Пост о местности будет заменен запасным текстом!")


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
LOCATIONS_SHEET_NAME = 'Локации'
DEFAULT_START_CAPTION = 'Точка старта'
DEFAULT_FINISH_CAPTION = 'Точка финиша'
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
START_DATE = '13-01-2025'

# Папка для данных и бэкапов
if os.path.exists('/data') and os.path.isdir('/data'):
    DATA_DIR = '/data'
else:
    # Локально на компьютере создаст папку 'data' рядом с файлом settings.py
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception as e:
    print(f"Предупреждение при создании DATA_DIR: {e}")
