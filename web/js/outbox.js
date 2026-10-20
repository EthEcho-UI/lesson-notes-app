// Write side: optimistic small writes (outbox) and the resumable upload pipeline.
import * as gh from './gh.js';
import * as store from './store.js';
import { M, setFile, removeFile, emit, fileData, fileSha, getNote, saveInbox } from './model.js';
import { ulid, nowIso, sha256Hex, gitBlobSha, sleep, clone } from './util.js';
import { refresh, classify } from './sync.js';

export const TARGET_MAX = 90000000;
const INBOX_BLOCK = 943718400;
const MAX_CONFLICT = 5;

// ---- small JSON writes (notebooks, settings, speakers) ----------------------------------------------
export async function writeJson(path, { create, set }) {
  const cur = fileData(path);
  const ts = nowIso();
  const next = cur ? { ...cur, ...clone(set), updated_at: ts } : { ...clone(create), ...clone(set || {}) };
  const item = { id: ulid(), seq: Date.now() * 1000 + Math.floor(Math.random() * 1000), path, create: create ? clone(create) : null, set: clone(set || {}) };
  await store.put('outbox', item);
  M.outbox.push(item);
  await setFile(path, fileSha(path), next, true);
  emit();
  kick();
}

async function flushOutbox() {
  while (M.outbox.length) {
    const item = M.outbox[0];
    let merged = null, sha = null;
    for (let attempt = 0; ; attempt++) {
      const f = await gh.getFile(item.path, 'main');
      let body;
      if (!f) { if (!item.create) { merged = null; break; } body = { ...item.create, ...item.set }; sha = undefined; }
      else { body = { ...JSON.parse(f.text), ...item.set, updated_at: nowIso() }; sha = f.sha; }
      try {
        const r = await gh.putJson(item.path, 'main', body, { sha, message: msgFor(item.path) });
        merged = body; sha = r.sha; break;
      } catch (e) {
        if (e instanceof gh.HttpError && (e.status === 409 || e.status === 422) && attempt < MAX_CONFLICT) { await sleep(400 * (attempt + 1) * gh.getConfig().backoff); continue; }
        throw e;
      }
    }
    await store.del('outbox', item.id);
    M.outbox.shift();
    if (merged) {
      // only mark clean if no newer pending item targets the same path
      const stillPending = M.outbox.some((o) => o.path === item.path);
      if (!stillPending) await setFile(item.path, sha, merged, false);
    } else if (!M.outbox.some((o) => o.path === item.path)) await removeFile(item.path);
    emit();
  }
}
function msgFor(path) {
  const m = /^notes\/([^/]+)\//.exec(path);
  if (m) return `note ${m[1]}: update`;
  if (path.startsWith('notebooks/')) return 'notebook: update';
  return 'settings: update';
}

// ---- upload pipeline ----------------------------------------------------------------------------------
// parts: [{blob, name, ext, duration, recId?}]
export async function enqueueUpload({ noteId, existing, notebookId, kind, title, language, diarize, parts, recordedAt }) {
  const jobId = ulid();
  const cur = existing ? fileData(`notes/${noteId}/note.json`) : null;
  const base = cur ? Math.max(0, ...cur.parts.map((p) => p.order)) : 0;
  const up = {
    id: jobId, note_id: noteId, step: 'note', created_at: nowIso(), kind, title, notebook_id: notebookId, language, diarize: !!diarize,
    append: !!cur, recorded_at: recordedAt || nowIso(), error: null, parts: [], rec_ids: [],
  };
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    const order = base + i + 1;
    const nn = String(order).padStart(2, '0');
    const blobId = `${jobId}:${nn}`;
    await store.put('blobs', { id: blobId, blob: p.blob });
    up.parts.push({
      id: `p${nn}`, order, ext: p.ext, bytes: p.blob.size, sha256: await sha256Hex(p.blob), git_sha: await gitBlobSha(p.blob),
      duration: p.duration == null ? null : p.duration, blob_id: blobId, done: false,
    });
    if (p.recId) up.rec_ids.push(p.recId);
  }
  await store.put('uploads', up);
  M.uploads.push(up);
  for (const rid of up.rec_ids) { await store.update('recs', rid, (r) => (r ? { ...r, used: true } : undefined)); const r = M.recs.find((x) => x.id === rid); if (r) r.used = true; }
  // optimistic local note so it shows up in lists while queued/offline
  const note = cur ? clone(cur) : {
    schema_version: 1, id: noteId, kind, title, notebook_id: notebookId, created_at: up.created_at, updated_at: up.created_at,
    recorded_at: up.recorded_at, source: 'pwa', language, parts: [],
  };
  for (const p of up.parts) note.parts.push(notePart(p, 'uploading', null));
  await setFile(`notes/${noteId}/note.json`, fileSha(`notes/${noteId}/note.json`), note, true);
  emit();
  kick();
  return up;
}
const notePart = (p, status, uploadedAt) => ({
  id: p.id, order: p.order, filename: `${String(p.order).padStart(2, '0')}.${p.ext}`, bytes: p.bytes, sha256: p.sha256,
  duration: p.duration, uploaded_at: uploadedAt, status,
});

