// Small helpers: ids, time, hashing, base64. No DOM here except FileReader.
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

export function ulid(now = Date.now()) {
  let t = now, time = '';
  for (let i = 0; i < 10; i++) { time = CROCKFORD[t % 32] + time; t = Math.floor(t / 32); }
  const rnd = new Uint8Array(16);
  crypto.getRandomValues(rnd);
  let r = '';
  for (let i = 0; i < 16; i++) r += CROCKFORD[rnd[i] % 32];
  return time + r;
}
export const ULID_RE = /^[0-9A-HJKMNP-TV-Z]{26}$/;

export const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');
export const pad2 = (n) => String(n).padStart(2, '0');
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const clone = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));

export function utf8ToB64(str) {
  const bytes = new TextEncoder().encode(str);
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
export function b64ToUtf8(b64) {
  const bin = atob(b64.replace(/\s/g, ''));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}
export function blobToB64(blob) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onerror = () => reject(fr.error);
    fr.onload = () => { const s = String(fr.result); resolve(s.slice(s.indexOf(',') + 1)); };
    fr.readAsDataURL(blob);
  });
}
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
export async function sha256Hex(blob) {
  return hex(await crypto.subtle.digest('SHA-256', await blob.arrayBuffer()));
}
// git blob sha1 = sha1("blob <len>\0" + content); lets us compare with what GitHub reports
export async function gitBlobSha(blob) {
  const head = new TextEncoder().encode(`blob ${blob.size}\0`);
  return hex(await crypto.subtle.digest('SHA-1', await new Blob([head, blob]).arrayBuffer()));
}
export function gitBlobShaText(text) { return gitBlobSha(new Blob([text])); }

export function fmtDuration(sec) {
  if (sec == null || !isFinite(sec)) return '–';
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}
export function fmtBytes(n) {
  if (n == null) return '–';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} kB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}
export function fmtDate(iso, lang) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleString(lang, { dateStyle: 'medium', timeStyle: 'short' }); } catch { return iso; }
}
export function fmtRelative(iso, lang) {
  const diff = (new Date(iso).getTime() - Date.now()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(lang, { numeric: 'auto' });
  const a = Math.abs(diff);
  if (a < 60) return rtf.format(Math.round(diff), 'second');
  if (a < 3600) return rtf.format(Math.round(diff / 60), 'minute');
  if (a < 86400) return rtf.format(Math.round(diff / 3600), 'hour');
  return rtf.format(Math.round(diff / 86400), 'day');
}
export function norm(s) {
  return String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}
export const AUDIO_EXT = ['opus', 'ogg', 'm4a', 'mp3', 'wav', 'webm', 'flac'];
export function extFor(name, mime) {
  const m = /\.([a-z0-9]+)$/i.exec(name || '');
  const e = m && m[1].toLowerCase();
  if (e && AUDIO_EXT.includes(e)) return e;
  const t = (mime || '').toLowerCase();
  if (t.includes('webm')) return 'webm';
  if (t.includes('ogg')) return 'ogg';
  if (t.includes('opus')) return 'opus';
  if (t.includes('mp4') || t.includes('m4a') || t.includes('aac')) return 'm4a';
  if (t.includes('mpeg') || t.includes('mp3')) return 'mp3';
  if (t.includes('wav')) return 'wav';
  if (t.includes('flac')) return 'flac';
  return null;
}
export function probeDuration(blob) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const a = new Audio();
    let done = false;
    const fin = (v) => { if (done) return; done = true; URL.revokeObjectURL(url); resolve(v); };
    a.preload = 'metadata';
    a.onloadedmetadata = () => fin(isFinite(a.duration) ? Math.round(a.duration * 10) / 10 : null);
    a.onerror = () => fin(null);
    setTimeout(() => fin(null), 4000);
    a.src = url;
  });
}
