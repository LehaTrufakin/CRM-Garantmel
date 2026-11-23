"""CRM учёта рекламаций на лифтовое оборудование. Запуск: python app.py → http://localhost:5000"""
import datetime as dt
import functools
import io
import json
import logging
import os
import threading
import time

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import config
import db
import refs
import services
import xlsx_io

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("crm")

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024
db.init()


# ---------------- общие ----------------
@app.before_request
def open_db():
    g.con = db.connect()
    g.user = None
    if session.get("uid"):
        r = g.con.execute("SELECT * FROM users WHERE id=? AND active=1", (session["uid"],)).fetchone()
        g.user = dict(r) if r else None


@app.teardown_request
def close_db(exc):
    con = g.pop("con", None)
    if con:
        con.close()


def login_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrap


def can_edit():
    return g.user and g.user["role"] in ("admin", "engineer")


def admin_required(fn):
    @functools.wraps(fn)
    def wrap(*a, **kw):
        if not g.user:
            return redirect(url_for("login"))
        if g.user["role"] != "admin":
            abort(403)
        return fn(*a, **kw)
    return wrap


@app.context_processor
def inject():
    return {"user": g.get("user"), "GROUPS": refs.GROUPS, "today": dt.date.today().isoformat(),
            "can_edit": can_edit()}


