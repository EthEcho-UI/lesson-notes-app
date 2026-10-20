import { h, clear, btn, iconBtn, icon, toast, promptDialog, confirmDialog } from './ui.js';
import { t, getLang, labels } from './i18n.js';
import * as model from './model.js';
import { M, onChange } from './model.js';
import * as out from './outbox.js';
import * as sync from './sync.js';
import * as gh from './gh.js';
import * as store from './store.js';
import { Recorder, canRecord, freeRecs, deleteRec } from './recorder.js';
import { ulid, fmtDuration, fmtBytes, fmtDate, fmtRelative, extFor, probeDuration } from './util.js';

const COLORS = ['#2e7d6b', '#b5563a', '#4a6fa5', '#8a6d3b', '#7b5ea7', '#a2495f', '#5c7a3a'];
const go = (hash) => { location.hash = hash; };

// ---- shared bits -------------------------------------------------------------------------------------
export function statusText(d) {
  const lang = getLang();
  switch (d.code) {
    case 'uploading': {
      const u = d.upload; const done = u.parts.filter((p) => p.done).length;
      return `${t('st_uploading')} (${done}/${u.parts.length})`;
    }
    case 'upload_waiting': return t('st_upload_waiting');
    case 'transcribing': case 'diarizing': {
      const p = d.progress;
      return `${t('st_' + d.code)}${p ? ' · ' + t('partOf', { n: p.part_index, m: p.part_total }) + (p.percent != null ? ` ${Math.round(p.percent)} %` : '') : ''}`;
    }
    case 'waiting': {
      const lp = model.laptopState();
      let s = t('st_waiting');
      if (d.ahead) s += ` · ${t('jobsAhead', { n: d.ahead })}`;
      else if (d.queuePosition) s += ` · ${t('queuePos', { n: d.queuePosition })}`;
      if (lp) s += ` · ${t('laptopLastSeen', { t: fmtRelative(lp.last_seen, lang) })}`;
      return s;
    }
    default: return t('st_' + d.code);
  }
}
function badge(n) {
  const d = model.derive(n);
  const kids = [h('span', { class: 'dot' }), statusText(d)];
  if (d.updating && d.code !== 'done') kids.push(h('em', { class: 'upd' }, ` (${t('updating')})`));
  return h('span', { class: `badge b-${d.code}` }, kids);
}
function noteRow(n) {
  const nb = model.notebook(n.note.notebook_id);
  return h('a', { class: 'row-card', href: `#/n/${n.id}` },
    h('div', { class: 'rc-main' },
      h('strong', { class: 'rc-title' }, n.note.title),
      h('span', { class: 'rc-sub' }, `${nb ? nb.name + ' · ' : ''}${n.note.kind === 'meeting' ? t('kindMeeting') : t('kindLesson')} · ${fmtDate(n.note.recorded_at || n.note.created_at, getLang())}`)),
    badge(n));
}
function banners() {
  const box = h('div', { class: 'banners' });
  const add = (txt, cls = '', action) => box.append(h('div', { class: `banner ${cls}`, role: 'status' }, h('span', null, txt), action || null));
  if (!M.cfg.token) add(t('needToken'), 'warn', btn(t('goSettings'), () => go('#/settings'), { cls: 'sm' }));
  else if (M.net.auth) add(t('authBad'), 'bad', btn(t('goSettings'), () => go('#/settings'), { cls: 'sm' }));
  else if (!M.net.online || (M.net.error && M.net.error.kind === 'network')) add(t('offline'), 'info');
  else if (M.net.error && M.net.error.kind === 'rate') add(t('rateLimited'), 'warn');
  if (M.outbox.length) add(t('pendingWrites', { n: M.outbox.length }), 'info');
  const free = freeRecs();
  if (free.length) add(`${t('unsavedRecs')}: ${free.length}`, 'warn', btn(t('useRec'), () => go('#/new'), { cls: 'sm' }));
  if (M.inbox.loaded && M.inbox.bytes > (model.settings() ? model.settings().inbox_warn_bytes : 524288000)) add(t('inboxWarn', { size: fmtBytes(M.inbox.bytes) }), 'warn');
  return box;
}
export function laptopLine() {
  const lp = model.laptopState();
  if (!lp) return h('span', { class: 'muted' }, t('laptopUnknown'));
  const rel = fmtRelative(lp.last_seen, getLang());
  const q = lp.queue ? ` · ${t('queueLen', { n: lp.queue.jobs_pending })}` : '';
  return h('span', { class: lp.offline ? 'muted' : 'ok' }, h('span', { class: `dot ${lp.offline ? 'off' : 'on'}` }), (lp.offline ? t('laptopOffline', { t: rel }) : t('laptopOnline')) + q);
}
const empty = (txt) => h('p', { class: 'empty' }, txt);
const pageHead = (title, left, right) => h('div', { class: 'phead' }, left || null, h('h1', null, title), right || null);

