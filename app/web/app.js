// Laika chat client: WebSocket for live events, REST for history and settings.
const $ = (s) => document.querySelector(s);
const logEl = $('#log');
const listEl = $('#messages');
const typingEl = $('#typing');
const olderEl = $('#older');
const input = $('#input');
const statusEl = $('#status');

const messages = []; // sorted by id
const elements = new Map(); // message id -> element
const pending = new Map(); // client_id -> local element waiting for the server echo
let settings = {};
let ws;
let retry = 0;
let lastTypingSent = 0;
let loadingOlder = false;
let noMoreOlder = false;

// Audio: one element, a play queue, unlocked by the first tap (mobile autoplay rules).
const player = new Audio();
const playQueue = [];
let playing = false;
let unlocked = false;
function unlockAudio() {
  if (unlocked) return;
  unlocked = true;
  player.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=';
  player.play().catch(() => {});
}
function playNext() {
  const url = playQueue.shift();
  if (!url) { playing = false; return; }
  playing = true;
  player.src = url;
  player.play().catch(() => { playing = false; });
}
function enqueueAudio(url) { playQueue.push(url); if (!playing) playNext(); }
player.addEventListener('ended', playNext);

// WhatsApp-style ticks: one = delivered, two blue = read, clock = still sending.
const TICK_SENT = '<svg class="tick" viewBox="0 0 16 11" width="16" height="11" aria-label="delivered"><path d="M1.5 6 5 9.5 14.5 1.5"/></svg>';
const TICK_READ = '<svg class="tick read" viewBox="0 0 19 11" width="19" height="11" aria-label="read"><path d="M1.5 6 5 9.5 13.5 1.5"/><path d="M7.6 8.1 9 9.5 17.5 1.5"/></svg>';
const TICK_PENDING = '<svg class="tick pending" viewBox="0 0 12 12" width="12" height="12" aria-label="sending"><circle cx="6" cy="6" r="4.6"/><path d="M6 3.4V6l1.7 1.1"/></svg>';
const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const dayOf = (ts) => new Date(ts * 1000).toDateString();
const newId = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
const nearBottom = () => logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 120;
const toBottom = () => { logEl.scrollTop = logEl.scrollHeight; };

function bubble(m) {
  const el = document.createElement('div');
  el.dataset.id = m.id;
  const meta = m.meta || {};
  const play = m.audio_path ? `<button class="play" data-audio="/api/audio/${m.id}" aria-label="Play">▶︎</button>` : '';
  if (m.role === 'user') {
    el.className = 'bubble me';
    const tick = m.status === 'read' ? TICK_READ : m.status === 'received' ? TICK_SENT : TICK_PENDING;
    el.innerHTML = `${play}${meta.voice && !play ? '<span class="voice-tag">🎤 </span>' : ''}${esc(m.text)}<span class="meta">${hhmm(m.ts)}${tick}</span>`;
  } else if (m.role === 'companion') {
    el.className = 'bubble them';
    const pinyin = settings.pinyin && meta.pinyin ? `<span class="pinyin">${esc(meta.pinyin)}</span>` : '';
    el.innerHTML = `${play}${esc(m.text)}${pinyin}<span class="meta">${hhmm(m.ts)}</span>`;
  } else if (m.role === 'correction') {
    el.className = 'fix';
    const items = (meta.corrections || []).map((c) =>
      `<div class="item"><s>${esc(c.original)}</s> → <b>${esc(c.better)}</b><span class="why">${esc(c.why || '')}</span></div>`);
    el.innerHTML = `✏️ ${items.length ? items.join('') : esc(m.text)}`;
  } else {
    el.className = m.text.includes('\n') ? 'system multi' : 'system';
    const offer = meta.offer; // W5: "Switch to Русский?"; nothing changes unless I tap Switch
    el.textContent = m.text + (offer && offer.answer === 'not_now' ? ' · not now' : '');
    if (meta.download) { // P4.6: an Anki deck
      const a = document.createElement('a');
      a.href = meta.download;
      a.download = meta.filename || '';
      a.className = 'download';
      a.textContent = ' ⬇︎ Download';
      el.append(a);
    }
    if (offer && !offer.answer) {
      const row = document.createElement('span');
      row.className = 'offer';
      row.innerHTML = `<button data-offer="${m.id}" data-accept="1">Switch</button><button data-offer="${m.id}" data-accept="0">Not now</button>`;
      el.append(row);
    }
  }
  return el;
}

