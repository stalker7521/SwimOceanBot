import base64
from io import BytesIO
from tempfile import TemporaryDirectory
from pathlib import Path
import pandas as pd
import matplotlib

matplotlib.use('Agg')  # ДЛЯ СЕРВЕРА
import matplotlib.pyplot as plt
from telebot.types import ReactionTypeEmoji
from oauth2client.service_account import ServiceAccountCredentials
import os, json, telebot, gspread, threading, time
from settings import (
    TOKEN, SPREADSHEET_ID, WORKSHEET_NAME, user_column_map, SCOPE, START_DATE
)
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv  # для локальной работы env var

load_dotenv()  # для локальной работы env var

# Определяем пути и const для backup
BACKUP_DIR = '/data' if os.path.exists('/') else './data'
os.makedirs(BACKUP_DIR, exist_ok=True)
BACKUP_INTERVAL_DAYS = 1
BACKUP_RETENTION_DAYS = 14


def create_backup():
    """Функция скачивает таблицу и сохраняет в /data"""
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Начинаю создание бэкапа...")
        df = get_df_from_google_sheet(WORKSHEET_NAME)
        # Формируем имя файла с текущей датой
        date_str = datetime.now().strftime("%H-%M_%d-%m-%Y")
        file_name = f"swimocean_backup_{date_str}.xlsx"
        file_path = os.path.join(BACKUP_DIR, file_name)

        # Сохраняем в Excel
        df.to_excel(file_path, index=False, engine='openpyxl')
        print(f"Бэкап успешно сохранен: {file_path}")
    except Exception as e:
        print(f"Ошибка при создании бэкапа: {e}")


def cleanup_old_backups():
    """Удаляет бэкапы, которые старше BACKUP_RETENTION_DAYS"""
    try:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Проверка старых бэкапов...")
        current_time = time.time()
        # Вычисляем временную отсечку
        cutoff_time = current_time - (BACKUP_RETENTION_DAYS * 24 * 60 * 60)
        deleted_count = 0
        for filename in os.listdir(BACKUP_DIR):
            if filename.startswith("swimocean_backup_") and filename.endswith(".xlsx"):
                file_path = os.path.join(BACKUP_DIR, filename)

                # Проверяем время изменения файла
                if os.path.isfile(file_path):
                    file_mtime = os.path.getmtime(file_path)
                    if file_mtime < cutoff_time:
                        os.remove(file_path)
                        print(f"Удален старый бэкап: {filename}")
                        deleted_count += 1

        if deleted_count == 0:
            print("Старых бэкапов для удаления не найдено.")

    except Exception as e:
        print(f"Ошибка при очистке бэкапов: {e}")


def maintenance_job():
    """Фоновый процесс работы с backup"""
    while True:
        create_backup()

        cleanup_old_backups()

        time.sleep(BACKUP_INTERVAL_DAYS * 24 * 60 * 60)


# Инициализация бота
if TOKEN is None:
    raise ValueError("Ошибка: Переменная окружения TOKEN не установлена!")
bot = telebot.TeleBot(TOKEN)


# Функция для подключения к Google Sheets
def get_gsheet_client():
    cred_str_b64 = os.environ.get('CREDS')
    if not cred_str_b64:
        raise ValueError("Переменная окружения CREDS не найдена!")

    try:
        # Декодируем из Base64 в обычную строку с кавычками
        cred_str = base64.b64decode(cred_str_b64).decode('utf-8')
        creds_dict = json.loads(cred_str)
    except Exception as e:
        raise ValueError(f"Ошибка декодирования CREDS: {e}")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
    client = gspread.authorize(creds)
    return client


def get_df_from_google_sheet(sheet_name):
    client = get_gsheet_client()
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
    data = sheet.get_all_values()
    df = pd.DataFrame(data, columns=data[0])[1:]
    return df


