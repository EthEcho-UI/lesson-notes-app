"""Validation of every file the worker writes against the data contract (bundled copies of the
b1 schemas + the semantic rules JSON Schema cannot express)."""
import json
import re
from importlib import resources
from jsonschema import Draft202012Validator

_SCHEMAS = {}


def _schema(kind):
    if kind not in _SCHEMAS:
        text = (resources.files("lesson_worker") / "schemas" / f"{kind}.schema.json").read_text(encoding="utf-8")
        _SCHEMAS[kind] = Draft202012Validator(json.loads(text))
    return _SCHEMAS[kind]


URL_RE = re.compile(r"(https?://|www\.)", re.I)
LESSON_ORDER = ["title", "description", "outline", "key_points", "figures", "study_materials",
                "homework", "open_questions", "uncertainties"]
MEETING_ORDER = ["title", "description", "outline", "key_points", "figures", "decisions", "action_items",
                 "follow_up_materials", "open_questions", "uncertainties"]


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _images_rules(imgs, label, e):
    ids = [i["id"] for i in imgs]
    if len(set(ids)) != len(ids):
        e.append(f"{label}: duplicate image ids")
    names = [i.get("filename", i.get("file", "")).split("/")[-1] for i in imgs]
    if len(set(names)) != len(names):
        e.append(f"{label}: duplicate image file names")
    for i, n in zip(imgs, names):
        if n[:2] != i["id"][1:]:
            e.append(f"{label}: image {i['id']} does not match file {n}")


def _notes_v2(d, e):
    from . import svg_sanitize
    c = d["content"]
    order = LESSON_ORDER if d["kind"] == "lesson" else MEETING_ORDER
    keys = [k for k in c if k in order]
    if keys != sorted(keys, key=order.index):
        e.append(f"notes: content keys not in required order {order}")
    headings = [s["heading"] for s in c["outline"]]
    if len(set(headings)) != len(headings):
        e.append("notes: duplicate outline headings")
    fids = [f["id"] for f in c.get("figures", [])]
    if len(set(fids)) != len(fids):
        e.append("notes: duplicate figure ids")
    for f in c.get("figures", []):
        for v in svg_sanitize.violations(f["svg"]):
            e.append(f"notes: figure {f['id']} svg: {v}")
        if "refs" in f and f["refs"] not in headings:
            e.append(f"notes: figure {f['id']} refs {f['refs']!r} is not an outline heading")
    for m in c.get("study_materials", []) + c.get("follow_up_materials", []):
        if any(URL_RE.search(s) for s in _strings(m)):
            e.append(f"notes: URL in material {m['title']!r}")
    for s in c["outline"]:
        if not s["points"]:
            e.append(f"notes: empty section {s['heading']!r}")


def _semantic(kind, d):
    e = []
    v2 = d.get("schema_version") == 2

    def contiguous(parts, label, must_start_at_1):
        o = [p["order"] for p in parts]
        ids = [p.get("id", p.get("part_id")) for p in parts]
        if len(set(ids)) != len(ids):
            e.append(f"{label}: duplicate part ids")
        if len(set(o)) != len(o):
            e.append(f"{label}: duplicate order values")
        if o != sorted(o):
            e.append(f"{label}: parts not sorted by order")
        if o and sorted(o) != list(range(sorted(o)[0], sorted(o)[0] + len(o))):
            e.append(f"{label}: order gap {o}")
        if must_start_at_1 and o and min(o) != 1:
            e.append(f"{label}: order must start at 1")
        for p in parts:
            pid = p.get("id", p.get("part_id"))
            if pid != f"p{p['order']:02d}":
                e.append(f"{label}: id {pid} does not match order {p['order']}")

    if kind == "note":
        contiguous(d["parts"], "note.parts", True)
        if v2:
            _images_rules(d["images"], "note.images", e)
    elif kind == "job":
        if d["parts"]:
            contiguous(d["parts"], "job.parts", not d["append"])
        if v2:
            _images_rules(d["images"], "job.images", e)
            for i in d["images"]:
                if i["location"] == "inbox" and d["audio_location"] == "local":
                    e.append("job: image location inbox with audio_location local (use local)")
        if d["action"] == "regenerate_notes" and not d["append"]:
            e.append("job: regenerate_notes requires append=true")
    elif kind == "transcript":
        contiguous(d["parts"], "transcript.parts", True)
        off = 0.0
        for p in d["parts"]:
            if abs(p["offset"] - off) > 0.01:
                e.append(f"transcript: part {p['part_id']} offset {p['offset']} != expected {off}")
            off += p["duration"]
            prev = 0.0
            for s in p["segments"]:
                if s["end"] < s["start"]:
                    e.append(f"transcript: segment end<start in {p['part_id']}")
                if s["start"] < prev - 1e-6:
                    e.append(f"transcript: segments not ordered in {p['part_id']}")
                prev = s["start"]
                if v2:
                    m = d["language_mode"]
                    if m in ("cs", "en") and s["lang"] != m:
                        e.append(f"transcript: language_mode {m} but segment lang {s['lang']} in {p['part_id']}")
                    if m == "mixed" and s["lang"] not in ("cs", "en"):
                        e.append(f"transcript: mixed mode final segment lang must be cs|en, got {s['lang']}")
                    if "lang_detected" in s and s["lang_detected"] == s["lang"]:
                        e.append("transcript: lang_detected equals lang (omit it)")
            if v2 and abs(sum(x["share"] for x in p["detected_languages"]) - 1) > 0.05:
                e.append(f"transcript: detected_languages shares of {p['part_id']} do not sum to ~1")
        if abs(d["merged"]["duration"] - off) > 0.01:
            e.append("transcript: merged.duration != sum of part durations")
    elif kind == "notes":
        if len(set(d["based_on_parts"])) != len(d["based_on_parts"]):
            e.append("notes: duplicate based_on_parts")
        if v2:
            _notes_v2(d, e)
    return e


def problems(kind, doc):
    errs = [f"{'/'.join(map(str, x.absolute_path)) or '<root>'}: {x.message[:120]}"
            for x in _schema(kind).iter_errors(doc)]
    return errs or _semantic(kind, doc)


class InvalidDocument(ValueError):
    pass


def check(kind, doc):
    """Raise InvalidDocument if `doc` violates the contract; return doc otherwise."""
    p = problems(kind, doc)
    if p:
        raise InvalidDocument(f"{kind} violates contract: " + "; ".join(p[:4]))
    return doc


def dumps(doc):
    """Contract serialisation: UTF-8, 2-space indent, trailing newline."""
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