// ---- home ------------------------------------------------------------------------------------------------
export function homeView(root) {
  function render() {
    clear(root);
    const nbs = model.notebooks();
    const recent = model.allNotes().slice(0, 8);
    root.append(pageHead(t('notebooks'), null, btn(t('newNotebook'), createNotebook, { cls: 'sm', ic: 'plus' })), banners(),
      h('div', { class: 'laptop' }, laptopLine()),
      nbs.length ? h('ul', { class: 'cards' }, nbs.map((nb) => {
        const cnt = model.notesOf(nb.id).length;
        const a = h('a', { class: 'nb-card', href: `#/nb/${nb.id}` }, h('span', { class: 'swatch' }), h('strong', null, nb.name), h('span', { class: 'muted' }, t(cnt === 1 ? 'notesCount1' : cnt >= 2 && cnt <= 4 && getLang() === 'cs' ? 'notesCount2' : 'notesCount', { n: cnt })));
        if (nb.color) a.firstChild.style.setProperty('background', nb.color);
        return h('li', null, a);
      })) : empty(t('noNotebooks')),
      recent.length ? h('section', null, h('h2', null, t('recent')), h('div', { class: 'list' }, recent.map(noteRow))) : null);
  }
  render();
  return { update: render };
}
export async function createNotebook() {
  const r = await promptDialog({ title: t('newNotebook'), label: t('notebookName') });
  if (!r) return null;
  const all = model.notebooks(true);
  const id = ulid(); const ts = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  await out.writeJson(`notebooks/${id}.json`, { create: { schema_version: 1, id, name: r.value, created_at: ts, updated_at: ts, archived: false, color: COLORS[all.length % COLORS.length], sort: (Math.max(0, ...all.map((n) => n.sort || 0)) + 10) } });
  return id;
}

// ---- notebook ----------------------------------------------------------------------------------------------
export function notebookView(root, { id }) {
  function render() {
    clear(root);
    const nb = model.notebook(id);
    if (!nb) { root.append(pageHead(t('notebook'), iconBtn('back', t('back'), () => go('#/'))), empty(t('notFound'))); return; }
    const notes = model.notesOf(id);
    root.append(
      pageHead(nb.name, iconBtn('back', t('back'), () => go('#/')), h('div', { class: 'row' },
        iconBtn('more', t('rename'), async () => {
          const r = await promptDialog({ title: t('rename'), label: t('notebookName'), value: nb.name });
          if (r) out.writeJson(`notebooks/${id}.json`, { set: { name: r.value } });
        }),
        btn(t('newNote'), () => go(`#/new?nb=${id}`), { cls: 'primary sm', ic: 'mic' }))),
      banners(), notes.length ? h('div', { class: 'list' }, notes.map(noteRow)) : empty(t('noNotes')),
      h('div', { class: 'row end pad' }, btn(t('archive'), async () => { await out.writeJson(`notebooks/${id}.json`, { set: { archived: true } }); go('#/'); }, { cls: 'ghost sm' })));
  }
  render();
  return { update: render };
}

// ---- compose (new note / add parts) ---------------------------------------------------------------------
const draft = { nb: '', kind: 'lesson', title: '', parts: [], diarize: null, busy: false };
let recorder = null;
function resetDraft() { Object.assign(draft, { title: '', parts: [], diarize: null, busy: false }); }

