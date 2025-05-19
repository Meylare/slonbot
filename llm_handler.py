# llm_handler.py
import google.generativeai as genai
import os
import json
import logging
from typing import Union # <--- ВАЖНО: этот импорт должен быть
from datetime import date 

logger = logging.getLogger(__name__)

# Загружаем API ключ из переменной окружения
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
if not GEMINI_API_KEY:
    logger.error("GEMINI_API_KEY не найден в переменных окружения!")
    # Можно либо завершить работу, либо работать без LLM, но это нужно предусмотреть
    # raise ValueError("GEMINI_API_KEY not found in environment variables.")
else:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Ошибка конфигурации Gemini API: {e}")
        # raise

# Настройки модели
generation_config = {
    "temperature": 0.3,
    "top_p": 0.9,
    "top_k": 30,
    "max_output_tokens": 2048,
    "response_mime_type": "application/json",
}

safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
]

model = genai.GenerativeModel(model_name="gemini-1.5-flash-latest",
                              generation_config=generation_config,
                              safety_settings=safety_settings)

NLU_PROMPT_TEMPLATE = """
Ты продвинутый ассистент для управления задачами и проектами.
Твоя задача - извлечь из текста пользователя его намерение, а также связанные сущности.
Выведи результат в формате JSON.

Возможные намерения (intent):
- "add_project", "add_task", "update_progress", "query_status", "complete_item", "set_deadline", "link_task_to_project", "pause_reports", "resume_reports", "other".

Сущности (entities):
- "item_type": "project" или "task". Если неясно, может быть null. Если пользователь говорит "статус" или "мои дела", item_type должен быть null.
- "item_name_hint": Ключевые слова из названия. Если пользователь говорит "статус задач" или "мои проекты", item_name_hint должен быть null.
- "project_name_hint_for_task": Название проекта для задачи.
- "deadline": Словесное описание дедлайна или дата YYYY-MM-DD. (Примеры: "завтра", "конец недели", "20.12.2024")
- "progress_description": Текстовое описание прогресса.
- "raw_text": Оригинальный текст пользователя.

ВАЖНО: Сегодняшняя дата: {current_date_YYYY_MM_DD}. 
Если пользователь указывает конкретную дату, старайся вернуть ее в формате YYYY-MM-DD ИЛИ как текстовое описание, если формат неясен.
Если пользователь указывает относительный срок (например, "завтра", "через неделю"), ВЕРНИ ЭТО ОТНОСИТЕЛЬНОЕ ОПИСАНИЕ КАК ЕСТЬ в поле "deadline".

Примеры:
1. Текст: "создай проект исследование рынка до конца года"
   Результат: {{"intent": "add_project", "entities": {{"item_name_hint": "исследование рынка", "deadline": "конец года", "item_type": "project", "raw_text": "создай проект исследование рынка до конца года"}}}}
2. Текст: "по задаче АН2 сделал первую часть из трех"
   Результат: {{"intent": "update_progress", "entities": {{"item_name_hint": "АН2", "progress_description": "сделал первую часть из трех", "item_type": "task", "raw_text": "по задаче АН2 сделал первую часть из трех"}}}}
3. Текст: "какой статус у проекта Омега?"
   Результат: {{"intent": "query_status", "entities": {{"item_name_hint": "Омега", "item_type": "project", "raw_text": "какой статус у проекта Омега?"}}}}
4. Текст: "статус задачи Бета"
   Результат: {{"intent": "query_status", "entities": {{"item_name_hint": "Бета", "item_type": "task", "raw_text": "статус задачи Бета"}}}}
5. Текст: "статус"
   Результат: {{"intent": "query_status", "entities": {{"item_name_hint": null, "item_type": null, "raw_text": "статус"}}}}
6. Текст: "мои задачи"
   Результат: {{"intent": "query_status", "entities": {{"item_name_hint": null, "item_type": "task", "raw_text": "мои задачи"}}}}
7. Текст: "что там по проектам"
   Результат: {{"intent": "query_status", "entities": {{"item_name_hint": null, "item_type": "project", "raw_text": "что там по проектам"}}}}
8. Текст: "задача Б5 проект тест бота 2, дедлайн 22"
   Результат: {{"intent": "add_task", "entities": {{"item_name_hint": "Б5", "project_name_hint_for_task": "тест бота 2", "deadline": "22", "item_type": "task", "raw_text": "задача Б5 проект тест бота 2, дедлайн 22"}}}}


Проанализируй следующий текст пользователя и верни JSON:
Текст: "{user_input}"
Результат:
"""
PROGRESS_INTERPRETATION_PROMPT_TEMPLATE = """
Оцени прогресс в процентах от общей задачи (0-100) или в абсолютных единицах, на основе следующего описания.
Верни результат в формате JSON:
- Если определены проценты: {{"type": "percent", "value": <число от 0 до 100>}}
- Если определены абсолютные единицы: {{"type": "units", "value": <число>}} (может быть положительным или отрицательным)
- Если неясно или невозможно определить: {{"type": "unknown", "value": null}}
- Если это отмена или уменьшение: {{"type": "units", "value": <отрицательное_число>}} или {{"type": "percent_decrease", "value": <число>}}

Учитывай контекст:
- "половина", "почти половина", "около половины" -> примерно 50%
- "завершил", "готово", "сделал всё" -> 100%
- "начал", "приступил" -> может быть 5-10%
- "первая часть из трех" -> примерно 30%
- "две трети" -> примерно 66%
- "немного", "чуть-чуть" -> 5-15%
- "значительная часть", "много сделал" -> 60-80%
- "минус 2", "убрал 5 пунктов" -> отрицательные единицы
- "откатил прогресс на 10%" -> процентное уменьшение

Описание от пользователя: "{progress_description}"
Общий объем задачи/проекта (если известен, для контекста, но старайся извлечь из фразы): {total_units_context} единиц.

Результат:
"""

