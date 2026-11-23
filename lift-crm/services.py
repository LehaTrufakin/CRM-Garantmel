"""Бизнес-логика: создание рекламаций, смена статусов, автоответы, разбор писем, статистика."""
import datetime as dt
import logging
import re
import threading

import db
import refs

log = logging.getLogger("crm")
TODAY = lambda: dt.date.today()


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------- номера ----------------
def next_number(con, year=None):
    year = year or TODAY().year
    prefix = db.get_settings(con).get("number_prefix", "Р")
    yy = str(year)[-2:]
    row = con.execute("SELECT number FROM claims WHERE number LIKE ? ORDER BY number DESC LIMIT 1",
                      (f"{prefix}-{yy}-%",)).fetchone()
    seq = int(row["number"].rsplit("-", 1)[1]) + 1 if row else 1
    return f"{prefix}-{yy}-{seq:05d}"


def history(con, claim_id, kind, text, user="система"):
    con.execute("INSERT INTO history(claim_id, kind, text, user_name, ts) VALUES (?,?,?,?,?)",
                (claim_id, kind, text, user, now_str()))


def get_claim(con, claim_id):
    r = con.execute("""SELECT c.*, u.name AS responsible, u.email AS responsible_email
                       FROM claims c LEFT JOIN users u ON u.id=c.responsible_id WHERE c.id=?""", (claim_id,)).fetchone()
    return dict(r) if r else None


def _deadline(con, received):
    sla = int(db.get_settings(con).get("sla_days", 14))
    try:
        d = dt.date.fromisoformat(received[:10])
    except Exception:
        d = TODAY()
    return (d + dt.timedelta(days=sla)).isoformat()


def remember_values(con, data):
    """Новые компании/узлы/поставщики автоматически попадают в подсказки."""
    for key, label, typ, lst, *_ in refs.FIELDS:
        if typ == "list" and lst and data.get(key):
            con.execute("INSERT OR IGNORE INTO lists VALUES (?,?)", (lst, data[key].strip()))


# ---------------- создание ----------------
def create_claim(con, data, user="система", source="manual", notify=True):
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in data.items() if k in db.CLAIM_COLS}
    data.setdefault("received_date", TODAY().isoformat())
    data.setdefault("claim_date", data["received_date"])
    if not data.get("lift_count") and data.get("serials"):
        data["lift_count"] = str(len(re.findall(r"\d{6,}", data["serials"])) or 1)
    number = next_number(con, int(data["received_date"][:4]))
    cols = list(data.keys())
    cur = con.execute(
        f"INSERT INTO claims(number, year, status, status_changed_at, source, deadline, {', '.join(cols)}) "
        f"VALUES (?,?,?,?,?,?{', ?' * len(cols)})",
        [number, int(data["received_date"][:4]), "Новая", now_str(), source,
         _deadline(con, data["received_date"])] + [data[c] for c in cols])
    cid = cur.lastrowid
    history(con, cid, "created", f"Зарегистрирована ({ {'manual': 'вручную', 'email': 'из письма', 'tracker': 'из Трекера'}.get(source, source)})", user)
    remember_values(con, data)
    con.commit()
    if notify:
        after_status_change(cid, "Новая", comment="", user=user, created=True)
    return cid


# ---------------- изменение ----------------
def update_fields(con, claim_id, data, user):
    old = get_claim(con, claim_id)
    changes = {}
    labels = {f[0]: f[1] for f in refs.FIELDS}
    for k in db.CLAIM_COLS:
        if k in data:
            v = (data[k] or "").strip()
            if (old.get(k) or "") != v:
                changes[k] = v
    if not changes:
        return []
    sets = ", ".join(f"{k}=?" for k in changes)
    con.execute(f"UPDATE claims SET {sets}, updated_at=? WHERE id=?", list(changes.values()) + [now_str(), claim_id])
    txt = "; ".join(f"{labels[k]}: «{(old.get(k) or '—')[:60]}» → «{v[:60] or '—'}»" for k, v in changes.items())
    history(con, claim_id, "field", txt, user)
    remember_values(con, changes)
    con.commit()
    return list(changes)


