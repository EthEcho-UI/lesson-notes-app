// Notes tab: renders notes.json v2 (lesson and meeting) and v1. Everything is built with DOM text nodes (repo data is untrusted);
// figures go ONLY through <img src="data:image/svg+xml;utf8,..."> after the svgsafe check, never parsed as HTML.
import { h, dialog, btn } from './ui.js';
import { t, getLang } from './i18n.js';
import * as model from './model.js';
import { imageUrl } from './images.js';
import { safeSvg, svgDataUrl } from './svgsafe.js';
import { hwLoad, hwSave, hwId, KIND_LABEL, MATERIAL_LABEL, langModeLabel, labelColon } from './docmodel.js';
import { fmtRelative } from './util.js';

const section = (id, head, ...kids) => h('section', { class: 'blk', id }, h('h2', null, head), ...kids);
const list = (arr) => (arr && arr.length ? h('ul', null, arr.map((x) => h('li', null, x))) : null);

export function enlarge(src, alt, caption) {
  return dialog((d, close) => {
    d.classList.add('big');
    d.append(h('img', { class: 'bigimg', src, alt: alt || '' }), caption ? h('p', { class: 'cap-txt' }, caption) : null,
      h('div', { class: 'row end' }, btn(t('closeBig'), () => close(null), { attrs: { id: 'bigClose' } })));
  });
}

function outline(items) {
  const pts = (arr) => h('ul', null, arr.map((p) => (typeof p === 'string' ? h('li', null, p) : h('li', null, p.text, p.children && p.children.length ? pts(p.children) : null))));
  const wrap = h('div', { class: 'outline', id: 'outline' });
  const dets = items.map((o) => h('details', { class: 'ol', open: true }, h('summary', null, o.heading), pts(o.points || [])));
  wrap.append(h('div', { class: 'row end ol-ctl' },
    btn(t('collapseAll'), () => dets.forEach((d) => { d.open = false; }), { cls: 'ghost sm', attrs: { id: 'olCollapse' } }),
    btn(t('expandAll'), () => dets.forEach((d) => { d.open = true; }), { cls: 'ghost sm' })), ...dets);
  return wrap;
}
function keyPoints(arr) {
  const L = KIND_LABEL();
  return h('ul', { class: 'kps', id: 'keypoints' }, arr.map((k) => h('li', { class: `kp kp-${k.kind}`, 'data-kind': k.kind }, h('span', { class: 'kpb' }, L[k.kind] || k.kind), ' ', k.term ? h('strong', null, `${k.term}: `) : null, k.text)));
}
function figures(arr) {
  const out = h('div', { class: 'figs', id: 'figures' });
  for (const f of arr) {
    const svg = safeSvg(f.svg);
    if (!svg) { out.append(h('p', { class: 'muted fig-bad' }, `${f.title}: ${t('figureHidden')}`)); continue; }
    const src = svgDataUrl(svg);
    out.append(h('figure', { class: 'fig', 'data-id': f.id },
      h('button', { type: 'button', class: 'figbtn', 'aria-label': `${t('enlarge')}: ${f.title}`, onclick: () => enlarge(src, f.title, f.caption) }, h('img', { src, alt: f.title })),
      h('figcaption', null, h('strong', null, f.title), ' ', f.caption, f.refs ? h('small', { class: 'muted' }, ` (${f.refs})`) : null)));
  }
  return out;
}
function materials(arr, id) {
  const L = MATERIAL_LABEL();
  const out = h('div', { class: 'mats', id });
  for (const m of arr) {
    if (m.kind === 'flashcard') {
      let back = false;
      const face = h('span', { class: 'face' }, m.text);
      const card = h('button', { type: 'button', class: 'flash', 'aria-pressed': 'false', 'aria-label': `${t('flipCard')}: ${m.title}`, onclick: () => {
        back = !back; face.textContent = back ? m.answer : m.text; card.classList.toggle('back', back); card.setAttribute('aria-pressed', back ? 'true' : 'false');
      } }, h('span', { class: 'kpb' }, `${L.flashcard} · ${m.title}`), face);
      out.append(card);
    } else if (m.answer) {
      out.append(h('details', { class: 'qa' }, h('summary', null, h('span', { class: 'kpb' }, L[m.kind] || m.kind), ' ', h('strong', null, m.title), h('span', { class: 'qtext' }, m.text)), h('p', { class: 'ans' }, h('strong', null, `${t('answer')}: `), m.answer)));
    } else out.append(h('div', { class: 'mat' }, h('span', { class: 'kpb' }, L[m.kind] || m.kind), ' ', h('strong', null, m.title), h('p', null, m.text)));
  }
  return out;
}
function checklist(noteId, items, keyOf, textOf) {
  const st = hwLoad(noteId);
  return h('ul', { class: 'hw' }, items.map((x) => {
    const k = keyOf(x);
    return h('li', null, h('label', { class: 'chk' }, h('input', { type: 'checkbox', 'data-hw': k, checked: !!st[k], onchange: (e) => { const s = hwLoad(noteId); if (e.target.checked) s[k] = true; else delete s[k]; hwSave(noteId, s); } }), h('span', null, textOf(x))));
  }));
}
export function gallery(n) {
  const imgs = model.noteImages(n);
  if (!imgs.length) return null;
  const g = h('div', { class: 'gallery', id: 'gallery' });
  for (const im of imgs) {
    const img = h('img', { alt: im.caption || `${t('photo')} ${im.id}`, loading: 'lazy' });
    const b = h('button', { type: 'button', class: 'gbtn', 'data-id': im.id, 'aria-label': `${t('enlarge')}: ${im.caption || im.id}`, disabled: true, onclick: () => enlarge(img.src, img.alt, im.caption) }, img);
    imageUrl(n.id, im).then((u) => { if (u) { img.src = u; b.disabled = false; } else b.append(h('span', { class: 'muted ph-na' }, t('photoUnavailable'))); });
    g.append(h('figure', { class: 'gitem' }, b, im.caption ? h('figcaption', null, im.caption) : null));
  }
  return section('photosSec', t('photos'), g);
}

