import json, os, base64
from dotenv import load_dotenv  # для локальной работы env var

# /todo ДОБАВИТЬ ФУНКЦИЮ АВТОМАТИЧЕСКОГО ПЕРЕСЧЕТА СЕКРЕТА
# ИЗ JSON В BASE64
load_dotenv()

# --- TOKEN ---
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    print("CRITICAL ERROR: TOKEN не найден в переменных окружения!")

# ---  SPREADSHEET_ID ---
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
if not SPREADSHEET_ID:
    raise ValueError("CRITICAL ERROR: SPREADSHEET_ID не найден!")

# --- USER_COLUMN_MAP ---
raw_map_b64 = os.environ.get('USER_COLUMN_MAP')
user_column_map = {}
if not raw_map_b64:
    raise ValueError("CRITICAL ERROR: Переменная окружения USER_COLUMN_MAP не найдена!")

try:
    # Лечение выравнивания base64
    raw_map_b64 += "=" * ((4 - len(raw_map_b64) % 4) % 4)

    # Декодируем из Base64 в строку
    raw_map_str = base64.b64decode(raw_map_b64).decode('utf-8')

    # Превращаем строку JSON в словарь
    user_column_map = json.loads(raw_map_str)
    if not isinstance(user_column_map, dict):
        raise TypeError(f"Ожидался словарь (dict), а получен {type(user_column_map)}")

except Exception as e:
    raise ValueError(f"CRITICAL ERROR при обработке USER_COLUMN_MAP: {e}")
WORKSHEET_NAME = 'МетрыV2'
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
START_DATE = '13-01-2025'
