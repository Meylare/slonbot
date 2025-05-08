import json
import logging
from datetime import datetime, date
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Data storage file
DATA_FILE = 'bot_data.json'

# Load or initialize data
def load_data():
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Initialize default structure
        data = {
            'goal_date': None,  # YYYY-MM-DD
            'goal_target': 0,   # integer target
            'progress': 0,      # global progress
            'subscribers': []   # list of user IDs
        }
        save_data(data)  # Save initialized data to file
    return data

def save_data(data):
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных: {e}")

# Command: /start
def start(update: Update, context: CallbackContext):
    user = update.effective_user
    data = load_data()
    if user.id not in data['subscribers']:
        data['subscribers'].append(user.id)
        save_data(data)
    update.message.reply_text(
        "Привет! Я бот для отслеживания вашей цели.\n"
        "Команды:\n"
        "/set_goal YYYY-MM-DD target - установить дату и цель (только админ)\n"
        "/add X - добавить X единиц к выполненному\n"
        "/status - узнать текущий статус"
    )

# Command: /set_goal
def set_goal(update: Update, context: CallbackContext):
    user = update.effective_user
    args = context.args
    if len(args) != 2:
        update.message.reply_text("Использование: /set_goal YYYY-MM-DD target")
        return
    date_str, target_str = args
    try:
        goal_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        target = int(target_str)
        if target <= 0:
            raise ValueError("Цель должна быть положительным числом")
    except ValueError as e:
        update.message.reply_text(f"Неверный формат даты или цели. Пример: /set_goal 2025-06-01 100\nОшибка: {e}")
        return
    data = load_data()
    data['goal_date'] = date_str
    data['goal_target'] = target
    data['progress'] = 0
    save_data(data)
    update.message.reply_text(
        f"Установлена цель: {target} единиц до {date_str}. Прогресс сброшен."
    )

# Command: /add
def add_progress(update: Update, context: CallbackContext):
    args = context.args
    if len(args) != 1:
        update.message.reply_text("Использование: /add X (где X - целое число, может быть отрицательным)")
        return
    try:
        amount = int(args[0])
    except ValueError:
        update.message.reply_text("Пожалуйста, укажите корректное целое число. Пример: /add 5 или /add -3")
        return
    data = load_data()
    current_progress = data.get('progress', 0)
    new_progress = current_progress + amount
    if new_progress < 0:
        update.message.reply_text(f"Невозможно вычесть {abs(amount)}. Прогресс не может быть меньше 0. Текущий прогресс: {current_progress}")
        return
    data['progress'] = new_progress
    save_data(data)
    if amount > 0:
        update.message.reply_text(f"Добавлено {amount} единиц. Текущий прогресс: {new_progress}/{data['goal_target']}")
        # Отправляем уведомление всем подписчикам
        notify_subscribers(context.bot, amount, new_progress, data['goal_target'])
    elif amount < 0:
        update.message.reply_text(f"Вычтено {abs(amount)} единиц. Текущий прогресс: {new_progress}/{data['goal_target']}")
    else:
        update.message.reply_text(f"Прогресс не изменился. Текущий прогресс: {new_progress}/{data['goal_target']}")

# Функция для отправки уведомлений подписчикам о добавлении единиц
def notify_subscribers(bot, added_amount, current_progress, goal_target):
    data = load_data()
    if added_amount == 1:
         message = f"Добавлено видео. Текущий прогресс: {current_progress}/{goal_target}"
    else:
        message = f"Добавлено {added_amount} единиц к цели. Текущий прогресс: {current_progress}/{goal_target}"
    for uid in data['subscribers']:
        try:
            bot.send_message(chat_id=uid, text=message)
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления пользователю {uid}: {e}")

# Command: /status
def status(update: Update, context: CallbackContext):
    data = load_data()
    if not data['goal_date']:
        update.message.reply_text("Цель ещё не установлена. Используйте /set_goal.")
        return
    completed = data.get('progress', 0)
    goal = data.get('goal_target', 0)
    try:
        goal_date = datetime.strptime(data['goal_date'], '%Y-%m-%d').date()
    except ValueError:
        update.message.reply_text("Неверный формат даты цели.")
        return
    today = date.today()
    days_left = (goal_date - today).days
    update.message.reply_text(
        f"Сделано: {completed}/{goal} видео.\n"
        f"Осталось дней: {days_left}"
    )

# Function to broadcast daily status to all users
def broadcast_status(bot):
    data = load_data()
    if not data or not data['goal_date']:
        logger.info("Пропуск рассылки: цель не установлена или данные не загружены")
        return
    try:
        goal_date = datetime.strptime(data['goal_date'], '%Y-%m-%d').date()
    except ValueError:
        logger.error("Неверный формат goal_date")
        return
    today = date.today()
    days_left = (goal_date - today).days
    completed = data.get('progress', 0)
    target = data.get('goal_target', 0)
    for uid in data['subscribers']:
        try:
            bot.send_message(
                chat_id=uid,
                text=(
                    f"Ежедневный отчёт:\n"
                    f"Сделано: {completed}/{target} видео.\n"
                    f"Осталось: {days_left} дней"
                )
            )
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения пользователю {uid}: {e}")

def main():
    import os
    TOKEN = os.getenv('BOT_TOKEN')
    updater = Updater(TOKEN)
    dp = updater.dispatcher

    # Handlers
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('set_goal', set_goal))
    dp.add_handler(CommandHandler('add', add_progress))
    dp.add_handler(CommandHandler('status', status))

    # Scheduler for daily broadcast at 09:45 (Almaty time)
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Almaty'))
    scheduler.add_job(
        lambda: broadcast_status(updater.bot),
        CronTrigger(hour=9, minute=45, timezone=pytz.timezone('Asia/Almaty'))
    )
    scheduler.start()

    # Start bot
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