def get_statistics_for_period(start_date: str, end_date: str):
    """
    Возвращает статистики за выбранный период
    """
    df = get_df_from_google_sheet(WORKSHEET_NAME)
    df['Date'] = pd.to_datetime(df['Date'], dayfirst=True)
    df = df.set_index('Date')

    for col in df.columns:
        df[col] = df[col].replace('', 0)
        df[col] = df[col].fillna(0)
        df[col] = df[col].astype(float)

    df = df.reset_index()
    start_date = pd.to_datetime(start_date, dayfirst=True)
    end_date = pd.to_datetime(end_date, dayfirst=True)
    period_df = df[df['Date'].between(start_date, end_date)]
    period_df = period_df.set_index('Date')
    period_df = period_df.drop(['Day_distance', 'Cumulative_sum'], axis=1)
    return period_df


def get_sum_for_period(df):
    sum_df = df.sum().to_frame().reset_index()
    sum_df = sum_df.rename(columns={0: 'sum', 'index': 'people'})
    sum_df = sum_df.sort_values(by='sum', ascending=False)
    sum_df = sum_df[sum_df['sum'] > 0]

    min_date = df.index.min().strftime("%d.%m.%Y")
    max_date = df.index.max().strftime("%d.%m.%Y")

    sum_df = sum_df[sum_df['sum'] > 0]
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.subplots_adjust()
    bar_container = ax.bar(x=sum_df['people'],
                           height=sum_df['sum'],
                           width=0.6,
                           color='green'
                           )
    ax.bar_label(bar_container)
    ax.set(ylabel='Метраж',
           title=f'Метры за период {min_date} - {max_date}'
           )
    ax.tick_params(axis='x', labelrotation=90)

    tmp_dir = TemporaryDirectory()
    tmp_dir_path = Path(tmp_dir.name)
    img_path = tmp_dir_path / 'sum.png'
    fig.savefig(img_path, bbox_inches='tight')

    with open(img_path, 'rb') as file:
        img_data = file.read()

    # Создаем BytesIO объект
    img = BytesIO(img_data)
    tmp_dir.cleanup()
    return img


# Функция для записи данных в Google Sheets
def write_to_sheet(value, usr_name, date):
    try:
        client = get_gsheet_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
        """Ищем строку с указанной датой"""
        dates = sheet.col_values(1)  # Получаем все даты из столбца A (он с датами)

        usr_name = user_column_map[usr_name]  # вытаскиваем из словаря Имя пользователя по его tg-id
        col_names = sheet.row_values(1)  # список всех имен пользователей
        col_index = col_names.index(usr_name) + 1
        row_num = dates.index(date) + 1  # +1 т.к. нумерация с 1
        sheet.update_cell(row_num, col_index, value)  # добавляем в последнюю ячейку определенного столбца данные
        print(f'Value "{value}" appended to sheet')

    except Exception as e:
        print(f'An error occurred: {e}')


# Проверка есть ли ID пользователя в общей базе
def get_user_key(message):
    if message.from_user.id:
        user_id = str(message.from_user.id)
        if user_id in user_column_map.keys():
            return user_id
    if message.from_user.username:
        username = message.from_user.username
        if username in user_column_map.keys():
            return username
    else:
        user_frst_name = message.from_user.first_name
        if user_frst_name in user_column_map.keys():
            return user_frst_name
        else:
            return None


def is_date_valid(input_date_str):
    user_date = datetime.strptime(input_date_str, "%d.%m.%Y").date()
    now_utc = datetime.now(timezone.utc)
    # Добавляем смещение +5 часов и округляем до дня
    today = (now_utc + timedelta(hours=5)).date()
    return user_date <= today


def plus_message_handling(message):
    return message.text.startswith('+')


def plus_data_message_handing(message):
    return plus_message_handling(message) and message.text.split()[0][1:].isdigit() and len(message.text.split()) == 2


