"""Announce finalized recordings to the data repo, as the contract prescribes for laptop recordings (audio_location=local):
  1. notes/<noteId>/note.json on main  (created for a new note; for an existing note a part is appended with a sha-conditional PUT,
     exactly like the PWA does when it adds a part)
  2. jobs/<noteId>/job.json on the inbox branch, LAST (commit point). The audio itself never leaves the laptop.
Everything is idempotent and safe to repeat; while the laptop is offline the entry just stays in the outbox."""
import base64
import json
import logging

from lesson_worker import contract
from lesson_worker.ghapi import ApiError, NetworkError

from .common import ConfigError, now_iso, read_json, write_json
from .finalize import outbox_entries
from .remote import Remote, RemoteError

log = logging.getLogger("lesson_record.submit")


class Blocked(Exception):
    """Not an error: has to wait (the previous job of this note is still on the inbox)."""


class SubmitError(Exception):
    """Needs a human (contract violation, note vanished, conflicting part)."""


def _note_doc_parts(entry):
    return [{"id": p["id"], "order": p["order"], "filename": p["filename"], "bytes": p["bytes"], "sha256": p["sha256"],
             "duration": p["duration"], "uploaded_at": entry["finalized_at"], "status": "uploaded"} for p in entry["parts"]]


def _note_doc_images(entry, status="uploaded"):
    out = []
    for i in entry.get("images", []):
        d = {"id": i["id"], "filename": i["filename"], "bytes": i["bytes"], "sha256": i["sha256"], "width": i["width"], "height": i["height"],
             "uploaded_at": entry["finalized_at"] if status == "uploaded" else None, "status": status}
        if i.get("caption"):
            d["caption"] = i["caption"]
        out.append(d)
    return out


def _upgrade_v1(doc, mode, context):
    """A v1 note.json that we touch is rewritten as v2 (contract: writers write 2 whenever they touch a file)."""
    if doc.get("schema_version") == 2:
        return
    doc.pop("language", None)
    doc.update(schema_version=2, language_mode=mode, context=dict(context), images=[])


def _put_note(api, entry):
    """Create the note (new) or add this recording's part and photos to it. Returns the note document as written."""
    n = entry["note"]
    path = f"notes/{n['id']}/note.json"
    new_parts = _note_doc_parts(entry)
    new_images = _note_doc_images(entry)
    for _ in range(5):
        got = api.get_file(path)
        if got is None:
            if not n["new"]:
                raise SubmitError(f"note {n['id']} no longer exists in the data repo")
            now = now_iso()
            doc = {"schema_version": 2, "id": n["id"], "kind": n["kind"], "title": n["title"], "notebook_id": n["notebook_id"],
                   "created_at": n["created_at"], "updated_at": now, "recorded_at": n["recorded_at"], "source": "laptop",
                   "language_mode": n["language_mode"], "context": n["context"], "inputs_updated_at": now, "images": new_images,
                   "parts": new_parts}
            body = {"message": f"note {n['id']}: created (laptop)", "content": base64.b64encode(contract.dumps(contract.check("note", doc))).decode()}
        else:
            raw, sha = got
            doc = json.loads(raw)
            before = json.dumps(doc, sort_keys=True)
            _upgrade_v1(doc, n["language_mode"], n["context"])
            ov = n.get("overrides") or {}
            inputs_changed = False
            if "language_mode" in ov and doc["language_mode"] != ov["language_mode"]:
                doc["language_mode"], inputs_changed = ov["language_mode"], True
            for k in ("topic", "details"):
                if k in ov and doc["context"].get(k) != ov[k]:
                    doc["context"][k], inputs_changed = ov[k], True
            have = {p["id"]: p for p in doc["parts"]}
            for p in new_parts:
                cur = have.get(p["id"])
                if cur is None:
                    doc["parts"].append(p)
                elif cur["sha256"] != p["sha256"]:
                    raise SubmitError(f"part {p['id']} of note {n['id']} already exists with different audio (another device recorded it?)")
            have_i = {i["id"]: i for i in doc["images"]}
            for im in new_images:
                cur = have_i.get(im["id"])
                if cur is None:
                    doc["images"].append(im)
                    inputs_changed = True
                elif cur["sha256"] != im["sha256"]:
                    raise SubmitError(f"photo {im['id']} of note {n['id']} already exists with a different picture (the app added one meanwhile?)")
            doc["parts"].sort(key=lambda x: x["order"])
            doc["images"].sort(key=lambda x: x["id"])
            if inputs_changed:
                doc["inputs_updated_at"] = now_iso()
            if json.dumps(doc, sort_keys=True) == before:
                return doc
            doc["updated_at"] = now_iso()
            contract.check("note", doc)
            body = {"message": f"note {n['id']}: laptop part", "sha": sha, "content": base64.b64encode(contract.dumps(doc)).decode()}
        body["branch"] = "main"
        try:
            api.request("PUT", f"contents/{path}", body)
            return doc
        except ApiError as e:
            if e.status in (409, 422):
                continue
            raise
    raise SubmitError("note.json kept changing; try again later")


