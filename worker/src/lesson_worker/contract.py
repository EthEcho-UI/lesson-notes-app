"""Validation of every file the worker writes against the data contract (bundled copies of the
b1 schemas + the semantic rules JSON Schema cannot express)."""
import json
from importlib import resources
from jsonschema import Draft202012Validator

_SCHEMAS = {}


def _schema(kind):
    if kind not in _SCHEMAS:
        text = (resources.files("lesson_worker") / "schemas" / f"{kind}.schema.json").read_text(encoding="utf-8")
        _SCHEMAS[kind] = Draft202012Validator(json.loads(text))
    return _SCHEMAS[kind]


def _semantic(kind, d):
    e = []

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
    elif kind == "job":
        if d["parts"]:
            contiguous(d["parts"], "job.parts", not d["append"])
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
        if abs(d["merged"]["duration"] - off) > 0.01:
            e.append("transcript: merged.duration != sum of part durations")
    elif kind == "notes":
        if len(set(d["based_on_parts"])) != len(d["based_on_parts"]):
            e.append("notes: duplicate based_on_parts")
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
