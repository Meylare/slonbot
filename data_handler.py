# data_handler.py
import json
import logging
import os
from typing import Union, Dict, List, Any

logger = logging.getLogger(__name__)
if not logger.hasHandlers(): # Для самодостаточности при тестировании этого модуля
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

raw_admin_ids_dh = os.getenv('ADMIN_IDS', '0').split(',')
ADMIN_USER_IDS_DH = [int(admin_id.strip()) for admin_id in raw_admin_ids_dh if admin_id.strip() and admin_id.strip() != '0']
if not ADMIN_USER_IDS_DH:
    logger.warning("data_handler.py: ADMIN_IDS не настроены или указан только 0.")

DATA_FILE = 'bot_data_v2.json'

def get_default_data() -> Dict[str, Any]:
    # Используем ADMIN_USER_IDS_DH, определенные на уровне этого модуля
    return {
        "users": {}, 
        "projects": {}, 
        "tasks": {},
        "config": { "admin_ids": ADMIN_USER_IDS_DH if ADMIN_USER_IDS_DH else [] },
        "legacy_goal": {}
    }

def load_data() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data: Dict[str, Any] = json.load(f)
        
        default_data = get_default_data()
        for key_default, value_default in default_data.items():
            data.setdefault(key_default, value_default) 
            if key_default == "config" and isinstance(value_default, dict): 
                 for c_key, c_val_default in value_default.items():
                      if isinstance(data[key_default], dict):
                           data[key_default].setdefault(c_key, c_val_default)
                      else: 
                           data[key_default] = value_default
                           break 
        
        if "config" not in data or not isinstance(data["config"], dict): data["config"] = {"admin_ids": []}
        if "admin_ids" not in data["config"] or not isinstance(data["config"]["admin_ids"], list): data["config"]["admin_ids"] = []
        
        env_admins = ADMIN_USER_IDS_DH if ADMIN_USER_IDS_DH else []
        config_admins = data["config"].get("admin_ids", [])
        data["config"]["admin_ids"] = list(set(config_admins + env_admins))

    except (FileNotFoundError, json.JSONDecodeError):
        logger.info(f"Файл {DATA_FILE} не найден или поврежден. Создается новый.")
        data = get_default_data()
    return data

def save_data(data: Dict[str, Any]):
    try:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка при сохранении данных в {DATA_FILE}: {e}")

def is_admin(user_id: int, data: Dict[str, Any]) -> bool: # Переименовано в main_bot при импорте
    return user_id in data.get("config", {}).get("admin_ids", [])

def find_item_by_name_or_id(query: str, item_type_to_search: Union[str, None], data: Dict[str, Any]) -> Union[Dict[str, Any], None]:
    if not query: 
        logger.debug("find_item_by_name_or_id: пустой поисковый запрос.")
        return None
    
    query_lower = query.lower().strip()
    
    pools_to_check = []
    if item_type_to_search == "project" or item_type_to_search is None:
        pools_to_check.append({"name": "projects", "type_label": "project"})
    if item_type_to_search == "task" or item_type_to_search is None:
        pools_to_check.append({"name": "tasks", "type_label": "task"})

    # 1. Поиск по ID
    for pool_info in pools_to_check:
        items_data = data.get(pool_info["name"], {})
        if query in items_data: # query здесь - это ID
            item = items_data[query].copy()
            item['id'] = query
            item['item_type_db'] = pool_info["type_label"] 
            logger.debug(f"Элемент ({pool_info['type_label']}) найден по ID: {query}")
            return item
            
    # 2. Поиск по имени
    if item_type_to_search: # Если тип указан, ищем только в нем
        for pool_info in pools_to_check:
            if pool_info["type_label"] == item_type_to_search:
                items_data = data.get(pool_info["name"], {})
                for item_id, item_details in items_data.items():
                    if query_lower in item_details.get("name", "").lower():
                        found = item_details.copy(); found['id'] = item_id; found['item_type_db'] = pool_info["type_label"]
                        logger.debug(f"({pool_info['type_label']}) найден по имени '{query_lower}' в '{item_details.get('name','')}'. ID: {item_id}")
                        return found
    else: # Тип не указан, ищем сначала в проектах, потом в задачах (или наоборот, как решим)
        # Сначала проекты
        project_items_data = data.get("projects", {})
        for item_id, item_details in project_items_data.items():
            if query_lower in item_details.get("name", "").lower():
                found = item_details.copy(); found['id'] = item_id; found['item_type_db'] = "project"
                logger.debug(f"Проект найден по имени '{query_lower}' в '{item_details.get('name','')}'. ID: {item_id}")
                return found
        # Затем задачи
        task_items_data = data.get("tasks", {})
        for item_id, item_details in task_items_data.items():
            if query_lower in item_details.get("name", "").lower():
                found = item_details.copy(); found['id'] = item_id; found['item_type_db'] = "task"
                logger.debug(f"Задача найдена по имени '{query_lower}' в '{item_details.get('name','')}'. ID: {item_id}")
                return found
                
    logger.debug(f"Элемент по запросу '{query}' (тип: {item_type_to_search}) не найден.")
    return None