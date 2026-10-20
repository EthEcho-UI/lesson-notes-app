// In-memory model mirrored to IndexedDB (cache store). Views read from here; sync/outbox write to it.
import * as store from './store.js';
import { norm } from './util.js';

export const M = {
  files: new Map(),        // repo path -> {sha, data, pending?}
  inbox: { exists: false, jobs: new Map(), bytes: 0, loaded: false }, // noteId -> {sha, job}
  uploads: [],             // local upload queue (IndexedDB 'uploads')
  outbox: [],              // pending small writes
  recs: [],                // local recordings
  cfg: { token: '', repo: 'EthEcho-UI/lesson-notes-data', lang: 'cs', theme: 'auto', diarize: { lesson: false, meeting: true } },
  net: { online: true, lastSync: null, error: null, auth: false, syncing: false },
  version: 0,
};
const listeners = new Set();
export const onChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
let raf = 0;
export function emit() {
  M.version++;
  if (raf) return;
  raf = setTimeout(() => { raf = 0; for (const fn of [...listeners]) { try { fn(); } catch (e) { console.error(e); } } }, 30);
}

export async function loadAll() {
  const [files, uploads, outbox, recs] = await Promise.all([
    store.getAll('cache'), store.getAll('uploads'), store.getAll('outbox'), store.getAll('recs')]);
  for (const f of files) M.files.set(f.path, { sha: f.sha, data: f.data, pending: !!f.pending });
  M.uploads = uploads.sort((a, b) => a.created_at.localeCompare(b.created_at));
  M.outbox = outbox.sort((a, b) => a.seq - b.seq);
  M.recs = recs;
  const inbox = await store.kvGet('inbox');
  if (inbox) { M.inbox.exists = inbox.exists; M.inbox.bytes = inbox.bytes; M.inbox.jobs = new Map(inbox.jobs); M.inbox.loaded = true; }
}
export async function setFile(path, sha, data, pending = false) {
  await store.put('cache', { path, sha, data, pending });
  M.files.set(path, { sha, data, pending });
}
export async function removeFile(path) {
  await store.del('cache', path);
  M.files.delete(path);
}
export function saveInbox() {
  return store.kvSet('inbox', { exists: M.inbox.exists, bytes: M.inbox.bytes, jobs: [...M.inbox.jobs] });
}

// ---- accessors ----------------------------------------------------------------------------------
const data = (p) => (M.files.get(p) || {}).data || null;
export const fileData = data;
export const fileSha = (p) => (M.files.get(p) || {}).sha || null;

export function notebooks(includeArchived = false) {
  const out = [];
  for (const [p, f] of M.files) if (/^notebooks\/[^/]+\.json$/.test(p) && f.data) out.push(f.data);
  return out.filter((n) => includeArchived || !n.archived).sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0) || a.name.localeCompare(b.name));
}
export const notebook = (id) => data(`notebooks/${id}.json`);
export function allNotes() {
  const out = [];
  for (const [p, f] of M.files) {
    const m = /^notes\/([^/]+)\/note\.json$/.exec(p);
    if (m && f.data) out.push(getNote(m[1]));
  }
  return out.sort((a, b) => (b.note.recorded_at || b.note.created_at).localeCompare(a.note.recorded_at || a.note.created_at));
}
export function getNote(id) {
  const note = data(`notes/${id}/note.json`);
  if (!note) return null;
  return {
    id, note,
    status: data(`notes/${id}/status.json`),
    notes: data(`notes/${id}/notes.json`),
    transcript: data(`notes/${id}/transcript.json`),
    speakers: data(`notes/${id}/speakers.json`),
  };
}
export const notesOf = (nbId) => allNotes().filter((n) => n.note.notebook_id === nbId);
export const heartbeat = () => data('worker/heartbeat.json');
export const settings = () => data('settings/app.json');
export const uploadFor = (noteId) => M.uploads.find((u) => u.note_id === noteId && u.step !== 'done') || null;

// ---- derived display state -------------------------------------------------------------------
// code: uploading | upload_waiting | upload_incomplete | waiting | transcribing | diarizing | summarizing | done | failed | unknown
export function derive(n) {
  const up = uploadFor(n.id);
  const job = M.inbox.jobs.get(n.id);
  const st = n.status;
  const hasResults = !!(n.notes || n.transcript);
  const r = { code: 'unknown', updating: false, hasResults, job: job ? job.job : null, status: st };
  if (up) {
    r.code = up.error || !M.net.online || navigator.onLine === false ? 'upload_waiting' : 'uploading';
    r.upload = up;
    r.updating = hasResults;
    return r;
  }
  if (n.note.parts.some((p) => p.status === 'uploading')) { r.code = 'upload_incomplete'; return r; }
  if (job && job.job) {
    const jid = job.job.job_id;
    if (st && st.job_id === jid) return { ...r, ...fromStatus(st), updating: hasResults && st.state !== 'done' };
    if (st && (st.completed_job_ids || []).includes(jid)) return { ...r, code: 'done' };
    r.code = 'waiting';
    r.updating = hasResults;
    r.ahead = jobsAhead(job.job);
    return r;
  }
  if (st) return { ...r, ...fromStatus(st) };
  if (n.note.source === 'laptop') return { ...r, code: 'waiting' };
  return r;
}
function fromStatus(st) {
  const code = st.state === 'queued' ? 'waiting' : st.state;
  return { code, progress: st.progress || null, error: st.error || null, queuePosition: st.queue_position };
}
export function jobsAhead(job) {
  let n = 0;
  for (const [noteId, j] of M.inbox.jobs) {
    if (!j.job || j.job.job_id === job.job_id) continue;
    const s = data(`notes/${noteId}/status.json`);
    const done = s && ((s.completed_job_ids || []).includes(j.job.job_id) || (s.job_id === j.job.job_id && s.state === 'done'));
    if (!done && j.job.created_at < job.created_at) n++;
  }
  return n;
}
// May the user add parts / regenerate right now? (contract: one unprocessed job dir per note)
export function canAddParts(n) {
  const d = derive(n);
  if (d.code === 'uploading' || d.code === 'upload_waiting' || d.code === 'upload_incomplete') return false;
  if (!d.job) return true;
  if (d.code === 'done') return true;
  return d.code === 'failed' && d.error && d.error.retryable === false;
}
export function canRetry(n) {
  const d = derive(n);
  return d.code === 'failed' && !!d.job && d.error && d.error.retryable && d.status.audio && d.status.audio.state === 'on_inbox';
}
export function laptopState(now = Date.now()) {
  const hb = heartbeat();
  if (!hb) return null;
  const age = now - new Date(hb.last_seen).getTime();
  return { last_seen: hb.last_seen, offline: hb.state === 'offline' || age > 30 * 60 * 1000, state: hb.state, queue: hb.queue || null };
}

