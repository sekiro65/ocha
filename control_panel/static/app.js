// OCHA control panel. Behaviour talks to the kit only through 'lg-change'.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const announce = t => { $('announce').textContent = t; };
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function api(path, data) {
  const opts = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  // A request that never answers must end in an error state, not an endless skeleton.
  const r = await fetch(path, { credentials: 'same-origin', signal: AbortSignal.timeout(20000), ...opts })
    .catch(e => { throw new Error(e.name === 'TimeoutError' ? 'The server took too long to answer.' : 'Can\'t reach OCHA.'); });
  const json = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== '/api/login') { showLogin(); throw new Error('Sign in first'); }
  if (r.status === 403 && json.must_change) { showChange(); throw new Error(json.error); }
  if (!r.ok) throw new Error(json.error || `Request failed (${r.status})`);
  return json;
}

function setMsg(el, text, isErr) { el.textContent = text; el.classList.toggle('err', !!isErr); }

// ---------------------------------------------------------------- loading kit
// One set of loading parts for every screen (playbook: show nothing for the first
// 300 ms, then keep an indicator at least 400 ms so it never flickers).
const SHOW_AFTER = 300, MIN_SHOW = 400;

async function settle(started) {
  const elapsed = performance.now() - started;
  if (elapsed > SHOW_AFTER && elapsed < SHOW_AFTER + MIN_SHOW) await sleep(SHOW_AFTER + MIN_SHOW - elapsed);
}

// Skeletons match the real rows/fields; CSS keeps them invisible for 300 ms.
const skel = {
  rows: (n, pill = true) => Array.from({ length: n }, () =>
    `<div class="row"><div class="grow"><span class="sk sk-line"></span><span class="sk sk-line short"></span></div>${pill ? '<span class="sk sk-pill"></span>' : ''}</div>`).join(''),
  fields: n => Array.from({ length: n }, () => '<div class="field"><span class="sk sk-label"></span><span class="sk sk-field"></span></div>').join(''),
  log: n => Array.from({ length: n }, (_, i) => `<span class="sk sk-log" style="width:${[92, 74, 86, 60, 80][i % 5]}%"></span>`).join(''),
};

const errorState = msg =>
  `<div class="error-state"><p>Couldn't load. ${esc(msg)}</p><button class="lg-button lg-button--quiet" type="button" data-retry>Try again</button></div>`;

// Thin bar at the top while a page loads.
const progress = {
  n: 0, t0: 0, timer: null,
  start() {
    if (this.n++ === 0) {
      this.t0 = performance.now();
      this.timer = setTimeout(() => { $('progress').hidden = false; }, SHOW_AFTER);
    }
  },
  async done() {
    this.n = Math.max(0, this.n - 1);
    if (this.n) return;
    clearTimeout(this.timer);
    await settle(this.t0);
    if (!this.n) $('progress').hidden = true;
  },
};

// Buttons show their own pending state, keep their width, and can't double-submit.
async function busy(btn, label, fn) {
  if (btn.getAttribute('aria-busy') === 'true') return;
  const html = btn.innerHTML;
  btn.style.width = `${btn.offsetWidth}px`;   // lock the width while busy
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  const t0 = performance.now();
  const timer = setTimeout(() => { btn.innerHTML = `<span class="spin" aria-hidden="true"></span><span>${esc(label)}</span>`; }, SHOW_AFTER);
  try {
    return await fn();
  } finally {
    clearTimeout(timer);
    await settle(t0);
    btn.innerHTML = html;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    btn.style.width = '';
  }
}

const bootStart = performance.now();
async function hideSplash() {
  const s = $('splash');
  if (!s) return;
  await settle(bootStart);
  s.classList.add('gone');
  setTimeout(() => s.remove(), 300);
}

function setLive(ok) {
  const l = $('live');
  l.hidden = false;
  l.classList.toggle('off', !ok);
  l.textContent = ok ? 'Live' : 'Reconnecting…';
}

