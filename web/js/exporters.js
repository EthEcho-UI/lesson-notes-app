// Client-side export of one note: Markdown, PDF (print stylesheet + window.print()) and DOCX. Nothing leaves the device.
// Markdown decision: ONE .md file; figures are inline SVG data URIs (![](data:image/svg+xml;base64,...)) so the file
// is self-contained and renders in Obsidian/VS Code/GitHub; photos are NOT embedded (listed by caption), use DOCX/PDF for photos.
// Copy/share variants omit the figure data (it would make the clipboard huge).
import { h } from './ui.js';
import { t } from './i18n.js';
import { buildDoc, fileSlug } from './docmodel.js';
import { makeDocx } from './docx.js';
import { utf8ToB64 } from './util.js';
import { svgDataUrl } from './svgsafe.js';

export async function toMarkdown(n, { transcript = false, figures = 'inline' } = {}) {
  const doc = await buildDoc(n, { transcript, photos: false });
  const L = [];
  for (const b of doc.blocks) {
    switch (b.t) {
      case 'title': L.push(`# ${b.text}`, ''); break;
      case 'meta': L.push(`*${b.text}*`, ''); break;
      case 'p': L.push(b.text, ''); break;
      case 'h2': L.push('', `## ${b.text}`, ''); break;
      case 'h3': L.push('', `### ${b.text}`, ''); break;
      case 'list': for (const it of b.items) L.push(`${'  '.repeat(it.level)}- ${it.runs.map((r) => (r.bold ? `**${r.text.trimEnd()}**${/\s$/.test(r.text) ? ' ' : ''}` : r.text)).join('')}`); L.push(''); break;
      case 'task': L.push(`- [${b.done ? 'x' : ' '}] ${b.text}`, ''); break;
      case 'qa': L.push(`**${b.label}: ${b.title}**`, '', b.q, ''); if (b.a) L.push(`> ${t('answer')}: ${b.a}`, ''); break;
      case 'figure':
        L.push(`**${b.title}**`, '');
        if (figures === 'inline') L.push(`![${b.title.replace(/[[\]]/g, '')}](data:image/svg+xml;base64,${utf8ToB64(b.svg)})`, '');
        if (b.caption) L.push(`*${b.caption}*`, '');
        break;
      case 'photo': L.push(`- ${t('photo')} ${b.id}${b.caption ? `: ${b.caption}` : ''}`, ''); break;
      case 'tr': L.push(`[${b.time}] ${b.speaker ? `**${b.speaker}**: ` : ''}${b.text}  `); break;
      default: break;
    }
  }
  return L.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd() + '\n';
}

// ---- print / PDF ----------------------------------------------------------------------------------------------
let printUrls = [];
export async function buildPrintRoot(n, { transcript = false } = {}) {
  const doc = await buildDoc(n, { transcript, photos: true });
  removePrintRoot();
  const root = h('div', { id: 'printRoot', lang: document.documentElement.lang });
  let ul = null;
  const flush = () => { ul = null; };
  for (const b of doc.blocks) {
    if (b.t !== 'list') flush();
    switch (b.t) {
      case 'title': root.append(h('h1', { class: 'pr-title' }, b.text)); break;
      case 'meta': root.append(h('p', { class: 'pr-meta' }, b.text)); break;
      case 'p': root.append(h('p', null, b.text)); break;
      case 'h2': root.append(h('h2', null, b.text)); break;
      case 'h3': root.append(h('h3', null, b.text)); break;
      case 'list': {
        const wrap = h('ul', { class: 'pr-list' });
        for (const it of b.items) wrap.append(h('li', { style: null, class: `lv${it.level}` }, it.runs.map((r) => (r.bold ? h('strong', null, r.text) : r.text))));
        root.append(wrap); break;
      }
      case 'task': root.append(h('p', { class: 'pr-task' }, h('span', { class: 'pr-box' }, b.done ? '☑' : '☐'), ' ', b.text)); break;
      case 'qa': root.append(h('div', { class: 'pr-qa' }, h('p', null, h('strong', null, `${b.label}: ${b.title}`)), b.q ? h('p', null, b.q) : null, b.a ? h('p', { class: 'pr-a' }, h('em', null, `${t('answer')}: ${b.a}`)) : null)); break;
      case 'figure': root.append(h('figure', { class: 'pr-fig' }, h('img', { src: svgDataUrl(b.svg), alt: b.title }), h('figcaption', null, h('strong', null, b.title), b.caption ? ` ${b.caption}` : ''))); break;
      case 'photo': {
        if (!b.blob) break;
        const u = URL.createObjectURL(b.blob); printUrls.push(u);
        root.append(h('figure', { class: 'pr-photo' }, h('img', { src: u, alt: b.caption || b.id }), b.caption ? h('figcaption', null, b.caption) : null)); break;
      }
      case 'tr': root.append(h('p', { class: 'pr-tr' }, h('span', { class: 'pr-ts' }, `[${b.time}] `), b.speaker ? h('strong', null, `${b.speaker}: `) : null, b.text)); break;
      default: break;
    }
  }
  document.body.append(root);
  // wait until all images decoded so page.pdf()/print never sees half-loaded pictures
  await Promise.all([...root.querySelectorAll('img')].map((i) => (i.decode ? i.decode().catch(() => {}) : Promise.resolve())));
  return root;
}
export function removePrintRoot() {
  const old = document.getElementById('printRoot');
  if (old) old.remove();
  for (const u of printUrls) URL.revokeObjectURL(u);
  printUrls = [];
}
export async function exportPdf(n, opts) {
  const prevTitle = document.title;
  const root = await buildPrintRoot(n, opts);
  document.title = fileSlug(root.querySelector('.pr-title') ? root.querySelector('.pr-title').textContent : n.note.title); // suggested PDF file name
  window.print();
  setTimeout(() => { document.title = prevTitle; }, 1500);
}

// ---- downloads -----------------------------------------------------------------------------------------------------
export function download(blob, name) {
  const a = h('a', { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}
export async function exportMd(n, { transcript }) {
  const md = await toMarkdown(n, { transcript, figures: 'inline' });
  download(new Blob([md], { type: 'text/markdown' }), `${fileSlug(n.note.title)}.md`);
}
export async function exportDocx(n, { transcript }) {
  const doc = await buildDoc(n, { transcript, photos: true });
  const bytes = await makeDocx(doc);
  download(new Blob([bytes], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }), `${fileSlug(n.note.title)}.docx`);
}
