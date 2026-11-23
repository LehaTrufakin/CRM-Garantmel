// Небольшой JS без зависимостей: распознавание письма, карточка, диалоги.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function postJSON(url, data) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  return r.json();
}

// ---------- новая рекламация ----------
function initNewForm() {
  const form = $('#nf'), raw = $('#raw'), res = $('#parseRes');
  const touched = new Set();
  $$('input,textarea,select', form).forEach(el => el.addEventListener('input', () => touched.add(el.name)));

  async function parse() {
    const text = raw.value.trim();
    if (!text) return;
    const d = await postJSON('/api/parse', { text });
    let n = 0;
    for (const [k, v] of Object.entries(d)) {
      const el = form.elements[k];
      if (!el || !v || touched.has(k) || el === raw) continue;
      if (el.value && k !== 'description') continue;
      el.value = v; n++;
      el.classList.add('filled'); setTimeout(() => el.classList.remove('filled'), 2500);
    }
    res.textContent = n ? `Заполнено полей: ${n}. Проверьте подсвеченные.` : 'Новых данных не найдено';
    lookupSerial();
  }
  raw.addEventListener('paste', () => setTimeout(parse, 50));
  raw.addEventListener('change', parse);
  $('#parseBtn').addEventListener('click', () => { touched.clear(); parse(); });

  const sn = $('#serials'), box = $('#serialHist');
  async function lookupSerial() {
    const first = (sn.value.match(/\d{6,}/) || [])[0];
    if (!first) { box.innerHTML = ''; return; }
    const d = await (await fetch('/api/lookup?serial=' + first)).json();
    const h = d.history || [];
    if (!h.length) { box.innerHTML = '<span class="muted">по этому лифту рекламаций не было</span>'; return; }
    const last = h[0];
    box.innerHTML = `<b>Ранее рекламаций: ${h.length}</b> · последняя <a href="/claim/${last.id}" target="_blank">${last.number}</a> (${last.node || '—'}, ${last.status}) `
      + `<button type="button" class="btn sm" id="fillFromSn">подставить данные объекта</button>`;
    $('#fillFromSn').onclick = () => {
      for (const k of ['company', 'address', 'ownership']) {
        const el = form.elements[k];
        if (el && !el.value && last[k]) { el.value = last[k]; el.classList.add('filled'); }
      }
    };
  }
  sn.addEventListener('change', lookupSerial);
  sn.addEventListener('blur', lookupSerial);
}

// ---------- карточка ----------
function initClaim(id) {
  // вкладки
  $$('.tabs button').forEach(b => b.addEventListener('click', () => {
    $$('.tabs button').forEach(x => x.classList.toggle('on', x === b));
    $$('.tabpane').forEach(p => p.classList.toggle('on', p.id === 'tab-' + b.dataset.tab));
    history.replaceState(null, '', '#' + b.dataset.tab);
  }));
  const h = location.hash.slice(1);
  const tb = h && $(`.tabs button[data-tab="${h}"]`);
  if (tb) tb.click();

  // несохранённые изменения
  const form = $('#cf'), bar = $('#savebar'), st = $('#saveSt');
  let dirty = false;
  if (form && bar) {
    form.addEventListener('input', e => {
      dirty = true; bar.classList.add('dirty'); st.textContent = 'Есть несохранённые изменения';
      const pane = e.target.closest('.tabpane');
      if (pane) { const btn = $(`.tabs button[data-tab="${pane.id.slice(4)}"]`); if (btn && !btn.querySelector('.dot')) btn.insertAdjacentHTML('beforeend', '<span class="dot"></span>'); }
    });
    form.addEventListener('submit', () => { dirty = false; });
    window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
    document.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); dirty = false; form.requestSubmit(); }
    });
  }

  // ответственный — сохраняется сразу
  const resp = $('#resp');
  if (resp) resp.addEventListener('change', async () => {
    const fd = new FormData(); fd.append('responsible_id', resp.value);
    await fetch(`/claim/${id}/assign`, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } });
    location.reload();
  });

  window.takeMe = (uid) => { resp.value = uid; resp.dispatchEvent(new Event('change')); };

  // смена статуса: показываем письмо клиенту до отправки
  window.askStatus = async (status) => {
    if (dirty && confirm('Сначала сохранить изменения в карточке?')) {
      const fd = new FormData(form);
      await fetch(form.action, { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' } });
      dirty = false;
    }
    const d = await (await fetch(`/claim/${id}/preview?status=` + encodeURIComponent(status))).json();
    $('#stName').textContent = status;
    $('#stInput').value = status;
    $('#stComment').value = '';
    $('#mTo').value = d.email; $('#mSubj').value = d.subject; $('#mBody').value = d.body;
    $('#stSend').checked = d.notify && !!d.email;
    toggleMail();
    $('#stDlg').showModal();
    $('#stComment').focus();
  };
  const send = $('#stSend');
  function toggleMail() { $('#mailBox').style.display = send.checked ? '' : 'none'; }
  if (send) send.addEventListener('change', () => {
    if (send.checked && !$('#mBody').value) $('#mBody').value = 'Здравствуйте!\n\n';
    toggleMail();
  });
  // комментарий подставляем в текст письма вместо пустой строки {comment}
  const cm = $('#stComment');
  if (cm) {
    let prev = '';
    cm.addEventListener('input', () => {
      const body = $('#mBody');
      if (prev && body.value.includes(prev)) body.value = body.value.replace(prev, cm.value);
      else if (!prev && cm.value) body.value = body.value.replace(/\n\n(--\n)/, '\n' + cm.value + '\n\n$1');
      prev = cm.value;
    });
  }
}

// ---------- настройки ----------
async function testTracker() {
  const out = $('#ttRes'); out.textContent = 'Проверяю…';
  const d = await (await fetch('/settings/tracker-test', { method: 'POST' })).json();
  out.innerHTML = d.ok
    ? `<span style="color:var(--ok)">Подключено как ${d.me}.</span> Ключи статусов: ` + d.statuses.map(s => `<code>${s.key}</code> — ${s.name}`).join(', ')
    : `<span style="color:var(--bad)">Ошибка: ${d.error}</span> (сначала сохраните настройки)`;
}
async function testMail() {
  const out = $('#mtRes'); out.textContent = 'Отправляю…';
  const fd = new FormData(); fd.append('to', $('#mtTo').value);
  const d = await (await fetch('/settings/mail-test', { method: 'POST', body: fd })).json();
  out.innerHTML = d.ok ? '<span style="color:var(--ok)">Отправлено</span>' : `<span style="color:var(--bad)">${d.error}</span>`;
}