def assign(con, claim_id, user_id, by_user):
    user_id = int(user_id) if user_id else None
    con.execute("UPDATE claims SET responsible_id=?, updated_at=? WHERE id=?", (user_id, now_str(), claim_id))
    u = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
    history(con, claim_id, "assign", f"Ответственный: {u['name'] if u else '—'}", by_user)
    con.commit()
    if u and u["email"] and db.get_settings(con).get("notify_responsible"):
        c = get_claim(con, claim_id)
        _bg(_mail_staff, u["email"], f"Вам назначена рекламация {c['number']}",
            f"{c['company'] or ''}\n{c['address'] or ''}\n{c['description'] or ''}\n\n"
            f"Открыть: {__import__('config').PUBLIC_URL}/claim/{claim_id}")
    _bg(_tracker_push, claim_id, "assign")


def change_status(con, claim_id, status, user, comment="", send_mail=None, from_tracker=False):
    c = get_claim(con, claim_id)
    if not c or c["status"] == status:
        return False
    st = db.status_map(con).get(status)
    if not st:
        raise ValueError(f"Неизвестный статус: {status}")
    extra = {}
    if st["grp"] in ("closed", "rejected") and not c.get("close_date"):
        extra["close_date"] = TODAY().isoformat()
    if status == "Отправка/ Не забрал заказчик" and not c.get("ship_date"):
        extra["ship_date"] = TODAY().isoformat()
    sets = "".join(f", {k}=?" for k in extra)
    con.execute(f"UPDATE claims SET status=?, status_changed_at=?, updated_at=?{sets} WHERE id=?",
                [status, now_str(), now_str()] + list(extra.values()) + [claim_id])
    history(con, claim_id, "status", f"{c['status']} → {status}" + (f". {comment}" if comment else ""), user)
    con.commit()
    after_status_change(claim_id, status, comment, user, send_mail=send_mail, from_tracker=from_tracker)
    return True


def add_comment(con, claim_id, text, user):
    history(con, claim_id, "comment", text, user)
    con.execute("UPDATE claims SET updated_at=? WHERE id=?", (now_str(), claim_id))
    con.commit()
    _bg(_tracker_comment, claim_id, f"{user}: {text}")