@app.template_filter("d")
def fmt_date(v):
    if not v:
        return ""
    try:
        return dt.date.fromisoformat(str(v)[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return v


@app.template_filter("dt")
def fmt_dt(v):
    try:
        return dt.datetime.fromisoformat(str(v)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return v or ""


# ---------------- вход ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        r = g.con.execute("SELECT * FROM users WHERE login=? AND active=1", (request.form["login"].strip(),)).fetchone()
        if r and r["password_hash"] and check_password_hash(r["password_hash"], request.form["password"]):
            session["uid"] = r["id"]
            session.permanent = True
            return redirect(request.args.get("next") or url_for("claims"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("claims"))


# ---------------- реестр ----------------
def _filters():
    a = request.args
    return dict(view=a.get("view", "open"), q=a.get("q", "").strip(), status=a.get("status", ""),
                responsible=a.get("resp", ""), company=a.get("company", ""), node=a.get("node", ""),
                year=a.get("year", ""))


@app.route("/claims")
@login_required
def claims():
    f = _filters()
    page = max(1, int(request.args.get("page", 1)))
    per = 50
    total, rows = services.search_claims(g.con, me=g.user["id"], limit=per, offset=(page - 1) * per, **f)
    return render_template("claims.html", rows=rows, total=total, page=page, pages=(total + per - 1) // per,
                           f=f, views=services.VIEWS, counts=services.view_counts(g.con, g.user["id"]),
                           statuses=db.statuses(g.con), users=db.users(g.con), lists=db.lists(g.con),
                           years=[r[0] for r in g.con.execute("SELECT DISTINCT year FROM claims ORDER BY 1 DESC")])


@app.route("/claims/export")
@login_required
def claims_export():
    f = _filters()
    _, rows = services.search_claims(g.con, me=g.user["id"], limit=100000, order="c.received_date, c.id", **f)
    full = [services.get_claim(g.con, r["id"]) for r in rows]
    data = xlsx_io.export_registry(full)
    name = f"Реестр рекламаций {dt.date.today():%d.%m.%Y}.xlsx"
    return send_file(io.BytesIO(data), download_name=name, as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------- новая рекламация ----------------
@app.route("/new", methods=["GET", "POST"])
@login_required
def new_claim():
    if not can_edit():
        abort(403)
    if request.method == "POST":
        data = {k: v for k, v in request.form.items() if k in db.CLAIM_COLS}
        if not data.get("description") and not data.get("company"):
            flash("Заполните хотя бы компанию или описание", "error")
            return redirect(url_for("new_claim"))
        notify = bool(request.form.get("notify"))
        cid = services.create_claim(g.con, data, user=g.user["name"], notify=notify)
        if request.form.get("responsible_id"):
            services.assign(g.con, cid, request.form["responsible_id"], g.user["name"])
        elif request.form.get("take"):
            services.assign(g.con, cid, g.user["id"], g.user["name"])
        if request.form.get("raw_email"):
            g.con.execute("INSERT INTO emails(claim_id, direction, sender, subject, body, sent_at) VALUES (?,?,?,?,?,?)",
                          (cid, "in", data.get("client_email", ""), "Текст обращения (вставлен вручную)",
                           request.form["raw_email"], services.now_str()))
            g.con.commit()
        c = services.get_claim(g.con, cid)
        flash(f"Рекламация {c['number']} зарегистрирована" + (" — клиенту отправлено подтверждение" if notify and c.get("client_email") else ""), "ok")
        return redirect(url_for("claim", cid=cid) if request.form.get("open") else url_for("new_claim"))
    fields = [f for f in refs.FIELDS if f[0] in refs.QUICK_FIELDS]
    fields.sort(key=lambda f: refs.QUICK_FIELDS.index(f[0]))
    return render_template("new.html", fields=fields, lists=db.lists(g.con), users=db.users(g.con))


@app.route("/api/parse", methods=["POST"])
@login_required
def api_parse():
    text = request.json.get("text", "")
    sender = ""
    m = services.EMAIL_RE.search(text)
    if m:
        sender = m.group(0).lower()
    res = services.parse_text(g.con, text, sender)
    body = "\n".join(l for l in text.splitlines()
                     if not __import__("re").match(r"(?i)\s*(от|кому|копия|отправлено|дата|from|to|sent|cc)\s*:", l))
    res["description"] = body.strip()[:3000]
    return jsonify(res)


@app.route("/api/lookup")
@login_required
def api_lookup():
    """Подсказка по заводскому номеру: что уже знаем об этом лифте и его прошлые рекламации."""
    sn = request.args.get("serial", "").strip()
    if len(sn) < 6:
        return jsonify({})
    rows = g.con.execute("SELECT id, number, received_date, node, status, company, address, ownership, warranty_terms, "
                         "manuf_date FROM claims WHERE serials LIKE ? ORDER BY id DESC LIMIT 10", (f"%{sn}%",)).fetchall()
    return jsonify({"history": [dict(r) for r in rows]})


# ---------------- карточка ----------------
@app.route("/claim/<int:cid>")
@login_required
def claim(cid):
    c = services.get_claim(g.con, cid)
    if not c:
        abort(404)
    smap = db.status_map(g.con)
    hist = [dict(r) for r in g.con.execute("SELECT * FROM history WHERE claim_id=? ORDER BY id DESC", (cid,))]
    emails = [dict(r) for r in g.con.execute("SELECT * FROM emails WHERE claim_id=? ORDER BY id", (cid,))]
    for e in emails:
        e["files"] = json.loads(e["attachments"] or "[]")
    folder = os.path.join(config.FILES_DIR, str(cid))
    files = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
    same = []
    if c.get("serials"):
        first = c["serials"].replace(",", " ").split()[0]
        same = [dict(r) for r in g.con.execute(
            "SELECT id, number, received_date, node, status FROM claims WHERE serials LIKE ? AND id<>? "
            "ORDER BY id DESC LIMIT 8", (f"%{first}%", cid))]
    s = db.get_settings(g.con)
    tracker_url = f"https://tracker.yandex.ru/{c['tracker_key']}" if c.get("tracker_key") else None
    return render_template("claim.html", c=c, st=smap.get(c["status"], {}), statuses=list(smap.values()),
                           next_steps=refs.NEXT_STEPS.get(c["status"], []), hist=hist, emails=emails, files=files,
                           fields=refs.FIELDS, sections=refs.SECTIONS, lists=db.lists(g.con), users=db.users(g.con),
                           same=same, tracker_url=tracker_url, smap=smap,
                           overdue=c["deadline"] and c["deadline"] < dt.date.today().isoformat()
                           and smap.get(c["status"], {}).get("grp") not in ("closed", "rejected"),
                           autoreply=s.get("autoreply_enabled"))


@app.route("/claim/<int:cid>/save", methods=["POST"])
@login_required
def claim_save(cid):
    if not can_edit():
        abort(403)
    data = dict(request.form)
    if "deadline" in data:
        g.con.execute("UPDATE claims SET deadline=? WHERE id=?", (data.pop("deadline") or None, cid))
        g.con.commit()
    changed = services.update_fields(g.con, cid, data, g.user["name"])
    if changed:
        services._bg(services._tracker_push, cid, "fields")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, changed=len(changed))
    flash("Сохранено" if changed else "Изменений нет", "ok")
    return redirect(url_for("claim", cid=cid))


@app.route("/claim/<int:cid>/preview")
@login_required
def claim_preview(cid):
    """Текст письма клиенту для выбранного статуса — показываем перед отправкой, можно отредактировать."""
    c = services.get_claim(g.con, cid)
    status = request.args.get("status")
    st = db.status_map(g.con).get(status, {})
    msg = services.render_template(g.con, status, c) if status else None
    return jsonify(notify=bool(st.get("notify")) and bool(msg), email=c.get("client_email") or "",
                   subject=msg[0] if msg else "", body=msg[1] if msg else "")


@app.route("/claim/<int:cid>/status", methods=["POST"])
@login_required
def claim_status(cid):
    if not can_edit():
        abort(403)
    f = request.form
    status, comment = f["status"], f.get("comment", "").strip()
    send = f.get("send_mail") == "1"
    services.change_status(g.con, cid, status, g.user["name"], comment, send_mail=False)
    if send and f.get("mail_body"):
        to = f.get("mail_to", "").strip()
        if to:
            c = services.get_claim(g.con, cid)
            if to != (c.get("client_email") or ""):
                services.update_fields(g.con, cid, {"client_email": to}, g.user["name"])
            services._bg(services.send_client_email, cid, f["mail_subject"], f["mail_body"], g.user["name"])
    if f.get("take") and not services.get_claim(g.con, cid)["responsible_id"]:
        services.assign(g.con, cid, g.user["id"], g.user["name"])
    flash(f"Статус: {status}" + (" · письмо клиенту отправляется" if send else ""), "ok")
    return redirect(url_for("claim", cid=cid))


@app.route("/claim/<int:cid>/assign", methods=["POST"])
@login_required
def claim_assign(cid):
    if not can_edit():
        abort(403)
    services.assign(g.con, cid, request.form.get("responsible_id"), g.user["name"])
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(url_for("claim", cid=cid))


@app.route("/claim/<int:cid>/comment", methods=["POST"])
@login_required
def claim_comment(cid):
    text = request.form.get("text", "").strip()
    if text:
        services.add_comment(g.con, cid, text, g.user["name"])
    return redirect(url_for("claim", cid=cid) + "#feed")


@app.route("/claim/<int:cid>/mail", methods=["POST"])
@login_required
def claim_mail(cid):
    if not can_edit():
        abort(403)
    f = request.form
    c = services.get_claim(g.con, cid)
    to = f.get("to", "").strip() or c.get("client_email")
    if not to:
        flash("Не указан e-mail клиента", "error")
        return redirect(url_for("claim", cid=cid))
    if to != c.get("client_email"):
        services.update_fields(g.con, cid, {"client_email": to}, g.user["name"])
    subject = f.get("subject", "").strip() or f"Рекламация № {c['number']}"
    if c["number"] not in subject:
        subject += f" [{c['number']}]"
    services._bg(services.send_client_email, cid, subject, f.get("body", ""), g.user["name"])
    flash("Письмо отправляется", "ok")
    return redirect(url_for("claim", cid=cid) + "#feed")


@app.route("/claim/<int:cid>/upload", methods=["POST"])
@login_required
def claim_upload(cid):
    folder = os.path.join(config.FILES_DIR, str(cid))
    os.makedirs(folder, exist_ok=True)
    names = []
    for fs in request.files.getlist("files"):
        if fs.filename:
            name = os.path.basename(fs.filename).replace("..", "_")
            fs.save(os.path.join(folder, name))
            names.append(name)
    if names:
        services.history(g.con, cid, "comment", "Добавлены файлы: " + ", ".join(names), g.user["name"])
        g.con.commit()
    return redirect(url_for("claim", cid=cid))


@app.route("/claim/<int:cid>/file/<path:name>")
@login_required
def claim_file(cid, name):
    return send_from_directory(os.path.join(config.FILES_DIR, str(cid)), name)


@app.route("/claim/<int:cid>/copy")
@login_required
def claim_copy(cid):
    """Повторная рекламация по тому же объекту: копируем данные заявки и оборудования."""
    c = services.get_claim(g.con, cid)
    keep = [f[0] for f in refs.FIELDS if f[4] in ("main", "equip") and f[0] not in ("description", "incoming_no",
                                                                                 "received_date", "claim_date")]
    data = {k: c[k] for k in keep if c.get(k)}
    new = services.create_claim(g.con, data, user=g.user["name"], notify=False)
    services.add_comment(g.con, new, f"Создана копированием из {c['number']}", g.user["name"])
    return redirect(url_for("claim", cid=new))


# ---------------- дашборд и отчёты ----------------
def _period():
    t = dt.date.today()
    preset = request.args.get("p", "year")
    if preset == "month":
        f = t.replace(day=1)
    elif preset == "quarter":
        f = t.replace(month=(t.month - 1) // 3 * 3 + 1, day=1)
    elif preset == "12m":
        f = (t.replace(day=1) - dt.timedelta(days=335)).replace(day=1)
    else:
        f = t.replace(month=1, day=1)
    return request.args.get("from") or f.isoformat(), request.args.get("to") or t.isoformat(), preset


@app.route("/dashboard")
@login_required
def dashboard():
    f, t, preset = _period()
    own = request.args.get("own", "")
    st = services.stats(g.con, f, t, own)
    return render_template("dashboard.html", s=st, f=f, t=t, preset=preset, own=own,
                           sla=db.get_settings(g.con).get("sla_days"))


@app.route("/reports")
@login_required
def reports():
    f, t, preset = _period()
    own = request.args.get("own", "")
    dim = request.args.get("dim", "node")
    dims = {"node": "Узел", "company": "Компания", "executors": "Исполнитель / поставщик",
            "confirmed_reason": "Причина (завод)", "declared_fault": "Заявленная неисправность",
            "responsible": "Ответственный", "status": "Статус", "ownership": "Принадлежность", "month": "Месяц"}
    col = {"responsible": "COALESCE(u.name,'— не назначен')", "month": "substr(c.received_date,1,7)",
           "status": "c.status"}.get(dim, f"COALESCE(NULLIF(trim(c.{dim}),''),'— не указано')")
    p = {"f": f, "t": t}
    w = "c.received_date BETWEEN :f AND :t"
    if own:
        w += " AND c.ownership=:own"
        p["own"] = own
    sla = int(db.get_settings(g.con).get("sla_days", 14))
    rows = [dict(r) for r in g.con.execute(f"""
        SELECT {col} AS name, count(*) AS total,
          sum(CASE WHEN s.grp IN ('closed','rejected') THEN 1 ELSE 0 END) AS closed,
          sum(CASE WHEN s.grp NOT IN ('closed','rejected') THEN 1 ELSE 0 END) AS open,
          sum(CASE WHEN s.grp NOT IN ('closed','rejected') AND c.deadline < date('now','localtime') THEN 1 ELSE 0 END) AS overdue,
          sum(CASE WHEN c.resume='гарантия' THEN 1 ELSE 0 END) AS warranty,
          sum(CASE WHEN c.resume='отказ' THEN 1 ELSE 0 END) AS refusal,
          sum(CASE WHEN c.problem_type='Системная' THEN 1 ELSE 0 END) AS systemic,
          round(avg(CASE WHEN c.close_date>=c.received_date THEN julianday(c.close_date)-julianday(c.received_date) END),1) AS lead,
          sum(CASE WHEN c.close_date>=c.received_date AND julianday(c.close_date)-julianday(c.received_date)<={sla} THEN 1 ELSE 0 END) AS in_sla,
          sum(CASE WHEN c.close_date>=c.received_date THEN 1 ELSE 0 END) AS with_lead
        FROM claims c LEFT JOIN statuses s ON s.name=c.status LEFT JOIN users u ON u.id=c.responsible_id
        WHERE {w} GROUP BY 1 ORDER BY {'1' if dim == 'month' else 'total DESC'}""", p)]
    if request.args.get("xlsx"):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Отчёт"
        ws.append([f"Отчёт по рекламациям: {dims[dim]}, период {fmt_date(f)} – {fmt_date(t)}"])
        ws.append([dims[dim], "Всего", "Закрыто", "В работе", "Просрочено", "Гарантия", "Отказ", "Системные",
                   "Ср. срок, дн", f"В срок ≤{sla} дн, %"])
        for r in rows:
            ws.append([r["name"], r["total"], r["closed"], r["open"], r["overdue"], r["warranty"], r["refusal"],
                       r["systemic"], r["lead"], round(100 * r["in_sla"] / r["with_lead"]) if r["with_lead"] else None])
        ws.column_dimensions["A"].width = 45
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(buf, download_name=f"Отчёт {dims[dim]} {f}_{t}.xlsx", as_attachment=True)
    return render_template("reports.html", rows=rows, dims=dims, dim=dim, f=f, t=t, preset=preset, own=own, sla=sla)


# ---------------- синхронизация ----------------
def run_sync():
    import mailer
    import tracker
    out = {}
    try:
        out["mail"] = mailer.fetch_inbox()
    except Exception as e:
        out["mail_error"] = str(e)
    con = db.connect()
    try:
        out["tracker"] = tracker.pull(con)
    except Exception as e:
        out["tracker_error"] = str(e)
    finally:
        con.close()
    return out


@app.route("/sync", methods=["POST"])
@login_required
def sync_now():
    r = run_sync()
    msg = []
    if "mail" in r:
        msg.append(f"почта: новых {r['mail'][0]}, ответов {r['mail'][1]}")
    if r.get("mail_error"):
        msg.append(f"почта: ошибка — {r['mail_error']}")
    if isinstance(r.get("tracker"), dict) and not r["tracker"].get("skipped"):
        t = r["tracker"]
        msg.append(f"Трекер: просмотрено {t['seen']}, новых {t['created']}, статусов {t['status']}")
    if r.get("tracker_error"):
        msg.append(f"Трекер: ошибка — {r['tracker_error']}")
    flash("; ".join(msg) or "Интеграции выключены (Настройки → Интеграции)", "ok")
    return redirect(request.referrer or url_for("claims"))


def worker_loop():
    time.sleep(5)
    while True:
        try:
            run_sync()
        except Exception:
            log.exception("sync failed")
        try:
            interval = max(1, int(db.get_settings().get("mail_interval_min", 2)))
        except Exception:
            interval = 2
        time.sleep(interval * 60)


# ---------------- настройки ----------------
@app.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    tab = request.args.get("tab", "users")
    if request.method == "POST":
        f = request.form
        a = f.get("action")
        if a == "user_save":
            uid = f.get("id")
            vals = (f["name"].strip(), f["login"].strip(), f.get("email", "").strip(), f.get("role", "engineer"),
                    f.get("tracker_login", "").strip(), 1 if f.get("active") else 0)
            if uid:
                g.con.execute("UPDATE users SET name=?, login=?, email=?, role=?, tracker_login=?, active=? WHERE id=?",
                              vals + (uid,))
            else:
                cur = g.con.execute("INSERT INTO users(name, login, email, role, tracker_login, active) VALUES (?,?,?,?,?,?)", vals)
                uid = cur.lastrowid
            if f.get("password"):
                g.con.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(f["password"]), uid))
        elif a == "statuses":
            for st in db.statuses(g.con):
                n = st["name"]
                g.con.execute("UPDATE statuses SET notify=?, tracker_status=?, sla_days=?, grp=? WHERE name=?",
                              (1 if f.get(f"notify_{n}") else 0, f.get(f"tracker_{n}", "").strip(),
                               int(f.get(f"sla_{n}") or 0), f.get(f"grp_{n}", st["grp"]), n))
            if f.get("new_status"):
                g.con.execute("INSERT OR IGNORE INTO statuses(name, grp, sort) VALUES (?,?,?)",
                              (f["new_status"].strip(), f.get("new_grp", "work"), 100))
        elif a == "template":
            g.con.execute("INSERT OR REPLACE INTO templates VALUES (?,?,?)", (f["status"], f["subject"], f["body"]))
        elif a == "template_del":
            g.con.execute("DELETE FROM templates WHERE status=?", (f["status"],))
        elif a == "lists":
            kind = f["kind"]
            g.con.execute("DELETE FROM lists WHERE kind=?", (kind,))
            for v in f["values"].splitlines():
                if v.strip():
                    g.con.execute("INSERT OR IGNORE INTO lists VALUES (?,?)", (kind, v.strip()))
        elif a == "integrations":
            for k, v in db.DEFAULT_SETTINGS.items():
                if k in ("tracker_last_sync",):
                    continue
                if isinstance(v, bool):
                    db.set_setting(k, bool(f.get(k)), g.con)
                elif k in f:
                    val = f[k]
                    if k.endswith("password") or k == "tracker_token":
                        if not val:
                            continue  # пустое поле = не менять
                    if isinstance(v, int):
                        val = int(val or 0)
                    elif isinstance(v, dict):
                        try:
                            val = json.loads(val or "{}")
                        except ValueError:
                            flash("Соответствие полей: неверный JSON", "error")
                            continue
                    db.set_setting(k, val, g.con)
        elif a == "import":
            fs = request.files.get("file")
            if fs and fs.filename:
                path = os.path.join(config.BASE_DIR, "data", "upload_import.xlsx")
                fs.save(path)
                msgs = []
                n = xlsx_io.import_registry(path, log=msgs.append)
                flash(f"Импортировано {n}: " + "; ".join(msgs), "ok")
        g.con.commit()
        flash("Сохранено", "ok")
        return redirect(url_for("settings", tab=tab))
    s = db.get_settings(g.con)
    return render_template("settings.html", tab=tab, users=db.users(g.con, active_only=False),
                           statuses=db.statuses(g.con), templates={r["status"]: dict(r) for r in g.con.execute("SELECT * FROM templates")},
                           lists=db.lists(g.con), s=s, list_kinds={f[3]: f[1] for f in refs.FIELDS if f[3]},
                           field_names=[(f[0], f[1]) for f in refs.FIELDS])


@app.route("/settings/tracker-test", methods=["POST"])
@admin_required
def tracker_test():
    import tracker
    try:
        cl = tracker.Client.from_settings(db.get_settings(g.con))
        me = cl.myself()
        sts = cl.statuses()
        return jsonify(ok=True, me=me.get("display"), statuses=[{"key": x["key"], "name": x.get("display")} for x in sts])
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/settings/mail-test", methods=["POST"])
@admin_required
def mail_test():
    import mailer
    try:
        mailer.send(request.form.get("to") or g.user.get("email"), "Проверка CRM рекламаций", "Почта настроена верно.")
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.route("/me", methods=["GET", "POST"])
@login_required
def me():
    if request.method == "POST":
        if request.form.get("password"):
            g.con.execute("UPDATE users SET password_hash=? WHERE id=?",
                          (generate_password_hash(request.form["password"]), g.user["id"]))
        g.con.execute("UPDATE users SET email=? WHERE id=?", (request.form.get("email", ""), g.user["id"]))
        g.con.commit()
        flash("Сохранено", "ok")
        return redirect(url_for("me"))
    return render_template("me.html")


@app.errorhandler(403)
def forbidden(e):
    return render_template("message.html", text="Недостаточно прав"), 403


if __name__ == "__main__":
    if os.environ.get("CRM_WORKER", "1") == "1":
        threading.Thread(target=worker_loop, daemon=True).start()
    try:
        from waitress import serve
        log.info("waitress на %s:%s", config.HOST, config.PORT)
        serve(app, host=config.HOST, port=config.PORT, threads=8)
    except ImportError:
        app.run(host=config.HOST, port=config.PORT, threaded=True)
