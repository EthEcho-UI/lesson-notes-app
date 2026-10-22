#!/usr/bin/env python3
"""Reference implementation of the restricted-SVG rules (contract v2, CONTRACT.md "SVG rules").

Pure stdlib. The worker (Python) imports/copies this; the PWA ports it to JS (DOMParser, same
whitelist) and must pass the same vectors in svg_vectors.json (see test_svg_sanitize.py).

  violations(svg: str) -> list[str]     # empty list == valid as-is (STRICT check, used by validate.py)
  sanitize(svg: str)  -> (str | None, list[str])
        # rebuilds the SVG from whitelisted elements/attributes only; second value lists what was
        # removed. Returns (None, reasons) if it cannot be made valid (not XML, too big, no <svg> root,
        # DOCTYPE/ENTITY, too many elements, ...). The result always satisfies violations() == [].
"""
import re
import xml.etree.ElementTree as ET

SVG_NS = "http://www.w3.org/2000/svg"
MAX_BYTES = 20000
MAX_ELEMENTS = 300
MAX_DEPTH = 12
MAX_TEXT = 200          # chars per text node
MAX_ATTR = 4000         # chars per attribute value (path data)

ELEMENTS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
            "text", "tspan", "defs", "marker", "title", "desc"}
TEXT_ELEMENTS = {"text", "tspan", "title", "desc"}

# Attributes allowed on every element (no style/class/on*/href/xlink:href: never allowed anywhere).
COMMON = {"id", "transform", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
          "stroke-dasharray", "stroke-miterlimit", "opacity", "fill-opacity", "stroke-opacity",
          "fill-rule"}
PER_ELEMENT = {
    "svg": {"viewBox", "width", "height", "xmlns", "preserveAspectRatio"},
    "g": set(),
    "path": {"d", "marker-start", "marker-mid", "marker-end"},
    "rect": {"x", "y", "width", "height", "rx", "ry"},
    "circle": {"cx", "cy", "r"},
    "ellipse": {"cx", "cy", "rx", "ry"},
    "line": {"x1", "y1", "x2", "y2", "marker-start", "marker-end"},
    "polyline": {"points", "marker-start", "marker-mid", "marker-end"},
    "polygon": {"points", "marker-start", "marker-mid", "marker-end"},
    "text": {"x", "y", "dx", "dy", "font-size", "font-family", "font-weight", "font-style",
             "text-anchor", "dominant-baseline"},
    "tspan": {"x", "y", "dx", "dy", "font-size", "font-family", "font-weight", "font-style",
              "text-anchor", "dominant-baseline"},
    "defs": set(),
    "marker": {"markerWidth", "markerHeight", "refX", "refY", "orient", "markerUnits", "viewBox"},
    "title": set(),
    "desc": set(),
}
SAFE_FONTS = {"sans-serif", "serif", "monospace", "arial", "helvetica", "dejavu sans",
              "liberation sans", "noto sans"}

NUM = r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
RE_LEN = re.compile(rf"^{NUM}(?:px|%|em|pt)?$")
RE_NUMLIST = re.compile(rf"^(?:{NUM}[\s,]*)+$")
RE_VIEWBOX = re.compile(rf"^{NUM}[\s,]+{NUM}[\s,]+{NUM}[\s,]+{NUM}$")
RE_PATH = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE\s,.+-]*$")
RE_TRANSFORM = re.compile(rf"^(?:(?:translate|scale|rotate|skewX|skewY|matrix)\(\s*(?:{NUM}[\s,]*)+\)\s*)+$")
RE_COLOR = re.compile(r"^(?:none|currentColor|#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,20}|"
                      r"rgba?\(\s*[\d.%\s,]+\))$")
RE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,40}$")
RE_URLREF = re.compile(r"^url\(#([A-Za-z][A-Za-z0-9_-]{0,40})\)$")
RE_ENUM_WORDS = re.compile(r"^[A-Za-z0-9 ,-]{1,40}$")
RE_BAD_SUBSTR = re.compile(r"(javascript:|data:|vbscript:|://|expression\(|@import|&#|<!)", re.I)

