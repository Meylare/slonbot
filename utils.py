# utils.py
import uuid
from datetime import datetime, date, timedelta 
from typing import Union
import re
import logging

logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

try:
    from dateutil import parser as dateutil_parser
    from dateutil.relativedelta import relativedelta
    DATEUTIL_AVAILABLE = True
except ImportError:
    print("WARNING (utils.py): python-dateutil не найден. Парсинг дат будет ограничен.")
    DATEUTIL_AVAILABLE = False

def generate_id(prefix="item"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"

def parse_natural_deadline_to_date(deadline_str: str) -> Union[date, None]:
    if not deadline_str:
        logger.debug("parse_natural_deadline_to_date: получена пустая строка дедлайна.")
        return None
    today = date.today()
    lower_deadline_str = deadline_str.lower().strip()
    logger.debug(f"parse_natural_deadline_to_date: парсинг '{lower_deadline_str}', сегодня: {today}")

    if lower_deadline_str == "сегодня": 
        logger.info("parse_natural_deadline_to_date: Распознано 'сегодня'")
        return today
    if lower_deadline_str == "завтра": 
        logger.info("parse_natural_deadline_to_date: Распознано 'завтра'")
        return today + timedelta(days=1)
    if lower_deadline_str == "послезавтра": 
        logger.info("parse_natural_deadline_to_date: Распознано 'послезавтра'")
        return today + timedelta(days=2)
       
    regex_rel = r"через\s*(\d+)\s*(ден|дня|дней|недел|месяц|год|лет)"
    m_rel = re.search(regex_rel, lower_deadline_str)
    if m_rel:
        try:
            val = int(m_rel.group(1)); unit = m_rel.group(2); delta = None
            if DATEUTIL_AVAILABLE:
                if "ден" in unit: delta = relativedelta.relativedelta(days=val)
                elif "недел" in unit: delta = relativedelta.relativedelta(weeks=val)
                elif "месяц" in unit: delta = relativedelta.relativedelta(months=val)
                elif "год" in unit or "лет" in unit: delta = relativedelta.relativedelta(years=val)
            else:
                if "ден" in unit: delta = timedelta(days=val)
                elif "недел" in unit: delta = timedelta(days=val*7)
                else: logger.warning(f"parse_natural_deadline_to_date: Парсинг '{unit}' без dateutil не поддерживается.")
            if delta: 
                calculated_date = today + delta
                logger.info(f"parse_natural_deadline_to_date: regex результат для 'через {val} {unit}': {calculated_date}")
                return calculated_date
        except Exception as e: 
            logger.error(f"parse_natural_deadline_to_date: Ошибка парсинга 'через X Y' для '{deadline_str}': {e}")
    
    days_map = {
        "понедельник": 0, "пн": 0, "в понедельник":0, "пон":0,
        "вторник": 1, "вт": 1, "во вторник":1,
        "среда": 2, "ср": 2, "в среду":2,
        "четверг": 3, "чт": 3, "в четверг":3,
        "пятница": 4, "пт": 4, "в пятницу":4,
        "суббота": 5, "сб": 5, "в субботу":5,
        "воскресенье": 6, "вс": 6, "в воскресенье":6,
    }
    if DATEUTIL_AVAILABLE:
        try:
            # default нужен, чтобы parse понимал относительные вещи типа "next Sunday" от сегодняшнего дня
            parsed_dt = dateutil_parser.parse(deadline_str, default=datetime.combine(today, datetime.min.time()))
            # Проверяем, что это не просто сегодняшняя дата, если в строке не было "сегодня"
            # И что это не дата в прошлом, если не просили "прошлый"
            if (parsed_dt.date() != today or "сегодня" in lower_deadline_str) and \
               (parsed_dt.date() >= today or "прошл" in lower_deadline_str or "минувш" in lower_deadline_str):
                # Дополнительно проверяем, не является ли распарсенная дата слишком далекой в будущем,
                # если в запросе был только день недели (например, "вторник" не должен стать вторником через год)
                is_just_day_of_week_request = any(re.search(r"\b" + re.escape(day_key) + r"\b", lower_deadline_str) for day_key in days_map if len(day_key) <=3 or day_key.startswith("в "))
                if is_just_day_of_week_request and (parsed_dt.date() - today).days > 10 : # Если это похоже на просто день недели и он далеко
                    logger.debug(f"dateutil дал слишком далекую дату {parsed_dt.date()} для '{deadline_str}', возможно, это не то. Пробуем ручной расчет.")
                else:
                    logger.info(f"parse_natural_deadline_to_date: dateutil (для дней недели/общего) распарсил '{deadline_str}' как: {parsed_dt.date()}")
                    return parsed_dt.date()
            elif parsed_dt.date() < today and not ("прошл" in lower_deadline_str or "минувш" in lower_deadline_str):
                 logger.debug(f"dateutil вернул прошедшую дату {parsed_dt.date()} для '{deadline_str}'. Пробуем ручной расчет дней недели.")
        except (dateutil_parser.ParserError, ValueError, TypeError): # Ошибки, которые может выдать parse
            logger.debug(f"dateutil не смог обработать '{deadline_str}' как день недели/общую дату, пробуем простой расчет.")

    # Если dateutil не помог или недоступен, или вернул прошлую дату, а нам нужен будущий день недели
    for day_keyword, target_weekday_num in days_map.items():
        if re.search(r"\b" + re.escape(day_keyword) + r"\b", lower_deadline_str): # Используем границы слова
            days_ahead = target_weekday_num - today.weekday()
            if days_ahead <= 0 and not ("прошл" in lower_deadline_str or "минувш" in lower_deadline_str): 
                days_ahead += 7
            elif days_ahead > 0 and ("прошл" in lower_deadline_str or "минувш" in lower_deadline_str): 
                days_ahead -= 7
            calculated_date = today + timedelta(days=days_ahead)
            logger.info(f"parse_natural_deadline_to_date: Парсер (дни недели) для '{day_keyword}' -> {calculated_date}")
            return calculated_date
            
    if "конец недели" in lower_deadline_str: 
        logger.info("parse_natural_deadline_to_date: Распознано 'конец недели'")
        return today + timedelta(days=6 - today.weekday()) # 0-пн, 6-вс
    if "конец месяца" in lower_deadline_str: 
        logger.info("parse_natural_deadline_to_date: Распознано 'конец месяца'")
        # Найти последний день текущего месяца
        return date(today.year, today.month + 1, 1) - timedelta(days=1) if today.month != 12 else date(today.year, 12, 31)
    if "конец года" in lower_deadline_str: 
        logger.info("parse_natural_deadline_to_date: Распознано 'конец года'")
        return date(today.year, 12, 31)

    # Общий парсинг конкретных дат с dateutil, если доступен
    if DATEUTIL_AVAILABLE:
        try: 
            # Пробуем с dayfirst=True, т.к. ДД.ММ.ГГГГ более вероятно для русскоязычных
            parsed_dt = dateutil_parser.parse(deadline_str, fuzzy=False, dayfirst=True).date()
            logger.info(f"parse_natural_deadline_to_date: dateutil (dayfirst=True) распарсил '{deadline_str}' как {parsed_dt}")
            return parsed_dt
        except (dateutil_parser.ParserError, ValueError, TypeError, OverflowError):
            try: 
                # Попытка без dayfirst (ММ.ДД или YYYY-MM-DD или другие форматы, понятные dateutil)
                parsed_dt = dateutil_parser.parse(deadline_str, fuzzy=False).date()
                logger.info(f"parse_natural_deadline_to_date: dateutil (no dayfirst) распарсил '{deadline_str}' как {parsed_dt}")
                return parsed_dt
            except (dateutil_parser.ParserError, ValueError, TypeError, OverflowError): 
                logger.warning(f"parse_natural_deadline_to_date: dateutil не смог распарсить '{deadline_str}' как конкретную дату")
    else: # Если dateutil недоступен, пробуем строгий YYYY-MM-DD
        try: 
            parsed_dt = datetime.strptime(deadline_str, '%Y-%m-%d').date()
            logger.info(f"parse_natural_deadline_to_date: strptime распарсил '{deadline_str}' как {parsed_dt}")
            return parsed_dt
        except ValueError: pass # Игнорируем, если не наш формат YYYY-MM-DD
    
    logger.error(f"parse_natural_deadline_to_date: Не удалось определить дату из строки: '{deadline_str}'")
    return None