def _job_images(note_doc, local_ids):
    """The COMPLETE photo set the model must see: this recording's photos are `local` (laptop cache), the rest is already stored with the note."""
    out = []
    for i in note_doc["images"]:
        if i["status"] != "uploaded":
            continue
        d = {"id": i["id"], "file": f"images/{i['filename']}", "bytes": i["bytes"], "sha256": i["sha256"], "width": i["width"],
             "height": i["height"], "location": "local" if i["id"] in local_ids else "main"}
        if i.get("caption"):
            d["caption"] = i["caption"]
        out.append(d)
    return out


def _job_doc(entry, note_doc, parts, append, local_ids):
    j = entry["job"]
    return {"schema_version": 2, "job_id": j["job_id"], "note_id": entry["note"]["id"], "kind": j["kind"], "created_at": j["created_at"],
            "source": "laptop", "language_mode": note_doc["language_mode"], "context": note_doc["context"], "action": "transcribe",
            "append": append, "audio_location": "local", "options": {"diarize": j["diarize"], "num_speakers": None},
            "parts": [{"id": p["id"], "order": p["order"], "file": p["file"], "bytes": p["bytes"], "sha256": p["sha256"],
                       "duration": p["duration"]} for p in parts],
            "images": _job_images(note_doc, local_ids)}


def _put_job(api, entry, note_doc):
    note = entry["note"]["id"]
    jpath = f"jobs/{note}/job.json"
    tree = api.tree("inbox")
    if tree is None:
        api.create_orphan_branch("inbox")
        tree = {}
    parts = list(entry["parts"])
    local_ids = {i["id"] for i in entry.get("images", [])}
    ent = tree.get(jpath)
    if ent is not None:
        old = json.loads(api.blob(ent["sha"]))
        if old.get("job_id") == entry["job"]["job_id"]:
            return                                                    # already submitted
        st = api.get_file(f"notes/{note}/status.json")
        st = json.loads(st[0]) if st else {}
        finished = old["job_id"] in st.get("completed_job_ids", []) or (st.get("job_id") == old["job_id"] and st.get("state") == "done")
        failed = st.get("job_id") == old["job_id"] and st.get("state") == "failed"
        if finished:
            pass                                                      # worker is only cleaning up; it never touches a newer job.json
        elif failed and old.get("audio_location") == "local" and old.get("action") == "transcribe":
            parts = old["parts"] + parts                              # nothing of it was transcribed: carry its parts along
            local_ids |= {i["id"] for i in old.get("images", []) if i["location"] == "local"}      # ... and its laptop photos
            parts.sort(key=lambda p: p["order"])
        else:
            raise Blocked(f"the previous job of this note is still waiting or running (state {st.get('state') or 'queued'})")
    append = min(p["order"] for p in parts) > 1
    doc = contract.check("job", _job_doc(entry, note_doc, parts, append, local_ids))
    api.commit_files("inbox", {jpath: contract.dumps(doc)}, f"job {note}")


def submit_entry(remote: Remote, entry):
    api = remote.api()
    note_doc = _put_note(api, entry)
    _put_job(api, entry, note_doc)


def process_outbox(remote: Remote, paths, only=None, log_fn=None):
    """Try every pending entry (oldest first). Returns [(rec_id, 'submitted'|'waiting'|'offline'|'error', message)]."""
    results = []
    for entry in outbox_entries(paths):
        if only and entry["rec_id"] != only:
            continue
        if entry["state"] not in ("pending", "error"):
            continue
        path = paths.outbox / f"{entry['rec_id']}.json"
        entry["attempts"] += 1
        try:
            submit_entry(remote, entry)
            entry.update(state="submitted", last_error=None, submitted_at=now_iso())
            res = ("submitted", "announced to the data repo; the worker will pick it up")
        except Blocked as e:
            entry.update(state="pending", last_error=str(e))
            res = ("waiting", str(e))
        except (NetworkError, RemoteError, ConfigError) as e:
            entry.update(state="pending", last_error=f"offline: {e}")
            res = ("offline", str(e))
        except (SubmitError, contract.InvalidDocument, ApiError) as e:
            entry.update(state="error", last_error=str(e))
            res = ("error", str(e))
            log.error("outbox %s: %s", entry["rec_id"], e)
        write_json(path, entry)
        results.append((entry["rec_id"], *res))
        if res[0] == "offline":
            break                                                     # no point trying the rest
    return results
