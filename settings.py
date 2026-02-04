import json, os, base64
from dotenv import load_dotenv  # для локальной работы env var
#/todo ПОФИКСИТЬ ПАРСИНГ BASE64
load_dotenv()
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    print("CRITICAL ERROR: TOKEN не найден в переменных окружения!")
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
# user_column_map = json.loads(os.environ.get('USER_COLUMN_MAP'))

raw_map_b64 = os.environ.get('USER_COLUMN_MAP')
if not raw_map_b64:
    raise ValueError("Переменная окружения USER_COLUMN_MAP не найдена!")
user_column_map = {}

try:
    usr_col_map_str = base64.b64decode(raw_map_b64).decode('utf-8')
except Exception as e:
    raise ValueError(f"Ошибка декодирования user_column_map: {e}")
try:
    user_column_map = json.loads(usr_col_map_str)
except json.JSONDecodeError:
    print(f"CRITICAL ERROR: USER_COLUMN_MAP содержит некорректный JSON: {user_column_map}")
WORKSHEET_NAME = 'МетрыV2'
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
START_DATE = '13-01-2025'
