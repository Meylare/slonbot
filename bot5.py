# main_bot.py
import asyncio
import json
import logging
from datetime import datetime, date, timedelta, time
import uuid
import os
from typing import Union, List, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode # Импортируем ParseMode для HTML
from telegram.error import Forbidden, BadRequest # Для обработки ошибок отправки
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    ConversationHandler, CallbackQueryHandler
)
import pytz

from llm_handler import interpret_user_input, interpret_progress_description
from data_handler import load_data, save_data, is_admin as is_user_admin_from_data, find_item_by_name_or_id
from utils import generate_id, parse_natural_deadline_to_date

from constants import (
    ASK_PROJECT_NAME, ASK_PROJECT_DEADLINE,
    ASK_TASK_NAME, ASK_TASK_PROJECT_LINK, ASK_TASK_DEADLINE_STATE,
    ACTIVE_CONVERSATION_KEY,
    ADD_PROJECT_CONV_STATE_VALUE, ADD_TASK_CONV_STATE_VALUE, UPDATE_PROGRESS_CONV_STATE_VALUE,
    LAST_PROCESSED_IN_CONV_MSG_ID_KEY, PENDING_PROGRESS_UPDATE_KEY,
    ASK_PROGRESS_ITEM_TYPE, ASK_PROGRESS_ITEM_NAME, ASK_PROGRESS_DESCRIPTION, ITEM_FOR_PROGRESS_UPDATE_KEY,
    CALLBACK_SHOW_PACE_DETAILS_PREFIX,
    CALLBACK_UPDATE_PARENT_PROJECT_PREFIX
)
from conversations import (
    new_project_command, received_project_name,
    received_project_deadline,
    new_task_command, received_task_name,
    received_task_project_link,
    received_task_deadline,
    progress_command, received_progress_item_type,
    received_progress_item_name_dialog, received_progress_description_dialog,
    universal_cancel,
    ask_for_progress_confirmation, confirm_progress_update_callback
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO # Рекомендую INFO для продакшена, DEBUG для разработки
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', '0')
ADMIN_USER_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip() and admin_id.strip() != '0']
if not BOT_TOKEN: logger.error("BOT_TOKEN не найден!"); exit()
if not ADMIN_USER_IDS : logger.warning("ADMIN_IDS не настроены в main_bot.py.")


def format_deadline_for_report(dl_str: Union[str, None], status: str) -> str:
    if not dl_str:
        return "<i>Без срока</i>"
    try:
        dl_date = datetime.strptime(dl_str, '%Y-%m-%d').date()
        today = date.today()
        days_left = (dl_date - today).days

        if status == "completed": # Для завершенных, если вдруг попадут
            return f"<s>{dl_str}</s>"

        if days_left < 0:
            return f"<b>Просрочено!</b> ({dl_str}, {-days_left} д. назад)"
        elif days_left == 0:
            return f"<b>Сегодня!</b> ({dl_str})"
        else:
            return f"{dl_str} (осталось {days_left} д.)"
    except ValueError:
        return f"<i>{dl_str} (неверный формат)</i>"

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ РАСЧЕТА И ФОРМАТИРОВАНИЯ ТЕМПА (УПРОЩЕННАЯ) ---
def format_pace_for_report(item_data: Dict[str, Any]) -> str:
    status_val = item_data.get('status')
    dl_str = item_data.get('deadline')
    created_at_iso = item_data.get("created_at")
    total_u = item_data.get('total_units', 0)
    curr_u = item_data.get('current_units', 0)
    forecast_str = ""

    if status_val == "active" and dl_str and created_at_iso and total_u > 0:
        try:
            created_at_dt = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
            created_date = created_at_dt.date()
            deadline_date = datetime.strptime(dl_str, '%Y-%m-%d').date()
            today = date.today()

            total_days_planned = (deadline_date - created_date).days
            days_passed = (today - created_date).days
            days_left_for_calc = (deadline_date - today).days
            units_left = total_u - curr_u

            if total_days_planned >= 0 and curr_u < total_u:
                required_pace_val = None
                actual_pace_val = None
                required_pace_text = "" # Без "не определен" по умолчанию

                if units_left <= 0: required_pace_text = "все сделано" # Не должно сюда попадать, т.к. curr_u < total_u
                elif days_left_for_calc > 0:
                    required_pace_val = units_left / days_left_for_calc
                    # required_pace_text = f"{required_pace_val:.1f} ед./д" # Убрал для краткости
                elif days_left_for_calc <=0: # Срок вышел или сегодня, но не сделано
                     required_pace_text = "срок вышел"

                if curr_u > 0 and days_passed > 0:
                    actual_pace_val = curr_u / days_passed
                # elif curr_u > 0 and days_passed == 0: # Сделано сегодня
                    # actual_pace_text = "сделано сегодня"

                if required_pace_text == "срок вышел":
                    forecast_str = "<i>Темп: Срок вышел</i> 😥"
                elif actual_pace_val is not None and required_pace_val is not None :
                    if actual_pace_val >= required_pace_val:
                        forecast_str = "<i>Темп: Успеваете</i> 👍"
                    else:
                        forecast_str = "<i>Темп: Нужно ускориться!</i> 🏃💨"
                elif curr_u > 0 and days_left_for_calc > 0 : # Есть прогресс, но темп не ясен (например, только создан)
                     forecast_str = "<i>Темп: В процессе</i>"


            elif curr_u >= total_u and total_u > 0: # Уже завершено
                forecast_str = "<i>Темп: Завершено</i> 🎉"
            # Если нет данных для темпа, forecast_str останется пустым
        except Exception as e:
            logger.warning(f"Ошибка расчета темпа в отчете для {item_data.get('id', 'N/A')}: {e}")
            # forecast_str = "<i>Темп: ошибка расчета</i>" # Можно не выводить
    return forecast_str

