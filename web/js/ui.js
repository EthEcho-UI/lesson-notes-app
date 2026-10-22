// DOM helpers. All text goes through textContent and text nodes (never parsed as HTML), because repo data is untrusted.
import { t } from './i18n.js';

export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked' || k === 'disabled' || k === 'hidden' || k === 'selected') el[k] = !!v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  add(el, kids);
  return el;
}
function add(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
}
export const clear = (el) => { while (el.firstChild) el.firstChild.remove(); return el; };

const ICONS = {
  mic: 'M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3',
  book: 'M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3zM5 17a3 3 0 0 1 3-3h11',
  search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM20 20l-4-4',
  gear: 'M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2',
  up: 'M12 19V5M6 11l6-6 6 6', down: 'M12 5v14M6 13l6 6 6-6', x: 'M6 6l12 12M18 6L6 18', plus: 'M12 5v14M5 12h14',
  back: 'M15 5l-7 7 7 7', play: 'M8 5v14l11-7z', pause: 'M8 5v14M16 5v14', stop: 'M7 7h10v10H7z', file: 'M6 3h8l4 4v14H6zM14 3v4h4', more: 'M5 12h.01M12 12h.01M19 12h.01',
  refresh: 'M20 12a8 8 0 1 1-3-6.2M20 4v5h-5', check: 'M5 12l5 5 9-10',
  camera: 'M4 8h3l2-3h6l2 3h3v11H4zM12 11a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7z', image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M9 9h.01', download: 'M12 4v11M7 11l5 5 5-5M5 20h14',
};
export function icon(name) {
  const NS = 'http://www.w3.org/2000/svg';
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('class', 'ico'); s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS(NS, 'path');
  p.setAttribute('d', ICONS[name] || ''); s.append(p);
  return s;
}
export function btn(label, onclick, { cls = '', ic, attrs = {} } = {}) {
  return h('button', { type: 'button', class: `btn ${cls}`, onclick, ...attrs }, ic ? icon(ic) : null, label ? h('span', null, label) : null);
}
export function iconBtn(ic, label, onclick, cls = '') {
  return h('button', { type: 'button', class: `ibtn ${cls}`, 'aria-label': label, title: label, onclick }, icon(ic));
}

let toastEl = null, toastT = 0;
export function toast(msg, kind = '') {
  if (!toastEl) { toastEl = h('div', { class: 'toast', role: 'status', 'aria-live': 'polite' }); document.body.append(toastEl); }
  toastEl.textContent = msg; toastEl.className = `toast show ${kind}`;
  clearTimeout(toastT); toastT = setTimeout(() => toastEl.classList.remove('show'), 3500);
}

// modal dialogs via <dialog>; resolves with a value or null on cancel
export function dialog(build) {
  return new Promise((resolve) => {
    const d = h('dialog', { class: 'dlg' });
    const close = (v) => { d.close(); d.remove(); resolve(v); };
    d.addEventListener('cancel', (e) => { e.preventDefault(); close(null); });
    build(d, close);
    document.body.append(d);
    d.showModal();
  });
}
export function promptDialog({ title, label, value = '', checkbox = null, okLabel }) {
  return dialog((d, close) => {
    const input = h('input', { type: 'text', value, class: 'inp', 'aria-label': label, maxlength: 100 });
    const cb = checkbox ? h('input', { type: 'checkbox', checked: checkbox.checked }) : null;
    const form = h('form', { onsubmit: (e) => { e.preventDefault(); const v = input.value.trim(); if (v) close({ value: v, checked: cb ? cb.checked : false }); } },
      h('h2', null, title), h('label', { class: 'lbl' }, label, input),
      cb ? h('label', { class: 'chk' }, cb, h('span', null, checkbox.label)) : null,
      h('div', { class: 'row end' }, btn(t('cancel'), () => close(null), { cls: 'ghost' }), h('button', { type: 'submit', class: 'btn primary' }, okLabel || t('save'))));
    d.append(form);
    setTimeout(() => { input.focus(); input.select(); }, 0);
  });
}
export function confirmDialog(message, okLabel) {
  return dialog((d, close) => {
    d.append(h('p', null, message), h('div', { class: 'row end' }, btn(t('cancel'), () => close(false), { cls: 'ghost' }), btn(okLabel || t('ok'), () => close(true), { cls: 'danger' })));
  }).then((v) => !!v);
}
