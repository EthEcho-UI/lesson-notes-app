// Photo section of the note forms (new note / add audio / edit context): pick from camera or gallery, resize, thumbnails, captions, remove.
import { h, icon, btn, iconBtn, toast } from './ui.js';
import { t } from './i18n.js';
import { ulid } from './util.js';
import { prepareImage, imageUrl, MAX_IMAGES } from './images.js';

// st: {photos:[new], existing:[note.json image entries], removed:Set(ids), captions:{id:text}, noteId, busy:false}
export function photosSection(st, rerender) {
  const keptCount = () => st.existing.filter((i) => !st.removed.has(i.id)).length + st.photos.length;
  async function add(files) {
    for (const f of files) {
      if (keptCount() >= MAX_IMAGES) { toast(t('maxPhotos', { n: MAX_IMAGES }), 'bad'); break; }
      try {
        st.busy = true; rerender();
        const p = await prepareImage(f);
        st.photos.push({ key: ulid(), ...p, thumbUrl: URL.createObjectURL(p.thumb), caption: '', name: f.name });
      } catch (e) { console.warn('photo', e.message); toast(t('photoError', { name: f.name || '' }), 'bad'); }
    }
    st.busy = false; rerender();
  }
  const pick = (id, label, attrs, ic) => h('label', { class: 'btn' }, icon(ic), h('span', null, label),
    h('input', { type: 'file', id, accept: 'image/*', class: 'visually-hidden', ...attrs, onchange: (e) => { add([...e.target.files]); e.target.value = ''; } }));
  const wrap = h('section', { class: 'photos-ed' }, h('h2', null, t('photos')), h('p', { class: 'hint' }, t('addPhotosHint')),
    h('div', { class: 'rec-actions' }, pick('camIn', t('takePhoto'), { capture: 'environment' }, 'camera'), pick('photoIn', t('fromGallery'), { multiple: true }, 'image')),
    st.busy ? h('p', { class: 'muted', id: 'photoBusy' }, t('preparing')) : null);
  const list = h('ul', { class: 'phlist', id: 'phList' });
  for (const im of st.existing.filter((i) => !st.removed.has(i.id))) {
    const img = h('img', { class: 'thumb', alt: im.caption || '' });
    imageUrl(st.noteId, im).then((u) => { if (u) img.src = u; });
    list.append(h('li', { class: 'ph', 'data-id': im.id }, img, h('div', { class: 'ph-main' }, h('strong', null, `${t('photo')} ${im.id}`),
      h('input', { class: 'inp cap', type: 'text', maxlength: 200, placeholder: t('photoCaption'), 'aria-label': t('photoCaption'), value: st.captions[im.id] ?? im.caption ?? '', oninput: (e) => { st.captions[im.id] = e.target.value; } })),
      iconBtn('x', t('removePhoto'), () => { st.removed.add(im.id); rerender(); })));
  }
  for (const p of st.photos) {
    list.append(h('li', { class: 'ph', 'data-new': '1' }, h('img', { class: 'thumb', src: p.thumbUrl, alt: '' }), h('div', { class: 'ph-main' }, h('strong', null, `${p.width}×${p.height}`),
      h('input', { class: 'inp cap', type: 'text', maxlength: 200, placeholder: t('photoCaption'), 'aria-label': t('photoCaption'), value: p.caption, oninput: (e) => { p.caption = e.target.value; } })),
      iconBtn('x', t('removePhoto'), () => { URL.revokeObjectURL(p.thumbUrl); st.photos.splice(st.photos.indexOf(p), 1); rerender(); })));
  }
  if (list.children.length) wrap.append(list);
  return wrap;
}