async def interpret_user_input(user_text: str) -> Union[dict, None]: # <--- ИЗМЕНЕНИЕ ЗДЕСЬ
    """
    Интерпретирует ввод пользователя для определения намерения и сущностей.
    """
    if not GEMINI_API_KEY:
        logger.warning("Gemini API не настроен. Пропуск NLU.")
        return {"intent": "other", "entities": {"raw_text": user_text}}
    # Проверка доступности модели (лучше делать это один раз при старте, но для простоты пока так)
    try:
        genai.get_model(model.model_name)
    except Exception as e:
        logger.error(f"Модель Gemini '{model.model_name}' недоступна или API не настроен: {e}")
        return {"intent": "other", "entities": {"raw_text": user_text}}

    current_date_str = date.today().strftime('%Y-%m-%d') # Получаем текущую дату

    prompt = NLU_PROMPT_TEMPLATE.format( 
         current_date_YYYY_MM_DD=current_date_str,
         user_input=user_text
    )
    try:
        logger.info(f"Отправка запроса в Gemini NLU: {user_text[:100]}...")
        response = await model.generate_content_async(prompt)
        
        if not response.parts or not response.text: # Добавил проверку response.text
            logger.error("Gemini NLU: Пустой ответ от API (нет 'parts' или 'text').")
            logger.debug(f"Полный ответ Gemini NLU: {response}")
            return None
            
        logger.debug(f"Ответ от Gemini NLU (сырой): {response.text}")
        
        cleaned_response_text = response.text.strip()
        if cleaned_response_text.startswith("```json"):
            cleaned_response_text = cleaned_response_text[7:]
        if cleaned_response_text.endswith("```"):
            cleaned_response_text = cleaned_response_text[:-3]
        cleaned_response_text = cleaned_response_text.strip()
        
        cleaned_response_text = cleaned_response_text.replace(",\n}", "\n}").replace(",\n]", "\n]")

        parsed_response = json.loads(cleaned_response_text)
        if "entities" in parsed_response and isinstance(parsed_response["entities"], dict) and \
           "raw_text" not in parsed_response["entities"]:
            parsed_response["entities"]["raw_text"] = user_text

        logger.info(f"Ответ от Gemini NLU (распарсенный): {parsed_response}")
        return parsed_response
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON от Gemini NLU: {e}. Ответ: {response.text if 'response' in locals() and hasattr(response, 'text') else 'Ответ не получен'}")
        return None
    except Exception as e:
        logger.error(f"Ошибка при вызове Gemini API (NLU): {e}")
        logger.debug(f"Полный ответ Gemini NLU (при ошибке): {response if 'response' in locals() else 'Ответ не получен'}")
        return None

