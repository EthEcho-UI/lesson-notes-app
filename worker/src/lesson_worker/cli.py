"""lesson-worker run | once | status | doctor | download-models | init-config"""
import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from . import __version__
from .cachestore import InstanceLock
from .clock import Clock, parse_iso
from .config import ConfigError, EXAMPLE, default_config_path, load
from .ghapi import ApiError, GitHub, NetworkError

log = logging.getLogger("lesson_worker")


def _api(cfg):
    return GitHub(cfg.repo, cfg.read_token(), cfg.api_base)


def _worker(cfg, api=None):
    from .asr import Transcriber
    from .worker import Worker
    return Worker(cfg, api or _api(cfg), asr=Transcriber(cfg))


def cmd_run(cfg, args):
    lock = InstanceLock(cfg.cache_dir / "worker.lock")
    if not lock.acquire():
        print("another lesson-worker is already running on this machine", file=sys.stderr)
        return 2
    w = _worker(cfg)

    def stop(signum, _frame):
        log.info("signal %s: finishing the current unit of work, then exiting", signum)
        w.stop_requested = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    w.cache.purge_older_than(cfg.cache_retention_days)
    log.info("lesson-worker %s started; repo %s", __version__, cfg.repo)
    w.run_forever()
    return 0


def cmd_once(cfg, args):
    lock = InstanceLock(cfg.cache_dir / "worker.lock")
    if not lock.acquire():
        print("another lesson-worker is already running on this machine", file=sys.stderr)
        return 2
    w = _worker(cfg)
    w.invalidate()
    n = w.drain()
    w.heartbeat("idle")
    print(f"processed {n} job(s); {w._last_queue['jobs_pending']} still pending (waiting for retry back-off or none)")
    return 0


def cmd_status(cfg, args):
    api = _api(cfg)
    got = api.get_file("worker/heartbeat.json")
    if got:
        hb = json.loads(got[0])
        age = (Clock().now() - parse_iso(hb["last_seen"])).total_seconds()
        print(f"worker {hb['worker_id']} v{hb.get('version', '?')}: {hb['state']}, last seen {int(age // 60)} min ago"
              f" ({'OFFLINE' if age > 1800 else 'online'}); queue {hb['queue']['jobs_pending']} job(s), {hb['queue']['bytes_pending']} bytes")
    else:
        print("no heartbeat on main yet")
    from .worker import Worker
    w = Worker(cfg, api)
    jobs = w.scan()
    w._refresh_statuses({j.note_id for j in jobs})
    for j in jobs:
        st = w.status_of(j.note_id)
        state = st["state"] if st and st["job_id"] == j.job_id else "no status yet"
        extra = ""
        if st and st["job_id"] == j.job_id and st.get("progress"):
            pr = st["progress"]
            extra = f" part {pr['part_index']}/{pr['part_total']} {pr.get('percent')}%"
        print(f"  {j.created_at:%Y-%m-%d %H:%M} note {j.note_id} job {j.job_id} {j.doc['action']} {j.doc['kind']}: {state}{extra}")
    if not jobs:
        print("  inbox is empty")
    return 0


def _ok(flag, name, detail="", required=True):
    print(f"[{'ok' if flag else ('FAIL' if required else 'warn')}] {name}" + (f": {detail}" if detail else ""))
    return flag


def model_check(cfg, run=subprocess.run):
    """(ok, detail): one tiny `claude -p` call with the configured notes model id (costs a fraction of a cent)."""
    from .notes import _empty_parent, minimal_env, resolve_model
    try:
        claude = cfg.find_claude()
    except Exception as e:
        return False, str(e)
    model = resolve_model(cfg)
    try:
        r = run([claude, "-p", "--output-format", "json", "--max-turns", "1", "--no-session-persistence", "--tools", "", "--model", model,
                 "--effort", "low", "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands",
                 "--system-prompt", "Reply with the single word: ok"],
                input="ping", capture_output=True, text=True, encoding="utf-8", env=minimal_env(claude), timeout=120, cwd=_empty_parent(cfg))
    except subprocess.TimeoutExpired:
        return False, f"model {model}: no answer within 120 s"
    except OSError as e:
        return False, f"cannot execute claude: {e}"
    d = None
    for line in reversed((r.stdout or "").strip().splitlines()):        # one object, or stream-json lines (result last)
        try:
            d = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(d, dict):
        return False, f"model {model}: unreadable answer (exit {r.returncode}): {(r.stdout or r.stderr or '')[:150]}"
    if d.get("is_error") or r.returncode != 0:
        return False, f"model {model} rejected: {str(d.get('result') or d.get('error') or r.stderr)[:200]}"
    used = list((d.get("modelUsage") or {}).keys())
    return True, f"{model} answered" + (f" (served by {', '.join(used)})" if used else "")


def photo_check():
    """Pillow can decode, scrub and re-check a JPEG with EXIF/GPS (no ffmpeg or ImageMagick involved)."""
    import io
    from PIL import Image
    from .images import clean_image, has_metadata
    im = Image.new("RGB", (32, 24), (200, 30, 30))
    ex = Image.Exif()
    ex[0x010F] = "doctor-camera"
    ex[0x8825] = {1: "N", 2: (50.0, 5.0, 0.0), 3: "E", 4: (14.0, 25.0, 0.0)}      # GPS IFD
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=ex)
    if not has_metadata(buf.getvalue(), "jpg"):
        return False, "test photo carries no EXIF (Pillow problem)"
    out = clean_image(buf.getvalue(), "jpg")
    if has_metadata(out.data, "jpg") or b"doctor-camera" in out.data:
        return False, "EXIF was not removed"
    return True, f"Pillow {Image.__version__}: EXIF/GPS scrubbing works"


