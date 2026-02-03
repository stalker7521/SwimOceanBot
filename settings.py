import json, os, sys
from dotenv import load_dotenv  # для локальной работы env var

load_dotenv()
TOKEN = os.environ.get('TOKEN')
if not TOKEN:
    print("CRITICAL ERROR: TOKEN не найден в переменных окружения!")
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
# user_column_map = json.loads(os.environ.get('USER_COLUMN_MAP'))

raw_map = os.environ.get('USER_COLUMN_MAP')
user_column_map = {}

if raw_map:
    try:
        user_column_map = json.loads(raw_map)
    except json.JSONDecodeError:
        print(f"CRITICAL ERROR: USER_COLUMN_MAP содержит некорректный JSON: {raw_map}")
else:
    print("WARNING: Переменная USER_COLUMN_MAP не найдена! Использую пустой словарь.")
WORKSHEET_NAME = 'МетрыV2'
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
START_DATE = '13-01-2025'