// ---------------------------------------------------------------- formatting
const units = ['B', 'KB', 'MB', 'GB', 'TB'];
function bytes(n) { let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return `${n.toFixed(n < 10 && i ? 1 : 0)} ${units[i]}`; }
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
const stateClass = s => /RUNNING|done/i.test(s) ? 'ok' : /FATAL|BACKOFF|error|not_found/i.test(s) ? 'bad' : /STARTING|working|queued|sent/i.test(s) ? 'busy' : '';
const stateWord = s => ({ RUNNING: 'Running', STOPPED: 'Stopped', EXITED: 'Stopped', FATAL: 'Failed', BACKOFF: 'Restarting',
  STARTING: 'Starting', unknown: 'Unavailable', done: 'Done', error: 'Failed', not_found: 'Not found', working: 'Working',
  queued: 'Queued', sent: 'Waiting' }[s] || s);
const state = s => `<span class="state ${stateClass(s)}">${esc(stateWord(s))}</span>`;
const SERVICES = { music_sorter: 'Music sorter', explo: 'Explo', scheduler: 'Playlist schedule', telegram_bot: 'Telegram' };
const SOURCES = { youtube: 'YouTube', slskd: 'Soulseek', telegram: 'Telegram' };

// ---------------------------------------------------------------- auth
let session = {};

function showAuth(form) {
  stopPolling();
  $('app').hidden = true;
  $('login').hidden = false;
  $('loginForm').hidden = form !== 'login';
  $('changeForm').hidden = form !== 'change';
  $(form === 'login' ? 'loginPw' : 'changePw').focus();
}
const showLogin = () => showAuth('login');
const showChange = () => showAuth('change');

function showApp() {
  $('login').hidden = true;
  $('app').hidden = false;
  go(0);
}

const enter = () => (session.must_change ? showChange() : showApp());

$('loginForm').addEventListener('submit', e => {
  e.preventDefault();
  busy($('loginBtn'), 'Signing in…', async () => {
    try {
      await api('/api/login', { password: $('loginPw').value });
      $('loginPw').value = '';
      setMsg($('loginMsg'), '');
      session = await api('/api/session');
      enter();
    } catch (err) { setMsg($('loginMsg'), err.message, true); }
  });
});

$('changeForm').addEventListener('submit', e => {
  e.preventDefault();
  if ($('changePw').value !== $('changePw2').value) { setMsg($('changeMsg'), 'The two passwords don\'t match.', true); return; }
  busy($('changeBtn'), 'Saving…', async () => {
    try {
      await api('/api/password', { new: $('changePw').value });
      $('changePw').value = $('changePw2').value = '';
      setMsg($('changeMsg'), '');
      session = await api('/api/session');
      enter();
    } catch (err) { setMsg($('changeMsg'), err.message, true); }
  });
});

for (const btn of [$('signOut'), ...document.querySelectorAll('[data-signout]')]) {
  btn.addEventListener('click', e => busy(e.currentTarget, 'Signing out…', async () => {
    await api('/api/logout', {});
    session.authenticated = false;
    loaded.clear();
    showLogin();
  }));
}

// ---------------------------------------------------------------- navigation
const titles = ['Activity', 'Request', 'Explo', 'Logs', 'Settings'];
const pages = [...document.querySelectorAll('.page')];
const loaded = new Set();   // pages whose first load is done (later visits skip skeletons)
let current = 0;

function go(index, force = false) {
  current = index;
  closeMenu();
  stopPolling();
  pages.forEach((p, i) => p.classList.toggle('active', i === index));
  $('title').textContent = titles[index];
  $('live').hidden = true;
  document.querySelector('.main').scrollTop = 0;
  if (force) loaded.delete(index);
  [loadActivity, loadRequests, openExplo, loadLogs, loadSettingsPage][index]().catch(() => {});
}
$('nav').addEventListener('lg-change', e => { go(e.detail.index); $('title').focus({ preventScroll: true }); });
document.addEventListener('click', e => {
  if (e.target.closest('[data-retry]')) go(current, true);
  const goto = e.target.closest('[data-goto]');
  if (goto) $('nav').lg.select(+goto.dataset.goto);
});

