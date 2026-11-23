"""Настройки приложения. Значения берутся из файла .env (рядом с app.py) или переменных окружения.
Почту и Трекер также можно настроить прямо в интерфейсе: «Настройки → Интеграции»."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

DB_PATH = os.environ.get("CRM_DB", os.path.join(BASE_DIR, "data", "crm.sqlite3"))
FILES_DIR = os.environ.get("CRM_FILES", os.path.join(BASE_DIR, "data", "files"))
SECRET_KEY = os.environ.get("CRM_SECRET", "change-me-please")
HOST = os.environ.get("CRM_HOST", "0.0.0.0")
PORT = int(os.environ.get("CRM_PORT", "5000"))
# Адрес системы — подставляется в ссылки в письмах сотрудникам
PUBLIC_URL = os.environ.get("CRM_PUBLIC_URL", f"http://localhost:{PORT}")
