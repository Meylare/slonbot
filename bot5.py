# main_bot.py
import asyncio
import json
import logging
from datetime import datetime, date, timedelta 
import uuid 
import os
from typing import Union

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    level=logging.DEBUG
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS_STR = os.getenv('ADMIN_IDS', '0')
ADMIN_USER_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id.strip() and admin_id.strip() != '0']
if not BOT_TOKEN: logger.error("BOT_TOKEN не найден!"); exit()
if not ADMIN_USER_IDS : logger.warning("ADMIN_IDS не настроены в main_bot.py.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; data = load_data() 
    user_id_str = str(user.id)
    is_admin_now = is_user_admin_from_data(user.id, data) 
    if user_id_str not in data["users"]:
        data["users"][user_id_str] = {"username": user.username or f"User_{user_id_str}", "receive_reports": True, "is_admin": is_admin_now, "timezone": "UTC"}
    else: data["users"][user_id_str].update({"is_admin": is_admin_now, "username": user.username or data["users"][user_id_str].get("username", f"User_{user_id_str}")})
    save_data(data); logger.info(f"User {user.id} ({user.username}) started/updated. Admin: {is_admin_now}")
    await update.message.reply_text(f"Привет, {user.first_name}! Я ваш менеджер проектов. /help")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_text = ""
    if is_user_admin_from_data(update.effective_user.id, load_data()):
        admin_text = "\n\n👑 *Админ-команды* (в разработке):\n..." 
    help_msg = ("🤖 *Команды:*\n/start, /help\n/newproject - создать проект\n/newtask - создать задачу\n/progress - обновить прогресс\n\n"
                "💡 *Общение в свободной форме:*\n'создай проект X дедлайн Y'\n'добавь задачу Z для проекта X'\n'прогресс по задаче X +5'" + admin_text)
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
    logger.debug(f"NLU result for '{user_text}': {nlu_result}") # DEBUG LOG FOR NLU

    if not nlu_result or "intent" not in nlu_result: 
        logger.warning(f"NLU failed or no intent for '{user_text}'. NLU_Result: {nlu_result}. User ID: {uid}")
        await update.message.reply_text("Не понял ваш запрос. Попробуйте переформулировать или используйте /help."); 
        return None
        
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
            data["projects"][new_id]={"id":new_id,"name":name,"deadline":final_dl,"owner_id":user_id_str,"created_at":created_at,"status":"active","total_units":0,"current_units":0,"last_report_day_counter":0, "is_public": False}
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
            data["tasks"][new_id]={"id":new_id,"name":task_name,"deadline":final_dl,"project_id":proj_id,"owner_id":user_id_str,"created_at":created_at,"status":"active","total_units":0,"current_units":0, "is_public": False}
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
            'item_id': item_id, 'item_name': item_name_val, 'item_type_db': item_type_db_val,
            'new_current_units': new_calc_units, 'old_current_units': current_units_val, 
            'total_units': total_units_val, 'action_type': 'update'
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

    elif intent == "query_status":
        item_name_hint = entities.get("item_name_hint"); item_type_llm = entities.get("item_type")
        reply_lines = []; keyboard_markup = None
        pace_details_for_button = {} 

        if item_name_hint: 
            found_item = find_item_by_name_or_id(item_name_hint, item_type_llm, data)
            if not found_item: 
                await update.message.reply_text(f"Не нашел '{item_name_hint}'."); 
                return None # Exit if specific item not found
            
            item_id=found_item['id']; item_name=found_item['name']; item_type_db=found_item['item_type_db']
            curr_u=found_item.get('current_units',0); total_u=found_item.get('total_units',0)
            status_val=found_item.get('status','активен'); dl_str=found_item.get('deadline')
            created_at_iso = found_item.get("created_at") 

            s_icon = "✅" if status_val=="completed" else ("⏳" if status_val=="active" else "❓")
            item_type_rus_single = "Проект" if item_type_db=="project" else "Задача"
            reply_lines.append(f"{s_icon} *{item_type_rus_single}: {item_name}* (ID: `{item_id}`)")
            reply_lines.append(f"Статус: {status_val.capitalize()}")
            
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
            
            forecast_str = None # Moved definition higher
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

                    logger.debug(
                        f"Темп - Входные данные для '{item_name}' (ID: {item_id}):\n"
                        f"  Создан: {created_date}, Дедлайн: {deadline_date}, Сегодня: {today}\n"
                        f"  Всего дней по плану: {total_days_planned}\n"
                        f"  Дней прошло: {days_passed}, Дней осталось: {days_left_for_calc}\n"
                        f"  Текущий прогресс: {curr_u}, Всего единиц: {total_u}"
                    )

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
                        logger.debug(f"Темп-РАСЧЕТ: required='{required_pace_text}', actual='{actual_pace_text}'")
                        
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
                            pace_data_key = f"pace_details_for_{item_id}"
                            context.user_data[pace_data_key] = pace_details_for_button
                            keyboard_buttons = [[InlineKeyboardButton("Показать детали темпа", callback_data=f"{CALLBACK_SHOW_PACE_DETAILS_PREFIX}_{item_id}")]]
                            keyboard_markup = InlineKeyboardMarkup(keyboard_buttons)
                            logger.debug(f"Темп-СОХРАНЕНО для кнопки: key='{pace_data_key}', details={pace_details_for_button}")
                    elif curr_u >= total_u and total_u > 0 : 
                         reply_lines.append("Прогноз: Завершено! 🎉")
                    else: 
                        reply_lines.append("Темп: Недостаточно данных для расчета (проверьте дедлайн и общие единицы).")
                except Exception as e:
                    logger.error(f"Ошибка при расчете темпа для {item_id}: {e}", exc_info=True)
                    reply_lines.append("Темп: Ошибка при расчете.")
            elif status_val == "active": 
                 reply_lines.append("Темп: Невозможно рассчитать (нет дедлайна, цели в ед. или даты создания).")

            if item_type_db == "task" and found_item.get("project_id"):
                proj_id = found_item.get("project_id")
                proj = data.get("projects",{}).get(proj_id)
                if proj: reply_lines.append(f"Проект: {proj.get('name','Неизвестный')}")
        
        else: # No item_name_hint, general status query
            items_found_for_listing = False 

            # --- Показываем ПРОЕКТЫ ---
            if item_type_llm == "project" or item_type_llm is None:
                user_projects = []
                for p_id, p_data in data.get("projects", {}).items(): 
                    is_owned_by_user = str(p_data.get("owner_id")) == user_id_str
                    is_active = p_data.get("status") == "active"
                    logger.debug(f"Проверка проекта для списка: ID={p_id}, Name='{p_data.get('name')}', OwnerMatch={is_owned_by_user}, Status='{p_data.get('status')}', IsActiveForList={is_active}")
                    if is_owned_by_user and is_active: # CORRECTED INDENTATION
                        p_data_copy = p_data.copy() # Avoid modifying original data if adding id
                        if 'id' not in p_data_copy: p_data_copy['id'] = p_id # Ensure id is present
                        user_projects.append(p_data_copy)
                
                if user_projects:
                    reply_lines.append("*Ваши активные проекты:*")
                    for p_item in sorted(user_projects, key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower())):
                        dl_info = f"(до {p_item['deadline']})" if p_item.get('deadline') else "(без срока)"
                        prog = ""
                        if p_item.get("total_units", 0) > 0:
                            prog = f" [{p_item.get('current_units',0)}/{p_item['total_units']}]"
                        elif p_item.get('current_units', 0) > 0 : 
                            prog = f" [{p_item.get('current_units',0)} ед.]"
                        reply_lines.append(f"  `{p_item['id']}`: {p_item['name']} {dl_info} {prog}")
                    items_found_for_listing = True
                elif item_type_llm == "project": # Searched only for projects and none active
                    reply_lines.append("У вас нет активных проектов.")
                    items_found_for_listing = True 

            # --- Показываем ЗАДАЧИ ---
            if item_type_llm == "task" or item_type_llm is None:
                user_tasks = []
                for t_id, t_data in data.get("tasks", {}).items():
                    is_owned_by_user = str(t_data.get("owner_id")) == user_id_str
                    is_active = t_data.get("status") == "active"
                    logger.debug(f"Проверка задачи для списка: ID={t_id}, Name='{t_data.get('name')}', OwnerMatch={is_owned_by_user}, Status='{t_data.get('status')}', IsActiveForList={is_active}")
                    if is_owned_by_user and is_active: # CORRECTED INDENTATION
                        t_data_copy = t_data.copy()
                        if 'id' not in t_data_copy: t_data_copy['id'] = t_id
                        user_tasks.append(t_data_copy)
                
                if user_tasks:
                    if items_found_for_listing and reply_lines: 
                        reply_lines.append("") 
                    reply_lines.append("*Ваши активные задачи:*")
                    for t_item in sorted(user_tasks, key=lambda x: (x.get("deadline") or "9999", x.get("name", "").lower())):
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
                        reply_lines.append(f"  `{t_item['id']}`: {t_item['name']}{project_link_str} {dl_info} {prog}")
                    items_found_for_listing = True
                elif item_type_llm == "task": # Searched only for tasks and none active
                    reply_lines.append("У вас нет активных задач.")
                    items_found_for_listing = True 
            
            if not items_found_for_listing:
                if not reply_lines: # Ensure we don't add this if specific "no projects/tasks" was already added
                    reply_lines.append("У вас нет активных проектов или задач. Время что-нибудь создать! 😊")
                    # items_found_for_listing = True # Set to true as we are providing a response
        
        # --- Отправка сообщения (ЕДИНЫЙ УПРОЩЕННЫЙ БЛОК) ---
        if reply_lines:
            final_reply_text = "\n".join(reply_lines)
            
            # Add a general header only if item_name_hint was None and we are showing actual lists of items
            has_actual_list_content = False
            if not item_name_hint:
                if any("*Ваши активные проекты:*" in line for line in reply_lines) or \
                   any("*Ваши активные задачи:*" in line for line in reply_lines):
                    has_actual_list_content = True
            
            if has_actual_list_content: # Only add this generic header if we are listing items.
                 final_reply_text = "🔍 *Ваш текущий статус:*\n" + final_reply_text
            
            await update.message.reply_text(final_reply_text, parse_mode='Markdown', reply_markup=keyboard_markup)
        else:
            # This 'else' branch is for cases where reply_lines is unexpectedly empty.
            # Specific item "not found" is handled earlier.
            # General query "no items" should populate reply_lines with a message.
            # So, this signifies an issue if reached.
            logger.error(f"query_status: reply_lines is unexpectedly empty AFTER processing. User: {uid}, Text: '{user_text}', NLU: {nlu_result}")
            await update.message.reply_text("Не удалось сформировать ответ по статусу. Пожалуйста, попробуйте еще раз или /help.")
        
        return None # End of query_status handling
        
    else: 
        await update.message.reply_text(f"Не совсем понял ваш запрос: '{user_text}'. Попробуйте /help.")
    return None