export function renderNotes(n) {
  const c = n.notes && n.notes.content;
  const photos = gallery(n);
  if (!c) return h('div', { class: 'notes' }, h('p', { class: 'empty' }, t('noNotesYet')), photos);
  if (!model.isV2Notes(n)) return renderV1(n, c, photos);
  const meeting = n.note.kind === 'meeting';
  const ctx = model.contextOf(n);
  const root = h('div', { class: 'notes v2' });
  root.append(h('h2', { class: 'n-title', id: 'nTitle' }, c.title), c.description ? h('p', { class: 'n-desc', id: 'nDesc' }, c.description) : null,
    h('p', { class: 'n-meta muted' }, h('span', { class: 'chip', id: 'langChip' }, `${t('languageMode')}: ${langModeLabel(model.languageMode(n))}`), ' ',
      n.notes.generated_at ? t('generatedAt', { t: fmtRelative(n.notes.generated_at, getLang()) }) : ''));
  if (ctx.topic || ctx.details) root.append(h('details', { class: 'ctx' }, h('summary', null, t('contextHeading')), h('p', null, h('strong', null, labelColon(t('topic'))), ctx.topic), ctx.details ? h('p', null, ctx.details) : null));
  if (c.outline && c.outline.length) root.append(section('outlineSec', t('outline'), outline(c.outline)));
  if (c.key_points && c.key_points.length) root.append(section('kpSec', t('keyPoints'), keyPoints(c.key_points)));
  if (c.figures && c.figures.length) root.append(section('figSec', t('figures'), figures(c.figures)));
  if (meeting) {
    if (c.decisions && c.decisions.length) root.append(section('decSec', t('decisions'), list(c.decisions)));
    if (c.action_items && c.action_items.length) root.append(section('aiSec', t('actionItems'), checklist(n.id, c.action_items, (a) => `ai:${a.task}`, (a) => a.task + ((a.owner || a.due) ? ` (${[a.owner, a.due].filter(Boolean).join(', ')})` : ''))));
    if (c.follow_up_materials && c.follow_up_materials.length) root.append(section('fuSec', t('followUp'), materials(c.follow_up_materials, 'followups')));
  } else {
    if (c.study_materials && c.study_materials.length) root.append(section('studySec', t('studyMaterials'), materials(c.study_materials, 'study')));
    if (c.homework && c.homework.length) {
      const sec = section('hwSec', t('homework'));
      for (const src of ['lesson', 'suggested']) {
        const items = c.homework.filter((x) => x.source === src);
        if (items.length) sec.append(h('h3', { class: `hw-${src}` }, src === 'lesson' ? t('hwLesson') : t('hwSuggested')), checklist(n.id, items, hwId, (x) => x.task + (x.details ? ` – ${x.details}` : '') + (x.due ? ` (${x.due})` : '')));
      }
      root.append(sec);
    }
  }
  if (c.open_questions && c.open_questions.length) root.append(section('oqSec', t('questionsOpen'), list(c.open_questions)));
  if (c.uncertainties && c.uncertainties.length) {
    root.append(h('aside', { class: 'uncertain', id: 'uncertain' }, h('h2', null, t('checkThis')), h('p', { class: 'hint' }, t('checkThisHint')),
      h('ul', null, c.uncertainties.map((u) => h('li', null, h('strong', null, u.text), ` – ${u.reason}`)))));
  }
  root.append(photos);
  return root;
}

function renderV1(n, c, photos) {
  return h('div', { class: 'notes' }, c.summary ? section('v1sum', t('summary'), h('p', null, c.summary)) : null,
    c.key_points && c.key_points.length ? section('v1kp', t('keyPoints'), list(c.key_points)) : null,
    c.definitions && c.definitions.length ? section('v1def', t('definitions'), h('dl', null, c.definitions.map((x) => [h('dt', null, x.term), h('dd', null, x.definition)]))) : null,
    n.note.kind === 'meeting' ? [c.decisions && c.decisions.length ? section('v1dec', t('decisions'), list(c.decisions)) : null,
      c.action_items && c.action_items.length ? section('v1ai', t('actionItems'), h('ul', null, c.action_items.map((a) => h('li', null, a.task, (a.owner || a.due) ? h('span', { class: 'muted' }, ` (${[a.owner, a.due].filter(Boolean).join(', ')})`) : null)))) : null] : null,
    c.todo && c.todo.length ? section('v1todo', t('todo'), list(c.todo)) : null, c.questions_open && c.questions_open.length ? section('v1oq', t('questionsOpen'), list(c.questions_open)) : null, photos);
}