# --- ФУНКЦИЯ ДЛЯ ОТПРАВКИ ЕЖЕДНЕВНЫХ ОТЧЕТОВ ---
async def send_daily_reports(context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Запуск задачи отправки ежедневных отчетов...")
    data = load_data()
    bot = context.bot
    today_date_str = date.today().strftime("%d.%m.%Y")

    for user_id_str, user_data in data.get("users", {}).items():
        if not user_data.get("receive_reports", False):
            logger.debug(f"Пропуск отчета для пользователя {user_id_str} (отключены).")
            continue

        report_parts: List[str] = []
        user_name = user_data.get("username", f"Пользователь {user_id_str}")

        # 1. Сбор ОБЩИХ активных элементов
        public_projects_for_report: List[Dict[str, Any]] = []
        public_tasks_for_report: List[Dict[str, Any]] = []

        for p_id, p_data_orig in data.get("projects", {}).items():
            p_data = p_data_orig.copy() # Работаем с копией
            p_data['id_orig'] = p_id # Сохраняем оригинальный ID для связи
            if p_data.get("is_public") and p_data.get("status") == "active":
                public_projects_for_report.append(p_data)

        for t_id, t_data_orig in data.get("tasks", {}).items():
            t_data = t_data_orig.copy()
            t_data['id_orig'] = t_id
            if t_data.get("status") != "active":
                continue

            is_task_public = t_data.get("is_public", False)
            parent_project_id = t_data.get("project_id")
            is_parent_project_public = False
            if parent_project_id and parent_project_id in data.get("projects", {}):
                if data["projects"][parent_project_id].get("is_public"):
                    is_parent_project_public = True
            
            # Задача попадает в общий отчет, если она сама public или ее родительский проект public
            if is_task_public or is_parent_project_public:
                public_tasks_for_report.append(t_data)
        
        # Сортировка
        public_projects_for_report.sort(key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower()))
        public_tasks_for_report.sort(key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower()))

        if public_projects_for_report or public_tasks_for_report:
            report_parts.append("📢 <b>Общие активные элементы:</b>")
            if public_projects_for_report:
                report_parts.append("  <u>Проекты:</u>")
                for p_item in public_projects_for_report:
                    deadline_info = format_deadline_for_report(p_item.get('deadline'), p_item.get('status'))
                    progress_info = f"{p_item.get('current_units',0)}/{p_item.get('total_units',0)}" if p_item.get('total_units',0) > 0 else f"{p_item.get('current_units',0)} ед."
                    pace_info = format_pace_for_report(p_item)
                    report_parts.append(f"    ▫️ {p_item['name']} ({progress_info})\n      <pre>└</pre>Дедлайн: {deadline_info} {pace_info}".strip())
                    # Задачи этого общего проекта
                    project_specific_tasks = [
                        task for task in public_tasks_for_report 
                        if task.get("project_id") == p_item['id_orig']
                    ]
                    for t_item_proj in project_specific_tasks:
                        deadline_info_t = format_deadline_for_report(t_item_proj.get('deadline'), t_item_proj.get('status'))
                        progress_info_t = f"{t_item_proj.get('current_units',0)}/{t_item_proj.get('total_units',0)}" if t_item_proj.get('total_units',0) > 0 else f"{t_item_proj.get('current_units',0)} ед."
                        pace_info_t = format_pace_for_report(t_item_proj)
                        report_parts.append(f"      <pre> L </pre>Задача: {t_item_proj['name']} ({progress_info_t})\n        <pre>  └</pre>Дедлайн: {deadline_info_t} {pace_info_t}".strip())
            
            # Общие задачи без проекта или те, чей проект не был public, но сами задачи public
            standalone_public_tasks = [
                task for task in public_tasks_for_report 
                if not task.get("project_id") or \
                   (task.get("project_id") not in [p['id_orig'] for p in public_projects_for_report] and task.get("is_public"))
            ]
            if standalone_public_tasks:
                if not public_projects_for_report: # Если не было раздела общих проектов
                     report_parts.append("  <u>Задачи:</u>")
                else: # Если были общие проекты, нужен отступ или другой заголовок
                     report_parts.append("  <u>Прочие общие задачи:</u>")

                for t_item in standalone_public_tasks:
                    deadline_info = format_deadline_for_report(t_item.get('deadline'), t_item.get('status'))
                    progress_info = f"{t_item.get('current_units',0)}/{t_item.get('total_units',0)}" if t_item.get('total_units',0) > 0 else f"{t_item.get('current_units',0)} ед."
                    pace_info = format_pace_for_report(t_item)
                    report_parts.append(f"    ▫️ {t_item['name']} ({progress_info})\n      <pre>└</pre>Дедлайн: {deadline_info} {pace_info}".strip())
            report_parts.append("") # Пустая строка для разделения

        # 2. Сбор ЛИЧНЫХ активных элементов пользователя
        owned_projects_for_report: List[Dict[str, Any]] = []
        owned_tasks_for_report: List[Dict[str, Any]] = []

        for p_id, p_data_orig in data.get("projects", {}).items():
            p_data = p_data_orig.copy()
            p_data['id_orig'] = p_id
            if str(p_data.get("owner_id")) == user_id_str and \
               not p_data.get("is_public") and \
               p_data.get("status") == "active":
                owned_projects_for_report.append(p_data)

        for t_id, t_data_orig in data.get("tasks", {}).items():
            t_data = t_data_orig.copy()
            t_data['id_orig'] = t_id
            if t_data.get("status") != "active":
                continue

            is_owner = str(t_data.get("owner_id")) == user_id_str
            is_task_not_public = not t_data.get("is_public", False)
            
            parent_project_id = t_data.get("project_id")
            parent_is_not_public_or_no_parent = True
            if parent_project_id and parent_project_id in data.get("projects", {}):
                if data["projects"][parent_project_id].get("is_public"): # Если родитель public, задача не личная
                    parent_is_not_public_or_no_parent = False
            
            if is_owner and is_task_not_public and parent_is_not_public_or_no_parent:
                owned_tasks_for_report.append(t_data)

        owned_projects_for_report.sort(key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower()))
        owned_tasks_for_report.sort(key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower()))
        
        if owned_projects_for_report or owned_tasks_for_report:
            report_parts.append("👤 <b>Ваши личные активные элементы:</b>")
            if owned_projects_for_report:
                report_parts.append("  <u>Проекты:</u>")
                for p_item in owned_projects_for_report:
                    deadline_info = format_deadline_for_report(p_item.get('deadline'), p_item.get('status'))
                    progress_info = f"{p_item.get('current_units',0)}/{p_item.get('total_units',0)}" if p_item.get('total_units',0) > 0 else f"{p_item.get('current_units',0)} ед."
                    pace_info = format_pace_for_report(p_item)
                    report_parts.append(f"    ▫️ {p_item['name']} ({progress_info})\n      <pre>└</pre>Дедлайн: {deadline_info} {pace_info}".strip())
                    # Задачи этого личного проекта
                    project_specific_tasks = [
                        task for task in owned_tasks_for_report 
                        if task.get("project_id") == p_item['id_orig']
                    ]
                    for t_item_proj in project_specific_tasks:
                        deadline_info_t = format_deadline_for_report(t_item_proj.get('deadline'), t_item_proj.get('status'))
                        progress_info_t = f"{t_item_proj.get('current_units',0)}/{t_item_proj.get('total_units',0)}" if t_item_proj.get('total_units',0) > 0 else f"{t_item_proj.get('current_units',0)} ед."
                        pace_info_t = format_pace_for_report(t_item_proj)
                        report_parts.append(f"      <pre> L </pre>Задача: {t_item_proj['name']} ({progress_info_t})\n        <pre>  └</pre>Дедлайн: {deadline_info_t} {pace_info_t}".strip())


            # Личные задачи без проекта или те, чей проект не был личным этого юзера (уже отфильтровано)
            standalone_owned_tasks = [
                task for task in owned_tasks_for_report if not task.get("project_id")
            ] # Задачи, привязанные к чужим личным проектам, сюда не попадут из-за фильтрации выше
            if standalone_owned_tasks:
                if not owned_projects_for_report:
                    report_parts.append("  <u>Задачи:</u>")
                else:
                    report_parts.append("  <u>Прочие ваши личные задачи:</u>")
                for t_item in standalone_owned_tasks:
                    deadline_info = format_deadline_for_report(t_item.get('deadline'), t_item.get('status'))
                    progress_info = f"{t_item.get('current_units',0)}/{t_item.get('total_units',0)}" if t_item.get('total_units',0) > 0 else f"{t_item.get('current_units',0)} ед."
                    pace_info = format_pace_for_report(t_item)
                    report_parts.append(f"    ▫️ {t_item['name']} ({progress_info})\n      <pre>└</pre>Дедлайн: {deadline_info} {pace_info}".strip())

        # Отправка отчета, если есть что отправлять
        if report_parts:
            greeting = f"Доброе утро, {user_name}! 👋\nВаша сводка на {today_date_str}:\n"
            final_report_text = greeting + "\n".join(report_parts)
            try:
                await bot.send_message(chat_id=int(user_id_str), text=final_report_text, parse_mode=ParseMode.HTML)
                logger.info(f"Отправлен отчет пользователю {user_id_str}.")
            except Forbidden:
                logger.warning(f"Не удалось отправить отчет пользователю {user_id_str}: бот заблокирован.")
                # Можно пометить пользователя для отключения отчетов в будущем
                # data["users"][user_id_str]["receive_reports"] = False
            except BadRequest as e: # Например, если user_id не найден или чат удален
                logger.error(f"Не удалось отправить отчет пользователю {user_id_str}: {e}")
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при отправке отчета пользователю {user_id_str}: {e}")
        else:
            logger.info(f"Для пользователя {user_id_str} нет активных элементов для отчета.")
    
    # save_data(data) # Если меняли receive_reports при Forbidden
    logger.info("Задача отправки ежедневных отчетов завершена.")