async def interpret_progress_description(description: str, total_units_context: int = 100) -> Union[dict, None]: # <--- ИЗМЕНЕНИЕ ЗДЕСЬ
    """
    Интерпретирует текстовое описание прогресса в проценты или единицы.
    """
    if not GEMINI_API_KEY:
        logger.warning("Gemini API не настроен. Пропуск интерпретации прогресса.")
        return {"type": "unknown", "value": None}
    try:
        genai.get_model(model.model_name)
    except Exception as e:
        logger.error(f"Модель Gemini '{model.model_name}' недоступна или API не настроен: {e}")
        return {"type": "unknown", "value": None}

    prompt = PROGRESS_INTERPRETATION_PROMPT_TEMPLATE.format(
        progress_description=description,
        total_units_context=total_units_context
    )
    try:
        logger.info(f"Отправка запроса в Gemini Progress: {description[:100]}...")
        response = await model.generate_content_async(prompt)

        if not response.parts or not response.text: # Добавил проверку response.text
            logger.error("Gemini Progress: Пустой ответ от API.")
            logger.debug(f"Полный ответ Gemini Progress: {response}")
            return None

        logger.debug(f"Ответ от Gemini Progress (сырой): {response.text}")
        
        cleaned_response_text = response.text.strip()
        if cleaned_response_text.startswith("```json"):
            cleaned_response_text = cleaned_response_text[7:]
        if cleaned_response_text.endswith("```"):
            cleaned_response_text = cleaned_response_text[:-3]
        cleaned_response_text = cleaned_response_text.strip()
        
        cleaned_response_text = cleaned_response_text.replace(",\n}", "\n}").replace(",\n]", "\n]")

        parsed_response = json.loads(cleaned_response_text)
        logger.info(f"Ответ от Gemini Progress (распарсенный): {parsed_response}")
        return parsed_response
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования JSON от Gemini Progress: {e}. Ответ: {response.text if 'response' in locals() and hasattr(response, 'text') else 'Ответ не получен'}")
        return None
    except Exception as e:
        logger.error(f"Ошибка при вызове Gemini API (Progress): {e}")
        logger.debug(f"Полный ответ Gemini Progress (при ошибке): {response if 'response' in locals() else 'Ответ не получен'}")
        return None

async def test_llm():
    if not GEMINI_API_KEY:
        print("Установите переменную окружения GEMINI_API_KEY для теста.")
        return

    test_phrases_nlu = [
        "добавь проект Солнечная система дедлайн 31 декабря этого года",
        "по задаче АН2 почти завершил первую часть",
        "какой статус у проекта Омега?",
        "новая задача сделать кофе для проекта Утро, дедлайн завтра",
        "я закончил с АН2",
        "сделал еще 20% по исследованию рынка",
        "напомни мне сделать отчет",
        "привяжи задачу 'написать документацию' к проекту 'Релиз 2.0'"
    ]
    for phrase in test_phrases_nlu:
        print(f"\nТестируем NLU: '{phrase}'")
        result = await interpret_user_input(phrase)
        print(f"Результат NLU: {result}")

    test_progress_phrases = [
        ("сделал первую часть из трех", 100),
        ("почти готово", 10),
        ("еще 20 процентов", 100),
        ("минус 5 единиц", 50),
        ("завершил все", 1),
        ("ничего не понял", 100),
        ("увеличил на 3 штуки", 20)
    ]
    for phrase, total_units in test_progress_phrases:
        print(f"\nТестируем Progress: '{phrase}' (контекст {total_units} единиц)")
        result = await interpret_progress_description(phrase, total_units)
        print(f"Результат Progress: {result}")

if __name__ == '__main__':
    import asyncio
    asyncio.run(test_llm())