# Обработчик сообщений вида: +метры дата_куда_нужно_записать_метры
@bot.message_handler(func=plus_data_message_handing)
def handle_number_with_data_message(message):
    number = message.text.split()[0][1:]
    date = str(message.text.split()[1])
    # Блок проверки валидности вводимой даты
    pattern_of_date = "%d.%m.%Y"  # паттерн правильной даты
    isValid = True
    try:
        isValid = bool(datetime.strptime(date, pattern_of_date))
    except ValueError:
        isValid = False
    if isValid and is_date_valid(date):
        user_key = get_user_key(message)
        if user_key:
            print(f'ID пользователя, который ввел данные: {user_key}')
            write_to_sheet(number, user_key, date)  # записываем число в таблицу
            bot.reply_to(message, f'Число {number} было записано в дату: {date}')
            bot.set_message_reaction(chat_id=message.chat.id,
                                     message_id=message.id,
                                     reaction=[ReactionTypeEmoji("✍")]
                                     )
        else:
            bot.reply_to(message, "Вас нет в таблице или вашего ID нет в общей базе")
    else:
        bot.set_message_reaction(chat_id=message.chat.id,
                                 message_id=message.id,
                                 reaction=[ReactionTypeEmoji("👎")])
        bot.reply_to(message, 'Дата введена неверно, ознакомьтесь с инструкцией в /help')


# Обработчик сообщений, начинающихся с "+" и числа
@bot.message_handler(func=plus_message_handling)
def handle_number_message(message):
    number = message.text[1:]
    if plus_message_handling(message) and message.text[1:].isdigit():
        """
        Извлекаем дату сообщения. Дата в формате unix timestamp. Прибавляем 18000 = 5 часов т.к. дата хранится в GMT+0
        """
        date_obj = datetime.fromtimestamp(message.date + 18000)  # Преобразовываем дату из unix timestamp в datetime obj
        date = date_obj.strftime("%d.%m.%Y")  # преобразуем в нормальный формат -> "13.04.2025"
        user_key = get_user_key(message)
        if user_key:
            print(f'ID пользователя, который ввел данные: {user_key}')
            write_to_sheet(number, user_key, date)  # записываем число в таблицу

            bot.set_message_reaction(chat_id=message.chat.id,
                                     message_id=message.id,
                                     reaction=[ReactionTypeEmoji("✍")]
                                     )
        else:
            msg = ("Вас нет в таблице или вашего ID нет в общей базе. "
                   "Обратитесь к администратору бота")
            bot.reply_to(message, msg)
    else:
        bot.set_message_reaction(chat_id=message.chat.id,
                                 message_id=message.id,
                                 reaction=[ReactionTypeEmoji("👎")])
        bot.reply_to(message, 'Команда введена неверно, ознакомьтесь с инструкцией в /help')


# Обработчик команды /start
@bot.message_handler(commands=['start'])
def handle_start(message):
    bot.reply_to(message, "Привет! Я бот клуба SwimOcean, который следит, чтобы все проплытые метры были учтены в "
                          "наших заплывах! "
                          "Чтобы увидеть список команд, которые я понимаю, и формат, "
                          "в котором нужно записывать метры, введите команду /help .\n"
                          "Чтобы посмотреть, где мы плывем, перейди по ссылке: https://swimocean.streamlit.app/")


# Обработчик команды /help
@bot.message_handler(commands=['help'])
def handle_help(message):
    bot.reply_to(message, "Ссылка для просмотра нашего местоположения: https://swimocean.streamlit.app/ \n"
                          "Расстояние нужно писать исключительно в виде метров.\n"
                          "Запись в даты в будущем невозможна.\n"
                          "Список команд и правил записи:\n\n"
                          "+<кол-во_метров> - записать метры (пример: +1000)\n"
                          "+<кол-во_метров дата> - записать в прошедшую дату\n (пример: +100 19.04.2025)\n\n"
                          "Существующие команды:\n"
                          "/start - вызов стартового сообщения\n"
                          "/help - вызов справки\n"
                          "/stat_my - вывод персональной статистики за весь период по месяцам\n"
                          "/stat_all - показ общей статистики")


def get_month_name_and_year(date) -> str:
    month_number = date.month
    year = str(date.year)[-2:]
    months = ['', 'Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
              'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь']
    return months[month_number] + "'" + year