let running = false, again = false, retryTimer = null;
export async function kick() {
  if (running) { again = true; return; }
  running = true;
  try {
    do {
      again = false;
      if (!M.cfg.token || navigator.onLine === false) break;
      try { await flushOutbox(); } catch (e) { noteError(e); if (['network', 'auth', 'rate'].includes(classify(e))) break; }
      for (const up of [...M.uploads]) {
        if (up.step === 'done' || up.error && up.error.fatal) continue;
        try { await runUpload(up); } catch (e) { await failUpload(up, e); if (['network', 'auth'].includes(classify(e))) break; }
      }
    } while (again);
  } finally {
    running = false;
    scheduleRetry();
    emit();
  }
}
function noteError(e) {
  M.net.error = { kind: classify(e), message: String(e.message || e) };
  M.net.auth = classify(e) === 'auth';
}
function scheduleRetry() {
  clearTimeout(retryTimer);
  const wait = M.uploads.some((u) => u.step !== 'done' && u.error && !u.error.fatal) || M.outbox.length;
  if (wait) retryTimer = setTimeout(kick, 20000 * gh.getConfig().backoff);
}
async function failUpload(up, e) {
  const kind = classify(e);
  const fatal = kind === 'other' && !(e instanceof gh.HttpError && [409, 422, 404].includes(e.status));
  const err = { kind, message: String(e.message || e), fatal, at: nowIso() };
  console.warn('upload error', kind, err.message);
  await patchUpload(up.id, (u) => ({ ...u, error: err }));
  if (kind === 'auth') M.net.auth = true;
}
async function patchUpload(id, fn) {
  const next = await store.update('uploads', id, (u) => (u ? fn(u) : undefined));
  const i = M.uploads.findIndex((u) => u.id === id);
  if (next && i >= 0) { Object.assign(M.uploads[i], next); for (const k of Object.keys(M.uploads[i])) if (!(k in next)) delete M.uploads[i][k]; }
  emit();
  return next;
}

export async function resumeUpload(id) {
  await patchUpload(id, (u) => ({ ...u, error: null }));
  kick();
}
export async function cancelUpload(id) {
  const up = M.uploads.find((u) => u.id === id);
  if (!up) return;
  for (const p of up.parts) await store.del('blobs', p.blob_id);
  await store.del('uploads', id);
  M.uploads = M.uploads.filter((u) => u.id !== id);
  // best-effort: mark the parts failed on main so other devices do not wait for them
  try {
    if (navigator.onLine !== false && fileSha(`notes/${up.note_id}/note.json`) && !fileData(`notes/${up.note_id}/note.json`).parts.every((p) => p.status !== 'uploading')) {
      await updateNote(up.note_id, (n) => { for (const p of n.parts) if (up.parts.some((x) => x.id === p.id) && p.status === 'uploading') p.status = 'failed'; return n; });
    }
  } catch (e) { console.warn('cancel: could not mark failed', e.message); }
  emit();
}