def cmd_doctor(cfg, args):
    from . import diar as diarmod
    from .notes import login_status
    good = True
    p = default_config_path()
    _ok(True, "config", f"{p} ({'present' if p.exists() else 'absent, using defaults'}); repo {cfg.repo}")
    ff = shutil.which("ffmpeg")
    good &= _ok(bool(ff), "ffmpeg", ff or "not installed (pacman -S ffmpeg)")
    try:
        tok = cfg.read_token()
        _ok(True, "token file", f"{cfg.token_file} (mode 600, {len(tok)} chars)")
    except ConfigError as e:
        tok = None
        good &= _ok(False, "token file", str(e))
    if tok:
        try:
            api = GitHub(cfg.repo, tok, cfg.api_base, retries=1)
            info = api.repo_info()
            perm = info.get("permissions", {})
            good &= _ok(bool(perm.get("push", True)), "GitHub API", f"{cfg.repo} reachable, private={info.get('private')}, push={perm.get('push')}")
            _ok(api.get_ref("main") is not None, "branch main", "exists" if api.get_ref("main") else "missing")
            _ok(True, "branch inbox", "exists" if api.get_ref("inbox") else "absent (the PWA or the worker creates it)")
        except (ApiError, NetworkError) as e:
            good &= _ok(False, "GitHub API", str(e))
    try:
        from .asr import model_path
        good &= _ok(True, f"whisper model {cfg.whisper_model}", model_path(cfg, local_only=True))
    except Exception:
        good &= _ok(False, f"whisper model {cfg.whisper_model}", f"not downloaded to {cfg.models_whisper}; run: lesson-worker download-models")
    _ok(diarmod.models_present(cfg), "diarization models (optional)", str(cfg.models_diar) if diarmod.models_present(cfg) else "missing; run: lesson-worker download-models (meetings then get no speaker labels)", required=False)
    try:
        import sherpa_onnx  # noqa: F401
        _ok(True, "sherpa-onnx import (optional)", "ok", required=False)
    except Exception as e:
        _ok(False, "sherpa-onnx import (optional)", f"{type(e).__name__}: {e}", required=False)
    ok, detail = login_status(cfg)
    good &= _ok(ok, "claude login", detail if ok else detail + " (run `claude` interactively once and log in)")
    if getattr(args, "no_model_call", False):
        _ok(True, "notes model call", "skipped (--no-model-call)", required=False)
    else:
        ok, detail = model_check(cfg)
        good &= _ok(ok, f"notes model {cfg.claude_model} (effort {cfg.claude_effort})", detail)
    try:
        ok, detail = photo_check()
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    good &= _ok(ok, "photo handling (Pillow)", detail)
    cache_ok = os.access(cfg.cache_dir.parent if not cfg.cache_dir.exists() else cfg.cache_dir, os.W_OK)
    good &= _ok(cache_ok, "cache dir writable", str(cfg.cache_dir))
    print("\nall required checks passed" if good else "\nsome required checks FAILED")
    return 0 if good else 1


def _download(url, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "lesson-notes-worker"}), timeout=120) as r, \
            tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as f:
        shutil.copyfileobj(r, f)
        tmp = f.name
    os.replace(tmp, dest)


def cmd_download_models(cfg, args):
    from . import diar as diarmod
    from .asr import model_path
    print(f"downloading Whisper {cfg.whisper_model} (~1.6 GB) to {cfg.models_whisper} ...")
    print(model_path(cfg, local_only=False))
    if not cfg.diar_embedding.is_file():
        print("downloading speaker embedding model ...")
        _download(diarmod.EMB_URL, cfg.diar_embedding)
    if not cfg.diar_segmentation.is_file():
        print("downloading speaker segmentation model ...")
        arc = cfg.models_diar / "seg.tar.bz2"
        _download(diarmod.SEG_URL, arc)
        with tarfile.open(arc) as t:
            t.extractall(cfg.models_diar, filter="data")
        arc.unlink()
    print("models ready")
    return 0


def cmd_init_config(cfg, args):
    p = default_config_path()
    if p.exists():
        print(f"{p} exists; not overwriting")
        return 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(EXAMPLE, encoding="utf-8")
    print(f"wrote {p}\nNext: put the GitHub token into {cfg.token_file} and run: chmod 600 {cfg.token_file}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lesson-worker")
    ap.add_argument("--config", help="path to worker.toml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, h in [("run", cmd_run, "daemon loop (polls the inbox, processes jobs)"),
                        ("once", cmd_once, "scan and process everything runnable, then exit"),
                        ("status", cmd_status, "show heartbeat and queue from GitHub"),
                        ("doctor", cmd_doctor, "check ffmpeg, models, claude login, token file, API"),
                        ("download-models", cmd_download_models, "download Whisper + diarization models"),
                        ("init-config", cmd_init_config, "write an example worker.toml")]:
        sp = sub.add_parser(name, help=h)
        sp.set_defaults(fn=fn)
        if name == "doctor":
            sp.add_argument("--no-model-call", action="store_true", help="skip the tiny claude call that checks the notes model id")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        cfg = load(args.config)
        return args.fn(cfg, args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except (ApiError, NetworkError) as e:
        print(f"GitHub error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
