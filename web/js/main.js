import * as store from './store.js';
import * as gh from './gh.js';
import * as model from './model.js';
import { M, onChange, emit } from './model.js';
import { setLang, t, getLang } from './i18n.js';
import { h, clear, icon } from './ui.js';
import { startPolling, refresh } from './sync.js';
import { kick } from './outbox.js';
import { recoverRecordings } from './recorder.js';
import * as views from './views.js';
import * as out from './outbox.js';

const LS = 'lessonnotes.';
const view = document.getElementById('view');
let current = null;

const ctx = {
  async saveConfig(patch) {
    Object.assign(M.cfg, patch);
    await store.kvSet('cfg', M.cfg);
    try { localStorage.setItem(LS + 'lang', M.cfg.lang); localStorage.setItem(LS + 'theme', M.cfg.theme); } catch { /* storage blocked */ }
    gh.configure({ token: M.cfg.token, repo: M.cfg.repo });
    applyLook();
    if ('token' in patch) { M.net.auth = false; M.net.error = null; }
    emit(); mountRoute();
  },
  async setDiarize(kind, value) {
    M.cfg.diarize = { ...M.cfg.diarize, [kind]: value };
    await store.kvSet('cfg', M.cfg);
    const nbs = model.notebooks(true);
    if (nbs.length) {
      const ts = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
      const s = model.settings();
      await out.writeJson('settings/app.json', {
        create: { schema_version: 1, updated_at: ts, default_language: 'cs', default_notebook_id: nbs[0].id, failed_audio_retention_days: 14, part_target_bytes: 20971520, inbox_warn_bytes: 524288000, inbox_block_bytes: 943718400, diarize_default: M.cfg.diarize },
        set: { diarize_default: { ...(s && s.diarize_default ? s.diarize_default : M.cfg.diarize), [kind]: value } },
      });
    }
  },
};

function applyLook() {
  setLang(M.cfg.lang);
  const r = document.documentElement;
  if (M.cfg.theme === 'auto') r.removeAttribute('data-theme'); else r.setAttribute('data-theme', M.cfg.theme);
  document.title = t('appName');
  document.getElementById('brandText').textContent = t('appName');
  renderNav();
}
function renderNav() {
  const nav = document.getElementById('nav');
  const hash = location.hash || '#/';
  const items = [['#/', 'home', 'book'], ['#/new', 'newNote', 'mic'], ['#/search', 'search', 'search'], ['#/settings', 'settings', 'gear']];
  clear(nav);
  for (const [href, key, ic] of items) {
    const on = href === '#/' ? /^#\/(nb\/.*)?$/.test(hash) : hash.startsWith(href);
    nav.append(h('a', { href, class: `navi ${on ? 'on' : ''}`, 'aria-current': on ? 'page' : null, id: `nav-${key}` }, icon(ic), h('span', null, t(key))));
  }
}

function route() {
  const [path, qs] = (location.hash || '#/').slice(1).split('?');
  const q = new URLSearchParams(qs || '');
  let m;
  if ((m = /^\/nb\/([^/]+)$/.exec(path))) return [views.notebookView, { id: m[1] }];
  if ((m = /^\/n\/([^/]+)\/add$/.exec(path))) return [views.composeView, { addTo: m[1] }];
  if ((m = /^\/n\/([^/]+)$/.exec(path))) return [views.noteView, { id: m[1] }];
  if (path === '/new') return [views.composeView, { nb: q.get('nb') }];
  if (path === '/search') return [views.searchView, {}];
  if (path === '/settings') return [(r, a) => views.settingsView(r, { ...a, ...ctx }), {}];
  return [views.homeView, {}];
}
function mountRoute() {
  clear(view);
  const [fn, args] = route();
  current = fn(view, args) || {};
  renderNav();
  window.scrollTo(0, 0);
}

function netDot() {
  const el = document.getElementById('netDot');
  const offline = navigator.onLine === false || (M.net.error && M.net.error.kind === 'network');
  el.className = `netdot ${M.net.auth ? 'bad' : offline ? 'off' : M.net.syncing ? 'sync' : 'on'}`;
  el.textContent = M.net.auth ? '!' : offline ? 'offline' : '';
  el.setAttribute('aria-label', offline ? t('offline') : '');
}

async function boot() {
  try { M.cfg.lang = localStorage.getItem(LS + 'lang') || M.cfg.lang; M.cfg.theme = localStorage.getItem(LS + 'theme') || M.cfg.theme; } catch { /* ignore */ }
  const saved = await store.kvGet('cfg');
  if (saved) Object.assign(M.cfg, saved);
  M.net.lastSync = await store.kvGet('lastSync');
  let backoff = 1;
  try { const b = Number(localStorage.getItem(LS + 'backoff')); if (b > 0) backoff = b; } catch { /* ignore */ }
  gh.configure({ token: M.cfg.token, repo: M.cfg.repo, backoff });
  await model.loadAll();
  await recoverRecordings();
  applyLook();
  mountRoute();
  window.addEventListener('hashchange', mountRoute);
  onChange(() => { netDot(); if (current && current.update) current.update(); });
  window.addEventListener('online', () => { M.net.online = true; M.net.error = null; emit(); kick(); });
  window.addEventListener('offline', () => { M.net.online = false; emit(); });
  M.net.online = navigator.onLine !== false;
  netDot();
  startPolling(() => (current && current.activeNote) || null);
  setInterval(() => { if (current && current.activeNote && current.update && !document.hidden) current.update(); }, 30000); // relative times
  refresh().then(() => kick());
  kick();
  if ('serviceWorker' in navigator) {
    try { await navigator.serviceWorker.register('sw.js', { scope: './' }); } catch (e) { console.warn('sw register failed', e.message); }
  }
}
boot().catch((e) => { console.error(e); view.textContent = 'Error: ' + e.message; });
