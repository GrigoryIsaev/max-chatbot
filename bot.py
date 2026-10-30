import csv
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from google_sheets import create_task_sheet, update_sheet_submission, sync_group_roster, close_sheet_task

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("homework-bot")

MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(BASE_DIR, DB_PATH)
STUDENTS_CSV = os.getenv("STUDENTS_CSV", "students.csv")
if not os.path.isabs(STUDENTS_CSV):
    STUDENTS_CSV = os.path.join(BASE_DIR, STUDENTS_CSV)
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
TEACHER_IDS = {
    x.strip() for x in os.getenv("TEACHER_IDS", "").split(",") if x.strip()
}
MAX_API_BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru").rstrip("/")
POLL_TIMEOUT = int(os.getenv("POLL_TIMEOUT", "30"))
POLL_PAUSE = float(os.getenv("POLL_PAUSE", "1.0"))

if not MAX_BOT_TOKEN:
    raise SystemExit("Не задан MAX_BOT_TOKEN в .env")

MAX_HEADERS = {"Authorization": MAX_BOT_TOKEN}
session = requests.Session()
session.headers.update(MAX_HEADERS)


def now_local():
    return datetime.now(TIMEZONE)


def parse_due(value):
    # Expected format: YYYY-MM-DD HH:MM, interpreted in configured timezone.
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                title TEXT NOT NULL,
                instructions TEXT NOT NULL,
                due_at TEXT NOT NULL,
                sheet_name TEXT NOT NULL DEFAULT '',
                sheet_id TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                reminder_24_sent INTEGER NOT NULL DEFAULT 0,
                reminder_12_sent INTEGER NOT NULL DEFAULT 0,
                deadline_notified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS class_chats (
                class_name TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                student_name TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id TEXT NOT NULL DEFAULT '',
                UNIQUE(task_id, student_name),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS processed_updates (
                update_key TEXT PRIMARY KEY,
                processed_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS teacher_notifications (
                event_key TEXT NOT NULL,
                teacher_id TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(event_key, teacher_id)
            )
        """)
        # Migrate databases created by older versions.
        columns = {row["name"] for row in con.execute("PRAGMA table_info(tasks)")}
        migrations = {
            "sheet_name": "TEXT NOT NULL DEFAULT ''",
            "sheet_id": "TEXT NOT NULL DEFAULT ''",
            "reminder_24_sent": "INTEGER NOT NULL DEFAULT 0",
            "reminder_12_sent": "INTEGER NOT NULL DEFAULT 0",
            "deadline_notified": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")


def setting_get(key, default=None):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(key, value):
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def api_request(method, path, **kwargs):
    url = f"{MAX_API_BASE}{path}"
    response = session.request(method, url, timeout=kwargs.pop("timeout", 45), **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"MAX API {method} {path}: HTTP {response.status_code}: {response.text[:500]}")
    if not response.text.strip():
        return {}
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(f"MAX API вернул не JSON: {response.text[:300]}")


def get_updates(marker=None):
    params = {"timeout": POLL_TIMEOUT, "limit": 100}
    if marker is not None:
        params["marker"] = marker
    return api_request("GET", "/updates", params=params, timeout=POLL_TIMEOUT + 15)


def send_message(chat_id, text):
    return api_request(
        "POST",
        "/messages",
        params={"chat_id": str(chat_id)},
        json={"text": text},
    )


def send_user_message(user_id, text):
    """Send a private MAX message to a user by their numeric user_id."""
    return api_request(
        "POST",
        "/messages",
        params={"user_id": str(user_id)},
        json={"text": text},
    )


def notify_teachers(text, event_key):
    """Send an event once to each configured teacher; retry only failed recipients."""
    if not TEACHER_IDS:
        log.warning("Не настроены TEACHER_IDS: уведомление %s не отправлено", event_key)
        return False

    all_sent = True
    for teacher_id in sorted(TEACHER_IDS):
        with db() as con:
            already_sent = con.execute(
                "SELECT 1 FROM teacher_notifications WHERE event_key=? AND teacher_id=?",
                (event_key, teacher_id),
            ).fetchone()
        if already_sent:
            continue
        try:
            send_user_message(teacher_id, text)
            with db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO teacher_notifications(event_key,teacher_id,sent_at) VALUES(?,?,?)",
                    (event_key, teacher_id, now_local().isoformat()),
                )
        except Exception as exc:
            all_sent = False
            log.warning("Не удалось отправить личное уведомление учителю %s (событие %s): %s. Учитель должен открыть диалог с ботом и нажать /start.", teacher_id, event_key, exc)
    return all_sent


def get_nested(obj, *paths, default=None):
    for path in paths:
        cur = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default


def normalize_id(value):
    return str(value) if value is not None else ""


def extract_message(update):
    # MAX event payloads may vary; support common message_created shapes.
    return (
        get_nested(update, "message"),
        get_nested(update, "message_created.message"),
        get_nested(update, "update.message"),
        get_nested(update, "message_created"),
    )


def extract_text(message):
    return (
        get_nested(message, "body.text", default=None)
        or get_nested(message, "text", default=None)
        or ""
    ).strip()


def extract_chat_id(update, message):
    return (
        get_nested(message, "recipient.chat_id")
        or get_nested(message, "recipient.chatId")
        or get_nested(update, "chat_id")
        or get_nested(update, "chatId")
        or get_nested(message, "chat_id")
        or get_nested(message, "chatId")
    )


def extract_sender_id(message):
    return (
        get_nested(message, "sender.user_id")
        or get_nested(message, "sender.id")
        or get_nested(message, "from.user_id")
        or get_nested(message, "from.id")
    )


def extract_message_id(message):
    return (
        get_nested(message, "body.mid")
        or get_nested(message, "mid")
        or get_nested(message, "message_id")
        or get_nested(message, "id")
    )


def extract_attachments(message):
    return (
        get_nested(message, "body.attachments", default=None)
        or get_nested(message, "attachments", default=None)
        or []
    )


def is_teacher(sender_id):
    return normalize_id(sender_id) in TEACHER_IDS


def load_students(class_name):
    students = []
    # CSV may come from Excel in UTF-8 or legacy Windows-1251 encoding.
    try:
        f = open(STUDENTS_CSV, "r", encoding="utf-8-sig", newline="")
        try:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames
        finally:
            f.close()
    except UnicodeDecodeError:
        with open(STUDENTS_CSV, "r", encoding="cp1251", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames
    if not fieldnames or not {"class", "full_name"}.issubset(set(fieldnames)):
        raise ValueError("students.csv должен содержать столбцы class,full_name")
    for row in rows:
        if (row.get("class") or "").strip() == class_name:
            full_name = (row.get("full_name") or "").strip()
            if full_name:
                students.append({"full_name": full_name})
    return students


def read_students_csv():
    """Read the complete roster while preserving the supported CSV schema."""
    if not os.path.exists(STUDENTS_CSV):
        raise FileNotFoundError(f"Не найден файл {STUDENTS_CSV}")
    try:
        with open(STUDENTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames
        encoding = "utf-8"
    except UnicodeDecodeError:
        with open(STUDENTS_CSV, "r", encoding="cp1251", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            fieldnames = reader.fieldnames
        encoding = "cp1251"
    if not fieldnames or not {"class", "full_name"}.issubset(set(fieldnames)):
        raise ValueError("students.csv должен содержать столбцы class,full_name")
    return fieldnames, rows, encoding


def write_students_csv(fieldnames, rows, encoding="utf-8"):
    """Write roster safely using a temporary file to avoid partial CSV writes."""
    temp_path = STUDENTS_CSV + ".tmp"
    with open(temp_path, "w", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp_path, STUDENTS_CSV)


def add_student_to_csv(class_name, full_name):
    fieldnames, rows, encoding = read_students_csv()
    norm_name = " ".join(full_name.split()).casefold().replace("ё", "е")
    for row in rows:
        row_class = (row.get("class") or "").strip()
        row_name = " ".join((row.get("full_name") or "").split()).casefold().replace("ё", "е")
        if row_class == class_name and row_name == norm_name:
            return False, "Такой ученик уже есть в students.csv."
    new_row = {field: "" for field in fieldnames}
    new_row["class"] = class_name
    new_row["full_name"] = " ".join(full_name.split())
    rows.append(new_row)
    write_students_csv(fieldnames, rows, encoding)
    return True, f"Ученик {new_row['full_name']} добавлен в класс {class_name}."


def delete_student_from_csv(class_name, full_name):
    fieldnames, rows, encoding = read_students_csv()
    norm_name = " ".join(full_name.split()).casefold().replace("ё", "е")
    kept_rows = []
    removed = []
    for row in rows:
        row_class = (row.get("class") or "").strip()
        row_name = " ".join((row.get("full_name") or "").split()).casefold().replace("ё", "е")
        if row_class == class_name and row_name == norm_name:
            removed.append((row.get("full_name") or "").strip())
        else:
            kept_rows.append(row)
    if not removed:
        return False, f"Ученик «{full_name}» не найден в классе {class_name}."
    write_students_csv(fieldnames, kept_rows, encoding)
    return True, f"Ученик {removed[0]} удалён из класса {class_name}."


def get_bound_chat(class_name):
    with db() as con:
        row = con.execute(
            "SELECT chat_id FROM class_chats WHERE class_name=?", (class_name,)
        ).fetchone()
    return row["chat_id"] if row else None


def get_bound_class(chat_id):
    with db() as con:
        row = con.execute(
            "SELECT class_name FROM class_chats WHERE chat_id=?", (str(chat_id),)
        ).fetchone()
    return row["class_name"] if row else None


def bind_class(class_name, chat_id):
    with db() as con:
        existing = con.execute(
            "SELECT chat_id FROM class_chats WHERE class_name=?", (class_name,)
        ).fetchone()
        if existing and existing["chat_id"] != str(chat_id):
            return False, "Этот класс уже привязан к другому чату. Сначала выполни /unbind в старом чате."
        con.execute(
            "INSERT INTO class_chats(class_name,chat_id) VALUES(?,?) "
            "ON CONFLICT(class_name) DO UPDATE SET chat_id=excluded.chat_id",
            (class_name, str(chat_id)),
        )
    return True, f"Чат привязан к классу {class_name}."


def unbind_class(class_name, chat_id):
    with db() as con:
        row = con.execute(
            "SELECT chat_id FROM class_chats WHERE class_name=?", (class_name,)
        ).fetchone()
        if not row:
            return False, f"Класс {class_name} не привязан."
        if row["chat_id"] != str(chat_id):
            return False, "Этот чат не привязан к указанному классу."
        con.execute("DELETE FROM class_chats WHERE class_name=?", (class_name,))
    return True, f"Привязка класса {class_name} удалена. Задания и таблицы сохранены."


def create_task(class_name, title, instructions, due_at):
    students = load_students(class_name)
    if not students:
        raise ValueError(f"В students.csv не найдены ученики класса {class_name}")

    with db() as con:
        cur = con.execute(
            """INSERT INTO tasks
            (class_name,subject,title,instructions,due_at,created_at)
            VALUES(?,?,?,?,?,?)""",
            (class_name, "Английский", title, instructions, due_at.isoformat(), now_local().isoformat()),
        )
        task_id = cur.lastrowid

    try:
        result = create_task_sheet(
            task_id=task_id,
            class_name=class_name,
            title=title,
            instructions=instructions,
            due_at=due_at.strftime("%Y-%m-%d %H:%M"),
            students=students,
        )
    except Exception:
        log.exception("Не удалось добавить задание #%s в лист группы", task_id)
        with db() as con:
            con.execute("UPDATE tasks SET is_active=0 WHERE id=?", (task_id,))
        raise

    with db() as con:
        con.execute("UPDATE tasks SET sheet_name=?, sheet_id=? WHERE id=?",
                    (result["sheet_name"], str(result.get("sheet_id", "")), task_id))
    return task_id, result["sheet_name"], result.get("spreadsheet_url", "")


def list_tasks(class_name=None):
    with db() as con:
        if class_name:
            rows = con.execute(
                "SELECT * FROM tasks WHERE is_active=1 AND class_name=? ORDER BY due_at",
                (class_name,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM tasks WHERE is_active=1 ORDER BY due_at"
            ).fetchall()
    return rows


def close_task(task_id, class_name=None):
    with db() as con:
        if class_name:
            row = con.execute("SELECT * FROM tasks WHERE id=? AND class_name=?", (task_id, class_name)).fetchone()
        else:
            row = con.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return False, "Задание с таким ID не найдено для этой группы."
        if not row["is_active"]:
            return False, "Это задание уже закрыто."
    close_sheet_task(row["class_name"], task_id)
    with db() as con:
        con.execute("UPDATE tasks SET is_active=0 WHERE id=?", (task_id,))
    return True, f"Задание #{task_id} закрыто."


def help_text():
    return (
        "Команды учителя в групповом чате:\n"
        "/bind КЛАСС — привязать этот чат к классу из students.csv\n"
        "/unbind КЛАСС — убрать привязку чата\n"
        "/new НАЗВАНИЕ|УСЛОВИЕ|ГГГГ-ММ-ДД ЧЧ:ММ — создать задание в этом чате\n"
        "/new student КЛАСС|ФИО — добавить ученика в students.csv\n"
        "/delete student КЛАСС|ФИО — удалить ученика из students.csv\n"
        "/list — активные задания этого класса\n"
        "/close ID — закрыть задание этого класса\n"
        "/sync — вручную обновить состав группы из students.csv\n"
        "/help — помощь\n\n"
        "В личном чате доступны только команды /start и /help.\n\n"
        "Сдача: прикрепи фото или видео и добавь подпись:\n"
        "ID задания: 12\nФИО: Иванов Иван Иванович\n\n"
        "Для управления заданиями используй команды в привязанном групповом чате."
    )


def parse_submission_caption(text):
    values = {}
    for line in text.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().casefold().replace("ё", "е")
            value = value.strip()
            if key in ("id задания", "id задания #", "id", "задание id", "номер задания"):
                values["id"] = value.lstrip("#№ ")
            elif key in ("фио", "ф.и.о.", "ученик", "фио ученика"):
                values["фио"] = value
    if values.get("id", "").isdigit() and values.get("фио"):
        return {"id": int(values["id"]), "фио": values["фио"]}
    # Short form is also accepted: 12, Иванов Иван Иванович
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    if len(parts) >= 2 and parts[0].lstrip("#№ ").isdigit():
        return {"id": int(parts[0].lstrip("#№ ")), "фио": parts[1]}
    return None


def is_media_submission(attachments):
    for attachment in attachments or []:
        typ = str(attachment.get("type", "")).casefold()
        if typ in {"image", "video", "photo", "file"}:
            return True
        if any(k in attachment for k in ("image", "video", "payload")):
            return True
    return False


def find_task_by_id(class_name, task_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM tasks WHERE id=? AND class_name=? AND is_active=1",
            (int(task_id), class_name),
        ).fetchone()


def process_submission(chat_id, message, update):
    caption = extract_text(message)
    parsed = parse_submission_caption(caption)
    if not parsed:
        return
    if not is_media_submission(extract_attachments(message)):
        return

    class_name = get_bound_class(chat_id)
    if class_name is None:
        return

    task = find_task_by_id(class_name, parsed["id"])
    if not task:
        send_message(chat_id, f"Не нашёл активное задание с ID #{parsed['id']} для класса {class_name}. Проверь ID задания.")
        return

    student_name = parsed["фио"].strip()
    roster = {s["full_name"].casefold() for s in load_students(class_name)}
    if student_name.casefold() not in roster:
        send_message(chat_id, f"Не нашёл ученика «{student_name}» в списке класса {class_name}. Проверь ФИО.")
        return

    submitted_at = now_local()
    due_at = datetime.fromisoformat(task["due_at"])
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=TIMEZONE)
    status = "Вовремя" if submitted_at <= due_at else "С опозданием"
    message_id = normalize_id(extract_message_id(message))
    event_suffix = message_id or submitted_at.isoformat()

    with db() as con:
        existing = con.execute(
            "SELECT id FROM submissions WHERE task_id=? AND lower(student_name)=lower(?)",
            (task["id"], student_name),
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE submissions SET submitted_at=?,status=?,message_id=? WHERE task_id=? AND lower(student_name)=lower(?)",
                (submitted_at.isoformat(), status, message_id, task["id"], student_name),
            )
        else:
            con.execute(
                "INSERT INTO submissions(task_id,student_name,submitted_at,status,message_id) VALUES(?,?,?,?,?)",
                (task["id"], student_name, submitted_at.isoformat(), status, message_id),
            )

    try:
        # Сдача фиксируется в БД и Google Sheets без сообщений в чат и без
        # личных уведомлений преподавателям.
        update_sheet_submission(
            class_name=class_name,
            task_id=task["id"],
            student_name=student_name,
            submitted_at=submitted_at.strftime("%Y-%m-%d %H:%M:%S"),
            status=status,
            message_id=message_id,
        )
    except Exception:
        log.exception("Не удалось обновить Google Sheets после сдачи")


def handle_command(chat_id, sender_id, text):
    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if command in ("/help", "/start"):
        send_message(chat_id, help_text())
        return True
    if command not in {"/bind", "/unbind", "/new", "/delete", "/list", "/close", "/sync"}:
        return False
    if not is_teacher(sender_id):
        send_message(chat_id, "Эта команда доступна только преподавателю.")
        return True

    # Управление списком учеников доступно преподавателю из группового чата.
    # Класс указывается явно, поэтому команда не зависит от привязки текущего чата.
    if command == "/new" and arg.lower().startswith("student "):
        payload = arg[len("student "):].strip()
        fields = [part.strip() for part in payload.split("|", 1)]
        if len(fields) != 2 or not all(fields):
            send_message(chat_id, "Формат: /new student КЛАСС|Фамилия Имя Отчество\nПример: /new student 7А|Иванов Иван Иванович")
            return True
        class_name, full_name = fields
        try:
            ok, msg = add_student_to_csv(class_name, full_name)
            if ok and get_bound_chat(class_name):
                try:
                    result = sync_group_roster(class_name, load_students(class_name))
                    msg += f"\nСостав в Google Sheets обновлён: {result.get('current_count', 0)} учеников."
                except Exception as exc:
                    log.exception("Не удалось обновить Google Sheets после добавления ученика")
                    msg += f"\nВ CSV ученик добавлен, но Google Sheets обновить не удалось: {exc}"
            send_message(chat_id, msg)
        except Exception as exc:
            log.exception("Ошибка добавления ученика в students.csv")
            send_message(chat_id, f"Не удалось добавить ученика: {exc}")
        return True

    if command == "/delete" and arg.lower().startswith("student "):
        payload = arg[len("student "):].strip()
        fields = [part.strip() for part in payload.split("|", 1)]
        if len(fields) != 2 or not all(fields):
            send_message(chat_id, "Формат: /delete student КЛАСС|Фамилия Имя Отчество\nПример: /delete student 7А|Иванов Иван Иванович")
            return True
        class_name, full_name = fields
        try:
            ok, msg = delete_student_from_csv(class_name, full_name)
            if ok and get_bound_chat(class_name):
                try:
                    result = sync_group_roster(class_name, load_students(class_name))
                    msg += f"\nСостав в Google Sheets обновлён: {result.get('current_count', 0)} учеников."
                except Exception as exc:
                    log.exception("Не удалось обновить Google Sheets после удаления ученика")
                    msg += f"\nВ CSV ученик удалён, но Google Sheets обновить не удалось: {exc}"
            send_message(chat_id, msg)
        except Exception as exc:
            log.exception("Ошибка удаления ученика из students.csv")
            send_message(chat_id, f"Не удалось удалить ученика: {exc}")
        return True

    if command == "/bind":
        if not arg:
            send_message(chat_id, "Формат: /bind КЛАСС")
            return True
        ok, msg = bind_class(arg, chat_id)
        send_message(chat_id, msg)
        if ok:
            try:
                result = sync_group_roster(arg, load_students(arg))
                send_message(chat_id, f"Состав обновлён: {result.get('current_count', 0)} учеников. Лист: {result.get('sheet_name', arg)}")
            except Exception as exc:
                log.exception("Ошибка синхронизации состава при привязке")
                send_message(chat_id, f"Привязка сохранена, но таблицу состава обновить не удалось: {exc}")
        return True

    if command == "/unbind":
        class_name = get_bound_class(chat_id)
        if class_name is None:
            send_message(chat_id, "Этот чат не привязан к классу.")
        elif arg and arg != class_name:
            send_message(chat_id, f"Этот чат привязан к классу {class_name}. Для отвязки укажи /unbind {class_name}")
        else:
            ok, msg = unbind_class(class_name, chat_id)
            send_message(chat_id, msg)
        return True

    class_name = get_bound_class(chat_id)
    if command in {"/new", "/list", "/close", "/sync"} and class_name is None:
        send_message(chat_id, "Сначала привяжи этот групповой чат: /bind КЛАСС")
        return True

    if command == "/new":
        fields = [x.strip() for x in arg.split("|")]
        if len(fields) != 3:
            send_message(chat_id, "Формат: /new НАЗВАНИЕ|УСЛОВИЕ|ГГГГ-ММ-ДД ЧЧ:ММ\nПример: /new Workbook p. 15|Сделать упражнения 1–5|2026-11-02 18:00")
            return True
        title, instructions, due_str = fields
        try:
            due_at = parse_due(due_str)
            if due_at <= now_local():
                raise ValueError("Срок сдачи должен быть в будущем.")
            task_id, sheet_name, sheet_url = create_task(class_name, title, instructions, due_at)
            announcement = (
                f"Новое задание по английскому языку\n"
                f"Группа: {class_name}\nЗадание: {title}\nУсловие: {instructions}\n"
                f"Срок сдачи: {due_at.strftime('%d.%m.%Y %H:%M')}\n"
                f"Номер задания: #{task_id}\n\n"
                f"Пришли фото или видео с подписью:\nID задания: {task_id}\nФИО: Фамилия Имя Отчество"
            )
            send_message(chat_id, announcement)
            send_message(chat_id, f"Задание #{task_id} создано. Таблица группы: {sheet_url}")
        except Exception as exc:
            log.exception("Ошибка создания задания")
            send_message(chat_id, f"Не удалось создать задание: {exc}")
        return True

    if command == "/list":
        rows = list_tasks(class_name)
        if not rows:
            send_message(chat_id, "Активных заданий нет.")
        else:
            lines = []
            for row in rows:
                due = datetime.fromisoformat(row["due_at"]).astimezone(TIMEZONE)
                lines.append(f"#{row['id']} | {row['title']} | до {due.strftime('%d.%m.%Y %H:%M')}")
            send_message(chat_id, "Активные задания группы:\n" + "\n".join(lines))
        return True

    if command == "/close":
        try:
            task_id = int(arg.lstrip("#№ "))
            ok, msg = close_task(task_id, class_name)
            send_message(chat_id, msg)
        except ValueError:
            send_message(chat_id, "Формат: /close ID")
        except Exception as exc:
            log.exception("Не удалось закрыть задание")
            send_message(chat_id, f"Не удалось закрыть задание в таблице: {exc}")
        return True

    if command == "/sync":
        try:
            result = sync_group_roster(class_name, load_students(class_name))
            send_message(chat_id, f"Состав группы обновлён. В CSV сейчас {result.get('current_count', 0)} учеников; новых добавлено: {result.get('added', 0)}. Лист: {result.get('sheet_name', class_name)}")
        except Exception as exc:
            log.exception("Ошибка ручной синхронизации состава")
            send_message(chat_id, f"Не удалось обновить состав: {exc}")
        return True
    return False


def update_key(update):
    # Prefer stable update/message IDs; fall back to canonical JSON hash-like text.
    for path in ("update_id", "id", "message.body.mid", "message.mid",
                 "message_created.message.body.mid", "message_created.message.mid"):
        value = get_nested(update, path)
        if value is not None:
            return f"{path}:{value}"
    return json.dumps(update, ensure_ascii=False, sort_keys=True)[:2000]


def already_processed(key):
    with db() as con:
        return con.execute(
            "SELECT 1 FROM processed_updates WHERE update_key=?", (key,)
        ).fetchone() is not None


def mark_processed(key):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO processed_updates(update_key,processed_at) VALUES(?,?)",
            (key, now_local().isoformat()),
        )


def handle_update(update):
    key = update_key(update)
    if already_processed(key):
        return

    message_candidates = extract_message(update)
    message = next((m for m in message_candidates if isinstance(m, dict)), None)
    if not message:
        mark_processed(key)
        return

    chat_id = extract_chat_id(update, message)
    sender_id = extract_sender_id(message)
    text = extract_text(message)

    # В личном чате доступны только /start и /help. MAX обычно передаёт
    # для личного сообщения recipient.user_id, а для группового — recipient.chat_id.
    if chat_id is None:
        if sender_id is not None and text.startswith("/"):
            command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
            if command in ("/start", "/help"):
                send_user_message(sender_id, help_text())
        # Любые остальные личные команды и вложения игнорируются.
    elif text.startswith("/"):
        handle_command(chat_id, sender_id, text)
    else:
        # Приём домашних работ обрабатывается только в групповом чате.
        process_submission(chat_id, message, update)

    mark_processed(key)


def check_due_reminders():
    now = now_local()
    with db() as con:
        rows = con.execute(
            "SELECT * FROM tasks WHERE is_active=1 ORDER BY due_at"
        ).fetchall()

    for task in rows:
        try:
            due_at = datetime.fromisoformat(task["due_at"])
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=TIMEZONE)
            remaining = due_at - now
            chat_id = get_bound_chat(task["class_name"])

            if remaining.total_seconds() <= 0:
                # Задание остаётся активным до /close. Поэтому после истечения
                # срока отправляем одно уведомление именно в групповой чат.
                if not task["deadline_notified"] and chat_id:
                    message = (
                        f"⏰ СРОК ЗАДАНИЯ ИСТЁК — задание ещё не закрыто\n"
                        f"Группа: {task['class_name']}\n"
                        f"Задание №{task['id']}: {task['title']}\n"
                        f"Срок сдачи: {due_at.strftime('%d.%m.%Y %H:%M')}\n\n"
                        f"Преподаватель, закройте задание командой /close {task['id']}, "
                        f"если приём работ завершён."
                    )
                    try:
                        send_message(chat_id, message)
                        with db() as con:
                            con.execute("UPDATE tasks SET deadline_notified=1 WHERE id=?", (task["id"],))
                    except Exception:
                        log.exception("Не удалось отправить в групповой чат уведомление об истечении срока задания #%s", task["id"])
                continue

            if not chat_id:
                continue
            # Send only the closest applicable unsent reminder after downtime.
            reminder_column = None
            hours = None
            if remaining <= timedelta(hours=12) and not task["reminder_12_sent"]:
                reminder_column, hours = "reminder_12_sent", 12
            elif remaining <= timedelta(hours=24) and not task["reminder_24_sent"]:
                reminder_column, hours = "reminder_24_sent", 24

            if not reminder_column:
                continue

            msg = (
                f"Напоминание: до сдачи домашнего задания осталось около {hours} ч.\n"
                f"ID задания: #{task['id']}\n"
                f"Задание: {task['title']}\n"
                f"Срок сдачи: {due_at.strftime('%d.%m.%Y %H:%M')}"
            )
            send_message(chat_id, msg)
            with db() as con:
                con.execute(
                    f"UPDATE tasks SET {reminder_column}=1 WHERE id=?",
                    (task["id"],),
                )
        except Exception:
            log.exception("Ошибка проверки срока/напоминания для задания #%s", task["id"])



def sync_all_rosters():
    with db() as con:
        classes = {row["class_name"] for row in con.execute("SELECT class_name FROM class_chats").fetchall()}
    try:
        # Include all classes currently present in CSV, even before their chat is bound.
        with open(STUDENTS_CSV, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames and {"class", "full_name"}.issubset(set(reader.fieldnames)):
                classes.update((row.get("class") or "").strip() for row in reader if (row.get("class") or "").strip())
    except UnicodeDecodeError:
        with open(STUDENTS_CSV, "r", encoding="cp1251", newline="") as f:
            reader = csv.DictReader(f)
            classes.update((row.get("class") or "").strip() for row in reader if (row.get("class") or "").strip())
    for class_name in sorted(classes):
        try:
            sync_group_roster(class_name, load_students(class_name))
        except Exception:
            log.exception("Не удалось синхронизировать список учеников класса %s", class_name)

def main():
    init_db()
    marker = setting_get("max_marker")
    last_reminder_check = 0
    last_roster_sync = 0
    log.info("Бот запущен в режиме Long Polling.")
    sync_all_rosters()
    while True:
        try:
            if time.monotonic() - last_reminder_check >= 30:
                check_due_reminders()
                last_reminder_check = time.monotonic()
            if time.monotonic() - last_roster_sync >= 300:
                sync_all_rosters()
                last_roster_sync = time.monotonic()

            data = get_updates(marker)
            updates = data.get("updates", [])
            new_marker = data.get("marker")
            for update in updates:
                try:
                    handle_update(update)
                except Exception:
                    log.exception("Ошибка обработки события")
            if new_marker is not None:
                marker = new_marker
                setting_set("max_marker", marker)
            elif not updates:
                time.sleep(POLL_PAUSE)
        except KeyboardInterrupt:
            log.info("Остановка по Ctrl+C")
            break
        except Exception:
            log.exception("Ошибка Long Polling; повтор через 10 секунд")
            time.sleep(10)


if __name__ == "__main__":
    main()
