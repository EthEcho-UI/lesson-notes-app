"""lesson-record start | stop | pause | resume | status | list-notes | recover | submit | ui"""
import argparse
import json
import logging
import sys

from .common import ConfigError, Paths, load_recorder_config, load_worker_config
from . import session
from .remote import Remote
from .submit import process_outbox


def _fmt_t(s):
    s = int(s or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _print_last(last):
    if not last:
        return
    if last.get("ok"):
        print(f"saved: {len(last.get('parts', []))} part(s), {_fmt_t(last.get('seconds'))} of audio, note {last.get('note_id')}")
        sub = last.get("submit")
        if sub:
            print(f"  worker hand-off: {sub['status']} - {sub['message']}")
        elif last.get("message"):
            print(f"  {last['message']}")
        if last.get("warning"):
            print(f"  warning: {last['warning']}")
    else:
        print(f"NOT saved: {last.get('message')}", file=sys.stderr)


def cmd_start(a, ctx):
    wcfg, rcfg, paths = ctx
    st = session.start(wcfg, rcfg, paths, kind=a.kind, mode=a.mode, notebook=a.notebook, title=a.title, note=a.note,
                       language=a.language, diarize=a.diarize, language_mode=a.language_mode, topic=a.topic, details=a.details,
                       images=a.image or [])
    for r in st.get("recovered_orphans") or []:
        print("recovered an interrupted earlier recording first:")
        _print_last(r)
    new = "new note" if st["new_note"] else "adding a part to note"
    print(f"recording ({st['mode']}, {st['kind']}) '{st['title']}' - {new} {st['note_id']}")
    print("It runs in the background: you can close this terminal. Stop with: lesson-record stop")
    return 0


def cmd_stop(a, ctx):
    r = session.stop(*ctx)
    if "orphans" in r:
        for x in r["orphans"]:
            _print_last(x)
        return 0
    _print_last(r)
    return 0 if r.get("ok") else 1


def cmd_pause(a, ctx):
    session.pause(ctx[2])
    print("paused (microphone released); resume with: lesson-record resume")
    return 0


def cmd_resume(a, ctx):
    session.resume(ctx[2])
    print("recording again")
    return 0


def cmd_status(a, ctx):
    wcfg, rcfg, paths = ctx
    st = session.status(wcfg, rcfg, paths, auto_recover=not a.no_recover)
    if a.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0
    for r in st.get("recovered", []):
        print("recovered an interrupted recording:")
        _print_last(r)
    state = st.get("state", "idle")
    if st.get("orphan"):
        print("ORPHANED recording found (the recorder died); run: lesson-record recover")
    elif state in ("recording", "paused", "starting", "stopping"):
        print(f"{state.upper()}: '{st.get('title')}' ({st.get('mode')}, {st.get('kind')}) {_fmt_t(st.get('elapsed'))}, note {st.get('note_id')}"
              + (f", {st['parts']} part file(s)" if st.get("parts", 0) > 1 else ""))
        cap = st.get("capture") or {}
        if cap:
            print(f"  capture: mic={cap.get('mic')} monitor={cap.get('monitor')} (restarts: {st.get('capture_restarts', 0)})")
        for w in st.get("warnings", []):
            print(f"  WARNING: {w}")
        for e in st.get("events", [])[-3:]:
            print(f"  {e['t']} {e['msg']}")
    else:
        print("idle")
        if st.get("last") and not st["last"].get("ok"):
            print(f"  last recording failed: {st['last'].get('message')}")
    for o in st.get("outbox", []):
        print(f"  waiting for hand-off: note {o['note_id']} ({_fmt_t(o['seconds'])}) {o['state']}" + (f" - {o['last_error']}" if o.get("last_error") else ""))
    return 0


def cmd_list_notes(a, ctx):
    wcfg, rcfg, paths = ctx
    remote = Remote(wcfg, paths)
    c = remote.refresh()
    if not c.get("online"):
        print(f"(offline, showing cached data from {c.get('fetched_at') or 'never'}: {c.get('error')})", file=sys.stderr)
    nb = {n["id"]: n["name"] for n in c.get("notebooks", [])}
    notes = c.get("notes", [])[: a.limit]
    if a.json:
        print(json.dumps({"notes": notes, "notebooks": c.get("notebooks", [])}, indent=2, ensure_ascii=False))
        return 0
    for n in notes:
        secs = sum(p.get("duration") or 0 for p in n["parts"])
        print(f"{n['id']}  {n['created_at'][:16].replace('T', ' ')}  {n['kind']:7} {nb.get(n['notebook_id'], '?'):16.16} "
              f"{len(n['parts'])} part(s) {_fmt_t(secs)}  {(n['state'] or 'no status')[:12]:12} {n['title']}")
    if not notes:
        print("no notes" + ("" if c.get("online") else " in the cache"))
    hb = c.get("heartbeat")
    if hb:
        print(f"worker: {hb.get('state')} (last seen {hb.get('last_seen')}), {hb.get('queue', {}).get('jobs_pending', '?')} job(s) queued")
    return 0


def cmd_recover(a, ctx):
    rs = session.recover(*ctx)
    if not rs:
        print("nothing to recover")
    for r in rs:
        _print_last(r)
    return 0


def cmd_submit(a, ctx):
    wcfg, rcfg, paths = ctx
    res = process_outbox(Remote(wcfg, paths), paths)
    if not res:
        print("nothing waiting")
    for rec, state, msg in res:
        print(f"{rec}: {state} - {msg}")
    return 0 if all(s in ("submitted", "waiting") for _, s, _ in res) else 1


def cmd_note_edit(a, ctx):
    from .noteedit import EditError, edit_note
    wcfg, rcfg, paths = ctx
    remote = Remote(wcfg, paths)
    n = remote.find_note(a.note_id)
    nb = next((x["name"] for x in remote.cached().get("notebooks", []) if n and x["id"] == n["notebook_id"]), None)
    try:
        print(edit_note(remote, a.note_id, language_mode=a.language_mode, topic=a.topic, details=a.details, add_images=a.image or [],
                        remove_images=a.remove_image or [], notebook_name=nb))
    except EditError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_ui(a, ctx):
    from .ui import serve, stop_running
    if a.stop:
        return stop_running(ctx[2])
    return serve(*ctx, port=a.port, open_browser=not a.no_browser)


def main(argv=None):
    p = argparse.ArgumentParser(prog="lesson-record", description="Record lessons/meetings on this laptop and hand them to the lesson-notes worker.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="start recording in the background")
    s.add_argument("--mode", choices=["mic", "meeting"], help="mic = microphone only; meeting = microphone + system audio (default: by kind)")
    s.add_argument("--kind", choices=["lesson", "meeting"], default="lesson")
    s.add_argument("--notebook", help="notebook name or id (new notes)")
    s.add_argument("--title", help="note title (new notes)")
    s.add_argument("--note", help="existing note id: record another PART of that note")
    s.add_argument("--language", help=argparse.SUPPRESS)       # old spelling of --language-mode cs|en
    s.add_argument("--language-mode", choices=["cs", "en", "mixed"], help="spoken language: cs, en or mixed (default: settings, else mixed); notes are always Czech")
    s.add_argument("--topic", help="what the lesson/meeting is about (context for the notes)")
    s.add_argument("--details", help="free text for the AI: terms, teacher, exam relevance (max 2000 characters)")
    s.add_argument("--image", action="append", metavar="PATH", help="photo of the whiteboard/slides/notebook (repeatable, max 12 per note; resized to JPEG, EXIF removed)")
    s.add_argument("--diarize", action=argparse.BooleanOptionalAction, default=None, help="speaker labels (default: lesson no, meeting yes)")
    sub.add_parser("stop", help="stop, finalize and hand over to the worker")
    sub.add_parser("pause")
    sub.add_parser("resume")
    s = sub.add_parser("status", help="state of the recorder; recovers an interrupted recording automatically")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-recover", action="store_true")
    s = sub.add_parser("list-notes", help="recent notes and their processing state")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    sub.add_parser("recover", help="turn recordings interrupted by a crash/power loss into parts")
    sub.add_parser("submit", help="retry the hand-off of finished recordings that could not be announced (offline)")
    s = sub.add_parser("note-edit", help="change language mode / topic / details / photos of a note and regenerate its notes")
    s.add_argument("note_id")
    s.add_argument("--language-mode", choices=["cs", "en", "mixed"])
    s.add_argument("--topic")
    s.add_argument("--details")
    s.add_argument("--image", action="append", metavar="PATH", help="add a photo (repeatable)")
    s.add_argument("--remove-image", action="append", metavar="iNN", help="remove a photo from the note (repeatable)")
    s = sub.add_parser("ui", help="local web UI on 127.0.0.1")
    s.add_argument("--port", type=int, default=0, help="default: a free port")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--stop", action="store_true", help="close a UI server started earlier (does not touch a running recording)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        wcfg = load_worker_config()
        ctx = (wcfg, load_recorder_config(), Paths(wcfg))
        return globals()["cmd_" + a.cmd.replace("-", "_")](a, ctx)
    except (session.SessionError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