async function answerOffer(btn) {
  const buttons = btn.parentElement.querySelectorAll('button');
  buttons.forEach((b) => { b.disabled = true; });
  const r = await fetch(`/api/offer/${btn.dataset.offer}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ accept: btn.dataset.accept === '1' }),
  }).catch(() => null);
  if (!r || !r.ok) buttons.forEach((b) => { b.disabled = false; }); // the server's update removes them on success
}

function dayPill(ts) {
  const el = document.createElement('div');
  el.className = 'day';
  el.textContent = new Date(ts * 1000).toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' });
  return el;
}

function renderAll() {
  listEl.replaceChildren();
  elements.clear();
  let lastDay = null;
  for (const m of messages) {
    if (dayOf(m.ts) !== lastDay) listEl.append(dayPill(m.ts));
    lastDay = dayOf(m.ts);
    const el = bubble(m);
    elements.set(m.id, el);
    listEl.append(el);
  }
  for (const el of pending.values()) listEl.append(el);
}

function upsert(m) {
  const stick = nearBottom() || m.role === 'user';
  const local = m.meta && m.meta.client_id && pending.get(m.meta.client_id);
  if (local) { local.remove(); pending.delete(m.meta.client_id); }
  const i = messages.findIndex((x) => x.id === m.id);
  if (i >= 0) {
    messages[i] = m;
    const el = bubble(m);
    elements.get(m.id)?.replaceWith(el);
    elements.set(m.id, el);
  } else if (!messages.length || m.id > messages[messages.length - 1].id) {
    const last = messages[messages.length - 1];
    messages.push(m);
    if (!last || dayOf(last.ts) !== dayOf(m.ts)) listEl.append(dayPill(m.ts));
    const el = bubble(m);
    elements.set(m.id, el);
    listEl.append(el);
    for (const p of pending.values()) listEl.append(p);
  } else {
    messages.push(m);
    messages.sort((a, b) => a.id - b.id);
    renderAll();
  }
  if (stick) toBottom();
}

function setStatus(text) { statusEl.textContent = text; }

// Header status: "typing…" only while Laika writes, "recording audio…" while voice notes are made.
let typingOn = false;
let recordings = 0;
let typingTimer = null;
let recordingTimer = null;

function refreshStatus() {
  const online = ws && ws.readyState === 1;
  setStatus(typingOn ? 'typing…' : recordings > 0 ? 'recording audio…' : online ? 'online' : 'offline');
}

function showTyping(on) {
  const stick = nearBottom();
  typingOn = on;
  typingEl.hidden = !on;
  clearTimeout(typingTimer);
  if (on) typingTimer = setTimeout(() => showTyping(false), 25000); // never stuck if a stop event is missed
  refreshStatus();
  if (on && stick) toBottom();
}

function trackRecording(delta) {
  recordings = Math.max(0, recordings + delta);
  clearTimeout(recordingTimer);
  if (recordings) recordingTimer = setTimeout(() => { recordings = 0; refreshStatus(); }, 60000);
  refreshStatus();
}

function handle(ev) {
  if (ev.type === 'message') upsert(ev.message);
  else if (ev.type === 'read') {
    for (const id of ev.ids) {
      const m = messages.find((x) => x.id === id);
      if (m) upsert({ ...m, status: 'read' });
    }
  } else if (ev.type === 'audio') {
    upsert(ev.message);
    trackRecording(-1);
    if (ev.origin === 'web') enqueueAudio(`/api/audio/${ev.message.id}`);
  } else if (ev.type === 'audio_failed') trackRecording(-1);
  else if (ev.type === 'recording') trackRecording(1);
  else if (ev.type === 'typing') showTyping(true);
  else if (ev.type === 'typing_stop') showTyping(false);
  else if (ev.type === 'settings') { applySettings(ev.settings); renderAll(); }
  else if (ev.type === 'error') {
    const el = document.createElement('div');
    el.className = 'error';
    el.textContent = ev.text;
    listEl.append(el);
    toBottom();
  }
}

async function loadLatest() {
  const r = await fetch('/api/history?limit=60');
  const { messages: page } = await r.json();
  for (const m of page) {
    const i = messages.findIndex((x) => x.id === m.id);
    if (i >= 0) messages[i] = m; else messages.push(m);
  }
  messages.sort((a, b) => a.id - b.id);
  renderAll();
  toBottom();
}

async function loadOlder() {
  if (loadingOlder || noMoreOlder || !messages.length) return;
  loadingOlder = true;
  olderEl.hidden = false;
  const r = await fetch(`/api/history?before=${messages[0].id}&limit=60`);
  const { messages: page } = await r.json();
  olderEl.hidden = true;
  if (!page.length) noMoreOlder = true;
  const before = logEl.scrollHeight;
  messages.unshift(...page);
  renderAll();
  logEl.scrollTop += logEl.scrollHeight - before;
  loadingOlder = false;
}

function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => { retry = 0; refreshStatus(); loadLatest(); };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => {
    setStatus('offline · reconnecting…');
    setTimeout(connect, Math.min(10000, 1000 * 2 ** retry++));
  };
}

function send() {
  unlockAudio();
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== 1) return;
  const clientId = newId();
  const el = bubble({ id: 0, role: 'user', text, ts: Date.now() / 1000, status: 'local', meta: {} });
  pending.set(clientId, el);
  listEl.append(el);
  toBottom();
  ws.send(JSON.stringify({ type: 'message', text, client_id: clientId }));
  input.value = '';
  lastTypingSent = 0; // the next keystroke starts a new "typing" right away
  autosize();
  saveDraft();
  updateSendBtn();
}

// The unsent text survives closing the app (this browser only).
const DRAFT_KEY = 'laika-draft';
function saveDraft() {
  try {
    if (input.value) localStorage.setItem(DRAFT_KEY, input.value);
    else localStorage.removeItem(DRAFT_KEY);
  } catch (e) { /* storage blocked: the draft just isn't kept */ }
}
function restoreDraft() {
  try { input.value = localStorage.getItem(DRAFT_KEY) || ''; } catch (e) { /* nothing kept */ }
}

// One button, like WhatsApp: 🎤 while the box is empty, ➤ once there is text, ■ while recording.
const sendBtn = $('#send');
function updateSendBtn() {
  const mode = recorder ? 'rec' : input.value.trim() ? 'send' : 'mic';
  sendBtn.textContent = { rec: '■', send: '➤', mic: '🎤' }[mode];
  sendBtn.setAttribute('aria-label', { rec: 'Stop and send the voice note', send: 'Send', mic: 'Record a voice note' }[mode]);
  sendBtn.classList.toggle('rec', mode === 'rec');
}

// Composing signals: Laika holds her reply while I type, keep a draft, or record.
function sendTyping(fields) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'typing', ...fields }));
}
function onInput() {
  if (!input.value.trim()) { lastTypingSent = 0; sendTyping({ active: false }); return; }
  if (Date.now() - lastTypingSent > 1500) { lastTypingSent = Date.now(); sendTyping({ active: true, draft: true }); }
}

function autosize() {
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function applySettings(s) {
  settings = s;
  $('#lang').value = s.practice_lang;
}

async function saveSettings(patch) {
  const r = await fetch('/api/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  });
  if (r.ok) applySettings(await r.json());
}

$('#composer').addEventListener('submit', (e) => { e.preventDefault(); send(); });
listEl.addEventListener('click', (e) => {
  const offer = e.target.closest('[data-offer]');
  if (offer) { answerOffer(offer); return; }
  const btn = e.target.closest('.play');
  if (!btn) return;
  unlockAudio();
  playQueue.length = 0;
  playing = true;
  player.src = btn.dataset.audio;
  player.play().catch(() => { playing = false; });
});

// Voice notes: tap 🎤 to start, tap ■ to send (max 2 minutes).
let recorder = null;
sendBtn.addEventListener('click', () => {
  if (recorder) recorder.stop();
  else if (input.value.trim()) send();
  else record();
});
async function record() {
  unlockAudio();
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    handle({ type: 'error', text: 'Microphone not available (open the app through its https address).' });
    return;
  }
  const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find((t) => MediaRecorder.isTypeSupported(t));
  const rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  const started = Date.now();
  let lastRecordingSent = 0;
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    input.placeholder = `● ${s}s · tap ■ to send`;
    if (Date.now() - lastRecordingSent >= 2000) { lastRecordingSent = Date.now(); sendTyping({ active: true, recording: true }); }
    if (s >= 120) rec.stop();
  }, 500);
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  rec.onstop = async () => {
    clearInterval(tick);
    stream.getTracks().forEach((t) => t.stop());
    input.placeholder = 'Message…';
    recorder = null;
    updateSendBtn();
    const blob = new Blob(chunks, { type: rec.mimeType });
    if (Date.now() - started < 700 || blob.size < 1500) return;
    const ext = blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm';
    const form = new FormData();
    form.append('file', blob, `voice.${ext}`);
    setStatus('sending voice note…');
    const r = await fetch('/api/voice', { method: 'POST', body: form });
    refreshStatus();
    if (!r.ok) handle({ type: 'error', text: 'The voice note could not be processed.' });
  };
  recorder = rec;
  rec.start();
  updateSendBtn();
}
// Books and files for her library (P4.1): the server answers with 📚 messages as it reads them.
const fileInput = $('#file');
$('#attach').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', async () => {
  const file = fileInput.files[0];
  fileInput.value = '';
  if (!file) return;
  if (file.size > 100 * 2 ** 20) { handle({ type: 'error', text: 'That file is over 100 MB: put it in Library/_inbox in the vault.' }); return; }
  const form = new FormData();
  form.append('file', file, file.name);
  setStatus(`sending ${file.name}…`);
  try {
    const r = await fetch('/api/library', { method: 'POST', body: form });
    if (!r.ok) handle({ type: 'error', text: `${file.name} could not be uploaded.` });
  } catch (e) {
    handle({ type: 'error', text: `${file.name} could not be uploaded (offline?).` });
  }
  refreshStatus();
});
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
input.addEventListener('input', () => { autosize(); onInput(); saveDraft(); updateSendBtn(); });
restoreDraft();
autosize();
updateSendBtn();
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { lastTypingSent = 0; sendTyping({ active: false }); }
});
logEl.addEventListener('scroll', () => { if (logEl.scrollTop < 80) loadOlder(); });
$('#lang').addEventListener('change', (e) => saveSettings({ practice_lang: e.target.value }));

const dialog = $('#settings');
$('#settings-btn').addEventListener('click', () => {
  const f = dialog.querySelector('form');
  for (const key of ['corrections', 'voice_mode', 'level_en', 'level_ru', 'level_zh']) f.elements[key].value = settings[key] ?? '';
  f.elements.voice_speed.value = String(settings.voice_speed ?? 1);
  f.elements.pinyin.checked = !!settings.pinyin;
  dialog.showModal();
});
dialog.addEventListener('close', () => {
  if (dialog.returnValue !== 'save') return;
  const f = dialog.querySelector('form');
  const patch = { pinyin: f.elements.pinyin.checked, voice_speed: Number(f.elements.voice_speed.value) };
  for (const key of ['corrections', 'voice_mode', 'level_en', 'level_ru', 'level_zh']) patch[key] = f.elements[key].value;
  saveSettings(patch).then(renderAll);
});

(async () => {
  try {
    applySettings(await (await fetch('/api/settings')).json());
    const { name } = await (await fetch('/api/profile')).json();
    if (name) { $('#name').textContent = name; document.title = name; }
  } catch (e) { /* offline: the socket retry loop takes over */ }
  connect();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
})();
