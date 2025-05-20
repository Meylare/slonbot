# conversations.py
import logging
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
import pytz

from constants import (
    ASK_PROJECT_NAME, ASK_PROJECT_DEADLINE, ASK_PROJECT_GOAL,
    ACTIVE_CONVERSATION_KEY, ADD_PROJECT_CONV_STATE_VALUE,
    LAST_PROCESSED_IN_CONV_MSG_ID_KEY,
    ASK_TASK_NAME, ASK_TASK_PROJECT_LINK, ASK_TASK_DEADLINE_STATE, ASK_TASK_GOAL,
    ADD_TASK_CONV_STATE_VALUE, NEW_TASK_INFO_KEY,
    PENDING_PROGRESS_UPDATE_KEY,
    ASK_PROGRESS_ITEM_TYPE, ASK_PROGRESS_ITEM_NAME, ASK_PROGRESS_DESCRIPTION, 
    UPDATE_PROGRESS_CONV_STATE_VALUE, ITEM_FOR_PROGRESS_UPDATE_KEY,
    CALLBACK_UPDATE_PARENT_PROJECT_PREFIX # Для кнопок Да/Нет при обновлении проекта
)
   
from utils import parse_natural_deadline_to_date, generate_id
from data_handler import load_data, save_data, find_item_by_name_or_id
from llm_handler import interpret_progress_description

logger = logging.getLogger(__name__)

# --- Диалог для ПРОЕКТОВ ---
async def new_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; logger.debug(f"/newproject от {uid}")
    context.user_data.pop('new_project_info', None)
    context.user_data[ACTIVE_CONVERSATION_KEY] = ADD_PROJECT_CONV_STATE_VALUE
    await update.message.reply_text("Название нового проекта? (/cancel для отмены)")
    return ASK_PROJECT_NAME

async def received_project_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; name = update.message.text.strip()
    if not name: await update.message.reply_text("Название не может быть пустым. Или /cancel."); return ASK_PROJECT_NAME
    context.user_data['new_project_info'] = {'name': name}
    await update.message.reply_text(f"Проект '{name}'. Дедлайн? ('нет', 'завтра', ДД.ММ.ГГГГ) /cancel")
    return ASK_PROJECT_DEADLINE

