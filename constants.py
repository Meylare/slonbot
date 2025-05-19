# constants.py

# Состояния для ConversationHandler'а добавления проекта
ASK_PROJECT_NAME, ASK_PROJECT_DEADLINE, ASK_PROJECT_GOAL = range(3) 

# Состояния для ConversationHandler'а добавления ЗАДАЧИ
ASK_TASK_NAME, ASK_TASK_PROJECT_LINK, ASK_TASK_DEADLINE_STATE, ASK_TASK_GOAL = range(10, 14)

# Состояния для ConversationHandler'а обновления ПРОГРЕССА (запускаемый командой /progress)
ASK_PROGRESS_ITEM_TYPE, ASK_PROGRESS_ITEM_NAME, ASK_PROGRESS_DESCRIPTION = range(30, 33)

# Ключи для context.user_data
ACTIVE_CONVERSATION_KEY = 'active_conversation_handler_type'
LAST_PROCESSED_IN_CONV_MSG_ID_KEY = 'last_conv_msg_id' 

# Значения для ACTIVE_CONVERSATION_KEY
ADD_PROJECT_CONV_STATE_VALUE = 'state_adding_project' 
ADD_TASK_CONV_STATE_VALUE = 'state_adding_task'
UPDATE_PROGRESS_CONV_STATE_VALUE = 'state_updating_progress'

# Ключи для хранения промежуточных данных диалогов
# 'new_project_info' - используется в conversations.py
NEW_TASK_INFO_KEY = 'new_task_info'
ITEM_FOR_PROGRESS_UPDATE_KEY = 'item_for_progress_update'

# Ключ для данных, ожидающих подтверждения кнопками (обновление/завершение прогресса)
PENDING_PROGRESS_UPDATE_KEY = 'pending_progress_update_info'

# Callback data для кнопки "Детали темпа"
CALLBACK_SHOW_PACE_DETAILS_PREFIX = "show_pace_details"

# Callback data для кнопок обновления прогресса РОДИТЕЛЬСКОГО ПРОЕКТА (простое Да/Нет)
CALLBACK_UPDATE_PARENT_PROJECT_PREFIX = "upd_parent_proj" 