# --- КОМАНДА ДЛЯ УПРАВЛЕНИЯ ПУБЛИЧНОСТЬЮ ЭЛЕМЕНТОВ ---
async def toggle_public_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажите название или ID элемента после команды.\nПример: `/public МойПроект`")
        return

    query = " ".join(context.args)
    data = load_data()
    found_item = find_item_by_name_or_id(query, None, data) # Ищем и среди проектов, и среди задач

    if not found_item:
        await update.message.reply_text(f"Элемент '{query}' не найден.")
        return

    item_id = found_item['id']
    item_type_db = found_item['item_type_db'] # 'project' или 'task'
    item_pool_name = "projects" if item_type_db == "project" else "tasks"
    item_name = found_item['name']

    # Проверка прав: только владелец или админ может менять публичность
    # Этого условия не было в ТЗ, но оно кажется логичным. Если не нужно, можно убрать.
    item_owner_id = found_item.get("owner_id")
    is_admin = is_user_admin_from_data(user_id, data)
    
    # Убрал проверку на админа/владельца, как не было в ТЗ
    # if str(user_id) != str(item_owner_id) and not is_admin:
    #     await update.message.reply_text(f"Вы не можете изменить статус публичности для '{item_name}', так как не являетесь его владельцем.")
    #     return

    current_is_public = data[item_pool_name][item_id].get("is_public", False)
    new_is_public = not current_is_public
    data[item_pool_name][item_id]["is_public"] = new_is_public
    save_data(data)

    public_status_text = "общим" if new_is_public else "личным"
    item_type_text = "Проект" if item_type_db == "project" else "Задача"
    await update.message.reply_text(f"{item_type_text} '{item_name}' теперь {public_status_text}.")
    logger.info(f"Пользователь {user_id} изменил статус публичности для {item_type_db} '{item_name}' (ID: {item_id}) на {public_status_text}.")

