// Read side: mirrors main + inbox from GitHub into the model with conditional requests.
import * as gh from './gh.js';
import * as store from './store.js';
import { M, setFile, removeFile, saveInbox, emit, derive, getNote } from './model.js';

const WANTED = /^(notebooks\/[^/]+\.json|notes\/[^/]+\/(note|status|notes|transcript|speakers)\.json|settings\/app\.json|worker\/heartbeat\.json)$/;
let inflight = null;
let lastList = 0, lastNotePoll = 0;

export function classify(e) {
  if (e instanceof gh.AuthError) return 'auth';
  if (e instanceof gh.RateLimitError) return 'rate';
  if (e instanceof gh.NetworkError) return 'network';
  return 'other';
}
function setError(e) {
  const k = classify(e);
  M.net.error = { kind: k, message: String(e.message || e) };
  M.net.auth = k === 'auth';
  if (k !== 'network') console.warn('sync error', k, e.message);
}

export function refresh() {
  if (inflight) return inflight;
  inflight = doRefresh().finally(() => { inflight = null; M.net.syncing = false; emit(); });
  return inflight;
}

async function doRefresh() {
  if (!M.cfg.token) return;
  M.net.syncing = true; emit();
  lastList = Date.now();
  try {
    const t = await gh.getTree('main', await store.kvGet('etag:main'));
    if (t.tree) {
      await applyMain(t.tree);
      await store.kvSet('etag:main', t.etag);
    }
    await refreshInbox();
    M.net.lastSync = new Date().toISOString();
    M.net.error = null; M.net.auth = false;
    await store.kvSet('lastSync', M.net.lastSync);
  } catch (e) { setError(e); }
}

async function pool(items, n, fn) {
  let i = 0;
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
    while (i < items.length) { const it = items[i++]; await fn(it); }
  }));
}

async function applyMain(tree) {
  const remote = new Map();
  for (const e of tree) if (e.type === 'blob' && WANTED.test(e.path)) remote.set(e.path, e);
  const todo = [];
  M.mainImages = new Map(tree.filter((e) => e.type === 'blob' && /^notes\/[^/]+\/images\/[^/]+$/.test(e.path)).map((e) => [e.path, e.sha]));
  await store.kvSet('mainImages', [...M.mainImages]);
  for (const [path, e] of remote) {
    const local = M.files.get(path);
    if (local && local.pending) continue;          // our own unsent optimistic write wins until the outbox flushed
    if (!local || local.sha !== e.sha) todo.push(e);
  }
  // small files first so the list renders fast; transcripts (big) last
  todo.sort((a, b) => /transcript/.test(a.path) - /transcript/.test(b.path));
  let netErr = null;
  await pool(todo, 4, async (e) => {
    if (netErr) return;
    try {
      const text = await gh.getBlobText(e.sha);
      let data = null;
      try { data = JSON.parse(text); } catch { return; }
      await setFile(e.path, e.sha, data);
      emit();
    } catch (err) { if (!(err instanceof gh.HttpError) || err instanceof gh.AuthError || err instanceof gh.RateLimitError) netErr = err; }
  });
  for (const path of [...M.files.keys()]) {
    const local = M.files.get(path);
    if (WANTED.test(path) && !remote.has(path) && !local.pending) await removeFile(path);
  }
  if (netErr) throw netErr;
}

async function refreshInbox() {
  const t = await gh.getTree('inbox', await store.kvGet('etag:inbox'));
  if (t.notModified) { M.inbox.loaded = true; return; }
  if (t.missing) { M.inbox.exists = false; M.inbox.jobs = new Map(); M.inbox.bytes = 0; M.inbox.loaded = true; await store.kvDel('etag:inbox'); await saveInbox(); return; }
  const jobs = new Map();
  let bytes = 0;
  const files = new Map(), shas = new Map();
  for (const e of t.tree) {
    if (e.type !== 'blob') continue;
    const m = /^jobs\/([^/]+)\//.exec(e.path);
    if (!m) continue;
    bytes += e.size || 0;
    if (!files.has(m[1])) files.set(m[1], []);
    files.get(m[1]).push(e.path);
    if (!shas.has(m[1])) shas.set(m[1], {});
    shas.get(m[1])[e.path] = e.sha;
    if (e.path === `jobs/${m[1]}/job.json`) jobs.set(m[1], { sha: e.sha, job: null, files: [] });
  }
  for (const [id, j] of jobs) {
    j.files = files.get(id);
    j.shas = shas.get(id);
    const old = M.inbox.jobs.get(id);
    if (old && old.sha === j.sha && old.job) { j.job = old.job; continue; }
    try { j.job = JSON.parse(await gh.getBlobText(j.sha)); } catch (e) { if (e instanceof gh.NetworkError || e instanceof gh.AuthError) throw e; }
  }
  M.inbox.exists = true; M.inbox.jobs = jobs; M.inbox.bytes = bytes; M.inbox.loaded = true;
  await store.kvSet('etag:inbox', t.etag);
  await saveInbox();
  emit();
}

export async function pollNote(id) {
  lastNotePoll = Date.now();
  const path = `notes/${id}/status.json`;
  const key = `etag:${path}`;
  try {
    const f = await gh.getFile(path, 'main', await store.kvGet(key));
    if (!f) return;
    if (f.notModified) return;
    const before = (M.files.get(path) || {}).sha;
    if (f.sha !== before && !(M.files.get(path) || {}).pending) {
      await setFile(path, f.sha, JSON.parse(f.text));
      await store.kvSet(key, f.etag);
      emit();
      const st = JSON.parse(f.text);
      if (st.state === 'done' || st.state === 'failed') refresh();
    }
  } catch (e) { setError(e); }
}

// ---- scheduler -----------------------------------------------------------------------------------
let timer = null, activeId = () => null;
export function startPolling(getActive) {
  activeId = getActive;
  if (timer) return;
  timer = setInterval(tick, 2000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && Date.now() - lastList > 10000) refresh(); });
  window.addEventListener('online', () => { refresh(); });
  tick();
}
export function noteInterval(code) {
  if (code === 'transcribing' || code === 'diarizing' || code === 'reading_images' || code === 'summarizing') return 5000;
  if (code === 'waiting') return 15000;
  return 0;
}
function tick() {
  if (document.hidden || !M.cfg.token || navigator.onLine === false || M.net.auth) return;
  const now = Date.now();
  const id = activeId();
  if (id) {
    const n = getNote(id);
    const iv = n ? noteInterval(derive(n).code) : 0;
    if (iv && now - lastNotePoll >= iv) pollNote(id);
  }
  if (now - lastList >= 60000) refresh();
}