// Refresh the visible page every few seconds; first loads are handled per page.
let timer = null;
function poll(fn, ms) {
  stopPolling();
  const tick = async () => {
    try { await fn(); setLive(true); } catch { setLive(false); }
    if (timer !== null) timer = setTimeout(tick, ms);
  };
  timer = setTimeout(tick, ms);
}
function stopPolling() { if (timer) clearTimeout(timer); timer = null; }
document.addEventListener('visibilitychange', () => {
  if ($('app').hidden) return;
  if (document.hidden) stopPolling(); else go(current);
});

// A page's first visit: top bar + skeletons in its regions, then the content, or an
// error with "Try again" in each region. Later visits just refresh in place.
// `refresh` (optional) keeps the page live by polling.
async function firstLoad(index, regions, task, render, refresh, ms = 3000) {
  if (loaded.has(index)) {
    if (refresh) {
      await refresh().then(() => setLive(true), () => setLive(false));
      poll(refresh, ms);
    }
    return;
  }
  progress.start();
  const t0 = performance.now();
  regions.forEach(([el, sk]) => { el.setAttribute('aria-busy', 'true'); el.innerHTML = sk; });
  try {
    const data = await task();
    await settle(t0);
    render(data);
    loaded.add(index);
    if (refresh) { setLive(true); poll(refresh, ms); }
  } catch (err) {
    await settle(t0);
    regions.forEach(([el]) => { el.innerHTML = errorState(err.message); });
    if (refresh) setLive(false);
    throw err;
  } finally {
    regions.forEach(([el]) => el.removeAttribute('aria-busy'));
    progress.done();
  }
}

// ---------------------------------------------------------------- activity
const lastSize = new Map();
let lastStatus = null;

function renderActivity(s) {
  lastStatus = s;
  $('stSource').textContent = SOURCES[s.downloader] || s.downloader;
  $('stLibrary').textContent = s.library == null ? '–' : s.library.toLocaleString();
  $('stDisk').textContent = bytes(s.disk.free);
  $('stActive').textContent = s.staging.filter(f => f.partial).length;

  $('staging').innerHTML = s.staging.length ? s.staging.map(f => {
    const prev = lastSize.get(f.name);
    const rate = prev && f.partial ? Math.max(0, (f.size - prev) / 3) : 0;
    lastSize.set(f.name, f.size);
    const what = f.partial ? `Downloading${rate ? ` · ${bytes(rate)}/s` : ''}` : 'Waiting to sort';
    return `<div class="row"><div class="grow"><div>${esc(f.name)}</div><small class="dim">${what}</small></div><span class="num">${bytes(f.size)}</span></div>`;
  }).join('') : '<div class="empty-state"><span>Nothing downloading.</span><button class="lg-button lg-button--quiet" type="button" data-goto="1">Request a song</button></div>';

  const telegram = s.downloader === 'telegram';
  $('services').innerHTML = Object.entries(SERVICES).map(([key, label]) => {
    const st = s.services[key]?.state || 'unknown';
    let hint = '';
    if (key === 'telegram_bot' && st !== 'RUNNING') hint = telegram ? 'Sign in under Settings → Downloads' : 'Not used with this source';
    return `<div class="row"><div class="grow"><div>${label}</div>${hint ? `<small class="dim">${hint}</small>` : ''}</div>${state(st)}
      <button class="lg-button lg-button--quiet" type="button" data-restart="${key}" aria-label="Restart ${label}">Restart</button></div>`;
  }).join('');

  $('jobsCard').hidden = !telegram;
  $('jobs').innerHTML = s.jobs.length ? s.jobs.map(j =>
    `<div class="row"><div class="grow"><div>${esc(j.title)}</div><small class="dim">${esc(j.artist)}</small></div>${state(j.state)}<span class="num dim">${ago(j.at)}</span></div>`
  ).join('') : '<div class="empty-state"><span>No Telegram requests yet.</span></div>';
}

const refreshActivity = async () => renderActivity(await api('/api/status'));