export function composeView(root, { addTo, nb: nbParam }) {
  const target = addTo ? model.getNote(addTo) : null;
  if (!target && addTo) { root.append(empty(t('notFound'))); return {}; }
  if (!target && nbParam) draft.nb = nbParam;
  let timerEl = null;
  const diarizeFor = (kind) => { const s = model.settings(); return (s && s.diarize_default ? s.diarize_default[kind] : M.cfg.diarize[kind]); };

  const onTick = (r) => { if (timerEl) timerEl.textContent = fmtDuration(r.elapsed); if (!r.state || r.state === 'idle') render(); };

  async function addRecToDraft(rec) {
    const rb = await store.get('blobs', rec.blob_id);
    if (!rb) return;
    draft.parts.push({ key: ulid(), recId: rec.id, blob: rb.blob, name: t('record'), ext: extFor('', rec.mime), duration: rec.duration, source: 'rec' });
  }
  async function startRec() {
    if (!canRecord()) return toast(t('recUnsupported'), 'bad');
    recorder = new Recorder(onTick);
    try { await recorder.start(); } catch (e) { recorder = null; toast(t('micDenied'), 'bad'); return; }
    render();
  }
  async function stopRec() {
    const r = recorder; if (!r) return;
    const rec = await r.stop(); recorder = null;
    if (rec) await addRecToDraft(rec);
    render();
  }
  async function importFiles(files) {
    for (const f of files) {
      const ext = extFor(f.name, f.type);
      if (!ext) { toast(t('unsupportedFile', { name: f.name }), 'bad'); continue; }
      if (f.size > out.TARGET_MAX) { toast(t('tooBig', { name: f.name }), 'bad'); continue; }
      draft.parts.push({ key: ulid(), blob: f, name: f.name, ext, duration: await probeDuration(f), source: 'file' });
    }
    render();
  }
  async function submit() {
    if (draft.busy) return;
    const nbId = target ? target.note.notebook_id : draft.nb;
    if (!target && !draft.title.trim()) return toast(t('needTitle'), 'bad');
    if (!nbId) return toast(t('needNotebook'), 'bad');
    if (!draft.parts.length) return toast(t('needParts'), 'bad');
    if (recorder && recorder.state !== 'idle') await stopRec();
    const s = model.settings();
    const block = s ? s.inbox_block_bytes : 943718400;
    if (M.inbox.loaded && M.inbox.bytes >= block) return toast(t('inboxFull'), 'bad');
    draft.busy = true; render();
    try {
      const kind = target ? target.note.kind : draft.kind;
      const noteId = target ? target.id : ulid();
      await out.enqueueUpload({
        noteId, existing: !!target, notebookId: nbId, kind, title: target ? target.note.title : draft.title.trim(),
        language: target ? target.note.language : (s && s.default_language) || 'cs', diarize: draft.diarize ?? diarizeFor(kind), parts: draft.parts,
      });
      resetDraft();
      go(`#/n/${noteId}`);
    } catch (e) { draft.busy = false; render(); toast(t('error', { msg: e.message }), 'bad'); }
  }

  function partsEditor() {
    const wrap = h('section', { class: 'parts' }, h('h2', null, t('audioParts')));
    const rs = recorder ? recorder.state : 'idle';
    timerEl = h('span', { class: 'timer', 'aria-live': 'off' }, fmtDuration(recorder ? recorder.elapsed : 0));
    if (rs === 'idle') {
      wrap.append(h('div', { class: 'rec-actions' },
        btn(t('record'), startRec, { cls: 'primary big', ic: 'mic', attrs: { id: 'recStart' } }),
        h('label', { class: 'btn' }, icon('file'), h('span', null, t('importFiles')),
          h('input', { type: 'file', id: 'fileIn', accept: 'audio/*,.opus,.ogg,.m4a,.mp3,.wav,.webm,.flac', multiple: true, class: 'visually-hidden', onchange: (e) => { importFiles([...e.target.files]); e.target.value = ''; } }))));
    } else {
      wrap.append(h('div', { class: 'recbox' },
        h('div', { class: 'rec-state' }, h('span', { class: `rec-dot ${rs}` }), h('strong', null, rs === 'paused' ? t('paused') : t('recording')), timerEl),
        h('div', { class: 'row' },
          rs === 'recording' ? btn(t('pause'), () => { recorder.pause(); render(); }, { ic: 'pause', attrs: { id: 'recPause' } }) : btn(t('resume'), () => { recorder.resume(); render(); }, { ic: 'play', attrs: { id: 'recResume' } }),
          btn(t('stop'), stopRec, { cls: 'primary', ic: 'stop', attrs: { id: 'recStop' } }))));
    }
    wrap.append(h('p', { class: 'hint warnbox' }, t('lockWarn')));
    const free = freeRecs().filter((r) => !draft.parts.some((p) => p.recId === r.id));
    if (free.length) {
      wrap.append(h('div', { class: 'tray' }, h('h3', null, t('unsavedRecs')), h('p', { class: 'muted' }, t('unsavedRecsHint')),
        free.map((r) => h('div', { class: 'part' }, h('div', { class: 'p-main' }, h('strong', null, fmtDate(r.started_at, getLang())), h('span', { class: 'muted' }, `${fmtDuration(r.duration)} · ${fmtBytes(r.bytes)}`)),
          btn(t('useRec'), async () => { await addRecToDraft(r); render(); }, { cls: 'sm' }), iconBtn('x', t('delete'), async () => { if (await confirmDialog(t('delete') + '?')) deleteRec(r.id).then(render); })))));
    }
    if (!draft.parts.length) wrap.append(empty(t('noParts')));
    else wrap.append(h('ol', { class: 'plist' }, draft.parts.map((p, i) => {
      const au = h('audio', { controls: true, preload: 'none', class: 'au' });
      au.src = URL.createObjectURL(p.blob);
      return h('li', { class: 'part' }, h('div', { class: 'p-main' }, h('strong', null, `${t('part')} ${i + 1}`), h('span', { class: 'muted' }, `${p.name} · ${fmtDuration(p.duration)} · ${fmtBytes(p.blob.size)}`), au),
        h('div', { class: 'p-act' },
          iconBtn('up', t('moveUp'), () => { if (i > 0) { [draft.parts[i - 1], draft.parts[i]] = [draft.parts[i], draft.parts[i - 1]]; render(); } }, i === 0 ? 'dis' : ''),
          iconBtn('down', t('moveDown'), () => { if (i < draft.parts.length - 1) { [draft.parts[i + 1], draft.parts[i]] = [draft.parts[i], draft.parts[i + 1]]; render(); } }, i === draft.parts.length - 1 ? 'dis' : ''),
          iconBtn('x', t('remove'), () => { draft.parts.splice(i, 1); render(); })));
    })));
    return wrap;
  }

  function render() {
    clear(root);
    const nbs = model.notebooks();
    if (!target && !draft.nb && nbs.length) draft.nb = nbs[0].id;
    const form = h('div', { class: 'form' });
    if (!target) {
      form.append(
        h('label', { class: 'lbl' }, t('pickNotebook'), h('div', { class: 'row' },
          h('select', { class: 'inp', id: 'nbSel', onchange: (e) => { draft.nb = e.target.value; } }, nbs.map((n) => h('option', { value: n.id, selected: n.id === draft.nb }, n.name))),
          btn('', async () => { const id = await createNotebook(); if (id) { draft.nb = id; render(); } }, { ic: 'plus', attrs: { 'aria-label': t('newNotebook'), id: 'nbNew' } }))),
        h('div', { class: 'seg', role: 'radiogroup', 'aria-label': t('kind') },
          ['lesson', 'meeting'].map((k) => h('button', { type: 'button', role: 'radio', 'aria-checked': draft.kind === k ? 'true' : 'false', class: `segb ${draft.kind === k ? 'on' : ''}`, id: `kind-${k}`, onclick: () => { draft.kind = k; draft.diarize = null; render(); } }, k === 'lesson' ? t('kindLesson') : t('kindMeeting')))),
        h('label', { class: 'lbl' }, t('title'), h('input', { class: 'inp', id: 'titleIn', type: 'text', maxlength: 200, placeholder: t('titlePh'), value: draft.title, oninput: (e) => { draft.title = e.target.value; } })));
      const dz = draft.diarize ?? diarizeFor(draft.kind);
      form.append(h('label', { class: 'chk' }, h('input', { type: 'checkbox', id: 'diarizeIn', checked: dz, onchange: (e) => { draft.diarize = e.target.checked; } }), h('span', null, t('diarize'), h('small', { class: 'muted' }, ' ' + t('diarizeHint')))));
    } else form.append(h('p', { class: 'muted' }, `${target.note.title} · ${t('addPartsHint')}`));
    form.append(partsEditor(),
      h('div', { class: 'row end pad' }, h('button', { type: 'button', class: 'btn primary big', id: 'submitBtn', disabled: draft.busy, onclick: submit }, draft.busy ? t('preparing') : t('startUpload'))));
    root.append(pageHead(target ? t('addPartsTitle') : t('newNote'), iconBtn('back', t('back'), () => history.back())), banners(), form);
  }
  render();
  return { update: () => { /* compose re-renders itself; avoid clobbering inputs */ } };
}