def main():
    builder = Application.builder().token(BOT_TOKEN)
    logger.info("Инициализация Application без встроенной JobQueue (job_queue=None).")
    builder.job_queue(None) 
    application = builder.build()

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

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    
    application.add_handler(add_project_conv, group=1)
    application.add_handler(add_task_conv, group=1)
    application.add_handler(update_progress_conv, group=1)
    
    application.add_handler(CallbackQueryHandler(confirm_progress_update_callback, pattern=r"^confirm_progress_(yes|no)$"), group=1)
    application.add_handler(CallbackQueryHandler(show_pace_details_callback, pattern=f"^{CALLBACK_SHOW_PACE_DETAILS_PREFIX}_"), group=1)
    application.add_handler(CallbackQueryHandler(handle_parent_project_progress_no_thanks, pattern=f"^{CALLBACK_UPDATE_PARENT_PROJECT_PREFIX}_no_"), group=1)
    application.add_handler(CallbackQueryHandler(handle_parent_project_progress_yes, pattern=f"^{CALLBACK_UPDATE_PARENT_PROJECT_PREFIX}_yes_"), group=1)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message), group=2) 
    
    logger.info("Запуск бота...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Бот остановлен.")

if __name__ == '__main__':
    if not os.getenv('GEMINI_API_KEY'): logger.error("GEMINI_API_KEY не установлен!"); exit()
    try: import pytz; import tzlocal 
    except ImportError: logger.error("pytz или tzlocal не установлены! `pip install pytz tzlocal`"); exit()
    if not os.getenv('TZ'): os.environ['TZ'] = 'UTC'; logger.info(f"TZ установлена в 'UTC'.")
    else: logger.info(f"Используется TZ из окружения: {os.getenv('TZ')}")
    main()