const TILES = ['stSource', 'stLibrary', 'stActive', 'stDisk'];
function loadActivity() {
  if (!loaded.has(0)) TILES.forEach(id => { $(id).innerHTML = '<span class="sk sk-value"></span>'; });
  return firstLoad(0, [[$('staging'), skel.rows(2, false)], [$('services'), skel.rows(4)], [$('jobs'), skel.rows(3)]],
    () => api('/api/status'), renderActivity, refreshActivity)
    .catch(err => { TILES.forEach(id => { $(id).textContent = '–'; }); throw err; });
}

$('services').addEventListener('click', e => {
  const btn = e.target.closest('[data-restart]');
  if (!btn) return;
  busy(btn, 'Restarting…', async () => {
    try {
      await api(`/api/services/${btn.dataset.restart}/restart`, {});
      announce(`${SERVICES[btn.dataset.restart]} restarted`);
    } catch (err) { announce(err.message); }
  }).then(() => refreshActivity().catch(() => {}));
});

// ---------------------------------------------------------------- request
function renderRequests(s) {
  $('requests').innerHTML = s.requests.length ? s.requests.map(r =>
    `<div class="row"><div class="grow"><div>${esc(r.detail || r.query)}</div><small class="dim">${esc(r.result || SOURCES[r.via] || r.via)}</small></div>${state(r.state)}<span class="num dim">${ago(r.at)}</span></div>`
  ).join('') : '<div class="empty-state"><span>No requests yet. Type a song above.</span></div>';
}
const refreshRequests = async () => renderRequests(await api('/api/status'));
const loadRequests = () => firstLoad(1, [[$('requests'), skel.rows(3)]], () => api('/api/status'), renderRequests, refreshRequests);

$('requestForm').addEventListener('submit', e => {
  e.preventDefault();
  busy($('requestBtn'), 'Requesting…', async () => {
    try {
      const job = await api('/api/request', { query: $('requestQuery').value });
      $('requestQuery').value = '';
      setMsg($('requestMsg'), `Requested from ${SOURCES[job.via] || job.via}.`);
      await refreshRequests();
    } catch (err) { setMsg($('requestMsg'), err.message, true); }
  });
});

// ---------------------------------------------------------------- explo
// Explo lives on its own host or port. Sign it in with a one-time ticket sent
// in a POST body (never in the URL), then it keeps its own session cookie.
async function openExplo() {
  if (loaded.has(2)) return;
  progress.start();
  $('exploLoading').hidden = false;
  try {
    const { ticket, action } = await api('/api/explo-ticket', {});
    const frame = $('exploFrame');
    frame.addEventListener('load', () => { $('exploLoading').hidden = true; progress.done(); }, { once: true });
    const form = Object.assign(document.createElement('form'), { method: 'POST', action, target: 'exploFrame', hidden: true });
    form.append(Object.assign(document.createElement('input'), { type: 'hidden', name: 'ticket', value: ticket }));
    document.body.append(form);
    form.submit();
    form.remove();
    loaded.add(2);
  } catch (err) {
    $('exploLoading').innerHTML = errorState(err.message);
    progress.done();
  }
}

// ---------------------------------------------------------------- logs
let logName = 'telegram';

function renderLog({ text }) {
  const pre = $('logText');
  const atBottom = pre.hidden || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  pre.textContent = text || 'Nothing logged yet.';
  pre.hidden = false;
  $('logSkeleton').hidden = true;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}
const fetchLog = () => api(`/api/logs?name=${logName}`);
const refreshLog = async () => renderLog(await fetchLog());

function loadLogs() {
  if (!loaded.has(3)) { $('logText').hidden = true; $('logSkeleton').hidden = false; }
  return firstLoad(3, [[$('logSkeleton'), skel.log(10)]], fetchLog, renderLog, refreshLog);
}
$('logTabs').addEventListener('lg-change', e => {
  logName = e.detail.item.dataset.log;
  $('logText').style.opacity = '.5';           // keep the old text, dimmed, while the new one loads
  progress.start();
  refreshLog().catch(() => {}).finally(() => { $('logText').style.opacity = ''; progress.done(); });
});

