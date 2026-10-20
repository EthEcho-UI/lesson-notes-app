"""Build transcript.json: merge per-part segments (timeline offsets) and the `merged.text` fed to Claude."""


def merged_text(parts):
    blocks = []
    for p in parts:
        segs = p["segments"]
        if any(s.get("speaker") for s in segs):
            out, cur = [], None
            for s in segs:
                sp = s.get("speaker")
                if sp and sp != cur:
                    out.append(f"[{sp}] {s['text']}")
                    cur = sp
                else:
                    out.append(s["text"])
            body = " ".join(out)
        else:
            body = " ".join(s["text"] for s in segs)
        blocks.append(f"[Část {p['order']}]\n{body}".rstrip())
    return "\n".join(blocks)


def build(note_id, language, engine, diarization, old, new_parts, now_iso):
    """old: existing transcript doc or None; new_parts: list of part dicts for the parts produced by this job
    (they replace old parts with the same part_id; old parts keep their segments because their audio is gone)."""
    replaced = {p["part_id"] for p in new_parts}
    parts = [p for p in (old["parts"] if old else []) if p["part_id"] not in replaced] + list(new_parts)
    parts.sort(key=lambda p: p["order"])
    off = 0.0
    for p in parts:
        p["offset"] = round(off, 3)
        off += p["duration"]
    doc = {
        "schema_version": 1, "note_id": note_id, "language": language, "engine": engine,
        "diarization": diarization, "updated_at": now_iso, "parts": parts,
        "merged": {"duration": round(off, 3), "text": merged_text(parts)},
    }
    return doc
