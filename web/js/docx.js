// Minimal DOCX writer (WordprocessingML) on top of zip.js. Renders the neutral document model of docmodel.js.
// Why hand-written instead of a library: the `docx` npm package is a ~1 MB browser bundle that needs a build step
// (this repo has none) and would have to be vendored; the subset we need (headings, bullets, runs, images) is small.
import { makeZip } from './zip.js';
import { t, getLang } from './i18n.js';
import { svgDataUrl } from './svgsafe.js';

const BAD_XML = /[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g;
const esc = (s) => String(s ?? '').replace(BAD_XML, '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"';
const EMU_PER_PX = 9525, MAX_W = 6000000, MAX_H = 7600000;

const run = (text, { bold, italic, size, color } = {}) =>
  `<w:r><w:rPr>${bold ? '<w:b/>' : ''}${italic ? '<w:i/>' : ''}${color ? `<w:color w:val="${color}"/>` : ''}${size ? `<w:sz w:val="${size}"/><w:szCs w:val="${size}"/>` : ''}</w:rPr><w:t xml:space="preserve">${esc(text)}</w:t></w:r>`;
const para = (inner, { style, num, ind, keepNext, spacingAfter } = {}) =>
  `<w:p><w:pPr>${style ? `<w:pStyle w:val="${style}"/>` : ''}${keepNext ? '<w:keepNext/>' : ''}${num != null ? `<w:numPr><w:ilvl w:val="${num}"/><w:numId w:val="1"/></w:numPr>` : ''}${spacingAfter != null ? `<w:spacing w:after="${spacingAfter}"/>` : ''}${ind ? `<w:ind w:left="${ind}"/>` : ''}</w:pPr>${inner}</w:p>`;

function svgSize(svg) {
  const m = /viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"/.exec(svg);
  const w = m ? parseFloat(m[1]) : 400, h = m ? parseFloat(m[2]) : 300;
  return { w: w > 0 ? w : 400, h: h > 0 ? h : 300 };
}
// SVG -> PNG bytes (rasterised through <img> + canvas; the SVG never touches the DOM tree)
export async function svgToPng(svg, widthPx = 1000) {
  const { w, h } = svgSize(svg);
  const k = widthPx / w;
  const cw = Math.round(w * k), ch = Math.min(4000, Math.round(h * k));
  const img = new Image();
  await new Promise((res, rej) => { img.onload = res; img.onerror = () => rej(new Error('svg render')); img.src = svgDataUrl(svg); });
  const c = document.createElement('canvas');
  c.width = cw; c.height = ch;
  const g = c.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, cw, ch);
  g.drawImage(img, 0, 0, cw, ch);
  const blob = await new Promise((res) => c.toBlob(res, 'image/png'));
  return { bytes: new Uint8Array(await blob.arrayBuffer()), width: cw, height: ch };
}

const STYLES = (lang) => `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="${lang}" w:eastAsia="${lang}"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="80"/></w:pPr><w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:b/><w:sz w:val="48"/><w:szCs w:val="48"/><w:color w:val="24463F"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="100"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:b/><w:sz w:val="32"/><w:szCs w:val="32"/><w:color w:val="24463F"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="200" w:after="60"/><w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:sz w:val="26"/><w:szCs w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:qFormat/><w:rPr><w:i/><w:sz w:val="19"/><w:szCs w:val="19"/><w:color w:val="555555"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Meta"><w:name w:val="Meta"/><w:basedOn w:val="Normal"/><w:rPr><w:color w:val="66726E"/><w:sz w:val="20"/><w:szCs w:val="20"/></w:rPr></w:style>
</w:styles>`;

const NUMBERING = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>${[0, 1, 2].map((l) => `<w:lvl w:ilvl="${l}"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="${['•', '–', '·'][l]}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="${360 + 360 * (l + 1)}" w:hanging="360"/></w:pPr></w:lvl>`).join('')}</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>`;

// doc: {title, blocks} from docmodel.buildDoc (photos loaded). Returns Uint8Array (.docx)
export async function makeDocx(doc) {
  const media = [];   // {name, bytes, ext}
  const body = [];
  let drawId = 1;
  const image = (bytes, ext, wpx, hpx, alt) => {
    const name = `image${media.length + 1}.${ext}`;
    media.push({ name, bytes, ext });
    const rid = `rIdImg${media.length}`;
    let cx = wpx * EMU_PER_PX, cy = hpx * EMU_PER_PX;
    const k = Math.min(1, MAX_W / cx, MAX_H / cy);
    cx = Math.round(cx * k); cy = Math.round(cy * k);
    const id = drawId++;
    return para(`<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="${cx}" cy="${cy}"/><wp:docPr id="${id}" name="Obrazek ${id}" descr="${esc(alt)}"/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="${id}" name="${esc(name)}" descr="${esc(alt)}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="${rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>`, { keepNext: true });
  };
  for (const b of doc.blocks) {
    switch (b.t) {
      case 'title': body.push(para(run(b.text), { style: 'Title' })); break;
      case 'meta': body.push(para(run(b.text), { style: 'Meta' })); break;
      case 'p': body.push(para(run(b.text))); break;
      case 'h2': body.push(para(run(b.text), { style: 'Heading1' })); break;
      case 'h3': body.push(para(run(b.text), { style: 'Heading2' })); break;
      case 'list': for (const it of b.items) body.push(para(it.runs.map((r) => run(r.text, r)).join(''), { num: Math.min(2, it.level), spacingAfter: 40 })); break;
      case 'task': body.push(para(run(`${b.done ? '☑' : '☐'} `) + run(b.text), { ind: 240, spacingAfter: 40 })); break;
      case 'qa':
        body.push(para(run(`${b.label}: `, { bold: true, color: '24463F' }) + run(b.title, { bold: true }), { keepNext: true, spacingAfter: 20 }));
        if (b.q) body.push(para(run(b.q), { ind: 240, spacingAfter: b.a ? 20 : 120 }));
        if (b.a) body.push(para(run(`${t('answer')}: `, { bold: true, italic: true }) + run(b.a, { italic: true }), { ind: 240 }));
        break;
      case 'figure': {
        try {
          const png = await svgToPng(b.svg);
          body.push(para(run(b.title, { bold: true }), { keepNext: true, spacingAfter: 40 }));
          body.push(image(png.bytes, 'png', png.width, png.height, `${b.title}. ${b.caption || ''}`));
          if (b.caption) body.push(para(run(b.caption), { style: 'Caption' }));
        } catch { body.push(para(run(`[${b.title}]`, { italic: true }))); }
        break;
      }
      case 'photo': {
        if (b.blob) {
          const bytes = new Uint8Array(await b.blob.arrayBuffer());
          const ext = /png/.test(b.blob.type) ? 'png' : /webp/.test(b.blob.type) ? 'webp' : 'jpeg';
          body.push(image(bytes, ext, b.width || 800, b.height || 600, b.caption || b.id));
          if (b.caption) body.push(para(run(b.caption), { style: 'Caption' }));
        } else body.push(para(run(`[${b.caption || b.id}]`, { italic: true })));
        break;
      }
      case 'tr': body.push(para(run(`[${b.time}] `, { size: 18, color: '66726E' }) + (b.speaker ? run(`${b.speaker}: `, { bold: true, size: 20 }) : '') + run(b.text, { size: 20 }), { spacingAfter: 60 })); break;
      default: break;
    }
  }
  const sect = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>';
  const lang = getLang() === 'en' ? 'en-US' : 'cs-CZ';
  const exts = new Set(media.map((m) => m.ext));
  const ctypes = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>${[...exts].map((e) => `<Default Extension="${e}" ContentType="image/${e}"/>`).join('')}<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>`;
  const rels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>`;
  const docRels = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rIdNum" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>${media.map((m, i) => `<Relationship Id="rIdImg${i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/${m.name}"/>`).join('')}</Relationships>`;
  const core = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>${esc(doc.title)}</dc:title><dc:language>${lang}</dc:language><dcterms:created xsi:type="dcterms:W3CDTF">${new Date().toISOString().replace(/\.\d+Z$/, 'Z')}</dcterms:created></cp:coreProperties>`;
  const document = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document ${NS}><w:body>${body.join('')}${sect}</w:body></w:document>`;
  return makeZip([
    { name: '[Content_Types].xml', data: ctypes }, { name: '_rels/.rels', data: rels }, { name: 'word/document.xml', data: document },
    { name: 'word/_rels/document.xml.rels', data: docRels }, { name: 'word/styles.xml', data: STYLES(lang) }, { name: 'word/numbering.xml', data: NUMBERING },
    { name: 'docProps/core.xml', data: core }, ...media.map((m) => ({ name: `word/media/${m.name}`, data: m.bytes })),
  ]);
}