// ---------------------------------------------------------------- glass dropdown
// A listbox whose menu is a liquid-glass panel attached to <body> (so it never
// nests inside the card's glass). The chosen value lives in a hidden input.
const menu = Object.assign(document.createElement('div'), { className: 'lg-card select-menu', role: 'listbox', hidden: true, id: 'selectMenu' });
menu.dataset.glass = 'panel';
document.body.append(menu);
let openSelect = null;

function glassSelect(f) {
  const id = `set_${f.key}`;
  const cur = f.options.find(o => o.value === f.value) || f.options[0];
  return `<button type="button" class="lg-input select-trigger" id="${id}" aria-haspopup="listbox" aria-expanded="false" aria-controls="selectMenu" aria-labelledby="${id}_label ${id}">
      <span class="select-value">${esc(cur.label)}</span>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>
    </button><input type="hidden" name="${f.key}" value="${esc(cur.value)}" data-options="${esc(JSON.stringify(f.options))}">`;
}

function openMenu(trigger) {
  closeMenu();
  const input = trigger.nextElementSibling;
  const options = JSON.parse(input.dataset.options);
  menu.innerHTML = options.map((o, i) => `<div role="option" id="opt_${i}" data-value="${esc(o.value)}" aria-selected="${o.value === input.value}">
      <span>${esc(o.label)}</span><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12l5 5L20 7"/></svg></div>`).join('');
  const r = trigger.getBoundingClientRect();
  const below = innerHeight - r.bottom > options.length * 48 + 24;
  Object.assign(menu.style, { left: `${r.left}px`, width: `${r.width}px`,
    top: below ? `${r.bottom + 8}px` : '', bottom: below ? '' : `${innerHeight - r.top + 8}px` });
  menu.hidden = false;
  openSelect = { trigger, input, index: Math.max(0, options.findIndex(o => o.value === input.value)) };
  trigger.setAttribute('aria-expanded', 'true');
  highlight(openSelect.index);
}

function highlight(i) {
  const items = [...menu.children];
  openSelect.index = (i + items.length) % items.length;
  items.forEach((el, j) => el.classList.toggle('active', j === openSelect.index));
  openSelect.trigger.setAttribute('aria-activedescendant', items[openSelect.index].id);
}

function choose(i) {
  const { trigger, input } = openSelect;
  const el = menu.children[i];
  input.value = el.dataset.value;
  trigger.querySelector('.select-value').textContent = el.textContent.trim();
  closeMenu();
  trigger.focus();
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

function closeMenu() {
  if (!openSelect) return;
  openSelect.trigger.setAttribute('aria-expanded', 'false');
  openSelect.trigger.removeAttribute('aria-activedescendant');
  menu.hidden = true;
  openSelect = null;
}

document.addEventListener('click', e => {
  const trigger = e.target.closest('.select-trigger');
  const option = e.target.closest('#selectMenu [role=option]');
  if (option) return choose([...menu.children].indexOf(option));
  if (trigger) return openSelect?.trigger === trigger ? closeMenu() : openMenu(trigger);
  closeMenu();
});
document.addEventListener('keydown', e => {
  const trigger = e.target.closest?.('.select-trigger');
  if (!trigger) return;
  const keys = { ArrowDown: 1, ArrowUp: -1 };
  if (!openSelect) {
    if (e.key in keys || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openMenu(trigger); }
    return;
  }
  if (e.key in keys) { e.preventDefault(); highlight(openSelect.index + keys[e.key]); }
  else if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); highlight(e.key === 'Home' ? 0 : -1); }
  else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); choose(openSelect.index); }
  else if (e.key === 'Escape' || e.key === 'Tab') closeMenu();
});
addEventListener('resize', closeMenu);
document.querySelector('.main').addEventListener('scroll', closeMenu, { passive: true });

// ---------------------------------------------------------------- settings
// Four tabs, one card each, each with its own Save that wakes up on a change.
const settingsPage = pages[4];
const setForms = [...document.querySelectorAll('.set-form')];
const panes = [...document.querySelectorAll('.pane')];
let settingsFields = [];

$('setTabs').addEventListener('lg-change', e => {
  closeMenu();
  panes.forEach((p, i) => p.classList.toggle('active', i === e.detail.index));
});

