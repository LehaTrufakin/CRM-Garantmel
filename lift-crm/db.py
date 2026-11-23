"""База данных SQLite: схема, начальное заполнение, простые помощники."""
import json
import os
import sqlite3
from contextlib import contextmanager

from werkzeug.security import generate_password_hash

import config
import refs

CLAIM_COLS = [f[0] for f in refs.FIELDS]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    login TEXT UNIQUE NOT NULL,
    email TEXT,
    password_hash TEXT,
    role TEXT DEFAULT 'engineer',       -- admin | engineer | viewer
    tracker_login TEXT,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS statuses (
    name TEXT PRIMARY KEY,
    grp TEXT NOT NULL,
    notify INTEGER DEFAULT 0,
    tracker_status TEXT,
    sla_days INTEGER DEFAULT 5,
    sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS templates (
    status TEXT PRIMARY KEY,
    subject TEXT,
    body TEXT
);
CREATE TABLE IF NOT EXISTS lists (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    number TEXT UNIQUE,
    year INTEGER,
    status TEXT DEFAULT 'Новая',
    status_changed_at TEXT,
    responsible_id INTEGER REFERENCES users(id),
    tracker_key TEXT,
    source TEXT DEFAULT 'manual',        -- manual | email | tracker | import
    deadline TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime')),
    {", ".join(c + " TEXT" for c in CLAIM_COLS)}
);
CREATE INDEX IF NOT EXISTS ix_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS ix_claims_resp ON claims(responsible_id);
CREATE INDEX IF NOT EXISTS ix_claims_recv ON claims(received_date);
CREATE INDEX IF NOT EXISTS ix_claims_tracker ON claims(tracker_key);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER REFERENCES claims(id) ON DELETE CASCADE,
    ts TEXT DEFAULT (datetime('now','localtime')),
    user_name TEXT,
    kind TEXT,           -- created | status | field | comment | mail_in | mail_out | tracker | assign
    text TEXT
);
CREATE INDEX IF NOT EXISTS ix_hist_claim ON history(claim_id);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER REFERENCES claims(id) ON DELETE CASCADE,
    direction TEXT,      -- in | out
    message_id TEXT,
    in_reply_to TEXT,
    sender TEXT,
    recipients TEXT,
    subject TEXT,
    body TEXT,
    sent_at TEXT,
    attachments TEXT     -- json: [имя файла]
);
CREATE INDEX IF NOT EXISTS ix_emails_claim ON emails(claim_id);
CREATE INDEX IF NOT EXISTS ix_emails_msgid ON emails(message_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

DEFAULT_SETTINGS = {
    "number_prefix": "Р",            # номер: Р-26-00001
    "sla_days": 14,                  # общий срок отработки рекламации, дней
    "mail_enabled": False,
    "imap_host": "imap.yandex.ru", "imap_port": 993, "imap_user": "", "imap_password": "", "imap_folder": "INBOX",
    "smtp_host": "smtp.yandex.ru", "smtp_port": 465, "smtp_user": "", "smtp_password": "", "smtp_from": "",
    "mail_interval_min": 2,
    "mail_ignore_from": "",          # через запятую: адреса/домены, письма с которых не регистрировать
    "autoreply_enabled": True,
    "notify_responsible": True,      # письмо сотруднику при назначении
    "tracker_enabled": False,
    "tracker_token": "", "tracker_org_id": "", "tracker_cloud_org": False, "tracker_queue": "MELBSSL",
    "tracker_push": True,            # создавать/обновлять задачи в Трекере
    "tracker_pull": True,            # забирать из Трекера новые задачи и смену статусов
    "tracker_pull_new": False,       # регистрировать задачи, созданные в Трекере (переходный период)
    "tracker_field_map": {},         # {"поле CRM": "id поля в Трекере"}
    "tracker_last_sync": "",
}


def connect():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def tx():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with tx() as con:
        con.executescript(SCHEMA)
        # недостающие колонки (если поля добавили в refs.FIELDS позже)
        have = {r["name"] for r in con.execute("PRAGMA table_info(claims)")}
        for c in CLAIM_COLS:
            if c not in have:
                con.execute(f"ALTER TABLE claims ADD COLUMN {c} TEXT")
        if not con.execute("SELECT 1 FROM statuses").fetchone():
            for i, (name, grp, notify, tst, sla) in enumerate(refs.STATUSES):
                con.execute("INSERT INTO statuses VALUES (?,?,?,?,?,?)", (name, grp, int(notify), tst, sla, i))
        if not con.execute("SELECT 1 FROM templates").fetchone():
            for st, (subj, body) in refs.DEFAULT_TEMPLATES.items():
                con.execute("INSERT INTO templates VALUES (?,?,?)", (st, subj, body))
        for kind, vals in refs.DEFAULT_LISTS.items():
            for v in vals:
                con.execute("INSERT OR IGNORE INTO lists VALUES (?,?)", (kind, v))
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))
        if not con.execute("SELECT 1 FROM users").fetchone():
            con.execute("INSERT INTO users(name, login, password_hash, role) VALUES (?,?,?,?)",
                        ("Администратор", "admin", generate_password_hash("admin"), "admin"))


# ---------- settings ----------
def get_settings(con=None):
    own = con is None
    con = con or connect()
    try:
        return {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT * FROM settings")}
    finally:
        if own:
            con.close()


def set_setting(key, value, con=None):
    own = con is None
    con = con or connect()
    try:
        con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        con.commit()
    finally:
        if own:
            con.close()


# ---------- справочники ----------
def statuses(con):
    return [dict(r) for r in con.execute("SELECT * FROM statuses ORDER BY sort")]


def status_map(con):
    return {s["name"]: s for s in statuses(con)}


def lists(con):
    out = {}
    for r in con.execute("SELECT kind, value FROM lists ORDER BY kind, value COLLATE NOCASE"):
        out.setdefault(r["kind"], []).append(r["value"])
    return out


def users(con, active_only=True):
    q = "SELECT * FROM users" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
    return [dict(r) for r in con.execute(q)]
