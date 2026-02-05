import os
import sys
import time

"""Debug скрипт проверки работы контейнера"""

print("--- ЗАПУСК DEBUG СКРИПТА ---", flush=True)

# 1. Проверка версии Python
print(f"Python version: {sys.version}", flush=True)

# 2. Проверка библиотек
print("Проверка импорта библиотек...", flush=True)
try:
    import pandas

    print("Pandas: OK", flush=True)
    import matplotlib

    # Сразу лечим matplotlib, как договаривались
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    print("Matplotlib: OK (Agg mode set)", flush=True)
    import telebot

    print("Telebot: OK", flush=True)
except Exception as e:
    print(f"ОШИБКА ИМПОРТА: {e}", flush=True)

# 3. Проверка переменных окружения
print("Проверка переменных окружения...", flush=True)
token = os.environ.get('TOKEN')
creds = os.environ.get('CREDS')
user_map = os.environ.get('USER_COLUMN_MAP')

if token:
    print(f"TOKEN: Найден (длина {len(token)} симв.)", flush=True)
else:
    print("TOKEN: НЕ НАЙДЕН ❌", flush=True)

if creds:
    print(f"CREDS: Найдены (длина {len(creds)} симв.)", flush=True)
else:
    print("CREDS: НЕ НАЙДЕНЫ ❌", flush=True)

if user_map:
    print(f"USER_COLUMN_MAP: Найден", flush=True)
else:
    print("USER_COLUMN_MAP: НЕ НАЙДЕН ❌", flush=True)

print("--- СКРИПТ УСПЕШНО ВЫПОЛНЕН. ПЕРЕХОЖУ В РЕЖИМ ОЖИДАНИЯ ---", flush=True)

# 4. Бесконечный цикл, чтобы контейнер не закрылся
while True:
    time.sleep(10)
