"""Post-processing of the notes content before it is committed (contract v2 F/G): the worker, not the model,
guarantees that committed files pass validation. Never raises on model quirks; repairs or drops instead."""
import logging
import re

log = logging.getLogger("lesson_worker.notes_final")

LESSON_ORDER = ["title", "description", "outline", "key_points", "figures", "study_materials", "homework", "open_questions", "uncertainties"]
MEETING_ORDER = ["title", "description", "outline", "key_points", "figures", "decisions", "action_items", "follow_up_materials",
                 "open_questions", "uncertainties"]
MAX_FIGURES = 6
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"']+", re.I)


def _url_to_terms(m):
    """A URL is replaced by its words (host without www/tld + path words): usable as search terms, not a link."""
    u = re.sub(r"^(?:https?://)?(?:www\.)?", "", m.group(0))
    host, _, path = u.partition("/")
    words = re.split(r"[^A-Za-z0-9ÁČĎÉĚÍŇÓŘŠŤÚŮÝŽáčďéěíňóřšťúůýž]+", host.rsplit(".", 1)[0] + " " + path)
    return " ".join(w for w in words if w)


def strip_urls(s):
    return re.sub(r"\s+([,.;:])", r"\1", URL_RE.sub(_url_to_terms, s)).strip()


def _strip_urls_deep(x):
    if isinstance(x, str):
        return strip_urls(x)
    if isinstance(x, list):
        return [_strip_urls_deep(v) for v in x]
    if isinstance(x, dict):
        return {k: _strip_urls_deep(v) for k, v in x.items()}
    return x


def sanitize_figures(figs, headings, sanitize):
    out, seen = [], set()
    for f in figs or []:
        if len(out) >= MAX_FIGURES:
            log.warning("more than %d figures: extra figures dropped", MAX_FIGURES)
            break
        svg, removed = sanitize(f.get("svg", ""))
        if svg is None or f.get("id") in seen:
            log.warning("figure %s dropped (%s)", f.get("id"), "; ".join(removed[:2]) if svg is None else "duplicate id")
            continue
        if removed:
            log.info("figure %s sanitized: %d change(s)", f.get("id"), len(removed))
        g = dict(f, svg=svg)
        if "refs" in g and g["refs"] not in headings:
            g.pop("refs")
        seen.add(g["id"])
        out.append(g)
    return out


def finalize(content, kind):
    """Return content in normative key order with figures sanitized, URLs removed from materials, small structural
    repairs applied. The caller still runs the full schema validation afterwards."""
    from . import svg_sanitize
    order = MEETING_ORDER if kind == "meeting" else LESSON_ORDER
    c = {k: content[k] for k in order if k in content}
    # outline: unique headings, no empty sections
    seen, outline = set(), []
    for s in c.get("outline", []):
        if not s.get("points"):
            continue
        h, n = s["heading"], 2
        while h in seen:
            h = f"{s['heading']} ({n})"
            n += 1
        seen.add(h)
        outline.append(dict(s, heading=h))
    c["outline"] = outline
    # key points: definition/formula need a term
    c["key_points"] = [kp if kp["kind"] not in ("definition", "formula") or kp.get("term") else dict(kp, kind="important")
                       for kp in c.get("key_points", [])]
    # figures
    if "figures" in c:
        c["figures"] = sanitize_figures(c["figures"], {s["heading"] for s in outline}, svg_sanitize.sanitize)
    # materials: no URLs; a flashcard needs an answer
    for key in ("study_materials", "follow_up_materials"):
        if key in c:
            mats = []
            for m in _strip_urls_deep(c[key]):
                if m.get("kind") == "flashcard" and not m.get("answer"):
                    m = dict(m, kind="topic_to_review")
                    m.pop("answer", None)
                if m.get("text", "").strip() or m.get("title", "").strip():
                    mats.append(dict(m, text=m.get("text") or m["title"]))
            c[key] = mats
    return c