# --- КОМАНДА ДЛЯ УПРАВЛЕНИЯ ПОДПИСКОЙ НА ОТЧЕТЫ ---
async def reports_preference_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    if not context.args or context.args[0].lower() not in ["on", "off"]:
        await update.message.reply_text("Используйте: `/reports on` или `/reports off`.")
        return

    preference = context.args[0].lower()
    data = load_data()

    if user_id not in data["users"]:
        # Этого не должно произойти, если пользователь запускал /start, но на всякий случай
        data["users"][user_id] = {"username": update.effective_user.username or f"User_{user_id}", "timezone": "UTC"} # Добавим is_admin?

    data["users"][user_id]["receive_reports"] = True if preference == "on" else False
    save_data(data)

    status_text = "включены" if preference == "on" else "отключены"
    await update.message.reply_text(f"Ежедневные отчеты для вас теперь {status_text}.")
    logger.info(f"Пользователь {user_id} изменил свои настройки отчетов на: {preference}.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; data = load_data()
    user_id_str = str(user.id)
    is_admin_now = is_user_admin_from_data(user.id, data)
    if user_id_str not in data["users"]:
        data["users"][user_id_str] = {
            "username": user.username or f"User_{user_id_str}",
            "receive_reports": True,  # По умолчанию отчеты включены
            "is_admin": is_admin_now,
            "timezone": "UTC" # Пока оставляем UTC, отчеты будут по Asia/Almaty
        }
    else:
        data["users"][user_id_str].setdefault("receive_reports", True) # Для существующих пользователей
        data["users"][user_id_str].update({
            "is_admin": is_admin_now,
            "username": user.username or data["users"][user_id_str].get("username", f"User_{user_id_str}")
        })
    save_data(data); logger.info(f"User {user.id} ({user.username}) started/updated. Admin: {is_admin_now}. Reports: {data['users'][user_id_str]['receive_reports']}")
    await update.message.reply_text(f"Привет, {user.first_name}! Я ваш менеджер проектов. /help")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = load_data()
    is_admin_user = is_user_admin_from_data(user_id, data) # Используем правильное имя

    admin_text_parts = []
    if is_admin_user: # Проверяем, является ли пользователь админом
        # Здесь можно добавить специфичные для админа команды в будущем
        pass # Пока нет специфичных админских команд для /help

    user_specific_text = ""
    if str(user_id) in data["users"] and data["users"][str(user_id)].get("receive_reports"):
        user_specific_text += "\n    `/reports off` - отключить ежедневные отчеты"
    else:
        user_specific_text += "\n    `/reports on` - включить ежедневные отчеты"


    help_msg = (
        "🤖 *Команды управления:*\n"
        "    `/newproject` - создать проект\n"
        "    `/newtask` - создать задачу\n"
        "    `/progress` - обновить прогресс\n"
        "    `/public <название или ID>` - сделать элемент общим/личным\n"
        f"    *Настройки отчетов:*{user_specific_text}\n\n"
        "💡 *Общение в свободной форме:*\n"
        "    'создай проект X дедлайн Y'\n"
        "    'добавь задачу Z для проекта X'\n"
        "    'прогресс по задаче X +5'"
    )
    if admin_text_parts: # Если есть админские команды для отображения
         help_msg += "\n\n👑 *Админ-команды*:\n" + "\n".join(admin_text_parts)

    await update.message.reply_text(help_msg, parse_mode='Markdown')

async def show_pace_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    callback_data = query.data
    original_message_id = query.message.message_id
    chat_id_for_reply = query.message.chat_id

    logger.debug(f"Callback для деталей темпа: {callback_data}")

    item_id = None
    try:
        prefix_to_check = CALLBACK_SHOW_PACE_DETAILS_PREFIX + "_"
        if callback_data.startswith(prefix_to_check):
            item_id = callback_data[len(prefix_to_check):]

        if not item_id:
            logger.error(f"Не удалось извлечь item_id из callback_data: {callback_data}")
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id=chat_id_for_reply, text="Ошибка: неверный ID для деталей темпа.")
            return

        logger.debug(f"Извлечен item_id: {item_id} для деталей темпа.")
        pace_data_key = f"pace_details_for_{item_id}"
        pace_details = context.user_data.pop(pace_data_key, None)

        if pace_details:
            details_text_md = "*Подробнее о темпе:*"

            required_pace = pace_details.get('required')
            if required_pace:
                req_pace_val_escaped = str(required_pace).replace('`', r'\`')
                details_text_md += f"\n- Требуемый темп: `{req_pace_val_escaped}`"

            actual_pace = pace_details.get('actual')
            if actual_pace:
                act_pace_val_escaped = str(actual_pace).replace('`', r'\`')
                details_text_md += f"\n- Ваш средний темп: `{act_pace_val_escaped}`"

            await context.bot.send_message(
                chat_id=chat_id_for_reply,
                text=details_text_md,
                parse_mode='Markdown',
                reply_to_message_id=original_message_id
            )
            await query.edit_message_reply_markup(reply_markup=None)
            logger.debug(f"Показаны детали темпа для {item_id} новым сообщением.")
        else:
            await query.edit_message_text(
                text=query.message.text + "\n\nДетали темпа не найдены или устарели.",
                reply_markup=None,
                parse_mode=None
            )
            logger.warning(f"Детали темпа для {item_id} не найдены в user_data ({pace_data_key}).")

    except Exception as e:
        logger.error(f"Ошибка в show_pace_details_callback для item_id '{item_id}': {e}", exc_info=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await context.bot.send_message(chat_id=chat_id_for_reply, text="Произошла ошибка при отображении деталей темпа.")
        except Exception as e_final:
            logger.error(f"Не удалось отправить/отредактировать сообщение после ошибки в show_pace_details_callback: {e_final}")

async def handle_parent_project_progress_no_thanks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer()
    logger.info(f"Пользователь {update.effective_user.id} отказался добавлять прогресс к родительскому проекту ({query.data}).")
    try: await query.edit_message_text("Хорошо, прогресс родительского проекта не изменен.")
    except Exception as e: logger.error(f"Ошибка edit_message_text в ...no_thanks: {e}")

async def handle_parent_project_progress_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    parts = query.data.split('_')
    logger.debug(f"Получен callback ДА для обновления родительского проекта: {query.data}")

    if len(parts) != 6 or parts[3] != "yes":
        logger.error(f"Некорректный callback_data для ДА обновления проекта: {query.data}")
        await query.edit_message_text("Ошибка обработки вашего выбора (yes).")
        return

    project_id = parts[4]
    try:
        units_to_add = int(parts[5])
    except (IndexError, ValueError):
        logger.error(f"Некорректное значение units_to_add в callback_data (yes): {query.data}")
        await query.edit_message_text("Ошибка в данных для обновления прогресса проекта (yes).")
        return

    data = load_data()
    if project_id in data.get("projects", {}):
        project_data = data["projects"][project_id]
        project_name = project_data.get("name", "Неизвестный проект")
        current_proj_units = project_data.get("current_units", 0)
        total_proj_units = project_data.get("total_units", 0)

        new_proj_units = current_proj_units + units_to_add
        if total_proj_units > 0 and new_proj_units > total_proj_units:
            new_proj_units = total_proj_units

        project_data["current_units"] = new_proj_units
        save_data(data)

        feedback_message = f"Прогресс проекта '{project_name}' обновлен до {new_proj_units}."
        if total_proj_units > 0: feedback_message += f" (из {total_proj_units})"
        await query.edit_message_text(feedback_message)
        logger.info(f"Прогресс проекта '{project_name}' (ID: {project_id}) обновлен на +{units_to_add} юзером {user_id} после завершения задачи.")
    else:
        await query.edit_message_text("Не удалось найти связанный проект для обновления.")
        logger.warning(f"Проект ID {project_id} не найден при попытке обновить прогресс после задачи (yes).")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Union[int, None]:
    uid = update.effective_user.id; user_text = update.message.text; current_message_id = update.message.message_id
    data = load_data()
    last_conv_msg_id = context.user_data.pop(LAST_PROCESSED_IN_CONV_MSG_ID_KEY, None)
    if last_conv_msg_id == current_message_id: logger.debug(f"Дубль после диалога: {current_message_id}. Пропуск."); return None
    active_conv_type = context.user_data.get(ACTIVE_CONVERSATION_KEY)
    if active_conv_type in [ADD_PROJECT_CONV_STATE_VALUE, ADD_TASK_CONV_STATE_VALUE, UPDATE_PROGRESS_CONV_STATE_VALUE]:
        logger.debug(f"Сообщение от {uid} ('{user_text}') проигнорировано (активен диалог '{active_conv_type}')"); return None
    logger.debug(f"handle_text_message для {uid}: '{user_text}' (ID: {current_message_id})")
    nlu_result = await interpret_user_input(user_text)
    if not nlu_result or "intent" not in nlu_result: await update.message.reply_text("Не понял запрос. /help"); return None
    intent = nlu_result.get("intent"); entities = nlu_result.get("entities", {}); user_id_str = str(uid)
    logger.info(f"NLU от {uid}: Intent='{intent}', Entities={entities}")

    if intent == "add_project":
        name=entities.get("item_name_hint");dl_llm=entities.get("deadline")
        if name:
            parsed_dl=parse_natural_deadline_to_date(dl_llm) if dl_llm else None;final_dl=parsed_dl.strftime('%Y-%m-%d') if parsed_dl else None
            dl_msg=f"с дедлайном {final_dl}" if final_dl else "без дедлайна"
            if dl_llm and not parsed_dl:await update.message.reply_text(f"Проект '{name}'. Дедлайн '{dl_llm}' не распознан. /newproject?");return None
            new_id=generate_id("proj");created_at=datetime.now(pytz.utc).isoformat()
            data.setdefault("projects", {})
            data["projects"][new_id]={
                "id":new_id,"name":name,"deadline":final_dl,
                "owner_id":user_id_str,"created_at":created_at,"status":"active",
                "total_units":0,"current_units":0,"last_report_day_counter":0,
                "is_public": False
            }
            save_data(data);await update.message.reply_text(f"🎉 Проект '{name}' {dl_msg} создан!\nID: `{new_id}`",parse_mode='Markdown')
        else:await update.message.reply_text("Не понял имя проекта. /newproject?")
        return None

    elif intent == "add_task":
        task_name=entities.get("item_name_hint");proj_hint=entities.get("project_name_hint_for_task");dl_llm=entities.get("deadline")
        if task_name:
            parsed_dl=parse_natural_deadline_to_date(dl_llm) if dl_llm else None;final_dl=parsed_dl.strftime('%Y-%m-%d') if parsed_dl else None
            dl_msg_task=f"с дедлайном {final_dl}" if final_dl else "без дедлайна";proj_id,proj_fb_msg=None,"без привязки"
            if proj_hint:
                found_proj=find_item_by_name_or_id(proj_hint,"project",data)
                if found_proj:proj_id=found_proj["id"];proj_fb_msg=f"к проекту '{found_proj['name']}'"
                else:await update.message.reply_text(f"Проект '{proj_hint}' не найден. Задача '{task_name}' без привязки. /newtask?")
            if dl_llm and not parsed_dl:await update.message.reply_text(f"Задача '{task_name}'. Дедлайн '{dl_llm}' не распознан. /newtask?");return None
            new_id=generate_id("task");created_at=datetime.now(pytz.utc).isoformat();data.setdefault("tasks",{})
            data["tasks"][new_id]={
                "id":new_id,"name":task_name,"deadline":final_dl,
                "project_id":proj_id,"owner_id":user_id_str,
                "created_at":created_at,"status":"active","total_units":0,"current_units":0,
                "is_public": False
            }
            save_data(data);await update.message.reply_text(f"💪 Задача '{task_name}' ({proj_fb_msg}) {dl_msg_task} создана!\nID: `{new_id}`",parse_mode='Markdown')
        else:await update.message.reply_text("Не понял имя задачи. /newtask?")
        return None

    elif intent == "update_progress":
        item_name_hint = entities.get("item_name_hint"); item_type_llm = entities.get("item_type"); progress_desc = entities.get("progress_description")

        if not item_name_hint:
            await update.message.reply_text("Непонятно, для чего обновить прогресс. Используйте /progress или уточните."); return None

        found_item = find_item_by_name_or_id(item_name_hint, item_type_llm, data)
        if not found_item:
            await update.message.reply_text(f"Не нашел '{item_name_hint}'. Используйте /progress."); return None

        if not progress_desc:
            context.user_data[ITEM_FOR_PROGRESS_UPDATE_KEY] = {
                'id': found_item['id'], 'name': found_item['name'], 'item_type_db': found_item['item_type_db'],
                'current_units': found_item.get('current_units', 0), 'total_units': found_item.get('total_units', 0),
                'llm_item_type': item_type_llm or found_item['item_type_db']
            }
            logger.info(f"Нет описания прогресса для '{found_item['name']}'. Запуск диалога update_progress.")
            context.user_data[ACTIVE_CONVERSATION_KEY] = UPDATE_PROGRESS_CONV_STATE_VALUE
            await update.message.reply_text(f"Обновляем {found_item['item_type_db']} '{found_item['name']}'.\nКак прогресс? ('+5', '50%') /cancel")
            return ASK_PROGRESS_DESCRIPTION

        item_id = found_item['id']; item_name_val = found_item['name']; item_type_db_val = found_item['item_type_db']
        current_units_val = found_item.get('current_units', 0); total_units_val = found_item.get('total_units', 0)

        prog_interp = await interpret_progress_description(progress_desc, total_units_val if total_units_val > 0 else 100)
        if not prog_interp or prog_interp.get("type") == "unknown" or prog_interp.get("value") is None:
            context.user_data[ITEM_FOR_PROGRESS_UPDATE_KEY] = {
                'id': item_id, 'name': item_name_val, 'item_type_db': item_type_db_val,
                'current_units': current_units_val, 'total_units': total_units_val,
                'llm_item_type': item_type_llm or item_type_db_val
            }
            logger.info(f"LLM2 не поняла '{progress_desc}' для '{item_name_val}'. Запуск диалога update_progress.")
            await update.message.reply_text(f"Не смог точно понять '{progress_desc}'.")
            context.user_data[ACTIVE_CONVERSATION_KEY] = UPDATE_PROGRESS_CONV_STATE_VALUE
            await update.message.reply_text(f"Как изменился прогресс для '{item_name_val}'? /cancel")
            return ASK_PROGRESS_DESCRIPTION

        new_calc_units = -1; prog_type = prog_interp.get("type"); prog_value_str = str(prog_interp.get("value","0")); prog_value = 0
        try: prog_value = int(float(prog_value_str))
        except ValueError: await update.message.reply_text(f"Ошибка значения прогресса от LLM: {prog_value_str}."); return None

        if prog_type == "units": new_calc_units = current_units_val + prog_value
        elif prog_type == "percent":
            base = total_units_val if total_units_val > 0 else 100
            new_calc_units = round((prog_value / 100) * base)
            if total_units_val == 0 : new_calc_units = prog_value
        elif prog_type == "absolute_units_set": new_calc_units = prog_value
        elif prog_type == "complete": new_calc_units = total_units_val if total_units_val > 0 else 100

        if new_calc_units < 0: new_calc_units = 0
        if total_units_val > 0 and new_calc_units > total_units_val: new_calc_units = total_units_val
        if new_calc_units == -1: await update.message.reply_text(f"Не удалось вычислить прогресс из: '{progress_desc}'."); return None
        if new_calc_units == current_units_val: await update.message.reply_text(f"Прогресс для '{item_name_val}' не изменился ({current_units_val})."); return None

        pending_info_for_confirmation = {
            'item_id': item_id,
            'item_name': item_name_val,
            'item_type_db': item_type_db_val,
            'new_current_units': new_calc_units,
            'old_current_units': current_units_val,
            'total_units': total_units_val,
            'action_type': 'update'
        }
        await ask_for_progress_confirmation(update, context, pending_info_for_confirmation)
        return None

    elif intent == "complete_item":
        item_name_hint = entities.get("item_name_hint"); item_type_llm = entities.get("item_type")
        if not item_name_hint: await update.message.reply_text("Что именно вы хотите завершить?"); return None

        found_item = find_item_by_name_or_id(item_name_hint, item_type_llm, data)
        if not found_item: await update.message.reply_text(f"Не нашел '{item_name_hint}'."); return None

        item_id=found_item['id']; item_name=found_item['name']; item_type_db=found_item['item_type_db']
        current_u=found_item.get('current_units',0); total_u=found_item.get('total_units',0)

        if found_item.get("status")=="completed":
            await update.message.reply_text(f"{item_type_db.capitalize()} '{item_name}' уже был отмечен как завершенный."); return None

        new_calc_u = total_u if total_u > 0 else 100

        pending_info_for_confirmation = {
            'item_id':item_id, 'item_type_db':item_type_db, 'item_name':item_name,
            'new_current_units':new_calc_u, 'old_current_units':current_u,
            'total_units':total_u if total_u > 0 else 100,
            'action_type':'complete'
        }
        await ask_for_progress_confirmation(update, context, pending_info_for_confirmation)
        return None

    elif intent == "query_status": # <-- ИЗМЕНЕНИЯ ЗДЕСЬ для удаления ID из вывода
        item_name_hint = entities.get("item_name_hint"); item_type_llm = entities.get("item_type")
        reply_lines = []; keyboard_markup = None
        pace_details_for_button = {}

        if item_name_hint:
            found_item = find_item_by_name_or_id(item_name_hint, item_type_llm, data)
            if not found_item: await update.message.reply_text(f"Не нашел '{item_name_hint}'."); return None

            # item_id = found_item['id'] # ID нам нужен для кнопки, но не для вывода
            item_id_for_button = found_item['id'] # Используем для callback_data кнопки темпа
            item_name=found_item['name']; item_type_db=found_item['item_type_db']
            curr_u=found_item.get('current_units',0); total_u=found_item.get('total_units',0)
            status_val=found_item.get('status','активен'); dl_str=found_item.get('deadline')
            created_at_iso = found_item.get("created_at")
            is_public_item = found_item.get("is_public", False) # Получаем статус публичности

            s_icon = "✅" if status_val=="completed" else ("⏳" if status_val=="active" else "❓")
            pub_icon = "📢" if is_public_item else "👤" # Иконка для публичности
            item_type_rus_single = "Проект" if item_type_db=="project" else "Задача"

            # reply_lines.append(f"{s_icon} *{item_type_rus_single}: {item_name}* (ID: `{item_id}`)") # СТАРАЯ СТРОКА
            reply_lines.append(f"{s_icon} {pub_icon} *{item_type_rus_single}: {item_name}*") # НОВАЯ СТРОКА без ID, с иконкой публичности
            reply_lines.append(f"Статус: {status_val.capitalize()}")
            if is_public_item:
                reply_lines[-1] += " (Общий)" # Добавляем информацию о публичности
            else:
                reply_lines[-1] += " (Личный)"


            if total_u>0:
                prog_perc=round((curr_u/total_u)*100)
                reply_lines.append(f"Прогресс: {curr_u}/{total_u} ({prog_perc}%)")
            elif curr_u>0:
                progress_text = f"Прогресс: {curr_u}"
                if status_val == "completed" and curr_u == 100 and total_u == 0:
                    progress_text += " (100% условно)"
                reply_lines.append(progress_text)
            else: reply_lines.append("Прогресс: 0 или не отслеживается")

            if dl_str:
                try:
                    dl_date = datetime.strptime(dl_str, '%Y-%m-%d').date()
                    days_left_val = (dl_date - date.today()).days
                    reply_lines.append(f"Дедлайн: {dl_str}")
                    if status_val != "completed":
                        if days_left_val < 0:
                            days_abs=abs(days_left_val); day_word="дней"
                            if days_abs % 10==1 and days_abs % 100!=11: day_word="день"
                            elif 2<=days_abs % 10<=4 and (days_abs % 100<10 or days_abs % 100>=20): day_word="дня"
                            reply_lines.append(f"Срок истек {days_abs} {day_word} назад! 🆘")
                        elif days_left_val == 0: reply_lines.append("Срок сегодня! 🔥")
                        else: reply_lines.append(f"Осталось дней: {days_left_val} 🗓️")
                except ValueError: reply_lines.append(f"Дедлайн: {dl_str} (ошибка формата)")
            else: reply_lines.append("Дедлайн: не установлен")

            forecast_str = None # Перенесли инициализацию сюда

            if status_val == "active" and dl_str and created_at_iso and total_u > 0:
                try:
                    created_at_dt = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
                    created_date = created_at_dt.date()
                    deadline_date = datetime.strptime(dl_str, '%Y-%m-%d').date()
                    today = date.today()

                    total_days_planned = (deadline_date - created_date).days
                    days_passed = (today - created_date).days
                    days_left_for_calc = (deadline_date - today).days
                    units_left = total_u - curr_u

                    if total_days_planned >= 0 and curr_u < total_u:
                        required_pace = None; actual_pace = None
                        required_pace_text = "не определен"; actual_pace_text = "не определен"

                        if units_left <= 0: required_pace_text = "все сделано!"
                        elif days_left_for_calc > 0:
                            required_pace = units_left / days_left_for_calc
                            required_pace_text = f"{required_pace:.2f} ед./день"
                        else: required_pace_text = "срок вышел"

                        if curr_u > 0:
                            if days_passed > 0:
                                actual_pace = curr_u / days_passed
                                actual_pace_text = f"{actual_pace:.2f} ед./день"
                            elif days_passed == 0: actual_pace_text = "сделано сегодня"
                            else: actual_pace_text = "прогресс до старта (?)"
                        elif curr_u == 0 and days_passed >= 0 : actual_pace_text = "еще не начато"
                        else: actual_pace_text = "ожидание начала"

                        pace_details_for_button['required'] = required_pace_text
                        pace_details_for_button['actual'] = actual_pace_text

                        if required_pace_text == "все сделано!": forecast_str = "Отличная работа, всё сделано!"
                        elif required_pace_text == "срок вышел": forecast_str = "Срок вышел, не успели. 😥"
                        elif actual_pace_text == "сделано сегодня":
                             forecast_str = "Отличный старт! 👍" if units_left > 0 else "Всё сделано сегодня! 🎉"
                        elif actual_pace_text == "прогресс до старта (?)": forecast_str = "Необычно, но прогресс есть!"
                        elif actual_pace_text not in ["еще не начато", "ожидание начала"] and required_pace is not None and actual_pace is not None:
                            if actual_pace >= required_pace: forecast_str = "Успеваете! 👍"
                            else: forecast_str = "Нужно ускориться! 🏃💨"

                        if forecast_str: reply_lines.append(f"Прогноз: {forecast_str}")
                        else:
                            if pace_details_for_button.get('required') and pace_details_for_button.get('actual'):
                                reply_lines.append(f"Темп: (см. детали)")

                        if pace_details_for_button:
                            # Используем item_id_for_button для callback_data
                            pace_data_key = f"pace_details_for_{item_id_for_button}"
                            context.user_data[pace_data_key] = pace_details_for_button
                            keyboard_buttons = [[InlineKeyboardButton("Показать детали темпа", callback_data=f"{CALLBACK_SHOW_PACE_DETAILS_PREFIX}_{item_id_for_button}")]]
                            keyboard_markup = InlineKeyboardMarkup(keyboard_buttons)
                    elif curr_u >= total_u and total_u > 0 :
                         reply_lines.append("Прогноз: Завершено! 🎉")
                    else:
                        reply_lines.append("Темп: Недостаточно данных для расчета (проверьте дедлайн и общие единицы).")
                except Exception as e:
                    logger.error(f"Ошибка при расчете темпа для {item_id_for_button}: {e}", exc_info=True) # Используем item_id_for_button
                    reply_lines.append("Темп: Ошибка при расчете.")
            elif status_val == "active":
                 reply_lines.append("Темп: Невозможно рассчитать (нет дедлайна, цели в ед. или даты создания).")

            if item_type_db == "task" and found_item.get("project_id"):
                proj_id = found_item.get("project_id")
                proj = data.get("projects",{}).get(proj_id)
                if proj: reply_lines.append(f"Проект: {proj.get('name','Неизвестный')}")

        else: # Пользователь НЕ указал имя, выводим списки
            items_found_for_listing = False
            user_id_str_for_list = str(uid) # Используем user_id_str из начала функции

            # СПИСКИ ОБЩИХ ЭЛЕМЕНТОВ
            public_projects_list: List[dict] = []
            for p_id, p_data in data.get("projects", {}).items():
                if p_data.get("is_public") and p_data.get("status") == "active":
                    # Добавляем ID для внутренней сортировки, но не для отображения
                    p_data_copy = p_data.copy()
                    p_data_copy['id_internal_sort'] = p_id
                    public_projects_list.append(p_data_copy)

            public_tasks_list: List[dict] = []
            for t_id, t_data in data.get("tasks", {}).items():
                # Задача общая, если она сама public ИЛИ ее родительский проект public
                is_task_standalone_public = t_data.get("is_public")
                parent_project_id = t_data.get("project_id")
                is_task_parent_project_public = False
                if parent_project_id and parent_project_id in data.get("projects", {}):
                    if data["projects"][parent_project_id].get("is_public"):
                        is_task_parent_project_public = True
                
                if (is_task_standalone_public or is_task_parent_project_public) and t_data.get("status") == "active":
                    t_data_copy = t_data.copy()
                    t_data_copy['id_internal_sort'] = t_id
                    public_tasks_list.append(t_data_copy)


            if public_projects_list or public_tasks_list:
                reply_lines.append("\n📢 *Общие активные элементы:*")
                items_found_for_listing = True
                if public_projects_list:
                    reply_lines.append("  *Проекты:*")
                    for p_item in sorted(public_projects_list, key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower())):
                        dl_info = f"(до {p_item['deadline']})" if p_item.get('deadline') else "(без срока)"
                        prog = ""
                        if p_item.get("total_units", 0) > 0:
                            prog = f" [{p_item.get('current_units',0)}/{p_item['total_units']}]"
                        elif p_item.get('current_units', 0) > 0 :
                            prog = f" [{p_item.get('current_units',0)} ед.]"
                        reply_lines.append(f"    ▫️ {p_item['name']} {dl_info}{prog}")
                if public_tasks_list:
                    reply_lines.append("  *Задачи:*")
                    for t_item in sorted(public_tasks_list, key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower())):
                        dl_info = f"(до {t_item['deadline']})" if t_item.get('deadline') else "(без срока)"
                        prog = ""
                        if t_item.get("total_units", 0) > 0:
                            prog = f" [{t_item.get('current_units',0)}/{t_item['total_units']}]"
                        elif t_item.get('current_units', 0) > 0:
                             prog = f" [{t_item.get('current_units',0)} ед.]"
                        project_link_str = ""
                        if t_item.get("project_id"):
                            project_data_for_task = data.get("projects", {}).get(t_item["project_id"])
                            if project_data_for_task:
                                project_link_str = f" (Проект: _{project_data_for_task.get('name','?')} _)"
                        reply_lines.append(f"    ▫️ {t_item['name']}{project_link_str} {dl_info}{prog}")


            # СПИСКИ ЛИЧНЫХ ЭЛЕМЕНТОВ
            user_owned_projects: List[dict] = []
            for p_id, p_data in data.get("projects", {}).items():
                if str(p_data.get("owner_id")) == user_id_str_for_list and \
                   not p_data.get("is_public") and \
                   p_data.get("status") == "active":
                    p_data_copy = p_data.copy()
                    p_data_copy['id_internal_sort'] = p_id
                    user_owned_projects.append(p_data_copy)

            user_owned_tasks: List[dict] = []
            for t_id, t_data in data.get("tasks", {}).items():
                # Задача личная, если она принадлежит юзеру, сама не public, И ее родительский проект (если есть) тоже не public
                is_task_owner = str(t_data.get("owner_id")) == user_id_str_for_list
                is_task_not_public = not t_data.get("is_public")
                
                parent_project_id = t_data.get("project_id")
                parent_is_not_public_or_no_parent = True
                if parent_project_id and parent_project_id in data.get("projects", {}):
                    if data["projects"][parent_project_id].get("is_public"):
                        parent_is_not_public_or_no_parent = False # Если родительский проект публичный, то задача не попадет в личные по этому критерию

                if is_task_owner and is_task_not_public and parent_is_not_public_or_no_parent and \
                   t_data.get("status") == "active":
                    t_data_copy = t_data.copy()
                    t_data_copy['id_internal_sort'] = t_id
                    user_owned_tasks.append(t_data_copy)

            if user_owned_projects or user_owned_tasks:
                reply_lines.append("\n👤 *Ваши личные активные элементы:*")
                items_found_for_listing = True
                if user_owned_projects:
                    reply_lines.append("  *Проекты:*")
                    for p_item in sorted(user_owned_projects, key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower())):
                        dl_info = f"(до {p_item['deadline']})" if p_item.get('deadline') else "(без срока)"
                        prog = ""
                        if p_item.get("total_units", 0) > 0:
                            prog = f" [{p_item.get('current_units',0)}/{p_item['total_units']}]"
                        elif p_item.get('current_units', 0) > 0 :
                            prog = f" [{p_item.get('current_units',0)} ед.]"
                        # reply_lines.append(f"    `{p_item['id_internal_sort']}`: {p_item['name']} {dl_info}{prog}") # СТАРОЕ с ID
                        reply_lines.append(f"    ▫️ {p_item['name']} {dl_info}{prog}") # НОВОЕ без ID
                if user_owned_tasks:
                    reply_lines.append("  *Задачи:*")
                    for t_item in sorted(user_owned_tasks, key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower())):
                        dl_info = f"(до {t_item['deadline']})" if t_item.get('deadline') else "(без срока)"
                        prog = ""
                        if t_item.get("total_units", 0) > 0:
                            prog = f" [{t_item.get('current_units',0)}/{t_item['total_units']}]"
                        elif t_item.get('current_units', 0) > 0:
                             prog = f" [{t_item.get('current_units',0)} ед.]"
                        project_link_str = ""
                        if t_item.get("project_id"):
                            project_data_for_task = data.get("projects", {}).get(t_item["project_id"])
                            if project_data_for_task:
                                project_link_str = f" (Проект: _{project_data_for_task.get('name','?')} _)"
                        # reply_lines.append(f"    `{t_item['id_internal_sort']}`: {t_item['name']}{project_link_str} {dl_info}{prog}") # СТАРОЕ с ID
                        reply_lines.append(f"    ▫️ {t_item['name']}{project_link_str} {dl_info}{prog}") # НОВОЕ без ID

            if not items_found_for_listing:
                reply_lines.append("У вас нет активных элементов (ни общих, ни личных).")


        if reply_lines:
            final_reply_text = "\n".join(reply_lines)
            # Убираем заголовок "Общий статус", если выводим списки
            # if not item_name_hint and items_found_for_listing :
            #     final_reply_text = "🔍 *Ваш текущий статус:*\n" + final_reply_text
            await update.message.reply_text(final_reply_text, parse_mode='Markdown', reply_markup=keyboard_markup)
        else:
             await update.message.reply_text("Информации по вашему запросу не найдено или у вас нет активных элементов.")
        return None

    else:
        await update.message.reply_text(f"Не совсем понял ваш запрос: '{user_text}'. Попробуйте /help.")
    return None


