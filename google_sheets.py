import os
import requests
from dotenv import load_dotenv

load_dotenv()
SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "").strip()
SCRIPT_KEY = os.getenv("GOOGLE_SCRIPT_KEY", "").strip()


def call_google_script(action, **data):
    if not SCRIPT_URL:
        raise RuntimeError("Не задан GOOGLE_SCRIPT_URL в .env")
    if not SCRIPT_KEY:
        raise RuntimeError("Не задан GOOGLE_SCRIPT_KEY в .env")
    response = requests.post(
        SCRIPT_URL,
        json={"api_key": SCRIPT_KEY, "action": action, **data},
        timeout=60,
    )
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Apps Script вернул не JSON. Проверь URL /exec и журнал выполнений.") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Неизвестная ошибка Google Apps Script"))
    return result.get("result", {})


def create_task_sheet(task_id, class_name, title, instructions, due_at, students):
    return call_google_script(
        "create_task",
        task_id=int(task_id), class_name=class_name, title=title,
        instructions=instructions, due_at=due_at, students=students,
    )


def update_sheet_submission(class_name, task_id, student_name, submitted_at, status, message_id):
    return call_google_script(
        "update_submission", class_name=class_name, task_id=int(task_id),
        student_name=student_name, submitted_at=submitted_at,
        status=status, message_id=message_id,
    )


def sync_group_roster(class_name, students):
    return call_google_script("sync_roster", class_name=class_name, students=students)


def close_sheet_task(class_name, task_id):
    return call_google_script("close_task", class_name=class_name, task_id=int(task_id))