async def received_project_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Эта функция теперь будет спрашивать про цель
    uid = update.effective_user.id; deadline_txt = update.message.text.strip().lower()
    info = context.user_data.get('new_project_info')
    if not info or 'name' not in info: # Маловероятно, но для безопасности
        await update.message.reply_text("Ошибка данных. Пожалуйста, начните заново с /newproject")
        context.user_data.pop('new_project_info', None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        return ConversationHandler.END

    final_dl_str = None
    dl_msg_part = "без дедлайна"
    if deadline_txt not in ['нет', 'пропустить', 'no', 'skip', '']:
        parsed_dl = parse_natural_deadline_to_date(deadline_txt)
        if parsed_dl:
            final_dl_str = parsed_dl.strftime('%Y-%m-%d')
            dl_msg_part = f"с дедлайном {final_dl_str}"
        else:
            await update.message.reply_text(f"Не удалось распознать дату '{deadline_txt}'. Попробуйте еще раз или введите 'нет'/'пропустить'. /cancel")
            return ASK_PROJECT_DEADLINE # Остаемся на том же шаге

    info['deadline'] = final_dl_str
    info['deadline_message_part'] = dl_msg_part # Сохраняем для финального сообщения
    context.user_data['new_project_info'] = info

    await update.message.reply_text(f"Проект '{info['name']}' ({dl_msg_part}).\n"
                                    "Какой общий объем проекта в единицах или процентах?\n"
                                    "(например, 100 для отслеживания в %, или количество подзадач, часов и т.д.)\n"
                                    "Введите число или /пропустить (будет 100 по умолчанию). /cancel")
    return ASK_PROJECT_GOAL # Переходим к запросу цели

async def received_project_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    goal_input_text = update.message.text.strip()
    info = context.user_data.get('new_project_info')

    if not info or 'name' not in info or 'deadline_message_part' not in info: # Проверка полноты info
        await update.message.reply_text("Произошла ошибка с данными проекта. Начните заново /newproject.")
        context.user_data.pop('new_project_info', None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id
        return ConversationHandler.END

    project_name = info['name']
    final_dl_str = info.get('deadline') # Может быть None
    dl_msg = info['deadline_message_part']
    total_units = 100 # Значение по умолчанию

    if goal_input_text.lower() not in ['/пропустить', 'пропустить', 'skip', '']:
        try:
            parsed_goal = int(goal_input_text)
            if parsed_goal > 0:
                total_units = parsed_goal
            else:
                await update.message.reply_text("Объем должен быть положительным числом. Попробуйте еще раз или /пропустить (для 100). /cancel")
                return ASK_PROJECT_GOAL
        except ValueError:
            await update.message.reply_text("Не удалось распознать число. Введите объем или /пропустить (для 100). /cancel")
            return ASK_PROJECT_GOAL
    
    goal_msg = f"с целью в {total_units} ед."

    data = load_data()
    new_id = generate_id("proj")
    created_at = datetime.now(pytz.utc).isoformat()
    data.setdefault("projects", {})
    data["projects"][new_id] = {
        "id": new_id, "name": project_name, "deadline": final_dl_str,
        "owner_id": str(uid), "created_at": created_at, "status": "active",
        "total_units": total_units, # ИСПОЛЬЗУЕМ НОВУЮ ЦЕЛЬ
        "current_units": 0, "last_report_day_counter": 0,
        "is_public": False
    }
    save_data(data)
    await update.message.reply_text(f"🎉 Проект '{project_name}' {dl_msg} {goal_msg} создан!\nID: `{new_id}`", parse_mode='Markdown')
    
    context.user_data.pop('new_project_info', None)
    context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
    context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id
    return ConversationHandler.END


# --- Диалог для ЗАДАЧ ---
async def new_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; logger.debug(f"/newtask от {uid}")
    context.user_data.pop(NEW_TASK_INFO_KEY, None)
    context.user_data[ACTIVE_CONVERSATION_KEY] = ADD_TASK_CONV_STATE_VALUE
    await update.message.reply_text("Название новой задачи? (/cancel)")
    return ASK_TASK_NAME

async def received_task_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; task_name = update.message.text.strip()
    if not task_name: await update.message.reply_text("Название не может быть пустым. /cancel"); return ASK_TASK_NAME
    context.user_data[NEW_TASK_INFO_KEY] = {'name': task_name}
    await update.message.reply_text(f"Задача: '{task_name}'.\nПроект? (название/ID или 'нет') /cancel")
    return ASK_TASK_PROJECT_LINK

async def received_task_project_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; project_input = update.message.text.strip().lower()
    task_info = context.user_data.get(NEW_TASK_INFO_KEY)
    if not task_info or 'name' not in task_info:
        await update.message.reply_text("Ошибка данных. /newtask"); context.user_data.pop(NEW_TASK_INFO_KEY, None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id; return ConversationHandler.END
    
    project_id, project_fb_msg = None, "без привязки к проекту"
    if project_input not in ['нет', 'пропустить', 'no', 'skip', '']:
        found_project = find_item_by_name_or_id(project_input, "project", load_data())
        if found_project:
            project_id = found_project["id"]
            project_fb_msg = f"к проекту '{found_project['name']}'"
        else:
            await update.message.reply_text(f"Проект '{project_input}' не найден. Попробуйте еще раз или введите 'нет'/'пропустить'. /cancel")
            return ASK_TASK_PROJECT_LINK
            
    task_info['project_id'] = project_id
    task_info['project_feedback'] = project_fb_msg
    context.user_data[NEW_TASK_INFO_KEY] = task_info
    await update.message.reply_text(f"Задача '{task_info['name']}' ({project_fb_msg}).\nДедлайн? ('завтра', 'нет') /cancel")
    return ASK_TASK_DEADLINE_STATE

async def received_task_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # Эта функция теперь будет спрашивать про цель задачи
    uid = update.effective_user.id; deadline_txt = update.message.text.strip().lower()
    task_info = context.user_data.get(NEW_TASK_INFO_KEY)
    if not task_info or 'name' not in task_info: # Проверка
        await update.message.reply_text("Ошибка данных. Пожалуйста, начните заново с /newtask")
        context.user_data.pop(NEW_TASK_INFO_KEY, None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        return ConversationHandler.END

    final_dl_str = None
    dl_msg_part = "без дедлайна"
    if deadline_txt not in ['нет', 'пропустить', 'no', 'skip', '']:
        parsed_dl = parse_natural_deadline_to_date(deadline_txt)
        if parsed_dl:
            final_dl_str = parsed_dl.strftime('%Y-%m-%d')
            dl_msg_part = f"с дедлайном {final_dl_str}"
        else:
            await update.message.reply_text(f"Не удалось распознать дату '{deadline_txt}'. Попробуйте еще раз или введите 'нет'/'пропустить'. /cancel")
            return ASK_TASK_DEADLINE_STATE

    task_info['deadline'] = final_dl_str
    task_info['deadline_message_part'] = dl_msg_part
    context.user_data[NEW_TASK_INFO_KEY] = task_info

    project_fb = task_info.get('project_feedback', "без привязки") # Получаем из сохраненного
    await update.message.reply_text(f"Задача '{task_info['name']}' ({project_fb}) ({dl_msg_part}).\n"
                                    "Какой общий объем задачи в единицах?\n"
                                    "(например, кол-во шагов, страниц и т.д.)\n"
                                    "Введите число или /пропустить (цель не будет задана - 0 ед.). /cancel")
    return ASK_TASK_GOAL # Переходим к запросу цели

async def received_task_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    goal_input_text = update.message.text.strip()
    task_info = context.user_data.get(NEW_TASK_INFO_KEY)

    if not task_info or 'name' not in task_info or 'deadline_message_part' not in task_info: # Проверка
        await update.message.reply_text("Произошла ошибка с данными задачи. Начните заново /newtask.")
        context.user_data.pop(NEW_TASK_INFO_KEY, None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id
        return ConversationHandler.END

    task_name = task_info['name']
    project_id = task_info.get('project_id')
    project_fb = task_info.get('project_feedback', "без привязки")
    final_dl_str = task_info.get('deadline')
    dl_msg = task_info['deadline_message_part']
    total_units = 0 # Значение по умолчанию для задач

    if goal_input_text.lower() not in ['/пропустить', 'пропустить', 'skip', '']:
        try:
            parsed_goal = int(goal_input_text)
            if parsed_goal > 0:
                total_units = parsed_goal
            else:
                await update.message.reply_text("Объем должен быть положительным числом. Попробуйте еще раз или /пропустить (для 0). /cancel")
                return ASK_TASK_GOAL
        except ValueError:
            await update.message.reply_text("Не удалось распознать число. Введите объем или /пропустить (для 0). /cancel")
            return ASK_TASK_GOAL
    
    goal_msg = f"с целью в {total_units} ед." if total_units > 0 else "без указания цели"

    data = load_data()
    new_id = generate_id("task")
    created_at = datetime.now(pytz.utc).isoformat()
    data.setdefault("tasks", {})
    data["tasks"][new_id] = {
        "id": new_id, "name": task_name, "deadline": final_dl_str,
        "project_id": project_id, "owner_id": str(uid),
        "created_at": created_at, "status": "active",
        "total_units": total_units, # ИСПОЛЬЗУЕМ НОВУЮ ЦЕЛЬ
        "current_units": 0,
        "is_public": False
    }
    save_data(data)
    await update.message.reply_text(f"💪 Задача '{task_name}' ({project_fb}) {dl_msg} {goal_msg} создана!\nID: `{new_id}`", parse_mode='Markdown')
    
    context.user_data.pop(NEW_TASK_INFO_KEY, None)
    context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
    context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id
    return ConversationHandler.END
    
# --- Диалог для ОБНОВЛЕНИЯ ПРОГРЕССА (запускается командой /progress) ---
async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; logger.debug(f"/progress от {uid}")
    context.user_data.pop(ITEM_FOR_PROGRESS_UPDATE_KEY, None); context.user_data[ACTIVE_CONVERSATION_KEY] = UPDATE_PROGRESS_CONV_STATE_VALUE
    logger.debug(f"Для {uid} установлен {ACTIVE_CONVERSATION_KEY}={UPDATE_PROGRESS_CONV_STATE_VALUE}")
    keyboard = [[InlineKeyboardButton("Проект",callback_data="progress_item_type_project"),InlineKeyboardButton("Задачу",callback_data="progress_item_type_task")],[InlineKeyboardButton("Отмена",callback_data="progress_item_type_cancel")]]
    await update.message.reply_text("Прогресс для чего обновить?",reply_markup=InlineKeyboardMarkup(keyboard)); return ASK_PROGRESS_ITEM_TYPE
async def received_progress_item_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer(); uid = update.effective_user.id; choice = query.data
    if choice == "progress_item_type_cancel":
        await query.edit_message_text("Обновление отменено."); context.user_data.pop(ITEM_FOR_PROGRESS_UPDATE_KEY, None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        # LAST_PROCESSED_IN_CONV_MSG_ID_KEY не нужен для callback отмены
        return ConversationHandler.END
    item_type = "project" if choice == "progress_item_type_project" else "task"; context.user_data[ITEM_FOR_PROGRESS_UPDATE_KEY] = {'llm_item_type': item_type}
    type_rus = "проекта" if item_type == "project" else "задачи"; await query.edit_message_text(f"ID или название {type_rus}? (/cancel)"); return ASK_PROGRESS_ITEM_NAME
async def received_progress_item_name_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; name_hint = update.message.text.strip(); item_info = context.user_data.get(ITEM_FOR_PROGRESS_UPDATE_KEY, {}); llm_item_type = item_info.get('llm_item_type')
    if not name_hint: await update.message.reply_text("Имя/ID не может быть пустым. /cancel"); return ASK_PROGRESS_ITEM_NAME
    found_item = find_item_by_name_or_id(name_hint, llm_item_type, load_data())
    if not found_item: await update.message.reply_text(f"Не нашел '{name_hint}'. /cancel"); return ASK_PROGRESS_ITEM_NAME
    item_info.update({'id':found_item['id'],'name':found_item['name'],'item_type_db':found_item['item_type_db'],'current_units':found_item.get('current_units',0),'total_units':found_item.get('total_units',0)})
    context.user_data[ITEM_FOR_PROGRESS_UPDATE_KEY] = item_info
    await update.message.reply_text(f"Обновляем {found_item['item_type_db']} '{found_item['name']}'.\nКак прогресс? ('+5', '50%', 'готово') /cancel"); return ASK_PROGRESS_DESCRIPTION

async def received_progress_description_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; prog_desc = update.message.text.strip(); item_info = context.user_data.get(ITEM_FOR_PROGRESS_UPDATE_KEY)
    if not item_info or not item_info.get('id'):
        await update.message.reply_text("Ошибка данных. /progress"); context.user_data.pop(ITEM_FOR_PROGRESS_UPDATE_KEY, None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id; return ConversationHandler.END

    item_original_name = item_info['name']

    total_u = item_info.get('total_units',0); current_u = item_info.get('current_units',0)
    prog_interp = await interpret_progress_description(prog_desc, total_u if total_u > 0 else 100)
    if not prog_interp or prog_interp.get("type")=="unknown" or prog_interp.get("value") is None:
        await update.message.reply_text(f"Не понял описание: '{prog_desc}'. Еще раз. /cancel"); return ASK_PROGRESS_DESCRIPTION
    new_calc=-1; p_type=prog_interp.get("type"); p_val_str=str(prog_interp.get("value","0")); p_val=0
    try:p_val=int(float(p_val_str))
    except ValueError: await update.message.reply_text(f"Ошибка значения от LLM: {p_val_str}. /cancel"); return ASK_PROGRESS_DESCRIPTION
    if p_type=="units":new_calc=current_u+p_val
    elif p_type=="percent":base=total_u if total_u>0 else 100;new_calc=round((p_val/100)*base);_=new_calc=p_val if total_u==0 else new_calc
    elif p_type=="absolute_units_set":new_calc=p_val
    elif p_type=="complete":new_calc=total_u if total_u>0 else 100
    if new_calc<0:new_calc=0
    if total_u>0 and new_calc>total_u:new_calc=total_u
    if new_calc==-1: await update.message.reply_text(f"Не вычислил прогресс из: '{prog_desc}'. /cancel"); return ASK_PROGRESS_DESCRIPTION
    if new_calc==current_u: 
        await update.message.reply_text(f"Прогресс для '{item_info['name']}' не изменился. Завершаю."); context.user_data.pop(ITEM_FOR_PROGRESS_UPDATE_KEY, None); context.user_data.pop(ACTIVE_CONVERSATION_KEY, None)
        context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id; return ConversationHandler.END
    
    pending_cb = {
        'item_id': item_info['id'],
        'item_name': item_info['name'], # Используем значение из item_info['name']
        'item_type_db': item_info['item_type_db'],
        'total_units': item_info.get('total_units', 0), # Явно передаем total_units
        'new_current_units': new_calc,
        'old_current_units': current_u,
        'action_type': 'update'
    }
    await ask_for_progress_confirmation(update,context,pending_cb) # Эта функция вызовет кнопки
    context.user_data.pop(ACTIVE_CONVERSATION_KEY, None); context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id
    return ConversationHandler.END # Этот диалог завершен, дальше кнопки

# --- Функции для кнопок подтверждения ПРОГРЕССА / ЗАВЕРШЕНИЯ ---
async def ask_for_progress_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, item_info: dict):
    logger.debug(f"ask_for_progress_confirmation получила item_info: {item_info}") # Добавим лог для отладки

    item_name = item_info['item_name']
    new_units = item_info['new_current_units']
    old_units = item_info['old_current_units']
    total_units = item_info.get('total_units', 0)
    action_type = item_info.get('action_type', 'update') # <<<--- УБЕДИТЕСЬ, ЧТО ЭТА СТРОКА ЕСТЬ И ПРАВИЛЬНА
    
    text = ""
    if action_type == 'complete':
        # item_type_db должен быть в item_info, если action_type == 'complete'
        item_type_display = item_info.get('item_type_db', 'элемент').capitalize() 
        text = f"Завершить {item_type_display} '{item_name}'?"
        if total_units > 0:
            text += f"\n(Прогресс будет установлен на {new_units}/{total_units})"
        elif new_units == 100: # Если total_units был 0, и мы завершаем до 100
            text += f"\n(Прогресс будет отмечен как 100%)"
    else: # action_type == 'update'
        text = f"Обновить прогресс для '{item_name}' с {old_units} до {new_units}"
        if total_units > 0:
            text += f" (из {total_units})?"
        else:
            text += "?"
    
    keyboard = [[
        InlineKeyboardButton("✅ Да", callback_data="confirm_progress_yes"),
        InlineKeyboardButton("❌ Нет", callback_data="confirm_progress_no")
    ]]
    # Сохраняем всю item_info (включая action_type) для confirm_progress_update_callback
    context.user_data[PENDING_PROGRESS_UPDATE_KEY] = item_info 
    logger.debug(f"Сохранено в PENDING_PROGRESS_UPDATE_KEY: {item_info} для {update.effective_user.id}")
    
    # Отправляем сообщение с кнопками
    if update.message: # Если это ответ на обычное сообщение
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query and update.callback_query.message: # Если это ответ на callback (маловероятно для этого вызова)
        # Обычно кнопки подтверждения вызываются после текстового сообщения, а не другого callback
        await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def confirm_progress_update_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query; await query.answer(); user_choice = query.data; user_id = update.effective_user.id
    original_message_id = query.message.message_id; chat_id = query.message.chat_id
    pending_update = context.user_data.pop(PENDING_PROGRESS_UPDATE_KEY, None) 
    if not pending_update: logger.warning(f"Нет PENDING_PROGRESS_UPDATE_KEY для {user_id}"); await query.edit_message_text("Ошибка."); return
    item_id = pending_update['item_id']; item_type_db = pending_update['item_type_db']; item_name = pending_update['item_name']
    new_units = pending_update['new_current_units']; action_type = pending_update.get('action_type', 'update')
    data = load_data(); item_pool_name = "projects" if item_type_db == "project" else "tasks"; item_pool = data.get(item_pool_name, {})
    if user_choice == "confirm_progress_yes":
        if item_id in item_pool:
            item_to_update = item_pool[item_id]; item_to_update['current_units'] = new_units; success_message = f"Прогресс для '{item_name}' обновлен до {new_units}."
            project_to_prompt_for_update_after_task = None
            if action_type == 'complete':
                item_to_update['status'] = 'completed'
                if item_to_update.get('total_units', 0) == 0 and new_units == 100: item_to_update['total_units'] = 100
                success_message = f"👍 {item_type_db.capitalize()} '{item_name}' завершен!"
                logger.info(f"{item_type_db.capitalize()} '{item_name}' (ID:{item_id}) ЗАВЕРШЕН юзером {user_id}.")
                # Логика для связанного проекта (B2) - ПРОСТОЕ ПРЕДЛОЖЕНИЕ +1
                if item_type_db == "task" and item_to_update.get("project_id"):
                    proj_id = item_to_update["project_id"]
                    if proj_id in data.get("projects", {}):
                        project_to_update_after_task = data["projects"][proj_id]
                        project_to_update_after_task["id"] = proj_id 
            save_data(data); await query.edit_message_text(success_message) 
            if action_type != 'complete': logger.info(f"Прогресс для {item_type_db} '{item_name}' ({item_id}) обновлен на {new_units} юзером {user_id}.")
            if project_to_prompt_for_update_after_task: 
                proj_name = project_to_prompt_for_update_after_task.get('name', 'Неизвестный проект')
                units_to_add = 1 
                keyboard_proj = [[
                    InlineKeyboardButton(f"Да (+{units_to_add} ед.)", callback_data=f"{CALLBACK_UPDATE_PARENT_PROJECT_PREFIX}_yes_{project_to_prompt_for_update_after_task['id']}_{units_to_add}"),
                    InlineKeyboardButton("Нет, спасибо", callback_data=f"{CALLBACK_UPDATE_PARENT_PROJECT_PREFIX}_no_{project_to_prompt_for_update_after_task['id']}_0"),
                ]]
                await context.bot.send_message(chat_id=chat_id, text=f"Задача '{item_name}' завершена. Добавить {units_to_add} ед. прогресса к проекту '{proj_name}'?", reply_markup=InlineKeyboardMarkup(keyboard_proj))
        else: await query.edit_message_text(f"Не найден {item_type_db} '{item_name}'."); logger.warning(f"{item_type_db} ID {item_id} не найден.")
    else: 
        final_message = "Завершение отменено." if action_type == 'complete' else "Обновление прогресса отменено."
        await query.edit_message_text(final_message)
    context.user_data.pop(ITEM_FOR_PROGRESS_UPDATE_KEY, None)

# --- Универсальная функция отмены ---
async def universal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id; active_conv = context.user_data.get(ACTIVE_CONVERSATION_KEY)
    logger.info(f"Пользователь {uid} отменил диалог (был активен: {active_conv}).")
    context.user_data.pop('new_project_info', None); context.user_data.pop(NEW_TASK_INFO_KEY, None)
    context.user_data.pop(ITEM_FOR_PROGRESS_UPDATE_KEY, None) 
    # context.user_data.pop(PROJECT_ID_FOR_PROGRESS_ADD_KEY, None) # Этот ключ не используется в этой версии
    context.user_data.pop(ACTIVE_CONVERSATION_KEY, None); logger.debug(f"Для {uid} снят {ACTIVE_CONVERSATION_KEY}.")
    context.user_data[LAST_PROCESSED_IN_CONV_MSG_ID_KEY] = update.message.message_id
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END