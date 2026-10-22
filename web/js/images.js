// Photos: client-side resize (EXIF orientation respected), hashing, thumbnails, and lookup of stored photos
// (local copy -> main branch -> inbox). Photo bytes are verified against the sha256 listed in note.json.
import * as gh from './gh.js';
import * as store from './store.js';
import { M } from './model.js';
import { sha256Hex, gitBlobSha } from './util.js';

export const MAX_EDGE = 1600, QUALITY = 0.8, MAX_BYTES = 614400, MAX_IMAGES = 12;
const keyOf = (sha) => `img:${sha}`;

async function decode(file) {
  if (typeof createImageBitmap === 'function') {
    try { const b = await createImageBitmap(file, { imageOrientation: 'from-image' }); return { src: b, w: b.width, h: b.height, close: () => b.close && b.close() }; } catch { /* fall through */ }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = 'async';
    await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error('decode')); img.src = url; });
    return { src: img, w: img.naturalWidth, h: img.naturalHeight, close: () => {} };
  } finally { URL.revokeObjectURL(url); }
}
const toBlob = (canvas, type, q) => new Promise((res) => canvas.toBlob(res, type, q));
function draw(dec, w, h) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const g = c.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);      // JPEG has no alpha
  g.drawImage(dec.src, 0, 0, w, h);
  return c;
}

// -> {blob, width, height, sha256, git_sha, ext:'jpg', thumb:Blob}
export async function prepareImage(file, { maxEdge = MAX_EDGE, quality = QUALITY, maxBytes = MAX_BYTES } = {}) {
  const dec = await decode(file);
  try {
    if (!dec.w || !dec.h) throw new Error('empty image');
    let edge = maxEdge, q = quality, blob = null, w = 0, h = 0;
    for (let i = 0; i < 12; i++) {
      const k = Math.min(1, edge / Math.max(dec.w, dec.h));
      w = Math.max(1, Math.round(dec.w * k)); h = Math.max(1, Math.round(dec.h * k));
      blob = await toBlob(draw(dec, w, h), 'image/jpeg', q);
      if (!blob) throw new Error('encode');
      if (blob.size <= maxBytes) break;
      if (q > 0.5) q -= 0.1; else edge = Math.round(edge * 0.85);
    }
    if (blob.size > maxBytes) throw new Error('too large');
    const tk = Math.min(1, 240 / Math.max(w, h));
    const thumb = await toBlob(draw(dec, Math.max(1, Math.round(w * tk)), Math.max(1, Math.round(h * tk))), 'image/jpeg', 0.7);
    return { blob, width: w, height: h, sha256: await sha256Hex(blob), git_sha: await gitBlobSha(blob), ext: 'jpg', thumb };
  } finally { dec.close(); }
}

export const putLocal = (sha, blob) => store.put('blobs', { id: keyOf(sha), blob });
export const dropLocal = (sha) => store.del('blobs', keyOf(sha));

const MIME = { jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp' };
const extOf = (f) => (/\.([a-z0-9]+)$/i.exec(f) || [])[1]?.toLowerCase() || 'jpg';
const memBlob = new Map(); // sha256 -> Blob
const memUrl = new Map();  // sha256 -> object URL
const pending = new Map(); // sha256 -> Promise<Blob|null>

// Blob for a note.json image entry or null. Never throws (offline -> null).
export function imageBlob(noteId, im) {
  if (memBlob.has(im.sha256)) return Promise.resolve(memBlob.get(im.sha256));
  if (pending.has(im.sha256)) return pending.get(im.sha256);
  const p = (async () => {
    try {
      const rec = await store.get('blobs', keyOf(im.sha256));
      if (rec && rec.blob) return rec.blob;
      const type = MIME[extOf(im.filename)] || 'application/octet-stream';
      const sources = [M.mainImages.get(`notes/${noteId}/images/${im.filename}`)];
      const job = M.inbox.jobs.get(noteId);
      if (job && job.shas) sources.push(job.shas[`jobs/${noteId}/images/${im.filename}`]);
      for (const sha of sources) {
        if (!sha) continue;
        const bytes = await gh.getBlobBytes(sha);
        const blob = new Blob([bytes], { type });
        if ((await sha256Hex(blob)) !== im.sha256) continue;   // wrong/old file under that name
        await store.put('blobs', { id: keyOf(im.sha256), blob });
        return blob;
      }
    } catch (e) { console.warn('image load failed', e.message); }
    return null;
  })().then((b) => { pending.delete(im.sha256); if (b) memBlob.set(im.sha256, b); return b; });
  pending.set(im.sha256, p);
  return p;
}
export async function imageUrl(noteId, im) {
  if (memUrl.has(im.sha256)) return memUrl.get(im.sha256);
  const b = await imageBlob(noteId, im);
  if (!b) return null;
  const u = URL.createObjectURL(b);
  memUrl.set(im.sha256, u);
  return u;
}
