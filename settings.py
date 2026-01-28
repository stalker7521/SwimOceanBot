import json, os
from dotenv import load_dotenv  # для локальной работы env var

load_dotenv()
TOKEN = os.environ.get('TOKEN')
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
user_column_map = json.loads(os.environ.get('user_column_map'))
WORKSHEET_NAME = 'МетрыV2'
SCOPE = ['https://www.googleapis.com/auth/spreadsheets']
START_DATE = '13-01-2025'
