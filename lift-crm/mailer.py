"""Почта: приём писем (IMAP) → регистрация рекламаций; отправка автоответов (SMTP)."""
import email
import email.utils
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage

import config
import db
import services

log = logging.getLogger("crm.mail")
NUM_RE = re.compile(r"\[([A-ZА-ЯЁ]{1,4}-\d{2}-\d{5})\]")


def send(to, subject, body, in_reply_to=None):
    s = db.get_settings()
    if not s.get("smtp_user"):
        raise RuntimeError("SMTP не настроен (Настройки → Интеграции)")
    msg = EmailMessage()
    msg["From"] = s.get("smtp_from") or s["smtp_user"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = email.utils.make_msgid(domain=(s["smtp_user"].split("@")[-1] or "crm"))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    port = int(s.get("smtp_port") or 465)
    if port == 465:
        with smtplib.SMTP_SSL(s["smtp_host"], port, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(s["smtp_user"], s["smtp_password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s["smtp_host"], port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(s["smtp_user"], s["smtp_password"])
            smtp.send_message(msg)
    return msg["Message-ID"]


def _dec(v):
    try:
        return str(make_header(decode_header(v or "")))
    except Exception:
        return v or ""


def _html_to_text(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    import html as h
    return re.sub(r"\n\s*\n+", "\n\n", h.unescape(html)).strip()


def _strip_quote(text):
    """Отрезаем цитату предыдущей переписки, чтобы в карточке был только новый текст."""
    for pat in (r"\n-{2,}\s*Original Message", r"\n-{2,}\s*Исходное сообщение", r"\n>.*", r"\nОт:\s.*\nОтправлено:",
                r"\n\d{1,2}\.\d{1,2}\.\d{4}.{0,40}пишет:", r"\nOn .{5,80}wrote:"):
        m = re.search(pat, text)
        if m and m.start() > 20:
            text = text[:m.start()]
    return text.strip()


def parse_message(raw):
    msg = email.message_from_bytes(raw)
    name, addr = email.utils.parseaddr(_dec(msg.get("From")))
    body_plain, body_html, files = "", "", []
    for part in msg.walk():
        if part.is_multipart():
            continue
        fn = part.get_filename()
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True) or b""
        if fn:
            files.append((_dec(fn), payload))
        elif ctype == "text/plain" and not body_plain:
            body_plain = payload.decode(part.get_content_charset() or "utf-8", "replace")
        elif ctype == "text/html" and not body_html:
            body_html = payload.decode(part.get_content_charset() or "utf-8", "replace")
    body = body_plain or _html_to_text(body_html)
    try:
        date = email.utils.parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        date = services.now_str()
    return {
        "message_id": (msg.get("Message-ID") or "").strip(),
        "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
        "references": msg.get("References") or "",
        "from_name": name, "from_addr": addr.lower(),
        "to": _dec(msg.get("To")), "subject": _dec(msg.get("Subject")),
        "body": body.strip(), "date": date, "files": files,
    }


def _save_files(claim_id, files):
    names = []
    folder = os.path.join(config.FILES_DIR, str(claim_id))
    os.makedirs(folder, exist_ok=True)
    for fn, data in files:
        safe = re.sub(r'[\\/:*?"<>|]+', "_", fn)[:150] or "file"
        path, i = os.path.join(folder, safe), 1
        while os.path.exists(path):
            base, ext = os.path.splitext(safe)
            path = os.path.join(folder, f"{base}_{i}{ext}")
            i += 1
        with open(path, "wb") as f:
            f.write(data)
        names.append(os.path.basename(path))
    return names


def find_existing_claim(con, m):
    """Ответ клиента в уже открытую рекламацию: по номеру в теме или по цепочке писем."""
    mm = NUM_RE.search(m["subject"])
    if mm:
        r = con.execute("SELECT id FROM claims WHERE number=?", (mm.group(1),)).fetchone()
        if r:
            return r["id"]
    ids = [m["in_reply_to"]] + re.findall(r"<[^>]+>", m["references"])
    for mid in filter(None, ids):
        r = con.execute("SELECT claim_id FROM emails WHERE message_id=?", (mid,)).fetchone()
        if r:
            return r["claim_id"]
    return None


def register_message(con, m):
    """Одно письмо → новая рекламация или запись в существующую. Возвращает (claim_id, is_new)."""
    if m["message_id"] and con.execute("SELECT 1 FROM emails WHERE message_id=?", (m["message_id"],)).fetchone():
        return None, False
    s = db.get_settings(con)
    ignore = [x.strip().lower() for x in (s.get("mail_ignore_from") or "").split(",") if x.strip()]
    if any(i in m["from_addr"] for i in ignore):
        return None, False
    cid = find_existing_claim(con, m)
    is_new = cid is None
    text = _strip_quote(m["body"])
    if is_new:
        guess = services.parse_text(con, m["subject"] + "\n" + text, m["from_addr"])
        data = dict(guess)
        data["description"] = (m["subject"] + "\n" + text).strip()[:3000]
        data["applicant"] = m["from_name"] or m["from_addr"]
        data["received_date"] = m["date"][:10]
        data["claim_date"] = m["date"][:10]
        cid = services.create_claim(con, data, user="почта", source="email", notify=False)
    files = _save_files(cid, m["files"]) if m["files"] else []
    con.execute("INSERT INTO emails(claim_id, direction, message_id, in_reply_to, sender, recipients, subject, body, "
                "sent_at, attachments) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, "in", m["message_id"], m["in_reply_to"], f'{m["from_name"]} <{m["from_addr"]}>', m["to"],
                 m["subject"], m["body"], m["date"], json.dumps(files, ensure_ascii=False)))
    services.history(con, cid, "mail_in", f"Письмо от {m['from_addr']}: {m['subject']}"
                     + (f" (вложений: {len(files)})" if files else ""), "почта")
    if not is_new:
        c = services.get_claim(con, cid)
        if c["status"] == "Требуется уточнение":  # клиент ответил — возвращаем в работу
            con.commit()
            services.change_status(con, cid, "В процессе", "почта", "Получен ответ клиента", send_mail=False)
    con.commit()
    if is_new:
        services.after_status_change(cid, "Новая", user="почта", created=True)
    return cid, is_new


def fetch_inbox():
    """Забрать непрочитанные письма из ящика. Возвращает число новых рекламаций и ответов."""
    s = db.get_settings()
    if not (s.get("mail_enabled") and s.get("imap_user")):
        return 0, 0
    new, replies = 0, 0
    with imaplib.IMAP4_SSL(s["imap_host"], int(s.get("imap_port") or 993)) as imap:
        imap.login(s["imap_user"], s["imap_password"])
        imap.select(s.get("imap_folder") or "INBOX")
        typ, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            typ, msgdata = imap.fetch(num, "(RFC822)")
            raw = msgdata[0][1]
            con = db.connect()
            try:
                cid, is_new = register_message(con, parse_message(raw))
                if cid:
                    new += is_new
                    replies += not is_new
            except Exception:
                log.exception("Ошибка обработки письма")
                imap.store(num, "-FLAGS", "\\Seen")  # оставим непрочитанным, попробуем позже
            finally:
                con.close()
    return new, replies
