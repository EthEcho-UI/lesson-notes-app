"""Turn the files of one recording (active/<rec_id>/part_NNN.opus) into parts of a note: measure, move to where the worker
reads them (local/<noteId>/parts/NN.opus) and write the outbox entry that announces them on GitHub (see submit.py).
Idempotent: safe to run again after a crash in the middle (used by `recover`)."""
import logging
import re
import shutil
from pathlib import Path

from . import media
from .common import iso_from_ts, now_iso, read_json, write_json

log = logging.getLogger("lesson_record.finalize")
MIN_SECONDS = 0.5
SILENT_DB = -70.0


class NoAudio(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def outbox_entries(paths):
    out = []
    for p in sorted(paths.outbox.glob("*.json")):
        d = read_json(p)
        if isinstance(d, dict) and d.get("rec_id"):
            out.append(d)
    return out


def next_order(paths, remote_note, note_id):
    """First free part order of a note: max over the note on GitHub (or cached), unsent/sent outbox entries and files on disk."""
    top = 0
    if remote_note:
        top = max([p["order"] for p in remote_note.get("parts", [])] or [0])
    for e in outbox_entries(paths):
        if e["note"]["id"] == note_id:
            top = max([top] + [p["order"] for p in e["parts"]])
    for f in (paths.local_audio / note_id / "parts").glob("*.*") if (paths.local_audio / note_id / "parts").is_dir() else []:
        m = re.match(r"(\d{2})\.", f.name)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def next_image_numbers(paths, remote_note, note_id, count):
    """`count` free photo numbers (ids iNN): above everything on the note (GitHub/cache), in unsent outbox entries and in the local photo dir."""
    top = 0
    if remote_note:
        top = max([int(i["id"][1:]) for i in remote_note.get("images") or []] or [0])
    for e in outbox_entries(paths):
        if e["note"]["id"] == note_id:
            top = max([top] + [int(i["id"][1:]) for i in e.get("images", [])])
    d = paths.local_audio / note_id / "images"
    for f in d.glob("*.*") if d.is_dir() else []:
        m = re.match(r"(\d{2})\.", f.name)
        if m:
            top = max(top, int(m.group(1)))
    if top + count > 99:
        raise NoAudio("too_many_images", "a note can have at most 99 photo numbers; remove old photos first")
    return list(range(top + 1, top + count + 1))


def _quarantine(paths, rec_dir, reason):
    dest = paths.failed / rec_dir.name
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(rec_dir), str(dest))
    (dest / "REASON.txt").write_text(reason + "\n")
    return dest


def finalize(paths, rec_dir: Path, meta: dict, remote_note=None) -> dict:
    """Returns the outbox entry. Raises NoAudio when nothing usable was recorded (recording dir is removed or quarantined)."""
    rec_dir = Path(rec_dir)
    entry_path = paths.outbox / f"{meta['rec_id']}.json"
    entry = read_json(entry_path)
    if entry is None:
        segs = []
        for s in sorted(rec_dir.glob("part_*.opus")):
            size = s.stat().st_size
            dur = media.duration(s) if size > 0 else 0.0
            if dur < MIN_SECONDS or size < 200:
                log.info("dropping empty segment %s (%.2f s, %d bytes)", s.name, dur, size)
                s.unlink()
                continue
            segs.append((s, dur, size))
        if not segs:
            shutil.rmtree(rec_dir, ignore_errors=True)
            raise NoAudio("no_audio", "no audio was captured (the recording is empty), nothing was saved")
        if max(media.max_volume_db(s) for s, _, _ in segs) < SILENT_DB:
            dest = _quarantine(paths, rec_dir, "recording is digital silence")
            raise NoAudio("silent", f"only silence was captured (muted or wrong input?); the file is kept in {dest} but no part was created")
        base = next_order(paths, remote_note if not meta["new_note"] else None, meta["note_id"])
        if base + len(segs) - 1 > 50:
            raise NoAudio("too_many_parts", "a note can have at most 50 parts")
        photos = meta.get("photos") or []
        if len((remote_note or {}).get("images") or []) + len(photos) > 12:
            raise NoAudio("too_many_images", "a note can have at most 12 photos")
        images = []
        for num, ph in zip(next_image_numbers(paths, remote_note if not meta["new_note"] else None, meta["note_id"], len(photos)), photos):
            sha, nbytes = media.sha256_file(rec_dir / ph["src"])
            im = {"id": f"i{num:02d}", "filename": f"{num:02d}.jpg", "file": f"images/{num:02d}.jpg", "bytes": nbytes, "sha256": sha,
                  "width": ph["width"], "height": ph["height"], "src": ph["src"]}
            if ph.get("caption"):
                im["caption"] = ph["caption"][:200]
            images.append(im)
        parts = []
        for i, (s, dur, size) in enumerate(segs):
            order = base + i
            sha, nbytes = media.sha256_file(s)
            parts.append({"id": f"p{order:02d}", "order": order, "filename": f"{order:02d}.opus", "file": f"parts/{order:02d}.opus",
                          "bytes": nbytes, "sha256": sha, "duration": dur, "src": s.name})
        entry = {"schema": 1, "rec_id": meta["rec_id"], "state": "finalizing", "finalized_at": now_iso(), "attempts": 0, "last_error": None,
                 "note": {"id": meta["note_id"], "new": meta["new_note"], "kind": meta["kind"], "title": meta["title"],
                          "notebook_id": meta["notebook_id"], "created_at": meta["started_at"], "recorded_at": meta["started_at"],
                          "language_mode": meta["language_mode"], "context": meta["context"], "overrides": meta.get("overrides") or {}},
                 "job": {"job_id": meta["job_id"], "kind": meta["kind"], "diarize": meta["diarize"], "created_at": now_iso()},
                 "parts": parts, "images": images}
        write_json(entry_path, entry)                       # recorded before anything moves: a crash from here on resumes
    if entry["state"] == "finalizing":
        dest_dir = paths.local_audio / entry["note"]["id"] / "parts"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for p in entry["parts"]:
            src, dst = rec_dir / p["src"], dest_dir / p["filename"]
            if src.exists():
                shutil.move(str(src), str(dst))
            elif not dst.exists():
                raise NoAudio("part_lost", f"segment {p['src']} vanished during finalize")
            sha, nbytes = media.sha256_file(dst)
            if (sha, nbytes) != (p["sha256"], p["bytes"]):
                raise NoAudio("part_changed", f"{dst} changed after it was measured")
        if entry.get("images"):
            idir = paths.local_audio / entry["note"]["id"] / "images"        # where the worker reads photos of audio_location=local jobs
            idir.mkdir(parents=True, exist_ok=True)
            for im in entry["images"]:
                src, dst = rec_dir / im["src"], idir / im["filename"]
                if src.exists():
                    shutil.move(str(src), str(dst))
                elif not dst.exists():
                    raise NoAudio("photo_lost", f"photo {im['src']} vanished during finalize")
                if media.sha256_file(dst) != (im["sha256"], im["bytes"]):
                    raise NoAudio("photo_changed", f"{dst} changed after it was measured")
        entry["state"] = "pending"
        write_json(entry_path, entry)
    shutil.rmtree(rec_dir, ignore_errors=True)
    return entry
