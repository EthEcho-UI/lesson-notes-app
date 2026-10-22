// Minimal GitHub REST client for the browser. Handles: spurious 401 (retry), 5xx/network backoff,
// rate limits, ETag/304, XHR upload progress. No token is ever logged or put in URLs.
import { sleep, b64ToUtf8, utf8ToB64, blobToB64 } from './util.js';

export const API = 'https://api.github.com';
export class HttpError extends Error {
  constructor(status, message, body) { super(message); this.status = status; this.body = body; }
}
export class AuthError extends HttpError {}
export class RateLimitError extends HttpError {}
export class NetworkError extends Error {}

let cfg = { token: '', repo: '', backoff: 1 };
export const configure = (c) => { cfg = { ...cfg, ...c }; };
export const getConfig = () => cfg;

function send(method, url, headers, body, onProgress) {
  if (!onProgress) {
    return fetch(url, { method, headers, body, cache: 'no-store' }).then(async (r) => ({
      status: r.status, headers: r.headers, text: await r.text(),
    }));
  }
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open(method, url);
    for (const [k, v] of Object.entries(headers)) x.setRequestHeader(k, v);
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    x.onload = () => resolve({ status: x.status, headers: { get: (n) => x.getResponseHeader(n) }, text: x.responseText });
    x.onerror = () => reject(new TypeError('network'));
    x.ontimeout = () => reject(new TypeError('timeout'));
    x.timeout = 10 * 60 * 1000;
    x.send(body);
  });
}

// opts: {body(obj|string), etag, accept, onProgress, ok:[statuses returned instead of thrown], retries}
export async function req(method, path, opts = {}) {
  if (!cfg.token) throw new AuthError(401, 'no token');
  const url = path.startsWith('http') ? path : `${API}/repos/${cfg.repo}/${path}`;
  const headers = {
    Authorization: `Bearer ${cfg.token}`,
    Accept: opts.accept || 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };
  let body;
  if (opts.body !== undefined) { headers['Content-Type'] = 'application/json'; body = typeof opts.body === 'string' ? opts.body : JSON.stringify(opts.body); }
  if (opts.etag) headers['If-None-Match'] = opts.etag;
  const maxTransient = opts.retries ?? 4;
  let transient = 0, authTries = 0;
  for (;;) {
    let res;
    try {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) throw new TypeError('offline');
      res = await send(method, url, headers, body, opts.onProgress);
    } catch (e) {
      if (e instanceof TypeError && (navigator.onLine === false || transient >= maxTransient)) throw new NetworkError(String(e.message || e));
      if (!(e instanceof TypeError)) throw e;
      await sleep(Math.min(16000, 800 * 2 ** transient) * cfg.backoff);
      transient++;
      continue;
    }
    const s = res.status;
    let data = null;
    if (res.text) { try { data = JSON.parse(res.text); } catch { data = res.text; } }
    const etag = res.headers.get('etag');
    if (s >= 200 && s < 300) return { status: s, data, etag, headers: res.headers };
    if (s === 304 || (opts.ok && opts.ok.includes(s))) return { status: s, data, etag, headers: res.headers };
    if (s === 401) {
      // b0c: GitHub sometimes returns a spurious 401 on big requests; retry before believing it
      if (authTries < 3) { await sleep([600, 1500, 3000][authTries] * cfg.backoff); authTries++; continue; }
      throw new AuthError(401, 'Bad credentials');
    }
    if (s === 403 || s === 429) {
      const msg = (data && data.message) || '';
      if (res.headers.get('x-ratelimit-remaining') === '0' || /rate limit|abuse|secondary/i.test(msg)) {
        const ra = Number(res.headers.get('retry-after'));
        if (ra && ra <= 30 && transient < 2) { await sleep(ra * 1000 * cfg.backoff); transient++; continue; }
        throw new RateLimitError(s, msg || 'rate limited', data);
      }
      throw new HttpError(s, msg || 'forbidden', data);
    }
    if (s >= 500) {
      if (transient < maxTransient) { await sleep(Math.min(16000, 800 * 2 ** transient) * cfg.backoff); transient++; continue; }
      throw new HttpError(s, 'server error', data);
    }
    throw new HttpError(s, (data && data.message) || `HTTP ${s}`, data);
  }
}

const enc = (p) => p.split('/').map(encodeURIComponent).join('/');