def main():
    builder = Application.builder().token(BOT_TOKEN)
    logger.info("Инициализация Application без встроенной JobQueue (job_queue=None).")

    application = builder.build()

    # Conversation Handlers (без изменений)
    add_project_conv = ConversationHandler(
        entry_points=[CommandHandler('newproject', new_project_command)],
        states={ ASK_PROJECT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_project_name, block=True)],
                 ASK_PROJECT_DEADLINE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_project_deadline, block=True)]},
        fallbacks=[CommandHandler('cancel', universal_cancel)], name="project_creation"
    )
    add_task_conv = ConversationHandler(
        entry_points=[CommandHandler('newtask', new_task_command)],
        states={ ASK_TASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_task_name, block=True)],
                 ASK_TASK_PROJECT_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_task_project_link, block=True)],
                 ASK_TASK_DEADLINE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_task_deadline, block=True)]},
        fallbacks=[CommandHandler('cancel', universal_cancel)], name="task_creation"
    )
    update_progress_conv = ConversationHandler(
        entry_points=[CommandHandler('progress', progress_command)],
        states={
            ASK_PROGRESS_ITEM_TYPE: [CallbackQueryHandler(received_progress_item_type, pattern=r"^progress_item_type_(project|task|cancel)$")],
            ASK_PROGRESS_ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_progress_item_name_dialog, block=True)],
            ASK_PROGRESS_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_progress_description_dialog, block=True)],
        },
        fallbacks=[CommandHandler('cancel', universal_cancel)], name="update_progress_conversation"
    )

    # --- Добавление обработчиков ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    # --- НОВЫЕ ОБРАБОТЧИКИ КОМАНД ---
    application.add_handler(CommandHandler("public", toggle_public_command))
    application.add_handler(CommandHandler("reports", reports_preference_command))
    # --- КОНЕЦ НОВЫХ ОБРАБОТЧИКОВ ---

    application.add_handler(add_project_conv, group=1)
    application.add_handler(add_task_conv, group=1)
    application.add_handler(update_progress_conv, group=1)

    application.add_handler(CallbackQueryHandler(confirm_progress_update_callback, pattern=r"^confirm_progress_(yes|no)$"), group=1)
    application.add_handler(CallbackQueryHandler(show_pace_details_callback, pattern=f"^{CALLBACK_SHOW_PACE_DETAILS_PREFIX}_"), group=1)
    application.add_handler(CallbackQueryHandler(handle_parent_project_progress_no_thanks, pattern=f"^{CALLBACK_UPDATE_PARENT_PROJECT_PREFIX}_no_"), group=1)
    application.add_handler(CallbackQueryHandler(handle_parent_project_progress_yes, pattern=f"^{CALLBACK_UPDATE_PARENT_PROJECT_PREFIX}_yes_"), group=1)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message), group=2)

    job_queue = application.job_queue
    if job_queue: # Убедимся, что job_queue существует
        report_time_utc = time(hour=4, minute=30, tzinfo=pytz.utc) # 9:30 Almaty (UTC+5) = 4:30 UTC
                                                               # Если Almaty UTC+6, то 3:30 UTC
        # Уточните ваш часовой пояс для Алматы. Если Asia/Almaty это UTC+5, то 9:30 - 5 = 4:30 UTC
        # Если Asia/Almaty это UTC+6, то 9:30 - 6 = 3:30 UTC
        # Я поставлю для UTC+5 (то есть 4:30 UTC) для примера.
        # ВАЖНО: JobQueue работает с UTC временем, если не указано иное явно для самой JobQueue
        # Либо можно использовать локальное время сервера и убедиться, что сервер в правильном TZ.
        # Наиболее надежно указывать время в UTC.

        # Давайте используем правильный подход с tzinfo для времени
        almaty_tz = pytz.timezone('Asia/Almaty')
        report_time_almaty = time(hour=9, minute=30, tzinfo=almaty_tz)

        job_queue.run_daily(
            send_daily_reports,
            time=report_time_almaty, # Передаем время с указанием часового пояса
            name="daily_report_job"
        )
        logger.info(f"Ежедневные отчеты запланированы на {report_time_almaty.strftime('%H:%M %Z%z')}")
    else:
        logger.error("JobQueue не инициализирована! Ежедневные отчеты не будут работать.")
    # --- КОНЕЦ ПЛАНИРОВАНИЯ ---

    logger.info("Запуск бота...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Бот остановлен.")

if __name__ == '__main__':
    if not os.getenv('GEMINI_API_KEY'): logger.error("GEMINI_API_KEY не установлен!"); exit()
    try: import pytz; import tzlocal
    except ImportError: logger.error("pytz или tzlocal не установлены! `pip install pytz tzlocal`"); exit()
    if not os.getenv('TZ'): 
        # Установка системного часового пояса для сессии Python, если TZ не задана
        # Это может быть полезно, если локальное время сервера используется где-то еще,
        # но для JobQueue мы явно указываем tzinfo.
        try:
            os.environ['TZ'] = tzlocal.get_localzone_name()
            time.tzset() # Unix-like systems
            logger.info(f"Системный TZ для Python установлен в '{os.environ['TZ']}' (локальный).")
        except Exception as e:
            os.environ['TZ'] = 'UTC' # Fallback
            time.tzset()
            logger.warning(f"Не удалось определить локальный TZ, установлен в 'UTC'. Ошибка: {e}")
    else: logger.info(f"Используется TZ из окружения: {os.getenv('TZ')}")
    main()