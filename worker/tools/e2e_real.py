#!/usr/bin/env python
"""Real end-to-end test against the REAL data repo, driven like the PWA would (uses the worker's own token file).

Uploads ONE short cs/en/cs clip (public FLEURS sentences) + ONE generated whiteboard photo with a fake camera EXIF tag, waits for the
running worker service to finish it, checks the results, and removes everything it created (unless --keep).
Makes ONE real notes call (the worker's configured model) - about USD 0.05-0.10. Run:
  .venv/bin/python tools/e2e_real.py --cs A.wav --cs B.wav --en C.wav [--keep] [--timeout 1500]
"""
import argparse, hashlib, io, json, os, secrets, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from lesson_worker import config, contract
from lesson_worker.ghapi import GitHub

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid():
    t, out = int(time.time() * 1000), ""
    for _ in range(10):
        out, t = CROCKFORD[t % 32] + out, t // 32
    return out + "".join(secrets.choice(CROCKFORD) for _ in range(16))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_audio(cs, en, workdir):
    """cs, en, cs sentences with 0.8 s pauses -> mono 16 kHz Opus 24k (what the PWA/recorder produce)."""
    inputs = [str(Path(x).resolve()) for x in (cs[0], en[0], cs[-1])]
    out = workdir / "01.opus"
    # normalise every clip first (FLEURS en is float32, cs is s16: the concat DEMUXER would glue them as raw bytes and garble one)
    labels = "".join(f"[{i}:a]aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono,apad=pad_dur=0.8[a{i}];" for i in range(3))
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for x in inputs:
        cmd += ["-i", x]
    cmd += ["-filter_complex", labels + "[a0][a1][a2]concat=n=3:v=0:a=1[o]", "-map", "[o]", "-c:a", "libopus", "-b:a", "24k", str(out)]
    subprocess.run(cmd, check=True)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True, check=True).stdout)
    return out.read_bytes(), round(dur, 1)


