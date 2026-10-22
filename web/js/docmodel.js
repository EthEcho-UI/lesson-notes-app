// One neutral document model of a note (blocks) that the three exporters (Markdown, print/PDF, DOCX) render.
import { t, getLang } from './i18n.js';
import * as model from './model.js';
import { imageBlob } from './images.js';
import { safeSvg } from './svgsafe.js';
import { fmtDuration, fmtDate } from './util.js';

// ---- homework checkbox state: local only, localStorage "lessonnotes.hw.<noteId>" ---------------------------
const hwKey = (id) => `lessonnotes.hw.${id}`;
export function hwLoad(noteId) { try { return JSON.parse(localStorage.getItem(hwKey(noteId)) || '{}') || {}; } catch { return {}; } }
export function hwSave(noteId, state) { try { localStorage.setItem(hwKey(noteId), JSON.stringify(state)); } catch { /* storage blocked */ } }
export const hwId = (hw) => `${hw.source}:${hw.task}`;

export const labelColon = (s) => (/[?:]$/.test(s) ? `${s} ` : `${s}: `);
export const KIND_LABEL = () => ({ key: t('kp_key'), important: t('kp_important'), definition: t('kp_definition'), formula: t('kp_formula'), example: t('kp_example') });
export const MATERIAL_LABEL = () => ({ practice_question: t('mat_practice_question'), flashcard: t('mat_flashcard'), summary: t('mat_summary'), topic_to_review: t('mat_topic_to_review'), search_terms: t('mat_search_terms'), exercise: t('mat_exercise') });
export const langModeLabel = (m) => t('lm_' + m);

const flatPoints = (pts, level = 0, out = []) => {
  for (const p of pts || []) {
    if (typeof p === 'string') out.push({ level, runs: [{ text: p }] });
    else { out.push({ level, runs: [{ text: p.text }] }); flatPoints(p.children, level + 1, out); }
  }
  return out;
};