COLOR_ATTRS = {"fill", "stroke"}
NUM_ATTRS = {"x", "y", "dx", "dy", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width",
             "height", "stroke-width", "stroke-miterlimit", "opacity", "fill-opacity",
             "stroke-opacity", "font-size", "markerWidth", "markerHeight", "refX", "refY"}
MARKER_ATTRS = {"marker-start", "marker-mid", "marker-end"}
WORD_ATTRS = {"stroke-linecap", "stroke-linejoin", "fill-rule", "font-weight", "font-style",
              "text-anchor", "dominant-baseline", "markerUnits", "preserveAspectRatio"}


def _local(tag):
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return name, ns
    return tag, ""


def _attr_problem(el, name, value):
    """Return None if (el, name, value) is allowed, else a reason string."""
    if len(value) > MAX_ATTR:
        return "attribute too long"
    if name.startswith("{") or ":" in name:
        return "namespaced attribute"            # xlink:href, xml:space, ...
    if name.lower().startswith("on"):
        return "event attribute"
    if name in ("href", "style", "class"):
        return f"{name} not allowed"
    if name not in COMMON and name not in PER_ELEMENT[el]:
        return "attribute not whitelisted"
    if RE_BAD_SUBSTR.search(value):
        return "forbidden url/scheme in value"
    v = value.strip()
    if name == "xmlns":
        return None if v == SVG_NS else "bad xmlns"
    if name == "id":
        return None if RE_ID.match(v) else "bad id"
    if name == "viewBox":
        return None if RE_VIEWBOX.match(v) else "bad viewBox"
    if name == "d":
        return None if RE_PATH.match(v) else "bad path data"
    if name == "points":
        return None if RE_NUMLIST.match(v) else "bad points"
    if name == "transform":
        return None if RE_TRANSFORM.match(v) else "bad transform"
    if name in COLOR_ATTRS:
        return None if RE_COLOR.match(v) else "bad color"
    if name in MARKER_ATTRS:
        return None if (v == "none" or RE_URLREF.match(v)) else "bad marker ref"
    if name in NUM_ATTRS:
        return None if RE_LEN.match(v) else "bad number"
    if name == "stroke-dasharray":
        return None if (v == "none" or RE_NUMLIST.match(v)) else "bad dasharray"
    if name == "orient":
        return None if (v in ("auto", "auto-start-reverse") or RE_LEN.match(v)) else "bad orient"
    if name == "font-family":
        fonts = [f.strip().strip("'\"").lower() for f in v.split(",")]
        return None if fonts and all(f in SAFE_FONTS for f in fonts) else "font-family not in safe list"
    if name in WORD_ATTRS:
        return None if RE_ENUM_WORDS.match(v) else "bad value"
    return "unhandled attribute"


def _parse(svg):
    if not isinstance(svg, str):
        return None, ["not a string"]
    if len(svg.encode("utf-8")) > MAX_BYTES:
        return None, [f"svg larger than {MAX_BYTES} bytes"]
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", svg, re.I) or "<?" in svg.replace("<?xml", "", 1):
        return None, ["DOCTYPE/ENTITY/processing instruction not allowed"]
    try:
        root = ET.fromstring(svg.strip())
    except ET.ParseError as ex:
        return None, [f"not well-formed XML: {ex}"]
    name, ns = _local(root.tag)
    if name != "svg":
        return None, ["root element must be <svg>"]
    return root, []


