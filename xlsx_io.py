"""Импорт существующего реестра (Google-таблица, выгруженная в .xlsx) и выгрузка в том же формате."""
import datetime as dt
import io
import re
import sys

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import db
import refs
import services

# Колонки реестра в исходном порядке (для выгрузки 1-в-1)
REGISTRY_HEADERS = [
    "номер п/п", "месяц рекламации", "Дата поступления рекламации", "Дата рекламации", "№ вх. МЭЛ",
    "Наименование компании", "Адрес объекта (город, ул., дом, подъезд)", "Принадлежность \n\nГК ПИК/Сторонние",
    "Заводской номер ЛО", "Кол-во лифтов в заявке", "Дата реализации", "Дата изготовления", "Условия гарантиии",
    "Дата ввода в эксплуатацию", "ФИО заявителя", "Контактные данные принимающего\n(ФИО, телефон, e-mail)", "Узел",
    "Наименование узлов", "Поставщик", "Кол-во узлов",
    "Описание дефектов, вышедшего из строя оборудования, неисправности (заказчик)",
    "Планируемые действия по решению причины рекламации", "Заявленная неисправность ",
    "Заготовительное производство / Цех №9 / ОГКЛО / ОКК / Поставщик / Обслуга / Монтажники",
    "Категория рекламации ", "Причина рекламации", "Мероприятия по исправлению",
    "Причина обращения (подтвержденная заводом)", "Для заказчика", "Комфорт пассажира", "Номер партии ",
    "Ссылка на сопроводительные документы  ", "Ссылка на акт устранения", "Дата отгрузки оборуд-я по рекламации",
    "Дата закрытия рекламации", "Отметка о выполнении (№ исх.)", "Комментарии", "Lead time", "Характер проблемы",
    "Резюме",
]
EXTRA_HEADERS = ["Номер CRM", "Задача Трекера", "Ответственный", "Срок", "№ исх."]

MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
          "Ноябрь", "Декабрь"]

norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _val(v, typ):
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date)):
        return v.strftime("%Y-%m-%d") if typ == "date" else MONTHS[v.month - 1] + f" {v.year}"
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return "" if s in ("-//-", "-", "None") else s


def _is_date(s):
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""))


def import_registry(path, sheets=None, log=print):
    db.init()
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    col_by_header = {norm(f[5]): f for f in refs.FIELDS if f[5]}
    con = db.connect()
    statuses = db.status_map(con)
    total = 0
    # --- справочники ---
    if "данные для списков" in wb.sheetnames:
        ws = wb["данные для списков"]
        cols = {}
        for r in ws.iter_rows(values_only=True):
            for i, v in enumerate(r):
                if v not in (None, ""):
                    cols.setdefault(i, []).append(str(v).strip())
        c1 = cols.get(1, [])
        if "Компания" in c1:
            for v in c1[c1.index("Компания") + 1:]:
                con.execute("INSERT OR IGNORE INTO lists VALUES ('company', ?)", (v,))
        c9 = cols.get(9, [])
        stop = c9.index("Ответственные") if "Ответственные" in c9 else len(c9)
        for v in c9[:stop]:
            con.execute("INSERT OR IGNORE INTO lists VALUES ('node', ?)", (v,))
        for name in c9[stop + 1:]:
            login = re.sub(r"[^a-zа-я0-9]", "", name.lower())[:20] or name
            con.execute("INSERT OR IGNORE INTO users(name, login, role) VALUES (?,?,?)", (name, login, "engineer"))
        con.commit()
    # --- рекламации ---
    for ws in wb.worksheets:
        if not ws.title.startswith("Рекламации") or (sheets and ws.title not in sheets):
            continue
        rows = ws.iter_rows(values_only=True)
        header, hdr_i = None, 0
        for r in rows:
            hdr_i += 1
            if r and any(norm(x) == "наименование компании" for x in r):
                header = r
                break
            if hdr_i > 5:
                break
        if not header:
            continue
        idx, status_i, month_i, lead_i = {}, None, None, None
        for i, h in enumerate(header):
            n = norm(h)
            if n in col_by_header and col_by_header[n][0] not in idx:
                idx[col_by_header[n][0]] = i
            if n.startswith("отметка о выполнении"):
                status_i = i
            if n == "месяц рекламации":
                month_i = i
        types = {f[0]: f[2] for f in refs.FIELDS}
        n_sheet = 0
        for r in rows:
            if not r or len(r) <= idx.get("company", 0) or not r[idx["company"]]:
                continue
            d = {k: _val(r[i], types[k]) for k, i in idx.items() if i < len(r)}
            extra = []
            for k in ("received_date", "claim_date", "ship_date", "close_date"):
                if d.get(k) and not _is_date(d[k]):
                    extra.append(f"{dict((f[0], f[1]) for f in refs.FIELDS)[k]}: {d[k]}")
                    d[k] = ""
            if not d.get("received_date"):
                m = r[month_i] if month_i is not None else None
                d["received_date"] = m.strftime("%Y-%m-%d") if isinstance(m, (dt.datetime, dt.date)) else ""
            if not d.get("received_date"):
                continue
            if extra:
                d["comments"] = "; ".join(filter(None, [d.get("comments", "")] + extra))
            st_raw = _val(r[status_i], "text") if status_i is not None and status_i < len(r) else ""
            status = st_raw if st_raw in statuses else ("Выполнено" if d.get("resume") else "В процессе")
            if st_raw and st_raw not in statuses:
                d["out_no"] = st_raw
            tk = re.search(r"MELBSSL-\d+", d.get("incoming_no", ""))
            year = int(d["received_date"][:4])
            number = services.next_number(con, year)
            cols = [k for k in d if d[k] != ""]
            closed = statuses[status]["grp"] in ("closed", "rejected")
            con.execute(
                f"INSERT INTO claims(number, year, status, status_changed_at, source, tracker_key, deadline"
                f"{''.join(', ' + c for c in cols)}) VALUES (?,?,?,?,?,?,?{', ?' * len(cols)})",
                [number, year, status, d.get("close_date") or d["received_date"], "import",
                 tk.group(0) if tk else None,
                 (dt.date.fromisoformat(d["received_date"]) + dt.timedelta(days=60 if closed else 14)).isoformat()]
                + [d[c] for c in cols])
            services.remember_values(con, d)
            n_sheet += 1
            if n_sheet % 500 == 0:
                con.commit()
        con.commit()
        log(f"Лист «{ws.title}»: загружено {n_sheet}")
        total += n_sheet
    con.execute("INSERT INTO history(claim_id, kind, text, user_name) SELECT id, 'created', 'Импорт из реестра', "
                "'импорт' FROM claims WHERE source='import' AND id NOT IN (SELECT claim_id FROM history)")
    con.commit()
    con.close()
    return total