// opts: {transcript:boolean, photos:boolean (load blobs)}
export async function buildDoc(n, opts = {}) {
  const c = n.notes && n.notes.content;
  const nb = model.notebook(n.note.notebook_id);
  const B = [];
  const v2 = model.isV2Notes(n);
  const title = (c && c.title) || n.note.title;
  const hw = hwLoad(n.id);
  B.push({ t: 'title', text: title });
  const meta = [nb && nb.name, n.note.kind === 'meeting' ? t('kindMeeting') : t('kindLesson'), fmtDate(n.note.recorded_at || n.note.created_at, getLang()), `${t('languageMode')}: ${langModeLabel(model.languageMode(n))}`];
  if (c && c.title && c.title !== n.note.title) meta.unshift(n.note.title);
  B.push({ t: 'meta', text: meta.filter(Boolean).join(' · ') });
  const ctx = model.contextOf(n);
  if (!c) B.push({ t: 'p', text: t('noNotesYet') });
  else if (v2) {
    if (c.description) B.push({ t: 'p', text: c.description });
    if (c.outline && c.outline.length) {
      B.push({ t: 'h2', text: t('outline') });
      for (const o of c.outline) { B.push({ t: 'h3', text: o.heading }); B.push({ t: 'list', items: flatPoints(o.points) }); }
    }
    if (c.key_points && c.key_points.length) {
      const L = KIND_LABEL();
      B.push({ t: 'h2', text: t('keyPoints') });
      B.push({ t: 'list', items: c.key_points.map((k) => ({ level: 0, runs: [{ text: `${L[k.kind] || k.kind}: `, bold: true }, ...(k.term ? [{ text: `${k.term}: `, bold: true }] : []), { text: k.text }] })) });
    }
    const figs = (c.figures || []).map((f) => ({ f, svg: safeSvg(f.svg) })).filter((x) => x.svg);
    if (figs.length) {
      B.push({ t: 'h2', text: t('figures') });
      for (const { f, svg } of figs) B.push({ t: 'figure', title: f.title, caption: f.caption, svg });
    }
    const mats = c.study_materials || c.follow_up_materials || [];
    if (mats.length) {
      const L = MATERIAL_LABEL();
      B.push({ t: 'h2', text: c.study_materials ? t('studyMaterials') : t('followUp') });
      for (const m of mats) {
        if (m.answer != null && m.answer !== '') B.push({ t: 'qa', label: L[m.kind] || m.kind, title: m.title, q: m.text, a: m.answer });
        else B.push({ t: 'qa', label: L[m.kind] || m.kind, title: m.title, q: m.text });
      }
    }
    if (n.note.kind === 'meeting') {
      if (c.decisions && c.decisions.length) { B.push({ t: 'h2', text: t('decisions') }); B.push({ t: 'list', items: c.decisions.map((d) => ({ level: 0, runs: [{ text: d }] })) }); }
      if (c.action_items && c.action_items.length) {
        B.push({ t: 'h2', text: t('actionItems') });
        for (const a of c.action_items) B.push({ t: 'task', text: a.task + ((a.owner || a.due) ? ` (${[a.owner, a.due].filter(Boolean).join(', ')})` : ''), done: !!hw[`ai:${a.task}`] });
      }
    }
    if (c.homework && c.homework.length) {
      B.push({ t: 'h2', text: t('homework') });
      for (const src of ['lesson', 'suggested']) {
        const items = c.homework.filter((x) => x.source === src);
        if (!items.length) continue;
        B.push({ t: 'h3', text: src === 'lesson' ? t('hwLesson') : t('hwSuggested') });
        for (const x of items) B.push({ t: 'task', text: x.task + (x.details ? ` – ${x.details}` : '') + (x.due ? ` (${x.due})` : ''), done: !!hw[hwId(x)] });
      }
    }
    if (c.open_questions && c.open_questions.length) { B.push({ t: 'h2', text: t('questionsOpen') }); B.push({ t: 'list', items: c.open_questions.map((q) => ({ level: 0, runs: [{ text: q }] })) }); }
    if (c.uncertainties && c.uncertainties.length) {
      B.push({ t: 'h2', text: t('checkThis') });
      B.push({ t: 'list', items: c.uncertainties.map((u) => ({ level: 0, runs: [{ text: u.text, bold: true }, { text: ` – ${u.reason}` }] })) });
    }
  } else {
    // v1 notes
    if (c.summary) { B.push({ t: 'h2', text: t('summary') }); B.push({ t: 'p', text: c.summary }); }
    const lst = (head, arr) => { if (arr && arr.length) { B.push({ t: 'h2', text: head }); B.push({ t: 'list', items: arr.map((x) => ({ level: 0, runs: [{ text: x }] })) }); } };
    lst(t('keyPoints'), c.key_points);
    if (c.definitions && c.definitions.length) { B.push({ t: 'h2', text: t('definitions') }); B.push({ t: 'list', items: c.definitions.map((d) => ({ level: 0, runs: [{ text: `${d.term}: `, bold: true }, { text: d.definition }] })) }); }
    lst(t('decisions'), c.decisions);
    if (c.action_items && c.action_items.length) lst(t('actionItems'), c.action_items.map((a) => a.task + (a.owner ? ` (${a.owner}${a.due ? ', ' + a.due : ''})` : a.due ? ` (${a.due})` : '')));
    lst(t('todo'), c.todo); lst(t('questionsOpen'), c.questions_open);
  }
  const imgs = model.noteImages(n);
  if (imgs.length) {
    B.push({ t: 'h2', text: t('photos') });
    for (const im of imgs) B.push({ t: 'photo', id: im.id, entry: im, blob: opts.photos ? await imageBlob(n.id, im) : null, width: im.width, height: im.height, caption: im.caption || '' });
  }
  if (ctx.topic || ctx.details) {
    B.push({ t: 'h2', text: t('contextHeading') });
    B.push({ t: 'list', items: [ctx.subject && { level: 0, runs: [{ text: `${t('subject')}: `, bold: true }, { text: ctx.subject }] }, ctx.topic && { level: 0, runs: [{ text: labelColon(t('topic')), bold: true }, { text: ctx.topic }] }, ctx.details && { level: 0, runs: [{ text: `${t('details')}: `, bold: true }, { text: ctx.details }] }].filter(Boolean) });
  }
  if (opts.transcript && n.transcript) {
    B.push({ t: 'h2', text: t('transcript') });
    for (const p of n.transcript.parts) {
      B.push({ t: 'h3', text: `${t('part')} ${p.order} (${fmtDuration(p.duration)})` });
      for (const s of p.segments) B.push({ t: 'tr', time: fmtDuration(s.start), speaker: s.speaker ? model.speakerName(n, p.part_id, s.speaker) : '', text: s.text });
    }
  }
  return { title, blocks: B };
}

export const fileSlug = (s) => (s || 'note').replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-+|-+$/g, '').slice(0, 60) || 'note';