function fieldHtml(f) {
  const id = `set_${f.key}`;
  if (f.type === 'info') return `<div class="field"><span>${esc(f.label)}</span><code class="path">${esc(f.value)}</code></div>`;
  let input;
  if (f.type === 'select') input = glassSelect(f);
  else if (f.type === 'secret') input = `<span class="lg-input"><input id="${id}" name="${f.key}" type="password" autocomplete="off" placeholder="${f.set ? 'Saved. Type to replace.' : 'Not set'}"></span>`;
  else input = `<span class="lg-input"><input id="${id}" name="${f.key}" value="${esc(f.value)}" placeholder="${esc(f.placeholder)}"></span>`;
  const label = f.type === 'select' ? `<span id="${id}_label">${esc(f.label)}</span>` : `<label for="${id}">${esc(f.label)}</label>`;
  return `<div class="field" data-key="${f.key}">${label}${input}</div>`;
}

function renderForm(form, groups) {
  const wanted = form.dataset.groups.split(',');
  const html = groups.filter(g => wanted.includes(g.title)).map(g => {
    const note = g.fields.some(f => f.type === 'info')
      ? '<p class="note">Change these in <code>.env</code> (DOWNLOADS_PATH, MUSIC_PATH), then run <code>docker compose up -d</code>.</p>' : '';
    return `<section class="stack"><h2>${esc(g.title)}</h2>${g.fields.map(fieldHtml).join('')}${note}</section>`;
  }).join('');
  form.querySelector('.fields').innerHTML = html;
  form.querySelector('[type=submit]').disabled = true;
}

// Show only the fields that apply (Subsonic login, the chosen source's settings)
function applyConditions() {
  const value = key => settingsPage.querySelector(`[name="${key}"]`)?.value;
  for (const f of settingsFields) {
    if (!f.when) continue;
    const el = settingsPage.querySelector(`[data-key="${f.key}"]`);
    if (el) el.hidden = !f.when.values.includes(value(f.when.key));
  }
  $('tgCard').hidden = value('DOWNLOAD_SERVICES') !== 'telegram' && $('tgSignedIn').hidden;
}

function loadSettingsPage() {
  return firstLoad(4, setForms.map(f => [f.querySelector('.fields'), skel.fields(4)]), () => api('/api/settings'), data => {
    settingsFields = data.groups.flatMap(g => g.fields);
    setForms.forEach(f => renderForm(f, data.groups));
    applyConditions();
    loadTelegram().catch(() => {});
  });
}

for (const form of setForms) {
  const save = form.querySelector('[type=submit]');
  const msg = form.querySelector('.msg');
  const dirty = () => { save.disabled = false; setMsg(msg, ''); };
  form.addEventListener('input', dirty);
  form.addEventListener('change', () => { dirty(); applyConditions(); });
  form.addEventListener('submit', e => {
    e.preventDefault();
    busy(save, 'Saving…', async () => {
      const values = Object.fromEntries([...form.querySelectorAll('[name]')].map(el => [el.name, el.value]));
      try {
        await api('/api/settings', { values });
        const { groups } = await api('/api/settings');
        renderForm(form, groups);
        applyConditions();
        session = await api('/api/session');
        setMsg(msg, 'Saved.');
        if ('DOWNLOAD_SERVICES' in values || 'TG_API_ID' in values) loadTelegram().catch(() => {});
      } catch (err) { setMsg(msg, err.message, true); }
    }).then(() => { if (!msg.classList.contains('err')) save.disabled = true; });
  });
}

$('pwForm').addEventListener('submit', e => {
  e.preventDefault();
  busy($('pwBtn'), 'Changing…', async () => {
    try {
      await api('/api/password', { current: $('pwCurrent').value, new: $('pwNew').value });
      $('pwCurrent').value = $('pwNew').value = '';
      setMsg($('pwMsg'), 'Password changed.');
    } catch (err) { setMsg($('pwMsg'), err.message, true); }
  });
});

// ---------------------------------------------------------------- telegram
let tgStep = 'phone';

