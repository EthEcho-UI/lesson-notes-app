// JS port of contract/svg_sanitize.py (CONTRACT section G). Figures from notes.json are untrusted input:
// they are checked here again and rendered ONLY through <img src="data:image/svg+xml;utf8,...">, so scripts never run.
// Never parse SVG text as HTML. Passes contract/svg_vectors.json (tests/run_tests.py test_svg_vectors).
const SVG_NS = 'http://www.w3.org/2000/svg';
const XMLNS_NS = 'http://www.w3.org/2000/xmlns/';
export const MAX_BYTES = 20000, MAX_ELEMENTS = 300, MAX_DEPTH = 12, MAX_TEXT = 200, MAX_ATTR = 4000;

const ELEMENTS = new Set(['svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'defs', 'marker', 'title', 'desc']);
const TEXT_ELEMENTS = new Set(['text', 'tspan', 'title', 'desc']);
const COMMON = new Set(['id', 'transform', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-miterlimit', 'opacity', 'fill-opacity', 'stroke-opacity', 'fill-rule']);
const TEXTATTR = ['x', 'y', 'dx', 'dy', 'font-size', 'font-family', 'font-weight', 'font-style', 'text-anchor', 'dominant-baseline'];
const PER_ELEMENT = {
  svg: ['viewBox', 'width', 'height', 'xmlns', 'preserveAspectRatio'], g: [],
  path: ['d', 'marker-start', 'marker-mid', 'marker-end'], rect: ['x', 'y', 'width', 'height', 'rx', 'ry'], circle: ['cx', 'cy', 'r'], ellipse: ['cx', 'cy', 'rx', 'ry'],
  line: ['x1', 'y1', 'x2', 'y2', 'marker-start', 'marker-end'], polyline: ['points', 'marker-start', 'marker-mid', 'marker-end'], polygon: ['points', 'marker-start', 'marker-mid', 'marker-end'],
  text: TEXTATTR, tspan: TEXTATTR, defs: [], marker: ['markerWidth', 'markerHeight', 'refX', 'refY', 'orient', 'markerUnits', 'viewBox'], title: [], desc: [],
};
const SAFE_FONTS = new Set(['sans-serif', 'serif', 'monospace', 'arial', 'helvetica', 'dejavu sans', 'liberation sans', 'noto sans']);

const NUM = '-?(?:\\d+\\.?\\d*|\\.\\d+)(?:[eE][-+]?\\d+)?';
const RE_LEN = new RegExp(`^${NUM}(?:px|%|em|pt)?$`);
const RE_NUMLIST = new RegExp(`^(?:${NUM}[\\s,]*)+$`);
const RE_VIEWBOX = new RegExp(`^${NUM}[\\s,]+${NUM}[\\s,]+${NUM}[\\s,]+${NUM}$`);
const RE_PATH = /^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.+-]*$/;
const RE_TRANSFORM = new RegExp(`^(?:(?:translate|scale|rotate|skewX|skewY|matrix)\\(\\s*(?:${NUM}[\\s,]*)+\\)\\s*)+$`);
const RE_COLOR = /^(?:none|currentColor|#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|rgba?\(\s*[\d.%\s,]+\))$/;
const RE_ID = /^[A-Za-z][A-Za-z0-9_-]{0,40}$/;
const RE_URLREF = /^url\(#([A-Za-z][A-Za-z0-9_-]{0,40})\)$/;
const RE_ENUM_WORDS = /^[A-Za-z0-9 ,-]{1,40}$/;
const RE_BAD_SUBSTR = /(javascript:|data:|vbscript:|:\/\/|expression\(|@import|&#|<!)/i;
const COLOR_ATTRS = new Set(['fill', 'stroke']);
const NUM_ATTRS = new Set(['x', 'y', 'dx', 'dy', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'width', 'height', 'stroke-width', 'stroke-miterlimit', 'opacity', 'fill-opacity', 'stroke-opacity', 'font-size', 'markerWidth', 'markerHeight', 'refX', 'refY']);
const MARKER_ATTRS = new Set(['marker-start', 'marker-mid', 'marker-end']);
const WORD_ATTRS = new Set(['stroke-linecap', 'stroke-linejoin', 'fill-rule', 'font-weight', 'font-style', 'text-anchor', 'dominant-baseline', 'markerUnits', 'preserveAspectRatio']);

const byteLen = (s) => new TextEncoder().encode(s).length;

function attrProblem(el, name, value, nsUri) {
  if (value.length > MAX_ATTR) return 'attribute too long';
  if (nsUri || name.includes(':')) return 'namespaced attribute';
  if (name.toLowerCase().startsWith('on')) return 'event attribute';
  if (name === 'href' || name === 'style' || name === 'class') return `${name} not allowed`;
  if (!COMMON.has(name) && !(PER_ELEMENT[el] || []).includes(name)) return 'attribute not whitelisted';
  if (RE_BAD_SUBSTR.test(value)) return 'forbidden url/scheme in value';
  const v = value.trim();
  if (name === 'xmlns') return v === SVG_NS ? null : 'bad xmlns';
  if (name === 'id') return RE_ID.test(v) ? null : 'bad id';
  if (name === 'viewBox') return RE_VIEWBOX.test(v) ? null : 'bad viewBox';
  if (name === 'd') return RE_PATH.test(v) ? null : 'bad path data';
  if (name === 'points') return RE_NUMLIST.test(v) ? null : 'bad points';
  if (name === 'transform') return RE_TRANSFORM.test(v) ? null : 'bad transform';
  if (COLOR_ATTRS.has(name)) return RE_COLOR.test(v) ? null : 'bad color';
  if (MARKER_ATTRS.has(name)) return (v === 'none' || RE_URLREF.test(v)) ? null : 'bad marker ref';
  if (NUM_ATTRS.has(name)) return RE_LEN.test(v) ? null : 'bad number';
  if (name === 'stroke-dasharray') return (v === 'none' || RE_NUMLIST.test(v)) ? null : 'bad dasharray';
  if (name === 'orient') return (v === 'auto' || v === 'auto-start-reverse' || RE_LEN.test(v)) ? null : 'bad orient';
  if (name === 'font-family') {
    const fonts = v.split(',').map((f) => f.trim().replace(/^['"]+|['"]+$/g, '').toLowerCase());
    return fonts.length && fonts.every((f) => SAFE_FONTS.has(f)) ? null : 'font-family not in safe list';
  }
  if (WORD_ATTRS.has(name)) return RE_ENUM_WORDS.test(v) ? null : 'bad value';
  return 'unhandled attribute';
}

function parse(svg) {
  if (typeof svg !== 'string') return { errs: ['not a string'] };
  if (byteLen(svg) > MAX_BYTES) return { errs: [`svg larger than ${MAX_BYTES} bytes`] };
  if (/<!\s*(DOCTYPE|ENTITY)/i.test(svg) || svg.replace('<?xml', '').includes('<?')) return { errs: ['DOCTYPE/ENTITY/processing instruction not allowed'] };
  let doc;
  try { doc = new DOMParser().parseFromString(svg.trim(), 'image/svg+xml'); } catch (e) { return { errs: ['not well-formed XML'] }; }
  if (doc.getElementsByTagName('parsererror').length) return { errs: ['not well-formed XML'] };
  const root = doc.documentElement;
  if (!root || root.localName !== 'svg') return { errs: ['root element must be <svg>'] };
  return { root, errs: [] };
}
const realAttrs = (el) => [...el.attributes].filter((a) => a.namespaceURI !== XMLNS_NS);
const textNodes = (el) => [...el.childNodes].filter((n) => n.nodeType === 3 || n.nodeType === 4).map((n) => n.nodeValue);

// STRICT check: [] == valid as-is.
export function violations(svg) {
  const { root, errs } = parse(svg);
  if (!root) return errs;
  let count = 0;
  const refs = [], markerIds = new Set();
  const walk = (el, depth, isRoot) => {
    count++;
    const name = el.localName;
    if (el.namespaceURI !== SVG_NS) { errs.push(`element <${name}> not in the SVG namespace`); return; }
    if (!ELEMENTS.has(name)) { errs.push(`element <${name}> not allowed`); return; }
    if (depth > MAX_DEPTH) { errs.push('nesting too deep'); return; }
    for (const a of realAttrs(el)) {
      const why = attrProblem(name, a.name, a.value, a.namespaceURI);
      if (why) errs.push(`<${name} ${a.name}>: ${why}`);
      else if (a.name === 'id' && name === 'marker') markerIds.add(a.value.trim());
      else if (MARKER_ATTRS.has(a.name) && a.value.trim() !== 'none') refs.push(a.value.trim());
    }
    if (isRoot && !el.hasAttribute('viewBox')) errs.push('root <svg> needs a viewBox');
    const texts = textNodes(el);
    if (TEXT_ELEMENTS.has(name)) {
      for (const t of texts) {
        if (t.length > MAX_TEXT) errs.push(`text node longer than ${MAX_TEXT} chars`);
        if (RE_BAD_SUBSTR.test(t)) errs.push('text contains forbidden url/scheme');
      }
    } else if (texts.some((t) => t.trim())) errs.push(`<${name}> may not contain text`);
    for (const c of el.children) walk(c, depth + 1, false);
  };
  walk(root, 1, true);
  if (count > MAX_ELEMENTS) errs.push(`more than ${MAX_ELEMENTS} elements`);
  for (const r of refs) { const m = RE_URLREF.exec(r); if (m && !markerIds.has(m[1])) errs.push(`marker reference #${m[1]} has no <marker id>`); }
  return errs;
}

const escAttr = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
const escText = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// Rebuild from the whitelist. Returns {svg: string|null, removed: string[]}.
export function sanitize(svg) {
  const { root, errs } = parse(svg);
  if (!root) return { svg: null, removed: errs };
  const removed = [];
  let count = 0;
  const rebuild = (el, depth, isRoot, dropMarkers) => {
    const name = el.localName;
    if (el.namespaceURI !== SVG_NS || !ELEMENTS.has(name)) { removed.push(`dropped element <${name}>`); return null; }
    count++;
    if (count > MAX_ELEMENTS || depth > MAX_DEPTH) { removed.push('dropped element (size/depth cap)'); return null; }
    let s = `<${name}${isRoot ? ` xmlns="${SVG_NS}"` : ''}`;
    for (const a of realAttrs(el)) {
      if (a.name === 'xmlns') continue;
      const why = attrProblem(name, a.name, a.value, a.namespaceURI);
      if (why) { removed.push(`dropped <${name} ${a.name}>: ${why}`); continue; }
      if (dropMarkers && MARKER_ATTRS.has(a.name)) continue;
      s += ` ${a.name}="${escAttr(a.value.trim())}"`;
    }
    s += '>';
    for (const n of el.childNodes) {
      if (n.nodeType === 1) { const r = rebuild(n, depth + 1, false, dropMarkers); if (r != null) s += r; }
      else if ((n.nodeType === 3 || n.nodeType === 4) && TEXT_ELEMENTS.has(name)) {
        const t = n.nodeValue;
        if (!RE_BAD_SUBSTR.test(t)) s += escText(t.slice(0, MAX_TEXT));
      }
    }
    return s + `</${name}>`;
  };
  let out = rebuild(root, 1, true, false);
  if (out == null || !root.hasAttribute('viewBox')) return { svg: null, removed: [...removed, 'root <svg> has no valid viewBox'] };
  if (violations(out).length) {
    count = 0;
    out = rebuild(root, 1, true, true); // drop dangling marker refs
    if (out == null || violations(out).length) return { svg: null, removed: [...removed, ...violations(out || '')] };
  }
  return { svg: out, removed };
}

// What the UI uses: the figure's svg if valid, else its sanitized form, else null (hide the figure).
export function safeSvg(svg) {
  if (!violations(svg).length) return svg;
  const r = sanitize(svg);
  return r.svg && !violations(r.svg).length ? r.svg : null;
}
export const svgDataUrl = (svg) => `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