// ---- note ----------------------------------------------------------------------------------------------------------
let noteTab = 'notes';
export function noteView(root, { id }) {
  let withTr = false;
  function render() {
    clear(root);
    const n = model.getNote(id);
    if (!n) { root.append(pageHead('', iconBtn('back', t('back'), () => go('#/'))), empty(t('notFound'))); return; }
    const d = model.derive(n);
    const back = iconBtn('back', t('back'), () => go(`#/nb/${n.note.notebook_id}`));
    root.append(pageHead(n.note.title, back), banners(), statusCard(n, d));
    const tabs = h('div', { class: 'tabs', role: 'tablist' }, [['notes', 'tabNotes'], ['transcript', 'tabTranscript'], ['audio', 'tabAudio']].map(([k, l]) =>
      h('button', { type: 'button', role: 'tab', id: `tab-${k}`, 'aria-selected': noteTab === k ? 'true' : 'false', class: `tab ${noteTab === k ? 'on' : ''}`, onclick: () => { noteTab = k; render(); } }, t(l))));
    root.append(tabs, h('div', { class: 'tabpanel', role: 'tabpanel' }, noteTab === 'notes' ? notesTab(n) : noteTab === 'transcript' ? transcriptTab(n) : audioTab(n, d)));
    root.append(actions(n, d));
  }
  function statusCard(n, d) {
    const c = h('div', { class: `status s-${d.code}`, id: 'statusCard' }, h('div', { class: 'row between' }, badge(n)));
    if (d.code === 'uploading' || d.code === 'upload_waiting') {
      const u = d.upload; const total = u.parts.reduce((s, p) => s + p.bytes, 0);
      const doneB = u.parts.filter((p) => p.done).reduce((s, p) => s + p.bytes, 0) + (u.sent && !u.parts.find((p) => p.id === u.sent.part).done ? u.sent.loaded * (u.parts.find((p) => p.id === u.sent.part).bytes / u.sent.total) : 0);
      const pct = u.step === 'parts' ? Math.min(99, Math.round((doneB / total) * 100)) : u.step === 'note' ? 2 : 100;
      c.append(h('progress', { max: 100, value: pct, 'aria-label': t('st_uploading') }), h('p', { class: 'muted' }, `${t('uploadStep_' + u.step)} · ${pct} %`));
      if (!M.net.online || navigator.onLine === false) c.append(h('p', { class: 'muted' }, t('offlineQueued')));
      if (u.error) c.append(h('p', { class: 'err' }, t('uploadError', { msg: u.error.message })));
      c.append(h('div', { class: 'row' }, u.error ? btn(t('resumeUpload'), () => out.resumeUpload(u.id), { cls: 'sm', attrs: { id: 'resumeBtn' } }) : null, btn(t('cancelUpload'), async () => { if (await confirmDialog(t('cancelUpload') + '?')) out.cancelUpload(u.id); }, { cls: 'ghost sm' })));
    }
    if (d.code === 'transcribing' || d.code === 'diarizing') { const p = d.progress; if (p && p.percent != null) c.append(h('progress', { max: 100, value: p.percent })); }
    if (d.code === 'waiting') c.append(h('p', { class: 'muted' }, laptopLine()));
    if (d.code === 'upload_incomplete') c.append(h('p', { class: 'muted' }, t('st_upload_incomplete')));
    if (d.code === 'failed') {
      c.append(h('p', { class: 'err' }, d.error ? `${d.error.code}: ${d.error.message}` : ''));
      if (model.canRetry(n)) c.append(btn(t('retryJob'), async () => { try { await out.retryJob(id); toast(t('retrySent')); } catch (e) { toast(t('error', { msg: e.message }), 'bad'); } }, { cls: 'primary sm', attrs: { id: 'retryBtn' } }));
      else c.append(h('p', { class: 'muted' }, t('needReupload')));
    }
    return c;
  }
  function list(head, arr) {
    if (!arr || !arr.length) return null;
    return h('section', { class: 'blk' }, h('h2', null, head), h('ul', null, arr.map((x) => h('li', null, x))));
  }
  function notesTab(n) {
    const c = n.notes && n.notes.content;
    if (!c) return empty(t('noNotesYet'));
    return h('div', { class: 'notes' }, c.summary ? h('section', { class: 'blk' }, h('h2', null, t('summary')), h('p', null, c.summary)) : null,
      list(t('keyPoints'), c.key_points),
      c.definitions && c.definitions.length ? h('section', { class: 'blk' }, h('h2', null, t('definitions')), h('dl', null, c.definitions.map((x) => [h('dt', null, x.term), h('dd', null, x.definition)]))) : null,
      n.note.kind === 'meeting' ? [list(t('decisions'), c.decisions), c.action_items && c.action_items.length ? h('section', { class: 'blk' }, h('h2', null, t('actionItems')), h('ul', null, c.action_items.map((a) => h('li', null, a.task, (a.owner || a.due) ? h('span', { class: 'muted' }, ` (${[a.owner, a.due].filter(Boolean).join(', ')})`) : null)))) : null] : null,
      list(t('todo'), c.todo), list(t('questionsOpen'), c.questions_open));
  }
  function transcriptTab(n) {
    if (!n.transcript) return empty(t('noTranscriptYet'));
    return h('div', { class: 'tr' }, n.transcript.parts.map((p) => h('section', { class: 'blk', id: `part-${p.part_id}` }, h('h2', null, `${t('part')} ${p.order}`, h('span', { class: 'muted' }, ` · ${fmtDuration(p.duration)}`)),
      p.segments.map((s) => h('p', { class: 'seg-row' }, h('span', { class: 'ts' }, fmtDuration(s.start)),
        s.speaker ? h('button', { type: 'button', class: 'spk', onclick: () => renameSpeaker(n, p.part_id, s.speaker) }, model.speakerName(n, p.part_id, s.speaker)) : null, ' ', s.text)))));
  }
  async function renameSpeaker(n, partId, label) {
    const cur = model.speakerName(n, partId, label);
    const r = await promptDialog({ title: t('renameSpeaker'), label: t('speakerName'), value: cur, checkbox: { label: t('applyAllParts'), checked: false } });
    if (!r) return;
    const names = { ...((n.speakers && n.speakers.names) || {}) };
    if (r.checked) { for (const p of n.transcript.parts) if (p.segments.some((s) => s.speaker === label)) names[`${p.part_id}:${label}`] = r.value; } else names[`${partId}:${label}`] = r.value;
    await out.writeJson(`notes/${id}/speakers.json`, { create: { schema_version: 1, note_id: id, updated_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'), names: {} }, set: { names } });
  }
  function audioTab(n, d) {
    const st = n.status;
    return h('div', { class: 'blk' }, h('p', { class: 'muted' }, t('audioInfoHint')),
      h('table', { class: 'tbl' }, h('thead', null, h('tr', null, ['part', 'duration', 'size', 'uploadedAt', 'audioState'].map((k) => h('th', null, k === 'part' ? t('part') : t(k))))),
        h('tbody', null, n.note.parts.map((p) => h('tr', null, h('td', null, `${p.order}`), h('td', null, fmtDuration(p.duration)), h('td', null, fmtBytes(p.bytes)),
          h('td', null, p.uploaded_at ? fmtDate(p.uploaded_at, getLang()) : t('partStatus_' + p.status)),
          h('td', null, t('partStatus_' + p.status)))))),
      st && st.audio ? h('p', null, `${t('audioState')}: ${t('audio_' + st.audio.state)}`) : null);
  }
  function actions(n, d) {
    const row = h('div', { class: 'actions' });
    const can = model.canAddParts(n);
    row.append(btn(t('addParts'), () => go(`#/n/${id}/add`), { ic: 'plus', attrs: { id: 'addPartsBtn', disabled: !can, title: can ? '' : t('cannotAdd') } }));
    if (can && n.transcript) row.append(btn(t('regenerate'), async () => { try { await out.regenerateNotes(id); toast(t('regenerateOk')); } catch (e) { toast(t('error', { msg: e.message }), 'bad'); } }, { attrs: { id: 'regenBtn' } }));
    const md = () => model.toMarkdown(n, labels(), withTr, (s) => fmtDuration(s));
    row.append(
      h('label', { class: 'chk inline' }, h('input', { type: 'checkbox', id: 'withTr', checked: withTr, onchange: (e) => { withTr = e.target.checked; } }), h('span', null, t('withTranscript'))),
      btn(t('copyMd'), async () => { try { await navigator.clipboard.writeText(md()); toast(t('copied')); } catch { toast(t('error', { msg: 'clipboard' }), 'bad'); } }, { attrs: { id: 'copyBtn' } }),
      btn(t('exportMd'), () => {
        const a = h('a', { href: URL.createObjectURL(new Blob([md()], { type: 'text/markdown' })), download: `${n.note.title.replace(/[^\p{L}\p{N}]+/gu, '-').slice(0, 60) || 'note'}.md` });
        document.body.append(a); a.click(); a.remove(); toast(t('downloaded'));
      }, { attrs: { id: 'exportBtn' } }));
    if (navigator.share) row.append(btn(t('share'), () => navigator.share({ title: n.note.title, text: md() }).catch(() => {})));
    row.append(btn(t('deleteNote'), async () => {
      if (!(await confirmDialog(t('deleteConfirm', { title: n.note.title }), t('delete')))) return;
      try { await out.deleteNote(id); toast(t('deleted')); go(`#/nb/${n.note.notebook_id}`); } catch (e) { toast(t('error', { msg: e.message }), 'bad'); }
    }, { cls: 'danger ghost', attrs: { id: 'deleteBtn' } }));
    return row;
  }
  render();
  return { update: render, activeNote: id };
}

// ---- search ----------------------------------------------------------------------------------------------------------
let lastQ = '';
export function searchView(root) {
  const res = h('div', { class: 'list' });
  function run(q) {
    lastQ = q; clear(res);
    if (!q.trim()) return;
    const hits = model.search(q);
    if (!hits.length) { res.append(empty(t('searchEmpty'))); return; }
    const seen = new Map();
    for (const r of hits) {
      const n = model.getNote(r.noteId); if (!n) continue;
      const i = r.low.indexOf(q.trim().split(/\s+/).map((w) => w.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase())[0]);
      const from = Math.max(0, i - 40);
      const snip = (from ? '…' : '') + r.text.slice(from, from + 140);
      res.append(h('a', { class: 'row-card hit', href: `#/n/${r.noteId}` }, h('div', { class: 'rc-main' }, h('strong', { class: 'rc-title' }, n.note.title), h('span', { class: 'rc-sub' }, `${r.kind === 'transcript' ? t('tabTranscript') + ' ' + fmtDuration(r.start) : r.kind === 'title' ? t('title') : t('tabNotes')}: ${snip}`))));
      seen.set(r.noteId, 1);
    }
  }
  const inp = h('input', { type: 'search', class: 'inp', id: 'searchIn', placeholder: t('searchPh'), value: lastQ, 'aria-label': t('search'), oninput: (e) => run(e.target.value) });
  root.append(pageHead(t('search')), h('div', { class: 'form' }, inp, h('p', { class: 'muted' }, t('searchHint'))), res);
  run(lastQ);
  return { update: () => run(inp.value) };
}

// ---- unlock ------------------------------------------------------------------------------------------------------
let failedUnlocks = 0, lockedUntil = 0;
export function unlockView(root, ctx) {
  const msg = h('p', { class: 'conn bad', id: 'unlockMsg', role: 'alert' });
  const inp = h('input', { type: 'password', class: 'inp', id: 'unlockIn', autocomplete: 'current-password', 'aria-label': t('passphrase') });
  const go2 = h('button', { type: 'submit', class: 'btn primary big', id: 'unlockBtn' }, t('unlockBtn'));
  const tickWait = () => {
    const left = Math.ceil((lockedUntil - Date.now()) / 1000);
    go2.disabled = left > 0;
    if (left > 0) { msg.textContent = t('waitS', { s: left }); setTimeout(tickWait, 500); } else if (failedUnlocks) msg.textContent = t('wrongPass');
  };
  const form = h('form', { class: 'form unlock', onsubmit: async (e) => {
    e.preventDefault();
    if (Date.now() < lockedUntil || !inp.value) return;
    go2.disabled = true;
    try { await ctx.unlock(inp.value); failedUnlocks = 0; } catch {
      failedUnlocks++; lockedUntil = Date.now() + Math.min(30000, 1000 * 2 ** (failedUnlocks - 1)); inp.value = ''; tickWait(); inp.focus();
    }
  } },
  h('h1', null, t('unlockTitle')), h('p', { class: 'muted' }, t('unlockHint')), h('label', { class: 'lbl' }, t('passphrase'), inp), go2, msg,
  h('button', { type: 'button', class: 'btn ghost danger sm', id: 'forgotBtn', onclick: async () => { if (await confirmDialog(t('forgot') + '?', t('delete'))) ctx.removeToken(); } }, t('forgot')));
  root.append(form);
  tickWait();
  setTimeout(() => inp.focus(), 0);
  return { update() {} };
}

// ---- settings ----------------------------------------------------------------------------------------------------------
let msg = null;
export function settingsView(root, ctx) {
  function render() {
    clear(root);
    const tokenIn = h('input', { type: 'password', class: 'inp', id: 'tokenIn', autocomplete: 'off', spellcheck: 'false', placeholder: t('tokenPh'), value: M.cfg.token });
    const repoIn = h('input', { type: 'text', class: 'inp', id: 'repoIn', autocomplete: 'off', spellcheck: 'false', value: M.cfg.repo });
    const passIn = h('input', { type: 'password', class: 'inp', id: 'passIn', autocomplete: 'new-password' });
    const passIn2 = h('input', { type: 'password', class: 'inp', id: 'passIn2', autocomplete: 'new-password' });
    const plainChk = h('input', { type: 'checkbox', id: 'plainChk', checked: !!(M.vault && M.vault.mode === 'plain') });
    const status = h('p', { class: `conn ${msg ? msg.kind : ''}`, id: 'connMsg', role: 'status' }, msg ? msg.text : '');
    const fail = (text) => { msg = { kind: 'bad', text }; status.className = 'conn bad'; status.textContent = text; return false; }; // no re-render: keep what the user typed
    async function save() {
      const token = tokenIn.value.trim(), repo = repoIn.value.trim();
      if (!token) return fail(t('needToken2'));
      if (token === M.cfg.token && M.vault) { await ctx.saveConfig({ repo }); msg = { kind: 'ok', text: t('tokenSaved') }; render(); return true; }
      if (plainChk.checked) {
        if (!(await confirmDialog(t('plainWarn'), t('plainOk')))) return false;
        await ctx.saveConfig({ repo }); await ctx.saveToken(token, 'plain'); msg = { kind: 'ok', text: t('savedPlain') };
      } else {
        if (passIn.value.length < 8) return fail(t('passShort'));
        if (passIn.value !== passIn2.value) return fail(t('passMismatch'));
        await ctx.saveConfig({ repo }); await ctx.saveToken(token, 'locked', passIn.value); msg = { kind: 'ok', text: t('savedLocked') };
      }
      render(); sync.refresh(); out.kick(); return true;
    }
    const lp = model.laptopState();
    const lang = h('select', { class: 'inp', id: 'langSel', onchange: (e) => ctx.saveConfig({ lang: e.target.value }) }, h('option', { value: 'cs', selected: M.cfg.lang === 'cs' }, 'Čeština'), h('option', { value: 'en', selected: M.cfg.lang === 'en' }, 'English'));
    const theme = h('select', { class: 'inp', id: 'themeSel', onchange: (e) => ctx.saveConfig({ theme: e.target.value }) }, [['auto', 'themeAuto'], ['light', 'themeLight'], ['dark', 'themeDark']].map(([v, l]) => h('option', { value: v, selected: M.cfg.theme === v }, t(l))));
    const dz = (k) => { const s = model.settings(); return s && s.diarize_default ? s.diarize_default[k] : M.cfg.diarize[k]; };
    const dcb = (k) => h('label', { class: 'chk' }, h('input', { type: 'checkbox', id: `dz-${k}`, checked: dz(k), onchange: (e) => ctx.setDiarize(k, e.target.checked) }), h('span', null, k === 'lesson' ? t('kindLesson') : t('kindMeeting')));
    root.append(pageHead(t('settings')), banners(), h('div', { class: 'form' },
      h('label', { class: 'lbl' }, t('token'), tokenIn, h('small', { class: 'muted' }, t('tokenHint'))),
      h('label', { class: 'lbl' }, t('repo'), repoIn),
      h('label', { class: 'lbl' }, t('passphrase'), passIn), h('label', { class: 'lbl' }, t('passphraseConfirm'), passIn2, h('small', { class: 'muted' }, t('passHint'))),
      h('label', { class: 'chk' }, plainChk, h('span', null, t('plainMode'))),
      h('div', { class: 'row' },
        btn(t('saveToken'), save, { cls: 'primary', attrs: { id: 'saveTokenBtn' } }),
        btn(t('testConn'), async () => {
          await ctx.saveConfig({ token: tokenIn.value.trim() || M.cfg.token, repo: repoIn.value.trim() }); msg = { kind: '', text: t('testing') }; render();
          try {
            const info = await gh.repoInfo();
            const tip = await gh.getRef('main');
            const canWrite = !info.permissions || info.permissions.push !== false;
            msg = canWrite && tip ? { kind: 'ok', text: t('connOk') } : { kind: 'bad', text: t('connReadOnly') };
          } catch (e) { msg = { kind: 'bad', text: t('connFail', { msg: e.message }) }; }
          render();
        }, { attrs: { id: 'testBtn' } }),
        M.vault && M.vault.mode === 'locked' ? btn(t('lockNow'), () => ctx.lockNow(), { cls: 'ghost sm', attrs: { id: 'lockBtn' } }) : null,
        M.vault || M.cfg.token ? btn(t('removeToken'), async () => { await ctx.removeToken(); msg = null; render(); }, { cls: 'ghost danger sm', attrs: { id: 'rmTokenBtn' } }) : null),
      status,
      h('section', { class: 'blk riskbox' }, h('h2', null, t('riskTitle')), h('p', null, t('riskText'))),
      h('details', { class: 'howto' }, h('summary', null, t('howToken')), h('ol', null, t('tokenSteps').map((s) => h('li', null, s)))),
      h('label', { class: 'lbl' }, t('uiLanguage'), lang), h('label', { class: 'lbl' }, t('theme'), theme),
      h('fieldset', { class: 'fs' }, h('legend', null, t('diarizeDefault')), dcb('lesson'), dcb('meeting')),
      h('section', { class: 'blk' }, h('h2', null, t('laptop')), h('p', null, laptopLine()),
        h('p', { class: 'muted' }, t('lastSync', { t: M.net.lastSync ? fmtRelative(M.net.lastSync, getLang()) : t('never') })),
        btn(t('syncNow'), () => { sync.refresh(); out.kick(); }, { ic: 'refresh', attrs: { id: 'syncBtn' } }))));
  }
  render();
  return { update: () => { if (!document.activeElement || !['INPUT', 'SELECT'].includes(document.activeElement.tagName)) render(); } };
}