async function updateNote(noteId, mutate, createIfMissing = null) {
  const path = `notes/${noteId}/note.json`;
  for (let attempt = 0; ; attempt++) {
    const f = await gh.getFile(path, 'main');
    let note, sha;
    if (!f) { if (!createIfMissing) throw new gh.HttpError(404, 'note missing'); note = clone(createIfMissing); }
    else { note = JSON.parse(f.text); sha = f.sha; }
    const next = mutate(note);
    next.updated_at = nowIso();
    try {
      const r = await gh.putJson(path, 'main', next, { sha, message: `note ${noteId}: update` });
      await setFile(path, r.sha, next, false);
      return next;
    } catch (e) {
      if (e instanceof gh.HttpError && (e.status === 409 || e.status === 422) && attempt < MAX_CONFLICT) { await sleep(400 * (attempt + 1) * gh.getConfig().backoff); continue; }
      throw e;
    }
  }
}

async function runUpload(up) {
  const id = up.id;
  if (up.error) await patchUpload(id, (u) => ({ ...u, error: null }));
  const cur = () => M.uploads.find((u) => u.id === id);
  // step 1: note.json with parts "uploading" (so every device sees the note exists)
  if (cur().step === 'note') {
    const local = fileData(`notes/${up.note_id}/note.json`);
    const create = clone(local); // optimistic copy already holds the new parts
    await updateNote(up.note_id, (n) => {
      for (const p of up.parts) {
        const ex = n.parts.find((x) => x.id === p.id);
        if (!ex) n.parts.push(notePart(p, 'uploading', null));
        else if (ex.sha256 !== p.sha256) throw new gh.HttpError(409, `part ${p.id} already exists with different content`);
        else if (ex.status === 'failed') ex.status = 'uploading';
      }
      n.parts.sort((a, b) => a.order - b.order);
      return n;
    }, create);
    await patchUpload(id, (u) => ({ ...u, step: 'parts' }));
  }
  // step 2: audio parts to the orphan inbox branch, sequentially
  if (cur().step === 'parts') {
    const total = up.parts.reduce((s, p) => s + p.bytes, 0);
    if (M.inbox.loaded && M.inbox.bytes + total > INBOX_BLOCK) throw Object.assign(new gh.RateLimitError(0, 'inbox full', null), { blocked: true });
    await gh.ensureInbox();
    for (const p of cur().parts) {
      if (p.done) continue;
      await uploadPart(up, p);
      await patchUpload(id, (u) => ({ ...u, parts: u.parts.map((x) => (x.id === p.id ? { ...x, done: true } : x)) }));
    }
    await patchUpload(id, (u) => ({ ...u, step: 'job' }));
  }
  // step 3: job.json LAST
  if (cur().step === 'job') {
    const jobPath = `jobs/${up.note_id}/job.json`;
    const job = {
      schema_version: 1, job_id: up.id, note_id: up.note_id, kind: up.kind, created_at: up.created_at, source: 'pwa', language: up.language,
      action: 'transcribe', append: up.append, audio_location: 'inbox', options: { diarize: up.diarize, num_speakers: null },
      parts: up.parts.map((p) => ({ id: p.id, order: p.order, file: `parts/${String(p.order).padStart(2, '0')}.${p.ext}`, bytes: p.bytes, sha256: p.sha256, duration: p.duration })),
    };
    await putInbox(jobPath, JSON.stringify(job, null, 2) + '\n', (ex) => { try { return JSON.parse(ex).job_id === up.id; } catch { return false; } }, `job ${up.note_id}: submit`);
    await patchUpload(id, (u) => ({ ...u, step: 'finalize' }));
  }
  // step 4: mark parts uploaded
  if (cur().step === 'finalize') {
    const at = nowIso();
    await updateNote(up.note_id, (n) => {
      for (const p of n.parts) if (up.parts.some((x) => x.id === p.id)) { p.status = 'uploaded'; p.uploaded_at = p.uploaded_at || at; }
      return n;
    });
    for (const p of up.parts) await store.del('blobs', p.blob_id);
    for (const rid of up.rec_ids) { await store.del('recs', rid); M.recs = M.recs.filter((r) => r.id !== rid); }
    await store.del('uploads', id);
    M.uploads = M.uploads.filter((u) => u.id !== id);
    emit();
    refresh();
  }
}

