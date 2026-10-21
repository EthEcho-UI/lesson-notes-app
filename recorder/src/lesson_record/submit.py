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


def _put_note(api, entry):
    n = entry["note"]
    path = f"notes/{n['id']}/note.json"
    new_parts = _note_doc_parts(entry)
    for _ in range(5):
        got = api.get_file(path)
        if got is None:
            if not n["new"]:
                raise SubmitError(f"note {n['id']} no longer exists in the data repo")
            doc = {"schema_version": 1, "id": n["id"], "kind": n["kind"], "title": n["title"], "notebook_id": n["notebook_id"],
                   "created_at": n["created_at"], "updated_at": now_iso(), "recorded_at": n["recorded_at"], "source": "laptop",
                   "language": n["language"], "parts": new_parts}
            body = {"message": f"note {n['id']}: created (laptop)", "content": base64.b64encode(contract.dumps(contract.check("note", doc))).decode()}
        else:
            raw, sha = got
            doc = json.loads(raw)
            have = {p["id"]: p for p in doc["parts"]}
            changed = False
            for p in new_parts:
                cur = have.get(p["id"])
                if cur is None:
                    doc["parts"].append(p)
                    changed = True
                elif cur["sha256"] != p["sha256"]:
                    raise SubmitError(f"part {p['id']} of note {n['id']} already exists with different audio (another device recorded it?)")
            if not changed:
                return
            doc["parts"].sort(key=lambda x: x["order"])
            doc["updated_at"] = now_iso()
            contract.check("note", doc)
            body = {"message": f"note {n['id']}: laptop part", "sha": sha, "content": base64.b64encode(contract.dumps(doc)).decode()}
        body["branch"] = "main"
        try:
            api.request("PUT", f"contents/{path}", body)
            return
        except ApiError as e:
            if e.status in (409, 422):
                continue
            raise
    raise SubmitError("note.json kept changing; try again later")


def _job_doc(entry, parts, append):
    j = entry["job"]
    return {"schema_version": 1, "job_id": j["job_id"], "note_id": entry["note"]["id"], "kind": j["kind"], "created_at": j["created_at"],
            "source": "laptop", "language": j["language"], "action": "transcribe", "append": append, "audio_location": "local",
            "options": {"diarize": j["diarize"], "num_speakers": None},
            "parts": [{"id": p["id"], "order": p["order"], "file": p["file"], "bytes": p["bytes"], "sha256": p["sha256"],
                       "duration": p["duration"]} for p in parts]}


def _put_job(api, entry):
    note = entry["note"]["id"]
    jpath = f"jobs/{note}/job.json"
    tree = api.tree("inbox")
    if tree is None:
        api.create_orphan_branch("inbox")
        tree = {}
    parts = list(entry["parts"])
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
            parts.sort(key=lambda p: p["order"])
        else:
            raise Blocked(f"the previous job of this note is still waiting or running (state {st.get('state') or 'queued'})")
    append = min(p["order"] for p in parts) > 1
    doc = contract.check("job", _job_doc(entry, parts, append))
    api.commit_files("inbox", {jpath: contract.dumps(doc)}, f"job {note}")


def submit_entry(remote: Remote, entry):
    api = remote.api()
    _put_note(api, entry)
    _put_job(api, entry)


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