def violations(svg):
    root, errs = _parse(svg)
    if root is None:
        return errs
    count = 0
    ids, marker_ids, refs = set(), set(), []

    def walk(el, depth, is_root):
        nonlocal count
        count += 1
        name, ns = _local(el.tag)
        if ns != SVG_NS:
            errs.append(f"element <{name}> not in the SVG namespace (xmlns missing/wrong)")
            return
        if name not in ELEMENTS:
            errs.append(f"element <{name}> not allowed")
            return
        if depth > MAX_DEPTH:
            errs.append("nesting too deep")
            return
        for a, v in el.attrib.items():
            if a == "xmlns":
                continue                         # ElementTree never reports it; kept for safety
            why = _attr_problem(name, a, v)
            if why:
                errs.append(f"<{name} {a}>: {why}")
            elif a == "id":
                (marker_ids if name == "marker" else ids).add(v.strip())
            elif a in MARKER_ATTRS and v.strip() != "none":
                refs.append(v.strip())
        if is_root and "viewBox" not in el.attrib:
            errs.append("root <svg> needs a viewBox")
        if name in TEXT_ELEMENTS:
            texts = [el.text or ""] + [c.tail or "" for c in el]
            for t in texts:
                if len(t) > MAX_TEXT:
                    errs.append(f"text node longer than {MAX_TEXT} chars")
                if RE_BAD_SUBSTR.search(t):
                    errs.append("text contains forbidden url/scheme")
        else:
            if (el.text or "").strip() or any((c.tail or "").strip() for c in el):
                errs.append(f"<{name}> may not contain text")
        for c in el:
            walk(c, depth + 1, False)

    walk(root, 1, True)
    # xmlns presence on the root is required for <img src="data:image/svg+xml,..."> rendering;
    # ElementTree exposes it through the tag namespace, checked above (ns != SVG_NS).
    if count > MAX_ELEMENTS:
        errs.append(f"more than {MAX_ELEMENTS} elements")
    for r in refs:
        m = RE_URLREF.match(r)
        if m and m.group(1) not in marker_ids:
            errs.append(f"marker reference #{m.group(1)} has no <marker id>")
    return errs


def sanitize(svg):
    """Rebuild from whitelist. Returns (clean_svg | None, removed_reasons)."""
    root, errs = _parse(svg)
    if root is None:
        return None, errs
    removed = []
    count = 0

    def rebuild(el, depth, is_root):
        nonlocal count
        name, ns = _local(el.tag)
        if ns != SVG_NS or name not in ELEMENTS:
            removed.append(f"dropped element <{name}>")
            return None
        count += 1
        if count > MAX_ELEMENTS or depth > MAX_DEPTH:
            removed.append("dropped element (size/depth cap)")
            return None
        out = ET.Element(f"{{{SVG_NS}}}{name}")
        for a, v in el.attrib.items():
            why = _attr_problem(name, a, v)
            if why:
                removed.append(f"dropped <{name} {a}>: {why}")
            else:
                out.set(a, v.strip())
        if name in TEXT_ELEMENTS:
            out.text = (el.text or "")[:MAX_TEXT] if not RE_BAD_SUBSTR.search(el.text or "") else ""
        for c in el:
            r = rebuild(c, depth + 1, False)
            if r is not None:
                out.append(r)
                if name in TEXT_ELEMENTS:
                    t = c.tail or ""
                    r.tail = t[:MAX_TEXT] if not RE_BAD_SUBSTR.search(t) else ""
        return out

    clean = rebuild(root, 1, True)
    if clean is None or "viewBox" not in clean.attrib:
        return None, removed + ["root <svg> has no valid viewBox"]
    ET.register_namespace("", SVG_NS)
    text = ET.tostring(clean, encoding="unicode")
    # drop marker refs whose marker was removed
    bad = violations(text)
    if bad:
        for a in list(clean.iter()):
            for m in MARKER_ATTRS:
                if m in a.attrib:
                    del a.attrib[m]
                    removed.append(f"dropped {m}: dangling reference")
        text = ET.tostring(clean, encoding="unicode")
        bad = violations(text)
    if bad:
        return None, removed + bad
    return text, removed


if __name__ == "__main__":
    import sys
    data = sys.stdin.read()
    out, info = sanitize(data)
    print("\n".join(info), file=sys.stderr)
    if out is None:
        sys.exit(1)
    print(out)
