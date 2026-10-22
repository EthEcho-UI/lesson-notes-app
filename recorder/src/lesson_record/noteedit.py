"""`lesson-record note-edit <noteId>`: change language mode / context / photos of an existing note, then ask the worker to regenerate
the notes (contract: edit note.json, then job action=regenerate_notes with the COMPLETE photo set; new photos go on the inbox next to
job.json, kept ones are referenced with location=main; job.json last)."""
import base64
import hashlib
import json

from lesson_worker import contract
from lesson_worker.ghapi import ApiError

from . import photos as photomod
from .common import LANGUAGE_MODES, ulid, now_iso
from .submit import _upgrade_v1


class EditError(Exception):
    pass


def _put_cond(api, path, mutate, message):
    for _ in range(5):
        got = api.get_file(path)
        if got is None:
            raise EditError(f"{path} does not exist in the data repo")
        raw, sha = got
        doc = mutate(json.loads(raw))
        contract.check("note", doc)
        try:
            api.request("PUT", f"contents/{path}", {"message": message, "sha": sha, "branch": "main",
                                                   "content": base64.b64encode(contract.dumps(doc)).decode()})
            return doc
        except ApiError as e:
            if e.status not in (409, 422):
                raise
    raise EditError("note.json kept changing; try again later")


def edit_note(remote, note_id, *, language_mode=None, topic=None, details=None, add_images=(), remove_images=(), notebook_name=None):
    """Returns a short message. Raises EditError."""
    if language_mode is not None and language_mode not in LANGUAGE_MODES:
        raise EditError(f"language mode must be one of: {', '.join(LANGUAGE_MODES)}")
    topic = None if topic is None else topic.strip()
    details = None if details is None else details.strip()
    if topic is not None and len(topic) > 300:
        raise EditError("topic is longer than 300 characters")
    if details is not None and len(details) > 2000:
        raise EditError("details are longer than 2000 characters")
    prepared = []
    for src in add_images:
        try:
            prepared.append(photomod.prepare_file(src))
        except photomod.PhotoError as e:
            raise EditError(str(e))
    if not any(x is not None for x in (language_mode, topic, details)) and not prepared and not remove_images:
        raise EditError("nothing to change: give --language-mode, --topic, --details, --image or --remove-image")
    api = remote.api()
    got = api.get_file(f"notes/{note_id}/note.json")
    if got is None:
        raise EditError(f"unknown note {note_id}")
    doc0 = json.loads(got[0])
    st = api.get_file(f"notes/{note_id}/status.json")
    st = json.loads(st[0]) if st else {}
    if st.get("state") != "done":
        raise EditError(f"the note has not been processed yet (state: {st.get('state') or 'queued'}); regenerating needs a finished transcript")
    jpath = f"jobs/{note_id}/job.json"
    tree_raw = api.tree("inbox")
    tree = tree_raw or {}
    ent = tree.get(jpath)
    if ent is not None:
        old = json.loads(api.blob(ent["sha"]))
        finished = old["job_id"] in st.get("completed_job_ids", []) or (st.get("job_id") == old["job_id"] and st.get("state") == "done")
        if not finished:
            raise EditError("the previous job of this note is still waiting or running; try again when it is done")
    ids = {i["id"] for i in doc0.get("images", [])}
    missing = [i for i in remove_images if i not in ids]
    if missing:
        raise EditError(f"no such photo: {', '.join(missing)} (have: {', '.join(sorted(ids)) or 'none'})")
    if len(ids) - len(remove_images) + len(prepared) > photomod.MAX_IMAGES:
        raise EditError(f"a note can have at most {photomod.MAX_IMAGES} photos")
    top = max([int(i[1:]) for i in ids] or [0])
    if top + len(prepared) > 99:
        raise EditError("photo numbers are used up (99); remove old photos first")
    new = []
    for k, (data, w, h) in enumerate(prepared, 1):
        num = top + k
        new.append({"id": f"i{num:02d}", "filename": f"{num:02d}.jpg", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                    "width": w, "height": h, "data": data})

    def mutate(doc, status):
        _upgrade_v1(doc, "mixed", {"subject": notebook_name or "?", "topic": "", "details": ""})
        if language_mode is not None:
            doc["language_mode"] = language_mode
        if topic is not None:
            doc["context"]["topic"] = topic
        if details is not None:
            doc["context"]["details"] = details
        doc["images"] = [i for i in doc["images"] if i["id"] not in remove_images and i["id"] not in {n["id"] for n in new}]
        for n in new:
            doc["images"].append({k: n[k] for k in ("id", "filename", "bytes", "sha256", "width", "height")} |
                                 {"uploaded_at": now_iso() if status == "uploaded" else None, "status": status})
        doc["images"].sort(key=lambda x: x["id"])
        doc["inputs_updated_at"] = doc["updated_at"] = now_iso()
        return doc

    doc = _put_cond(api, f"notes/{note_id}/note.json", lambda d: mutate(d, "uploading"), f"note {note_id}: inputs edited (laptop)")
    new_ids = {n["id"] for n in new}
    images = []
    for i in doc["images"]:
        if i["status"] == "uploaded" or i["id"] in new_ids:
            d = {"id": i["id"], "file": f"images/{i['filename']}", "bytes": i["bytes"], "sha256": i["sha256"], "width": i["width"],
                 "height": i["height"], "location": "inbox" if i["id"] in new_ids else "main"}
            if i.get("caption"):
                d["caption"] = i["caption"]
            images.append(d)
    job = contract.check("job", {"schema_version": 2, "job_id": ulid(), "note_id": note_id, "kind": doc["kind"], "created_at": now_iso(),
                                 "source": "laptop", "language_mode": doc["language_mode"], "context": doc["context"],
                                 "action": "regenerate_notes", "append": True, "audio_location": "inbox",
                                 "options": {"diarize": False, "num_speakers": None}, "parts": [], "images": images})
    if tree_raw is None:
        api.create_orphan_branch("inbox")
    files = {f"jobs/{note_id}/images/{n['filename']}": n["data"] for n in new}
    files[jpath] = contract.dumps(job)                    # job.json in the same commit as the photos = the commit point
    api.commit_files("inbox", files, f"job {note_id}")
    if new:
        def mark(d):
            for i in d["images"]:
                if i["id"] in new_ids and i["status"] == "uploading":
                    i.update(status="uploaded", uploaded_at=now_iso())
            return d
        _put_cond(api, f"notes/{note_id}/note.json", mark, f"note {note_id}: photos uploaded (laptop)")
    return (f"note {note_id} updated; regeneration queued ({len(images)} photo(s) for the model"
            + (f", {len(new)} new" if new else "") + ")")
