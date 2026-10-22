"""Heuristic layout lint for LLM-drawn figures (on top of svg_sanitize, which only checks safety).

No rendering: text boxes are estimated (0.56 x font-size per char). Catches the common failures of drawn
SVGs: text outside the canvas, tiny fonts, heavily overlapping labels. lint() never raises."""
import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
MIN_FONT = 14.0
CHAR_W = 0.56


def _num(v, default=0.0):
    m = re.match(r"\s*(-?\d+\.?\d*|-?\.\d+)", v or "")
    return float(m.group(1)) if m else default


def _canvas(root):
    vb = (root.get("viewBox") or "").replace(",", " ").split()
    try:
        x, y, w, h = (float(t) for t in vb)
        return x, y, w, h
    except ValueError:
        return 0.0, 0.0, 800.0, 500.0


def _text_boxes(root):
    boxes = []
    for el in root.iter(f"{{{SVG_NS}}}text"):
        size = _num(el.get("font-size"), 16.0)
        parts = [(el.text or "")] + [(c.text or "") + (c.tail or "") for c in el]
        txt = "".join(parts).strip()
        if not txt:
            continue
        x, y = _num(el.get("x")), _num(el.get("y"))
        w = len(txt) * CHAR_W * size
        anchor = el.get("text-anchor", "start")
        x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        boxes.append((x0, y - size, x0 + w, y + size * 0.25, txt, size))
    return boxes


def lint(svg):
    """Returns (issues: list[str], severe: bool)."""
    issues, severe = [], False
    try:
        root = ET.fromstring(svg.strip())
    except ET.ParseError:
        return ["not parseable"], True
    cx, cy, cw, ch = _canvas(root)
    boxes = _text_boxes(root)
    small = [b for b in boxes if b[5] < MIN_FONT - 0.01]
    if small:
        issues.append(f"{len(small)} text(s) smaller than {MIN_FONT:g}px")
    out = [b for b in boxes if b[0] < cx - 8 or b[1] < cy - 8 or b[2] > cx + cw + 8 or b[3] > cy + ch + 8]
    if out:
        issues.append(f"{len(out)} text(s) estimated outside the canvas, e.g. {out[0][4]!r}")
        if len(out) >= 2 or any(b[0] < cx - 40 or b[2] > cx + cw + 40 for b in out):
            severe = True
    overlaps = 0
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            ix = min(a[2], b[2]) - max(a[0], b[0])
            iy = min(a[3], b[3]) - max(a[1], b[1])
            if ix > 4 and iy > 4:
                smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
                if ix * iy / smaller > 0.3:
                    overlaps += 1
    if overlaps:
        issues.append(f"{overlaps} overlapping text pair(s)")
        if overlaps >= 3:
            severe = True
    if len(boxes) == 0:
        issues.append("no text labels")
        severe = True
    return issues, severe


def bump_fonts(svg):
    """Raise font-size below MIN_FONT to MIN_FONT (string-level, keeps everything else)."""
    def fix(m):
        v = float(m.group(1))
        return f'font-size="{MIN_FONT:g}"' if v < MIN_FONT else m.group(0)
    return re.sub(r'font-size="(\d+\.?\d*)(?:px)?"', fix, svg)