async function loadTelegram() {
  const t = await api('/api/telegram');
  $('tgSignedIn').hidden = !t.authorized;
  $('tgSignedOut').hidden = t.authorized || !t.configured;
  $('tgState').textContent = !t.configured ? 'Save the API ID and hash first.'
    : t.authorized ? `Signed in as ${t.account.name}.` : 'Not signed in.';
  applyConditions();
}

function setTgStep(step) {
  tgStep = step;
  $('tgCodeField').hidden = step === 'phone';
  $('tgPwField').hidden = step !== 'password';
  $('tgPhoneBtn').textContent = step === 'phone' ? 'Send code' : 'Sign in';
  (step === 'phone' ? $('tgPhone') : step === 'code' ? $('tgCode') : $('tgPw')).focus();
}

$('tgPhoneForm').addEventListener('submit', e => {
  e.preventDefault();
  busy($('tgPhoneBtn'), tgStep === 'phone' ? 'Sending…' : 'Signing in…', async () => {
    try {
      if (tgStep === 'phone') {
        await api('/api/telegram/code', { phone: $('tgPhone').value });
        setMsg($('tgMsg'), 'Code sent to your Telegram app.');
        setTgStep('code');
        return;
      }
      const r = await api('/api/telegram/verify', tgStep === 'password' ? { password: $('tgPw').value } : { code: $('tgCode').value });
      if (r.need_password) { setMsg($('tgMsg'), 'This account has two-step verification.'); setTgStep('password'); return; }
      setMsg($('tgMsg'), `Signed in as ${r.name}.`);
      $('tgCode').value = $('tgPw').value = '';
      setTgStep('phone');
      await loadTelegram();
    } catch (err) { setMsg($('tgMsg'), err.message, true); }
  });
});

$('tgLogout').addEventListener('click', e => busy(e.currentTarget, 'Signing out…', async () => {
  await api('/api/telegram/logout', {});
  setMsg($('tgMsg'), 'Signed out of Telegram.');
  await loadTelegram();
}));

// ---------------------------------------------------------------- display
try {
  if (localStorage.getItem('maf-solid') === '1') {
    document.documentElement.classList.add('lg-solid');
    $('solidSwitch').setAttribute('aria-checked', 'true');
  }
} catch {}
$('solidSwitch').addEventListener('lg-change', e => {
  document.documentElement.classList.toggle('lg-solid', e.detail.checked);
  try { localStorage.setItem('maf-solid', e.detail.checked ? '1' : '0'); } catch {}
});

// ---------------------------------------------------------------- background
const bg = document.querySelector('.bg');
function applyBackground(stamp) {
  bg.style.backgroundImage = stamp ? `url(/background?v=${stamp})` : '';
  $('bgState').textContent = stamp ? 'Custom image' : 'Default';
  $('bgRemove').hidden = !stamp;
}

// Resize to at most 2560 px wide and re-encode as WebP in the browser, so the
// server never stores a huge photo and every visit loads fast.
async function optimise(file) {
  const img = await createImageBitmap(file);
  const scale = Math.min(1, 2560 / img.width);
  const canvas = Object.assign(document.createElement('canvas'), { width: Math.round(img.width * scale), height: Math.round(img.height * scale) });
  canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
  const webp = await new Promise(r => canvas.toBlob(r, 'image/webp', 0.82));
  return webp && webp.size < file.size ? webp : file;  // keep the original if it was already smaller
}

$('bgFile').addEventListener('change', async e => {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  $('bgState').textContent = 'Uploading…';
  try {
    const body = await optimise(file);
    const r = await fetch('/api/background', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': body.type || 'image/webp' }, body });
    const json = await r.json();
    if (!r.ok) throw new Error(json.error);
    applyBackground(json.background);
    announce('Background updated');
  } catch (err) { $('bgState').textContent = err.message || 'Upload failed'; }
});
$('bgRemove').addEventListener('click', e => busy(e.currentTarget, 'Resetting…', async () =>
  applyBackground((await api('/api/background/remove', {})).background)));

// ---------------------------------------------------------------- boot
(async () => {
  try { session = await api('/api/session'); } catch { session = {}; }
  applyBackground(session.background);
  await hideSplash();
  session.authenticated ? enter() : showLogin();
})();
