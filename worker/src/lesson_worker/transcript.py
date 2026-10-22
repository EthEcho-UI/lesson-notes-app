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


def detected_shares(segments):
    """Raw detector output for one part: share of speech time per language (first detection, before any re-run)."""
    tot = {}
    for sg in segments:
        lg = sg.get("lang_detected") or sg.get("lang") or "cs"
        if lg:
            tot[lg] = tot.get(lg, 0.0) + max(sg["end"] - sg["start"], 0.01)
    s = sum(tot.values())
    if not s:
        return []
    out = [{"lang": k, "share": round(v / s, 3)} for k, v in sorted(tot.items(), key=lambda kv: -kv[1])]
    out[0]["share"] = round(out[0]["share"] + (1 - sum(x["share"] for x in out)), 3)      # shares sum to 1 despite rounding
    return out


def dominant_language(parts, default="cs"):
    """Dominant detected language over all parts (speech-time weighted); ties -> cs."""
    tot = {}
    for p in parts:
        for sg in p["segments"]:
            lg = sg.get("lang_detected") or sg.get("lang") or "cs"
            if lg:
                tot[lg] = tot.get(lg, 0.0) + max(sg["end"] - sg["start"], 0.01)
    if not tot:
        return default
    best = max(tot.values())
    return "cs" if tot.get("cs", -1) >= best else max(tot, key=lambda k: (tot[k], k == "cs"))


def upgrade_part_v1(part, lang="cs"):
    """A part written by a v1 worker has no language data: every segment was decoded as `lang` (v1: always Czech)."""
    for sg in part["segments"]:
        sg.setdefault("lang", lang)
    part.setdefault("detected_languages", [{"lang": lang, "share": 1.0}])
    return part


def effective_mode(wanted, parts):
    """language_mode to record for the whole document: the requested one, unless older parts (decoded in another
    mode) contain segments that would contradict it; `mixed` is always consistent as long as segments are cs/en."""
    langs = {sg.get("lang", "cs") for p in parts for sg in p["segments"]}
    if wanted in ("cs", "en") and langs <= {wanted}:
        return wanted
    return "mixed"


def build(note_id, language_mode, engine, diarization, old, new_parts, now_iso):
    """old: existing transcript doc or None; new_parts: list of part dicts for the parts produced by this job
    (they replace old parts with the same part_id; old parts keep their segments because their audio is gone).
    Always returns a v2 document (a v1 `old` is upgraded: its segments get lang=cs)."""
    replaced = {p["part_id"] for p in new_parts}
    kept = []
    for p in (old["parts"] if old else []):
        if p["part_id"] in replaced:
            continue
        if old.get("schema_version", 1) == 1:
            upgrade_part_v1(p, old.get("language", "cs"))
        kept.append(p)
    parts = kept + list(new_parts)
    parts.sort(key=lambda p: p["order"])
    off = 0.0
    for p in parts:
        p["offset"] = round(off, 3)
        p.setdefault("detected_languages", detected_shares(p["segments"]) or [{"lang": "cs", "share": 1.0}])
        off += p["duration"]
    doc = {
        "schema_version": 2, "note_id": note_id, "language": dominant_language(parts), "language_mode": effective_mode(language_mode, parts),
        "engine": engine, "diarization": diarization, "updated_at": now_iso, "parts": parts,
        "merged": {"duration": round(off, 3), "text": merged_text(parts)},
    }
    return doc
