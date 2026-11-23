"""Интеграция с Яндекс Трекером (API v2).

push: при регистрации создаётся задача в очереди (по умолчанию MELBSSL), смена статуса в CRM
      выполняет соответствующий переход в Трекере, ответственный и комментарии тоже передаются.
pull: раз в несколько минут забираются задачи, изменённые в Трекере, — статус и исполнитель
      переносятся в CRM; при включённой опции новые задачи из Трекера регистрируются в CRM
      (удобно на переходный период, пока письма ещё падают в Трекер).
"""
import datetime as dt
import logging

import requests

import db
import refs
import services

log = logging.getLogger("crm.tracker")
API = "https://api.tracker.yandex.net/v2"


class Client:
    def __init__(self, token, org_id, cloud=False, queue="MELBSSL"):
        self.queue = queue
        self.s = requests.Session()
        auth = token if token.lower().startswith(("oauth ", "bearer ")) else f"OAuth {token}"
        self.s.headers.update({"Authorization": auth,
                               ("X-Cloud-Org-ID" if cloud else "X-Org-ID"): str(org_id),
                               "Content-Type": "application/json"})

    @classmethod
    def from_settings(cls, s):
        return cls(s["tracker_token"], s["tracker_org_id"], s.get("tracker_cloud_org"), s.get("tracker_queue") or "MELBSSL")

    def _r(self, method, path, **kw):
        r = self.s.request(method, API + path, timeout=30, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    # --- базовые вызовы ---
    def myself(self):
        return self._r("GET", "/myself")

    def statuses(self):
        return self._r("GET", "/statuses")

    def users(self):
        return self._r("GET", "/users?perPage=500")

    def get(self, key):
        return self._r("GET", f"/issues/{key}")

    def create(self, fields):
        return self._r("POST", "/issues/", json=dict(queue=self.queue, **fields))

    def update(self, key, fields):
        return self._r("PATCH", f"/issues/{key}", json=fields)

    def add_comment(self, key, text):
        return self._r("POST", f"/issues/{key}/comments", json={"text": text})

    def transitions(self, key):
        return self._r("GET", f"/issues/{key}/transitions")

    def execute(self, key, transition_id, comment=None):
        body = {"comment": comment} if comment else {}
        return self._r("POST", f"/issues/{key}/transitions/{transition_id}/_execute", json=body)

    def search(self, query, per_page=100, max_pages=20):
        out = []
        for page in range(1, max_pages + 1):
            r = self.s.post(f"{API}/issues/_search?perPage={per_page}&page={page}", json={"query": query}, timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {r.text[:300]}")
            items = r.json()
            out += items
            if len(items) < per_page:
                break
        return out

    def move_to_status(self, key, status_key, comment=None):
        """Ищем переход, ведущий в нужный статус. Если прямого нет — сообщаем."""
        cur = self.get(key)["status"]["key"]
        if cur == status_key:
            return f"уже в статусе {status_key}"
        for t in self.transitions(key):
            if t.get("to", {}).get("key") == status_key:
                self.execute(key, t["id"], comment)
                return f"переход «{t.get('display') or t['id']}» → {status_key}"
        raise RuntimeError(f"нет перехода из «{cur}» в «{status_key}» — выполните его в Трекере вручную")


# ---------------- CRM → Трекер ----------------
def _summary(c):
    parts = [c.get("company"), c.get("node"), (c.get("description") or "").split("\n")[0][:80]]
    return f"{c['number']} " + " · ".join(p for p in parts if p)


def _description(c):
    labels = [(f[0], f[1]) for f in refs.FIELDS if f[4] in ("main", "equip")]
    lines = [f"**{lbl}:** {c.get(k)}" for k, lbl in labels if c.get(k) and k != "description"]
    return "\n".join(lines) + "\n\n" + (c.get("description") or "") + f"\n\n_Рекламация в CRM: {c['number']}_"


def _assignee(con, c):
    if not c.get("responsible_id"):
        return None
    r = con.execute("SELECT tracker_login FROM users WHERE id=?", (c["responsible_id"],)).fetchone()
    return r["tracker_login"] if r and r["tracker_login"] else None


def push_claim(con, c, what):
    s = db.get_settings(con)
    cl = Client.from_settings(s)
    fmap = s.get("tracker_field_map") or {}
    if not c.get("tracker_key"):
        fields = {"summary": _summary(c), "description": _description(c)}
        login = _assignee(con, c)
        if login:
            fields["assignee"] = login
        for local, remote in fmap.items():
            if c.get(local):
                fields[remote] = c[local]
        issue = cl.create(fields)
        con.execute("UPDATE claims SET tracker_key=? WHERE id=?", (issue["key"], c["id"]))
        c["tracker_key"] = issue["key"]
        msg = f"Создана задача {issue['key']}"
        if what == "create":
            return msg
    else:
        msg = ""
    key = c["tracker_key"]
    if what == "status":
        target = db.status_map(con).get(c["status"], {}).get("tracker_status")
        if target:
            res = cl.move_to_status(key, target, comment=f"CRM: статус «{c['status']}»")
            return (msg + "; " if msg else "") + f"{key}: {res}"
    if what == "assign":
        login = _assignee(con, c)
        cl.update(key, {"assignee": login} if login else {"assignee": None})
        return (msg + "; " if msg else "") + f"{key}: исполнитель {login or '—'}"
    if what == "fields":
        fields = {"summary": _summary(c)}
        for local, remote in fmap.items():
            fields[remote] = c.get(local) or None
        cl.update(key, fields)
    return msg


# ---------------- Трекер → CRM ----------------
def pull(con):
    s = db.get_settings(con)
    if not (s.get("tracker_enabled") and s.get("tracker_pull") and s.get("tracker_token")):
        return {"skipped": True}
    cl = Client.from_settings(s)
    since = s.get("tracker_last_sync") or (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    issues = cl.search(f'Queue: {cl.queue} Updated: >= "{since}" "Sort by": Updated ASC')
    smap = db.status_map(con)
    # статус Трекера → статус CRM (первый подходящий по порядку)
    reverse = {}
    for name, st in smap.items():
        if st.get("tracker_status"):
            reverse.setdefault(st["tracker_status"], name)
    logins = {u["tracker_login"]: u["id"] for u in db.users(con) if u.get("tracker_login")}
    stats = {"seen": len(issues), "created": 0, "status": 0, "assign": 0}
    for it in issues:
        key = it["key"]
        row = con.execute("SELECT id, status, responsible_id FROM claims WHERE tracker_key=?", (key,)).fetchone()
        if not row:
            if not s.get("tracker_pull_new"):
                continue
            created = (it.get("createdAt") or "")[:10] or dt.date.today().isoformat()
            data = {"description": ((it.get("summary") or "") + "\n" + (it.get("description") or "")).strip()[:3000],
                    "received_date": created, "claim_date": created, "incoming_no": key}
            ce = it.get("createdBy", {}).get("display")
            if ce:
                data["applicant"] = ce
            cid = services.create_claim(con, data, user="Трекер", source="tracker", notify=False)
            con.execute("UPDATE claims SET tracker_key=? WHERE id=?", (key, cid))
            con.commit()
            stats["created"] += 1
            row = con.execute("SELECT id, status, responsible_id FROM claims WHERE id=?", (cid,)).fetchone()
        tst = (it.get("status") or {}).get("key")
        local_tst = smap.get(row["status"], {}).get("tracker_status")
        if tst and tst != local_tst and tst in reverse:
            services.change_status(con, row["id"], reverse[tst], "Трекер",
                                   f"изменено в Трекере ({(it.get('status') or {}).get('display', tst)})",
                                   send_mail=False, from_tracker=True)
            stats["status"] += 1
        a = it.get("assignee") or {}
        login = a.get("login") or a.get("id") or a.get("display")
        uid = next((logins[x] for x in (a.get("login"), a.get("id"), a.get("display")) if x in logins), None) \
            or next((u["id"] for u in db.users(con) if a.get("display") and u["name"] == a.get("display")), None)
        if uid and uid != row["responsible_id"]:
            con.execute("UPDATE claims SET responsible_id=? WHERE id=?", (uid, row["id"]))
            services.history(con, row["id"], "assign", f"Исполнитель из Трекера: {login}", "Трекер")
            con.commit()
            stats["assign"] += 1
    db.set_setting("tracker_last_sync", started, con)
    return stats