// PUT to inbox; same = (existingText) => bool tells that remote already holds what we want
async function putInbox(path, content, same, message) {
  for (let attempt = 0; ; attempt++) {
    const f = await gh.getFile(path, 'inbox');
    if (f && same && same(f.text)) return;
    try { return await gh.putFile(path, 'inbox', content, { sha: f ? f.sha : undefined, message }); }
    catch (e) {
      if (e instanceof gh.HttpError && [404, 409, 422].includes(e.status) && attempt < MAX_CONFLICT) {
        if (e.status === 404 || e.status === 422) await gh.ensureInbox();
        await sleep(400 * (attempt + 1) * gh.getConfig().backoff); continue;
      }
      throw e;
    }
  }
}

async function uploadPart(up, p) {
  const nn = String(p.order).padStart(2, '0');
  const path = `jobs/${up.note_id}/parts/${nn}.${p.ext}`;
  const rec = await store.get('blobs', p.blob_id);
  if (!rec) throw new gh.HttpError(0, 'local audio missing');
  const onProgress = (loaded, tot) => {
    const u = M.uploads.find((x) => x.id === up.id);
    if (u) { u.sent = { part: p.id, loaded, total: tot }; emitSoft(); }
  };
  for (let attempt = 0; ; attempt++) {
    const dir = await gh.getDir(`jobs/${up.note_id}/parts`, 'inbox');
    const ex = dir.find((d) => d.name === `${nn}.${p.ext}`);
    if (ex && ex.sha === p.git_sha) return;             // already uploaded (resume)
    try {
      await gh.putFile(path, 'inbox', rec.blob, { sha: ex ? ex.sha : undefined, message: `job ${up.note_id}: part ${nn}`, onProgress });
      return;
    } catch (e) {
      if (e instanceof gh.HttpError && [404, 409, 422].includes(e.status) && attempt < MAX_CONFLICT) {
        if (e.status !== 409) await gh.ensureInbox();
        await sleep(400 * (attempt + 1) * gh.getConfig().backoff); continue;
      }
      throw e;
    }
  }
}
let softT = 0;
function emitSoft() { if (Date.now() - softT > 250) { softT = Date.now(); emit(); } }

// ---- direct actions (online required) -----------------------------------------------------------------
export async function regenerateNotes(noteId) {
  const n = getNote(noteId);
  const job = {
    schema_version: 1, job_id: ulid(), note_id: noteId, kind: n.note.kind, created_at: nowIso(), source: 'pwa', language: n.note.language || 'cs',
    action: 'regenerate_notes', append: true, audio_location: 'inbox', options: { diarize: false, num_speakers: null }, parts: [],
  };
  await gh.ensureInbox();
  await putInbox(`jobs/${noteId}/job.json`, JSON.stringify(job, null, 2) + '\n', null, `job ${noteId}: regenerate`);
  await refresh();
}
export async function retryJob(noteId) {
  const j = M.inbox.jobs.get(noteId);
  if (!j || !j.job) throw new Error('no job on inbox');
  const job = { ...clone(j.job), job_id: ulid(), created_at: nowIso() };
  await putInbox(`jobs/${noteId}/job.json`, JSON.stringify(job, null, 2) + '\n', null, `job ${noteId}: retry`);
  await refresh();
}
export async function deleteNote(noteId) {
  for (const u of M.uploads.filter((x) => x.note_id === noteId)) await cancelUpload(u.id);
  const paths = ['note', 'status', 'notes', 'transcript', 'speakers'].map((k) => `notes/${noteId}/${k}.json`).filter((p) => M.files.has(p));
  const pendingOnly = paths.every((p) => !fileSha(p));
  if (!pendingOnly) await gh.commitChanges('main', `note ${noteId}: delete`, paths.filter((p) => fileSha(p)).map((p) => ({ path: p, remove: true })));
  const j = M.inbox.jobs.get(noteId);
  if (j && j.files && j.files.length) {
    try { await gh.commitChanges('inbox', `job ${noteId}: delete`, j.files.map((path) => ({ path, remove: true }))); } catch (e) { console.warn('inbox cleanup failed', e.message); }
  }
  for (const p of paths) await removeFile(p);
  M.inbox.jobs.delete(noteId);
  await saveInbox();
  emit();
  refresh();
}
