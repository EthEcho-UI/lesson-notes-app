// Browser recorder. Chunks go to IndexedDB every few seconds so a crash/tab kill keeps the lecture.
import * as store from './store.js';
import { M, emit } from './model.js';
import { ulid, nowIso } from './util.js';

const TIMESLICE = 3000;
const HARD_STOP_BYTES = 80 * 1000 * 1000; // contract: 90 MB per part max

export const canRecord = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

function pickMime() {
  for (const m of ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4']) {
    try { if (MediaRecorder.isTypeSupported(m)) return m; } catch { /* ignore */ }
  }
  return '';
}

export class Recorder {
  constructor(onTick) {
    this.onTick = onTick || (() => {});
    this.state = 'idle'; // idle | recording | paused
    this.elapsed = 0;
    this.rec = null;
    this.wake = null;
  }
  async start() {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: true } });
    const mime = pickMime();
    const mr = new MediaRecorder(stream, { ...(mime ? { mimeType: mime } : {}), audioBitsPerSecond: 24000 });
    this.stream = stream; this.mr = mr;
    this.rec = { id: ulid(), started_at: nowIso(), mime: mr.mimeType || mime || 'audio/webm', status: 'recording', duration: 0, bytes: 0, chunks: 0 };
    await store.put('recs', this.rec);
    M.recs.push(this.rec); emit();
    let seq = 0;
    this.pending = Promise.resolve();
    mr.ondataavailable = (e) => {
      if (!e.data || !e.data.size) return;
      const s = seq++;
      this.rec.bytes += e.data.size; this.rec.chunks = seq;
      this.pending = this.pending.then(() => store.put('chunks', { rid: this.rec.id, seq: s, data: e.data })).catch(() => {});
      if (this.rec.bytes > HARD_STOP_BYTES) this.stop();
    };
    this.stopped = new Promise((res) => { mr.onstop = res; });
    mr.start(TIMESLICE);
    this.state = 'recording';
    this.lastTick = Date.now();
    this.timer = setInterval(() => this.tick(), 500);
    this.saveT = setInterval(() => this.saveMeta(), 5000);
    await this.lock();
    this.vis = () => { if (document.visibilityState === 'visible' && this.state === 'recording') this.lock(); };
    document.addEventListener('visibilitychange', this.vis);
    this.onTick(this);
  }
  tick() {
    const now = Date.now();
    if (this.state === 'recording') this.elapsed += (now - this.lastTick) / 1000;
    this.lastTick = now;
    this.onTick(this);
  }
  saveMeta() {
    if (!this.rec) return;
    const dur = Math.round(this.elapsed * 10) / 10;
    this.rec.duration = dur;
    return store.update('recs', this.rec.id, (r) => (r ? { ...r, duration: dur, bytes: this.rec.bytes } : undefined)).catch(() => {});
  }
  async lock() {
    try { if (navigator.wakeLock) { this.wake = await navigator.wakeLock.request('screen'); } } catch { /* not granted; non-fatal */ }
  }
  unlock() { try { if (this.wake) this.wake.release(); } catch { /* ignore */ } this.wake = null; }
  pause() { if (this.state !== 'recording') return; this.mr.pause(); this.state = 'paused'; this.unlock(); this.onTick(this); }
  resume() { if (this.state !== 'paused') return; this.mr.resume(); this.lastTick = Date.now(); this.state = 'recording'; this.lock(); this.onTick(this); }
  async stop() {
    if (this.state === 'idle') return null;
    this.tick();
    const mr = this.mr;
    this.state = 'idle';
    clearInterval(this.timer); clearInterval(this.saveT);
    document.removeEventListener('visibilitychange', this.vis);
    this.unlock();
    if (mr.state !== 'inactive') mr.stop();
    await this.stopped;
    this.stream.getTracks().forEach((t) => t.stop());
    await this.pending;
    const rec = await finalizeRecording(this.rec.id, this.elapsed);
    this.rec = null;
    this.onTick(this);
    return rec;
  }
}

// Assemble chunks into one blob (also used to recover a recording after a crash)
export async function finalizeRecording(id, duration) {
  const chunks = await store.chunksOf(id);
  const rec = await store.get('recs', id);
  if (!rec) return null;
  if (!chunks.length) { await store.del('recs', id); M.recs = M.recs.filter((r) => r.id !== id); emit(); return null; }
  const blob = new Blob(chunks.map((c) => c.data), { type: rec.mime });
  await store.put('blobs', { id: `rec:${id}`, blob });
  const next = await store.update('recs', id, (r) => ({ ...r, status: 'done', duration: duration ?? r.duration, bytes: blob.size, blob_id: `rec:${id}` }));
  await store.deleteChunks(id);
  const i = M.recs.findIndex((r) => r.id === id);
  if (i >= 0) M.recs[i] = next; else M.recs.push(next);
  emit();
  return next;
}
export async function recoverRecordings() {
  const all = await store.getAll('recs');
  for (const r of all) if (r.status === 'recording') await finalizeRecording(r.id, r.duration);
  M.recs = await store.getAll('recs');
}
export const freeRecs = () => M.recs.filter((r) => r.status === 'done' && !r.used);
export async function deleteRec(id) {
  const r = M.recs.find((x) => x.id === id);
  if (r && r.blob_id) await store.del('blobs', r.blob_id);
  await store.deleteChunks(id);
  await store.del('recs', id);
  M.recs = M.recs.filter((x) => x.id !== id);
  emit();
}