def build_photo():
    im = Image.new("RGB", (1000, 700), (236, 238, 236))
    d = ImageDraw.Draw(im)
    f = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSans-Oblique.ttf", 54)
    for i, line in enumerate(["F = m . a", "m = 3 kg, a = 2 m/s2", "F = 6 N"]):
        d.text((80, 90 + i * 130), line, fill=(20, 40, 150), font=f)
    exif = Image.Exif()
    exif[0x010F] = "E2ECameraMaker"                      # must be scrubbed by the worker
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=82, exif=exif)
    return buf.getvalue(), im.size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cs", action="append", required=True)
    ap.add_argument("--en", action="append", required=True)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--timeout", type=int, default=1500)
    a = ap.parse_args()
    cfg = config.load()
    gh = GitHub(cfg.repo, cfg.read_token(), cfg.api_base)
    work = Path(cfg.cache_dir) / "e2e"                      # not /tmp: scratch lives in the worker cache dir
    work.mkdir(parents=True, exist_ok=True)
    audio, dur = build_audio(a.cs, a.en, work)
    photo, (pw, ph) = build_photo()
    nb, nid, jid, t = ulid(), ulid(), ulid(), now()
    sha = lambda b: hashlib.sha256(b).hexdigest()
    ctx = {"subject": "E2E test (smazat)", "topic": "Technický test nahrávky: české a anglické ukázkové věty",
           "details": "Nejde o skutečnou hodinu. Věty jsou náhodné ukázky řeči (česky, anglicky, česky); fotka tabule ukazuje F = m . a."}
    notebook = {"schema_version": 1, "id": nb, "name": "E2E test (smazat)", "created_at": t, "updated_at": t, "archived": False, "color": "#2e7d32", "sort": 999}
    note = {"schema_version": 2, "id": nid, "kind": "lesson", "title": "E2E test", "notebook_id": nb, "created_at": t, "updated_at": t,
            "source": "pwa", "language_mode": "mixed", "context": ctx, "inputs_updated_at": t,
            "images": [{"id": "i01", "filename": "01.jpg", "bytes": len(photo), "sha256": sha(photo), "width": pw, "height": ph,
                        "caption": "tabule", "uploaded_at": t, "status": "uploaded"}],
            "parts": [{"id": "p01", "order": 1, "filename": "01.opus", "bytes": len(audio), "sha256": sha(audio), "duration": dur,
                       "uploaded_at": t, "status": "uploaded"}]}
    job = {"schema_version": 2, "job_id": jid, "note_id": nid, "kind": "lesson", "created_at": t, "source": "pwa", "language_mode": "mixed",
           "context": ctx, "action": "transcribe", "append": False, "audio_location": "inbox", "options": {"diarize": False, "num_speakers": None},
           "parts": [{"id": "p01", "order": 1, "file": "parts/01.opus", "bytes": len(audio), "sha256": sha(audio), "duration": dur}],
           "images": [{"id": "i01", "file": "images/01.jpg", "bytes": len(photo), "sha256": sha(photo), "width": pw, "height": ph,
                       "caption": "tabule", "location": "inbox"}]}
    for kind, doc in (("notebook", notebook), ("note", note), ("job", job)):
        contract.check(kind, doc)
    print(f"audio {dur}s {len(audio)} B, photo {len(photo)} B, note {nid}")
    created_main = [f"notebooks/{nb}.json", f"notes/{nid}/note.json"]
    try:
        gh.commit_files("main", {created_main[0]: contract.dumps(notebook), created_main[1]: contract.dumps(note)}, "e2e test: notebook+note")
        if gh.get_ref("inbox") is None:
            gh.create_orphan_branch("inbox")
        gh.commit_files("inbox", {f"jobs/{nid}/parts/01.opus": audio, f"jobs/{nid}/images/01.jpg": photo}, "e2e test: audio+photo")
        gh.commit_files("inbox", {f"jobs/{nid}/job.json": contract.dumps(job)}, "e2e test: job.json (last)")
        print("uploaded; waiting for the worker ...")
        seen, deadline, st = None, time.time() + a.timeout, None
        while time.time() < deadline:
            got = gh.get_file(f"notes/{nid}/status.json")
            st = json.loads(got[0]) if got else None
            key = (st or {}).get("state"), json.dumps((st or {}).get("progress"), sort_keys=True)
            if key != seen:
                print(time.strftime("%H:%M:%S"), key[0], key[1] if key[1] != "null" else "")
                seen = key
            if st and st["state"] in ("done", "failed"):
                break
            time.sleep(10)
        assert st and st["state"] == "done", f"not done: {st}"
        notes = json.loads(gh.get_file(f"notes/{nid}/notes.json")[0])
        tr = json.loads(gh.get_file(f"notes/{nid}/transcript.json")[0])
        contract.check("notes", notes)
        contract.check("transcript", tr)
        c = notes["content"]
        segs = [s for p in tr["parts"] for s in p["segments"]]
        langs = {}
        for s in segs:
            langs[s.get("lang")] = langs.get(s.get("lang"), 0) + 1
        print("\n=== RESULT ===")
        print("model:", notes["model"], "| notes language:", notes["language"], "| based_on_images:", notes["based_on_images"])
        print("segments:", len(segs), "languages:", langs)
        assert langs.get("en", 0) >= 1 and langs.get("cs", 0) >= 1, f"mixed mode did not find both languages: {langs}"
        print("transcript:", " ".join(s["text"] for s in segs)[:700])
        print("title:", c.get("title"))
        print("description:", c.get("description"))
        print("outline sections:", len(c.get("outline", [])), "| key_points:", len(c.get("key_points", [])), "| figures:", len(c.get("figures", [])),
              "| study:", len(c.get("study_materials", [])), "| homework:", len(c.get("homework", [])), "| uncertainties:", len(c.get("uncertainties", [])))
        stored = gh.get_file(f"notes/{nid}/images/01.jpg")
        assert stored and b"E2ECameraMaker" not in stored[0], "photo missing on main or EXIF not scrubbed"
        tree = gh.tree("inbox") or {}
        left = [p for p in tree if p.startswith(f"jobs/{nid}/")]
        print("photo kept on main, EXIF scrubbed: OK | audio left on inbox:", left or "none (deleted)")
        assert not left, "audio/job files still on inbox"
        print("images_kept:", st.get("images_kept"), "| status schema ok")
        print("\nE2E: PASS")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        if not a.keep:
            tree = gh.tree("main") or {}
            dele = {p: None for p in tree if p.startswith(f"notes/{nid}/")}
            dele[created_main[0]] = None
            gh.commit_files("main", dele, "e2e test: cleanup")
            itree = gh.tree("inbox") or {}
            idel = {p: None for p in itree if p.startswith(f"jobs/{nid}/")}
            if idel:
                gh.commit_files("inbox", idel, "e2e test: cleanup")
            print("cleanup done: removed", len(dele), "files from main,", len(idel), "from inbox")


if __name__ == "__main__":
    main()
