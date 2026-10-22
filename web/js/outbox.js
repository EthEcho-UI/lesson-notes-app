// Write side: optimistic small writes (outbox) and the resumable upload pipeline.
import * as gh from './gh.js';
import * as store from './store.js';
import { M, setFile, removeFile, emit, fileData, fileSha, getNote, saveInbox, languageMode as model_languageMode, contextOf as model_context } from './model.js';
import { ulid, nowIso, sha256Hex, gitBlobSha, sleep, clone } from './util.js';
import { refresh, classify } from './sync.js';
import * as images_ from './images.js';

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
// parts: [{blob, name, ext, duration, recId?}]; images: [{blob, width, height, sha256, git_sha, ext, caption}] (new photos);
// removeImages: ids to delete from note.json; captions: {id: caption|''} for existing photos.
// action 'transcribe' (audio parts) or 'regenerate_notes' (no new audio: context/photo edit).
export async function enqueueUpload({ noteId, existing, notebookId, kind, title, languageMode, context, diarize, parts = [], images = [], removeImages = [], captions = {}, action = 'transcribe', recordedAt }) {
  const jobId = ulid();
  const cur = existing ? fileData(`notes/${noteId}/note.json`) : null;
  const base = cur ? Math.max(0, ...cur.parts.map((p) => p.order)) : 0;
  const up = {
    id: jobId, note_id: noteId, step: 'note', created_at: nowIso(), kind, title, notebook_id: notebookId, language_mode: languageMode || 'mixed',
    context: clone(context), diarize: !!diarize, action, append: !!cur, recorded_at: recordedAt || nowIso(), error: null, parts: [], images: [], rec_ids: [],
    remove_images: [...removeImages], captions: clone(captions),
    remove_shas: cur ? (cur.images || []).filter((i) => removeImages.includes(i.id)).map((i) => i.sha256) : [],
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
  // photos: id = highest present + 1 (ids may have gaps), file name NN.ext with NN == id number
  let nextImg = Math.max(0, ...((cur && cur.images) || []).map((i) => Number(i.id.slice(1))));
  for (const im of images) {
    nextImg++;
    if (nextImg > 99) throw new Error('too many photos');
    const nn = String(nextImg).padStart(2, '0');
    await images_.putLocal(im.sha256, im.blob);
    up.images.push({ id: `i${nn}`, ext: im.ext, filename: `${nn}.${im.ext}`, bytes: im.blob.size, sha256: im.sha256, git_sha: im.git_sha, width: im.width, height: im.height, caption: im.caption || '', blob_id: `img:${im.sha256}`, done: false });
  }
  await store.put('uploads', up);
  M.uploads.push(up);
  for (const rid of up.rec_ids) { await store.update('recs', rid, (r) => (r ? { ...r, used: true } : undefined)); const r = M.recs.find((x) => x.id === rid); if (r) r.used = true; }
  // optimistic local note so it shows up in lists while queued/offline
  const note = cur ? clone(cur) : {
    schema_version: 2, id: noteId, kind, title, notebook_id: notebookId, created_at: up.created_at, updated_at: up.created_at,
    recorded_at: up.recorded_at, source: 'pwa', parts: [],
  };
  applyEdits(note, up);
  for (const p of up.parts) note.parts.push(notePart(p, 'uploading', null));
  await setFile(`notes/${noteId}/note.json`, fileSha(`notes/${noteId}/note.json`), note, true);
  emit();
  kick();
  return up;
}
// Applies the language/context/photo edits of an upload to a note.json object (idempotent). Touching a note makes it schema v2.
function applyEdits(n, up) {
  const before = JSON.stringify([n.language_mode, n.context, (n.images || []).map((i) => [i.id, i.caption || '', i.sha256])]);
  n.schema_version = 2;
  n.language_mode = up.language_mode;
  n.context = clone(up.context);
  n.images = (n.images || []).filter((i) => !(up.remove_images || []).includes(i.id));
  for (const i of n.images) if (i.id in (up.captions || {})) { if (up.captions[i.id]) i.caption = up.captions[i.id]; else delete i.caption; }
  for (const im of up.images || []) {
    const ex = n.images.find((x) => x.id === im.id);
    if (ex && ex.sha256 !== im.sha256) throw new gh.HttpError(409, `photo ${im.id} already exists with different content`);
    if (!ex) {
      const e = { id: im.id, filename: im.filename, bytes: im.bytes, sha256: im.sha256, width: im.width, height: im.height, uploaded_at: up.created_at, status: 'uploading' };
      if (im.caption) e.caption = im.caption;
      n.images.push(e);
    } else if (ex.status === 'failed') ex.status = 'uploading';
  }
  n.images.sort((a, b) => a.id.localeCompare(b.id));
  const after = JSON.stringify([n.language_mode, n.context, n.images.map((i) => [i.id, i.caption || '', i.sha256])]);
  if (!n.inputs_updated_at || before !== after) n.inputs_updated_at = up.created_at;
  return n;
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
  for (const im of up.images || []) await images_.dropLocal(im.sha256);
  await store.del('uploads', id);
  M.uploads = M.uploads.filter((u) => u.id !== id);
  // best-effort: mark the parts/photos failed on main so other devices do not wait for them
  try {
    const nn = fileData(`notes/${up.note_id}/note.json`);
    if (navigator.onLine !== false && fileSha(`notes/${up.note_id}/note.json`) && nn && (nn.parts.some((p) => p.status === 'uploading') || (nn.images || []).some((i) => i.status === 'uploading'))) {
      await updateNote(up.note_id, (n) => {
        for (const p of n.parts) if (up.parts.some((x) => x.id === p.id) && p.status === 'uploading') p.status = 'failed';
        for (const i of n.images || []) if ((up.images || []).some((x) => x.id === i.id) && i.status === 'uploading') i.status = 'failed';
        return n;
      });
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
      return applyEdits(n, up);
    }, create);
    for (const sha of up.remove_shas || []) await images_.dropLocal(sha);
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
    await patchUpload(id, (u) => ({ ...u, step: 'images' }));
  }
  // step 2b: new photos to inbox jobs/<id>/images/ (after the audio, before job.json)
  if (cur().step === 'images') {
    if (cur().images && cur().images.length) await gh.ensureInbox();
    for (const im of cur().images || []) {
      if (im.done) continue;
      const rec = await store.get('blobs', im.blob_id);
      if (!rec) throw new gh.HttpError(0, 'local photo missing');
      await uploadBlob(up, 'images', im.filename, rec.blob, im.git_sha, null);
      await patchUpload(id, (u) => ({ ...u, images: u.images.map((x) => (x.id === im.id ? { ...x, done: true } : x)) }));
    }
    await patchUpload(id, (u) => ({ ...u, step: 'job' }));
  }
  // step 3: job.json LAST
  if (cur().step === 'job') {
    const jobPath = `jobs/${up.note_id}/job.json`;
    const job = buildJob({
      jobId: up.id, noteId: up.note_id, kind: up.kind, createdAt: up.created_at, action: up.action || 'transcribe', append: up.append,
      languageMode: up.language_mode || 'mixed', context: up.context, diarize: up.diarize, parts: up.parts, newImageIds: (up.images || []).map((i) => i.id),
    });
    await putInbox(jobPath, JSON.stringify(job, null, 2) + '\n', (ex) => { try { return JSON.parse(ex).job_id === up.id; } catch { return false; } }, `job ${up.note_id}: submit`);
    await patchUpload(id, (u) => ({ ...u, step: 'finalize' }));
  }
  // step 4: mark parts uploaded
  if (cur().step === 'finalize') {
    const at = nowIso();
    await updateNote(up.note_id, (n) => {
      for (const p of n.parts) if (up.parts.some((x) => x.id === p.id)) { p.status = 'uploaded'; p.uploaded_at = p.uploaded_at || at; }
      for (const im of n.images || []) if ((up.images || []).some((x) => x.id === im.id) && im.status !== 'uploaded') { im.status = 'uploaded'; im.uploaded_at = at; }
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
  const rec = await store.get('blobs', p.blob_id);
  if (!rec) throw new gh.HttpError(0, 'local audio missing');
  await uploadBlob(up, 'parts', `${nn}.${p.ext}`, rec.blob, p.git_sha, p.id);
}
// dir: 'parts' | 'images'. Resumable: a file that already holds the expected git blob sha is skipped.
async function uploadBlob(up, dir, name, blob, gitSha, partId) {
  const path = `jobs/${up.note_id}/${dir}/${name}`;
  const onProgress = (loaded, tot) => {
    const u = M.uploads.find((x) => x.id === up.id);
    if (u && partId) { u.sent = { part: partId, loaded, total: tot }; emitSoft(); }
  };
  for (let attempt = 0; ; attempt++) {
    const list = await gh.getDir(`jobs/${up.note_id}/${dir}`, 'inbox');
    const ex = list.find((d) => d.name === name);
    if (ex && ex.sha === gitSha) return;             // already uploaded (resume)
    try {
      await gh.putFile(path, 'inbox', blob, { sha: ex ? ex.sha : undefined, message: `job ${up.note_id}: ${dir === 'parts' ? 'part' : 'image'} ${name.split('.')[0]}`, onProgress });
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
// job.json v2 (what the worker needs for THIS run). images[] = the COMPLETE set of photos shown to the model:
// new ones are on the inbox, earlier ones are kept on main (or still on the inbox if the worker has not kept them yet).
export function buildJob({ jobId, noteId, kind, createdAt, action, append, languageMode, context, diarize, parts, newImageIds }) {
  const note = fileData(`notes/${noteId}/note.json`) || { images: [] };
  const st = fileData(`notes/${noteId}/status.json`);
  const kept = new Set((st && st.images_kept) || []);
  const ij = M.inbox.jobs.get(noteId);
  const images = (note.images || []).filter((i) => i.status !== 'failed').map((i) => {
    const onInbox = newImageIds.includes(i.id) || (!kept.has(i.id) && ij && ij.shas && ij.shas[`jobs/${noteId}/images/${i.filename}`] && !M.mainImages.has(`notes/${noteId}/images/${i.filename}`));
    const e = { id: i.id, file: `images/${i.filename}`, bytes: i.bytes, sha256: i.sha256, width: i.width, height: i.height };
    if (i.caption) e.caption = i.caption;
    e.location = onInbox ? 'inbox' : 'main';
    return e;
  });
  const regen = action === 'regenerate_notes';
  return {
    schema_version: 2, job_id: jobId, note_id: noteId, kind, created_at: createdAt, source: 'pwa', language_mode: languageMode, context: clone(context),
    action, append: regen ? true : !!append, audio_location: 'inbox', options: { diarize: !!diarize, num_speakers: null },
    parts: regen ? [] : parts.map((p) => ({ id: p.id, order: p.order, file: `parts/${String(p.order).padStart(2, '0')}.${p.ext}`, bytes: p.bytes, sha256: p.sha256, duration: p.duration })),
    images,
  };
}
export async function regenerateNotes(noteId) {
  const n = getNote(noteId);
  const job = buildJob({
    jobId: ulid(), noteId, kind: n.note.kind, createdAt: nowIso(), action: 'regenerate_notes', append: true, languageMode: model_languageMode(n), context: model_context(n),
    diarize: false, parts: [], newImageIds: [],
  });
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
  const nn = getNote(noteId);
  const imgs = nn ? (nn.note.images || []) : [];
  const paths = ['note', 'status', 'notes', 'transcript', 'speakers'].map((k) => `notes/${noteId}/${k}.json`).filter((p) => M.files.has(p));
  const imgPaths = imgs.map((i) => `notes/${noteId}/images/${i.filename}`).concat([...M.mainImages.keys()].filter((p) => p.startsWith(`notes/${noteId}/images/`))).filter((p, i, a) => a.indexOf(p) === i && M.mainImages.has(p));
  for (const i of imgs) await images_.dropLocal(i.sha256);
  const pendingOnly = paths.every((p) => !fileSha(p));
  if (!pendingOnly) await gh.commitChanges('main', `note ${noteId}: delete`, paths.filter((p) => fileSha(p)).concat(imgPaths).map((p) => ({ path: p, remove: true })));
  for (const p of imgPaths) M.mainImages.delete(p);
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