# ---------------- письма и Трекер после смены статуса ----------------
def _bg(fn, *a, **kw):
    def run():
        try:
            fn(*a, **kw)
        except Exception as e:  # ошибки интеграций не должны ломать работу
            log.exception("background task failed: %s", e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def render_template(con, status, claim, comment=""):
    t = con.execute("SELECT * FROM templates WHERE status=?", (status,)).fetchone()
    if not t:
        return None
    vals = {k: (claim.get(k) or "") for k in db.CLAIM_COLS}
    vals.update(number=claim["number"], status=status, comment=comment or "",
                responsible=claim.get("responsible") or "")

    class Safe(dict):
        def __missing__(self, k):
            return ""
    subj = t["subject"].format_map(Safe(vals))
    body = t["body"].format_map(Safe(vals))
    body = re.sub(r"\n{3,}", "\n\n", body)
    return f"{subj} [{claim['number']}]", body


def after_status_change(claim_id, status, comment="", user="система", send_mail=None, created=False, from_tracker=False):
    con = db.connect()
    try:
        s = db.get_settings(con)
        st = db.status_map(con).get(status, {})
        do_mail = send_mail if send_mail is not None else bool(st.get("notify"))
        if do_mail and s.get("autoreply_enabled"):
            c = get_claim(con, claim_id)
            if c.get("client_email"):
                msg = render_template(con, status, c, comment)
                if msg:
                    _bg(send_client_email, claim_id, msg[0], msg[1], user)
    finally:
        con.close()
    if not from_tracker:
        _bg(_tracker_push, claim_id, "create" if created else "status")


def send_client_email(claim_id, subject, body, user="система"):
    import mailer
    con = db.connect()
    try:
        c = get_claim(con, claim_id)
        last_in = con.execute("SELECT message_id FROM emails WHERE claim_id=? AND direction='in' "
                              "ORDER BY id DESC LIMIT 1", (claim_id,)).fetchone()
        try:
            mid = mailer.send(c["client_email"], subject, body, in_reply_to=last_in["message_id"] if last_in else None)
            con.execute("INSERT INTO emails(claim_id, direction, message_id, recipients, subject, body, sent_at) "
                        "VALUES (?,?,?,?,?,?,?)", (claim_id, "out", mid, c["client_email"], subject, body, now_str()))
            history(con, claim_id, "mail_out", f"Письмо клиенту ({c['client_email']}): {subject}", user)
        except Exception as e:
            history(con, claim_id, "error", f"Не удалось отправить письмо: {e}", "система")
        con.commit()
    finally:
        con.close()


def _mail_staff(to, subject, body):
    import mailer
    try:
        mailer.send(to, subject, body)
    except Exception as e:
        log.warning("staff mail failed: %s", e)


def _tracker_push(claim_id, what):
    import tracker
    con = db.connect()
    try:
        s = db.get_settings(con)
        if not (s.get("tracker_enabled") and s.get("tracker_push") and s.get("tracker_token")):
            return
        c = get_claim(con, claim_id)
        try:
            msg = tracker.push_claim(con, c, what)
            if msg:
                history(con, claim_id, "tracker", msg)
        except Exception as e:
            history(con, claim_id, "error", f"Трекер: {e}")
        con.commit()
    finally:
        con.close()


def _tracker_comment(claim_id, text):
    import tracker
    con = db.connect()
    try:
        s = db.get_settings(con)
        c = get_claim(con, claim_id)
        if s.get("tracker_enabled") and s.get("tracker_push") and c.get("tracker_key"):
            try:
                tracker.Client.from_settings(s).add_comment(c["tracker_key"], text)
            except Exception as e:
                history(con, claim_id, "error", f"Трекер (комментарий): {e}")
                con.commit()
    finally:
        con.close()


# ---------------- разбор текста письма ----------------
SERIAL_RE = re.compile(r"(?<!\d)(7[0-9]{8})(?!\d)")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ADDR_LABEL_RE = re.compile(r"(?im)адрес[^:\n]{0,25}:\s*(.{5,200})$")
ADDR_MARK_RE = re.compile(r"(?i)(?<![\w])(г\.|гор\.|город\s|[\w-]+\s+обл(?:\.|асть|\b)|г\.о\.|ул\.|улица\s|пр-кт|проспект\s|"
                          r"[\w-]+\s+шоссе|пер\.|ЖК\s|мкр)")
ADDR_END_RE = re.compile(r"(?i)\s(?:зав\.?|заводск\w*|№|тел\.?|телефон|e-?mail|лифт\w*\s+(?:№|не|стоит))|[;\n]|\.\s+(?=[А-ЯЁ][а-яё]+\s[а-яё])")


def extract_address(text):
    m = ADDR_LABEL_RE.search(text)
    if m:
        return m.group(1).strip(" ,.;")
    for line in text.splitlines():
        if re.match(r"(?i)\s*(от|тема|кому|from|to|subject)\s*:", line):
            continue
        m = ADDR_MARK_RE.search(line)
        if not m:
            continue
        rest = line[m.start():]
        e = ADDR_END_RE.search(rest, 6)
        addr = (rest[:e.start()] if e else rest).strip(" ,.;")
        if len(addr) >= 8:
            return addr[:200]
    return ""


def parse_text(con, text, sender_email=""):
    """Пытается заполнить поля по тексту письма. Всё, что найдено, — только подсказка: инженер проверит."""
    text = text or ""
    res = {}
    serials = list(dict.fromkeys(SERIAL_RE.findall(text)))
    if serials:
        res["serials"] = ", ".join(serials)
        res["lift_count"] = str(len(serials))
    addr = extract_address(text)
    if addr:
        res["address"] = addr
    phones = PHONE_RE.findall(text)
    if phones:
        res["contacts"] = phones[0]
    if sender_email:
        res["client_email"] = sender_email
        # компания по прошлым письмам с того же адреса/домена
        dom = sender_email.split("@")[-1].lower()
        row = con.execute("SELECT company, ownership FROM claims WHERE lower(client_email)=? AND company<>'' "
                          "ORDER BY id DESC LIMIT 1", (sender_email.lower(),)).fetchone()
        if not row and dom not in ("mail.ru", "yandex.ru", "gmail.com", "bk.ru", "list.ru", "inbox.ru", "ya.ru", "rambler.ru"):
            row = con.execute("SELECT company, ownership FROM claims WHERE lower(client_email) LIKE ? AND company<>'' "
                              "ORDER BY id DESC LIMIT 1", ("%@" + dom,)).fetchone()
        if row:
            res["company"] = row["company"]
            if row["ownership"]:
                res["ownership"] = row["ownership"]
    low = text.lower()
    if serials:  # по заводскому номеру — что знаем об этом лифте
        row = con.execute("SELECT company, ownership, warranty_terms, manuf_date FROM claims WHERE serials LIKE ? "
                          "ORDER BY id DESC LIMIT 1", (f"%{serials[0]}%",)).fetchone()
        if row:
            res.update({k: row[k] for k in row.keys() if row[k] and not res.get(k)})
    if "company" not in res:  # название компании в тексте письма (целым словом)
        for comp in db.lists(con).get("company", []):
            core = re.sub(r'["«»]|\b(ООО|АО|ЗАО|ПАО|ОАО|УК|СК|ТД)\b|\(.*?\)', "", comp).strip().lower()
            if len(core) >= 5 and re.search(r"(?<!\w)" + re.escape(core) + r"(?!\w)", low):
                res["company"] = comp
                break
    # узел: сравниваем по основам слов, чтобы «отводного блока» нашло «Отводной блок»
    words = re.findall(r"[а-яёa-z0-9]+", low.replace("ё", "е"))
    best, best_len = "", 0
    stem = lambda w: w if len(w) <= 4 else w[:max(4, len(w) - 2)]
    has = lambda st: any(t == st if len(st) <= 3 else t.startswith(st) for t in words)
    for node in db.lists(con).get("node", []):
        n = node.lower().replace("ё", "е")
        req = [stem(w) for w in re.findall(r"[а-яa-z0-9]+", re.sub(r"\(.*?\)", "", n))]
        opt = [stem(w) for w in re.findall(r"[а-яa-z0-9]+", " ".join(re.findall(r"\((.*?)\)", n)))]
        if req and all(has(st) for st in req):
            score = sum(map(len, req)) + sum(len(st) for st in opt if has(st)) * 2 - 0.1 * len(opt)
            if score > best_len:
                best, best_len = node, score
    if best:
        res["node"] = best
    return res


# ---------------- выборки ----------------
VIEWS = {
    "open": ("Открытые", "s.grp NOT IN ('closed','rejected')"),
    "new": ("Новые / без ответственного", "(s.grp='new' OR c.responsible_id IS NULL) AND s.grp NOT IN ('closed','rejected')"),
    "mine": ("Мои", "c.responsible_id=:me AND s.grp NOT IN ('closed','rejected')"),
    "overdue": ("Просрочены", "s.grp NOT IN ('closed','rejected') AND c.deadline < date('now','localtime')"),
    "client": ("Ждём клиента", "s.grp='client'"),
    "closed": ("Закрытые", "s.grp IN ('closed','rejected')"),
    "all": ("Все", "1=1"),
}


def search_claims(con, view="open", q="", status="", responsible="", company="", node="", year="",
                  me=None, limit=200, offset=0, order="c.id DESC"):
    where = [VIEWS.get(view, VIEWS["open"])[1]]
    p = {"me": me}
    if q:
        where.append("(c.number LIKE :q OR c.company LIKE :q OR c.address LIKE :q OR c.serials LIKE :q "
                     "OR c.description LIKE :q OR c.incoming_no LIKE :q OR c.tracker_key LIKE :q OR c.node LIKE :q)")
        p["q"] = f"%{q}%"
    for key, col in (("status", "c.status"), ("company", "c.company"), ("node", "c.node")):
        val = locals()[key]
        if val:
            where.append(f"{col}=:{key}")
            p[key] = val
    if responsible:
        where.append("c.responsible_id=:resp" if responsible != "none" else "c.responsible_id IS NULL")
        p["resp"] = responsible
    if year:
        where.append("c.year=:year")
        p["year"] = int(year)
    w = " AND ".join(where)
    base = f"FROM claims c LEFT JOIN statuses s ON s.name=c.status LEFT JOIN users u ON u.id=c.responsible_id WHERE {w}"
    total = con.execute(f"SELECT count(*) {base}", p).fetchone()[0]
    rows = con.execute(f"""SELECT c.id, c.number, c.received_date, c.company, c.address, c.serials, c.node,
                c.description, c.status, c.deadline, c.tracker_key, c.ownership, c.close_date, s.grp,
                u.name AS responsible,
                CAST(julianday(COALESCE(c.close_date, date('now','localtime'))) - julianday(c.received_date) AS INT) AS days
                {base} ORDER BY {order} LIMIT :lim OFFSET :off""", dict(p, lim=limit, off=offset)).fetchall()
    return total, [dict(r) for r in rows]


def view_counts(con, me):
    out = {}
    for k, (_, cond) in VIEWS.items():
        out[k] = con.execute(f"SELECT count(*) FROM claims c LEFT JOIN statuses s ON s.name=c.status WHERE {cond}",
                             {"me": me}).fetchone()[0]
    return out


# ---------------- статистика для дашборда ----------------
def stats(con, date_from, date_to, ownership=""):
    p = {"f": date_from, "t": date_to}
    own = ""
    if ownership:
        own = " AND c.ownership=:own"
        p["own"] = ownership
    J = "FROM claims c LEFT JOIN statuses s ON s.name=c.status LEFT JOIN users u ON u.id=c.responsible_id"
    period = f"c.received_date BETWEEN :f AND :t{own}"

    def one(sql):
        return con.execute(sql, p).fetchone()[0] or 0

    def rows(sql):
        return [dict(r) for r in con.execute(sql, p)]

    k = {
        "received": one(f"SELECT count(*) {J} WHERE {period}"),
        "closed": one(f"SELECT count(*) {J} WHERE {period} AND s.grp IN ('closed','rejected')"),
        "open_now": one(f"SELECT count(*) {J} WHERE s.grp NOT IN ('closed','rejected'){own}"),
        "overdue": one(f"SELECT count(*) {J} WHERE s.grp NOT IN ('closed','rejected') "
                       f"AND c.deadline < date('now','localtime'){own}"),
        "unassigned": one(f"SELECT count(*) {J} WHERE s.grp NOT IN ('closed','rejected') AND c.responsible_id IS NULL{own}"),
        "lead_avg": one(f"SELECT round(avg(julianday(c.close_date)-julianday(c.received_date)),1) {J} "
                        f"WHERE c.close_date BETWEEN :f AND :t{own} AND c.close_date>=c.received_date"),
        "lead_median": None,
        "warranty": one(f"SELECT count(*) {J} WHERE {period} AND c.resume='гарантия'"),
        "refusal": one(f"SELECT count(*) {J} WHERE {period} AND c.resume='отказ'"),
        "lifts": one(f"SELECT sum(CAST(c.lift_count AS INT)) {J} WHERE {period}"),
    }
    leads = [r[0] for r in con.execute(
        f"SELECT julianday(c.close_date)-julianday(c.received_date) {J} WHERE c.close_date BETWEEN :f AND :t{own} "
        f"AND c.close_date>=c.received_date ORDER BY 1", p)]
    if leads:
        k["lead_median"] = round(leads[len(leads) // 2], 1)
        k["sla_ok"] = round(100 * sum(1 for x in leads if x <= int(db.get_settings(con).get("sla_days", 14))) / len(leads))
    else:
        k["sla_ok"] = None
    # закрытие считаем по когорте месяца поступления: у исторических записей из реестра нет даты закрытия
    monthly = rows(f"""SELECT substr(c.received_date,1,7) AS m, count(*) AS received,
            sum(CASE WHEN s.grp IN ('closed','rejected') THEN 1 ELSE 0 END) AS closed
            {J} WHERE {period} GROUP BY 1 ORDER BY 1""")
    top = lambda col, n=10: rows(f"SELECT COALESCE(NULLIF(trim({col}),''),'— не указано') AS name, count(*) AS n "
                                 f"{J} WHERE {period} GROUP BY 1 ORDER BY n DESC LIMIT {n}")
    by_status = rows(f"SELECT c.status AS name, s.grp, count(*) AS n {J} WHERE s.grp NOT IN ('closed','rejected'){own} "
                     f"GROUP BY 1 ORDER BY s.sort")
    by_resp = rows(f"""SELECT COALESCE(u.name,'— не назначен') AS name, count(*) AS n,
            sum(CASE WHEN c.deadline < date('now','localtime') THEN 1 ELSE 0 END) AS overdue
            {J} WHERE s.grp NOT IN ('closed','rejected'){own} GROUP BY 1 ORDER BY n DESC""")
    aging = rows(f"""SELECT CASE
              WHEN d<=7 THEN '0–7 дн' WHEN d<=14 THEN '8–14 дн' WHEN d<=30 THEN '15–30 дн'
              WHEN d<=60 THEN '31–60 дн' ELSE '> 60 дн' END AS name, count(*) AS n, min(d) AS o FROM (
            SELECT julianday(date('now','localtime'))-julianday(c.received_date) AS d {J}
            WHERE s.grp NOT IN ('closed','rejected'){own}) GROUP BY 1 ORDER BY o""")
    return {
        "k": k, "monthly": monthly, "by_status": by_status, "by_resp": by_resp, "aging": aging,
        "by_node": top("c.node"), "by_company": top("c.company"), "by_reason": top("c.confirmed_reason", 6),
        "by_declared": top("c.declared_fault", 8), "by_own": top("c.ownership", 3),
        "by_problem": top("c.problem_type", 3), "by_supplier": top("c.executors", 10),
    }