// ---- search --------------------------------------------------------------------------------------
let idx = { v: -1, rows: [] };
function buildIndex() {
  const rows = [];
  for (const n of allNotes()) {
    const add = (kind, text, extra = {}) => { if (text) rows.push({ noteId: n.id, kind, text, low: norm(text), ...extra }); };
    add('title', n.note.title);
    const c = n.notes && n.notes.content;
    if (c) {
      add('notes', c.title); add('notes', c.summary);
      for (const k of ['key_points', 'todo', 'questions_open', 'decisions']) (c[k] || []).forEach((t) => add('notes', t));
      (c.definitions || []).forEach((d) => add('notes', `${d.term}: ${d.definition}`));
      (c.action_items || []).forEach((a) => add('notes', `${a.task} ${a.owner || ''}`));
    }
    if (n.transcript) for (const p of n.transcript.parts) for (const s of p.segments) add('transcript', s.text, { partId: p.part_id, start: p.offset + s.start });
  }
  idx = { v: M.version, rows };
}
// A note matches when every word occurs somewhere in it (title, notes or transcript); the first row
// containing a word is shown as the snippet. Diacritics and case are ignored.
export function search(q, limit = 60) {
  const words = norm(q).split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  if (idx.v !== M.version) buildIndex();
  const byNote = new Map();
  for (const r of idx.rows) {
    let e = byNote.get(r.noteId);
    if (!e) { e = { seen: new Set(), first: null, titleRow: null }; byNote.set(r.noteId, e); }
    for (const w of words) if (r.low.includes(w)) { e.seen.add(w); if (!e.first) e.first = r; }
    if (r.kind === 'title' && !e.titleRow) e.titleRow = r;
  }
  const out = [];
  for (const e of byNote.values()) if (e.seen.size === words.length) { out.push(e.first); if (out.length >= limit) break; }
  return out;
}

// ---- markdown export -------------------------------------------------------------------------------
export function toMarkdown(n, labels, withTranscript = false, fmtTime = (s) => String(Math.round(s))) {
  const L = labels;
  const c = n.notes && n.notes.content;
  const lines = [`# ${n.note.title}`, ''];
  const nb = notebook(n.note.notebook_id);
  lines.push(`*${nb ? nb.name + ' · ' : ''}${L.kind[n.note.kind]} · ${(n.note.recorded_at || n.note.created_at).slice(0, 10)}*`, '');
  const list = (head, arr) => { if (arr && arr.length) { lines.push(`## ${head}`, ...arr.map((x) => `- ${x}`), ''); } };
  if (c) {
    if (c.summary) lines.push(`## ${L.summary}`, c.summary, '');
    list(L.keyPoints, c.key_points);
    if (c.definitions && c.definitions.length) lines.push(`## ${L.definitions}`, ...c.definitions.map((d) => `- **${d.term}**: ${d.definition}`), '');
    list(L.decisions, c.decisions);
    if (c.action_items && c.action_items.length) lines.push(`## ${L.actionItems}`, ...c.action_items.map((a) => `- ${a.task}${a.owner ? ` (${a.owner}${a.due ? ', ' + a.due : ''})` : a.due ? ` (${a.due})` : ''}`), '');
    list(L.todo, c.todo);
    list(L.questionsOpen, c.questions_open);
  } else lines.push(L.noNotesYet, '');
  if (withTranscript && n.transcript) {
    lines.push(`## ${L.transcript}`, '');
    for (const p of n.transcript.parts) {
      lines.push(`### ${L.part} ${p.order}`, '');
      for (const s of p.segments) {
        const sp = s.speaker ? `**${speakerName(n, p.part_id, s.speaker)}**: ` : '';
        lines.push(`[${fmtTime(s.start)}] ${sp}${s.text}`);
      }
      lines.push('');
    }
  }
  return lines.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd() + '\n';
}
export function speakerName(n, partId, label) {
  const names = (n.speakers && n.speakers.names) || {};
  return names[`${partId}:${label}`] || names[`*:${label}`] || label;
}