def centered(text, width) -> str:
    return f"{str(text):^{width}}"


def create_mobile_table(data, title):
    # Используем компактную горизонтальную таблицу
    lines = [title, ""]
    for row in data:
        month, volume, amount = row
        lst = [centered(month, 11), centered(volume, 8), centered(amount, 4)]
        line = " │ ".join(lst)
        lines.append(line)
        if row == data[0]:  # После заголовка
            lines.append("─" * (len(line) - 1))
    return f"<pre>{chr(10).join(lines)}</pre>"


# Персональная статистика
@bot.message_handler(commands=['stat_my'])
def handle_pstat(message):
    user_key = get_user_key(message)
    if user_key:
        user_name = user_column_map[user_key]
        start_date = pd.to_datetime(START_DATE, dayfirst=True)
        today = datetime.now().date().strftime("%d.%m.%Y")
        today = pd.to_datetime(today, dayfirst=True)

        period_df = get_statistics_for_period(start_date=start_date,
                                              end_date=today)

        sum_by_month = period_df[[user_name]].copy()
        sum_by_month = sum_by_month.groupby(pd.Grouper(freq='ME')).sum()
        sum_by_month = sum_by_month.astype(int)
        count_by_month = period_df[[user_name]].copy()
        count_by_month = count_by_month.replace(0, None).groupby(
            pd.Grouper(freq='ME')
        )
        count_by_month = count_by_month.count()

        merged_df = pd.merge(left=sum_by_month, right=count_by_month, on='Date')
        merged_df = merged_df.reset_index()
        merged_df['Date'] = merged_df['Date'].apply(
            lambda x: get_month_name_and_year(x)
        )

        data = [['Месяц', 'Объём, м', 'Кол-во']]
        data.extend(merged_df.values.tolist())

        # table = f"```\n{create_table(data, user_name)}\n```"
        result = create_mobile_table(data, user_name)
        bot.send_message(message.chat.id, result, parse_mode='HTML')
    else:
        msg = ("Вас нет в таблице или вашего ID нет в общей базе. "
               "Обратитесь к администратору бота")
        bot.reply_to(message, msg)


# Общая статистика
@bot.message_handler(commands=['stat_all'])
def handle_all_stat(message):
    start_date = pd.to_datetime(START_DATE, dayfirst=True)
    today = datetime.now().date().strftime("%d.%m.%Y")
    today = pd.to_datetime(today, dayfirst=True)
    period_df = get_statistics_for_period(start_date=start_date,
                                          end_date=today)
    img = get_sum_for_period(period_df)
    bot.send_photo(chat_id=message.chat.id,
                   photo=img,
                   caption='Общая статистика')


# Запрос копии таблицы с метрами
@bot.message_handler(commands=['get_table'])
def handle_get_table(message):
    user_key = get_user_key(message)
    # Проверяем, есть ли пользователь в базе
    if not user_key:
        bot.reply_to(message, "У вас нет доступа к этой команде. Обратитесь к администратору.")
        return

    try:
        df = get_df_from_google_sheet(WORKSHEET_NAME)

        # Создаем Excel файл в оперативной памяти
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Метры')

        # Обязательно "перематываем" файл в начало перед отправкой
        output.seek(0)
        # Задаем имя файла, которое увидит пользователь в Telegram
        file_name = f"SwimOcean_Metres_{datetime.now().strftime('%d-%m-%Y')}.xlsx"
        output.name = file_name

        # Отправляем документ
        bot.send_document(message.chat.id, document=output, caption="")

    except Exception as e:
        print(f"Ошибка выгрузки: {e}")
        bot.reply_to(message, "❌ Произошла ошибка при формировании таблицы.")


# Запуск бота
if __name__ == '__main__':
    print("Bot is starting...")
    # Запускаем бэкапы в отдельном фоновом потоке
    backup_thread = threading.Thread(target=maintenance_job(), daemon=True)
    backup_thread.start()

    bot.polling(none_stop=True)
