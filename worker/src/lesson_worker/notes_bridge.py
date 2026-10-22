"""The seam between the job plumbing (worker.py) and the notes engine (notes.py).

The worker calls `call(...)` (injectable in tests: Worker(notes=...)); keyword arguments are the stable interface:

    call(cfg, *, transcript, context, language_mode, kind, images, previous_notes, notebook_name="", usage=None) -> (content, model)

    transcript     v2 transcript.json document (parts[].segments[] with speaker/lang; merged.text)
    context        {"subject", "topic", "details"}
    language_mode  "cs" | "en" | "mixed"
    kind           "lesson" | "meeting"
    images         [{"id": "i01", "path": <local verified, metadata-free file>, "mime", "width", "height", "bytes", "caption"?}]
    previous_notes previous notes.json document (v1 or v2) or None (regenerate / append)
    usage          dict the engine fills (images_used, images_skipped, cost_usd, warnings, ...)
    returns        content in the v2 shape of the contract (lesson or meeting variant) and the model id.
    raises         lesson_worker.notes.NotesError(code, message, retryable)

This module maps those arguments onto the engine's own signature (notes.generate_notes, block b6).
"""
from . import notes as engine


def call(cfg, *, transcript, context, language_mode, kind, images, previous_notes, notebook_name="", usage=None):
    prev = (previous_notes or {}).get("content")
    if prev is not None and not prev.get("title"):
        prev = None
    return engine.generate_notes(
        cfg, kind, engine.transcript_for_model(transcript), context=context, language_mode=language_mode,
        notebook_name=notebook_name, images=[{k: i[k] for k in ("id", "path") if k in i} | {"caption": i.get("caption")} for i in images],
        previous=prev, usage=usage if usage is not None else {})