def export_registry(rows):
    """rows — список словарей рекламаций (как из services.get_claim). Возвращает bytes .xlsx."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Рекламации"
    by_header = {f[5]: f[0] for f in refs.FIELDS if f[5]}
    headers = REGISTRY_HEADERS + EXTRA_HEADERS
    ws.append(headers)
    for i, r in enumerate(rows, 1):
        try:
            rd = dt.date.fromisoformat((r.get("received_date") or "")[:10])
        except ValueError:
            rd = None
        lead = ""
        if rd and r.get("close_date"):
            try:
                lead = (dt.date.fromisoformat(r["close_date"][:10]) - rd).days
            except ValueError:
                pass
        line = []
        for h in REGISTRY_HEADERS:
            if h == "номер п/п":
                line.append(i)
            elif h == "месяц рекламации":
                line.append(f"{MONTHS[rd.month - 1]} {rd.year}" if rd else "")
            elif h == "Отметка о выполнении (№ исх.)":
                line.append(r.get("status"))
            elif h == "Lead time":
                line.append(lead)
            else:
                v = r.get(by_header.get(h, ""), "") or ""
                if v and h.startswith(("Дата поступления", "Дата рекламации", "Дата отгрузки", "Дата закрытия")):
                    try:
                        v = dt.date.fromisoformat(v[:10])
                    except ValueError:
                        pass
                elif v and h in ("Кол-во лифтов в заявке", "Кол-во узлов") and str(v).isdigit():
                    v = int(v)
                line.append(v)
        line += [r.get("number"), r.get("tracker_key"), r.get("responsible"), r.get("deadline"), r.get("out_no")]
        ws.append(line)
    head_fill = PatternFill("solid", fgColor="1F3A5F")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[1].height = 60
    widths = {"Адрес": 40, "Описание": 50, "Наименование компании": 28, "Условия": 30}
    for i, h in enumerate(headers, 1):
        w = next((v for k, v in widths.items() if h.startswith(k)), 14)
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if isinstance(c.value, dt.date):
                c.number_format = "DD.MM.YYYY"
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python xlsx_io.py путь_к_реестру.xlsx")
        sys.exit(1)
    n = import_registry(sys.argv[1])
    print(f"Готово, всего {n} рекламаций")