// ---- contents API --------------------------------------------------------------------------
// returns null when 404. text only for files whose content is delivered inline.
export async function getFile(path, ref, etag) {
  const r = await req('GET', `contents/${enc(path)}?ref=${encodeURIComponent(ref)}`, { ok: [404], etag });
  if (r.status === 404) return null;
  if (r.status === 304) return { notModified: true, etag };
  const d = r.data;
  if (Array.isArray(d)) return { dir: d, etag: r.etag };
  let text = null;
  if (d.encoding === 'base64' && typeof d.content === 'string' && (d.content || d.size === 0)) text = b64ToUtf8(d.content);
  else text = await getBlobText(d.sha);
  return { sha: d.sha, size: d.size, text, etag: r.etag };
}
export const getDir = async (path, ref) => {
  const r = await req('GET', `contents/${enc(path)}?ref=${encodeURIComponent(ref)}`, { ok: [404] });
  return r.status === 404 || !Array.isArray(r.data) ? [] : r.data;
};
export async function getBlobText(sha) {
  const r = await req('GET', `git/blobs/${sha}`);
  return b64ToUtf8(r.data.content);
}
export async function getBlobBytes(sha) {
  const r = await req('GET', `git/blobs/${sha}`);
  const bin = atob(r.data.content.replace(/\s/g, ''));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
// content: string (utf8) | Blob. Throws HttpError 409/422 for sha conflicts.
export async function putFile(path, branch, content, { sha, message, onProgress } = {}) {
  const b64 = typeof content === 'string' ? utf8ToB64(content) : await blobToB64(content);
  const body = { message: message || `update ${path.split('/')[1] || 'file'}`, content: b64, branch };
  if (sha) body.sha = sha;
  const r = await req('PUT', `contents/${enc(path)}`, { body, onProgress });
  return { sha: r.data.content.sha, commit: r.data.commit && r.data.commit.sha };
}
export const putJson = (path, branch, obj, o) => putFile(path, branch, JSON.stringify(obj, null, 2) + '\n', o);
export async function deleteFile(path, branch, sha, message) {
  await req('DELETE', `contents/${enc(path)}`, { body: { message: message || 'delete', sha, branch }, ok: [404] });
}

// ---- git data API ---------------------------------------------------------------------------
export async function getTree(branch, etag) {
  const r = await req('GET', `git/trees/${encodeURIComponent(branch)}?recursive=1`, { etag, ok: [404, 409] });
  if (r.status === 404 || r.status === 409) return { missing: true };
  if (r.status === 304) return { notModified: true };
  return { tree: r.data.tree, truncated: !!r.data.truncated, etag: r.etag };
}
export async function getRef(branch) {
  const r = await req('GET', `git/ref/heads/${encodeURIComponent(branch)}`, { ok: [404] });
  return r.status === 404 ? null : r.data.object.sha;
}
export async function ensureInbox() {
  if (await getRef('inbox')) return;
  const b = await req('POST', 'git/blobs', { body: { content: 'inbox', encoding: 'utf-8' } });
  const t = await req('POST', 'git/trees', { body: { tree: [{ path: '.keep', mode: '100644', type: 'blob', sha: b.data.sha }] } });
  const c = await req('POST', 'git/commits', { body: { message: 'init inbox', tree: t.data.sha, parents: [] } });
  await req('POST', 'git/refs', { body: { ref: 'refs/heads/inbox', sha: c.data.sha }, ok: [422] });
}
// One atomic commit with several changes. changes: [{path, text} | {path, remove:true}]
export async function commitChanges(branch, message, changes) {
  for (let attempt = 0; ; attempt++) {
    const tip = await getRef(branch);
    if (!tip) throw new HttpError(404, 'branch missing');
    const c = await req('GET', `git/commits/${tip}`);
    const tree = changes.map((ch) => (ch.remove
      ? { path: ch.path, mode: '100644', type: 'blob', sha: null }
      : { path: ch.path, mode: '100644', type: 'blob', content: ch.text }));
    const t = await req('POST', 'git/trees', { body: { base_tree: c.data.tree.sha, tree } });
    const nc = await req('POST', 'git/commits', { body: { message, tree: t.data.sha, parents: [tip] } });
    const u = await req('PATCH', `git/refs/heads/${encodeURIComponent(branch)}`, { body: { sha: nc.data.sha, force: false }, ok: [409, 422] });
    if (u.status < 300) return nc.data.sha;
    if (attempt >= 4) throw new HttpError(u.status, 'ref update conflict');
    await sleep(500 * (attempt + 1) * cfg.backoff);
  }
}
export async function repoInfo() {
  return (await req('GET', '')).data;
}
