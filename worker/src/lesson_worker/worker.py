"""The worker engine: scan the inbox branch, process jobs one at a time (oldest first), write results to main.
Safe to kill at any time: all outputs are idempotent (see CONTRACT section 3)."""
import datetime as dt
import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, contract, diar as diarmod, images as imgmod, notes_bridge, notes_final, transcript as tmod
from .audio import AudioCorrupt, load_f32, to_wav16k, wav_duration
from . import asr as asrmod
from .cachestore import Cache
from .clock import Clock, iso, parse_iso
from .ghapi import ApiError, GitHub, NetworkError, blob_sha
from .notes import NotesError

log = logging.getLogger("lesson_worker")

ULID = "[0-9A-HJKMNP-TV-Z]{26}"
MAX_COMPLETED = 20


def _with_lang(segs, mode):
    """Every segment carries `lang` (the ASR sets it in mixed mode; forced modes are filled in here)."""
    default = mode if mode in ("cs", "en") else "cs"
    return [dict(s, lang=s.get("lang") or default) for s in segs]


class Fail(Exception):
    def __init__(self, code, message, retryable, stage, auto_retry=False):
        super().__init__(message)
        self.code, self.message, self.retryable, self.stage, self.auto_retry = code, message, retryable, stage, auto_retry


class Stopped(Exception):
    """Clean shutdown requested between work units."""


@dataclass
class Job:
    note_id: str
    job_id: str
    doc: dict
    files: dict            # inbox path -> {sha,size} for jobs/<note>/**
    created_at: object = None

    @property
    def sort_key(self):
        return (self.created_at, self.job_id)

    @property
    def location(self):
        return self.doc["audio_location"]

    @property
    def bytes(self):
        return sum(p["bytes"] for p in self.doc["parts"])

    @property
    def language_mode(self):
        """v2: from the job. v1 jobs carried a forced language code: cs/en map to the same mode, anything else is detected."""
        m = self.doc.get("language_mode")
        if m:
            return m
        return self.doc.get("language", "cs") if self.doc.get("language", "cs") in ("cs", "en") else "mixed"

    @property
    def images(self):
        return self.doc.get("images", [])

    @property
    def context(self):
        return self.doc.get("context")


@dataclass
class Plan:
    pending: list = field(default_factory=list)
    cleanup: list = field(default_factory=list)
    failed: list = field(default_factory=list)


class Worker:
    def __init__(self, cfg, api: GitHub, clock=None, asr=None, notes=notes_bridge.call, diarizer=diarmod.diarize):
        self.cfg, self.api = cfg, api
        self.clock = clock or Clock()
        self.asr = asr
        self.notes = notes
        self.diarizer = diarizer
        self.cache = Cache(cfg.cache_dir)
        self.stop_requested = False
        self.started_at = self.clock.now_iso()
        self.last_scan_at = None
        self.busy_note = None
        self.resumes = 0
        self.retry_after = {}          # job_id -> monotonic time
        self._no_bump = set()          # job_ids interrupted by network trouble: do not count as an attempt
        self._job_docs = {}            # blob sha -> parsed job doc (or None if invalid)
        self._status = {}              # note_id -> (blob sha, doc | None, written_by_us)
        self._last_hb = -1e9
        self._last_queue = {"jobs_pending": 0, "bytes_pending": 0, "oldest_job_created_at": None}
        self._last_status_write = -1e9
        self._last_gc = -1e9

    # ------------------------------------------------------------------ scanning / planning
    def invalidate(self):
        """Forget all cached remote state: the next scan is a full scan (startup, resume from sleep)."""
        self.api.forget_etags()
        self._status.clear()
        self._job_docs.clear()

    def _job_doc(self, sha, note_id):
        if sha not in self._job_docs:
            doc = None
            try:
                doc = contract.check("job", json.loads(self.api.blob(sha)))
                if doc["note_id"] != note_id:
                    raise contract.InvalidDocument("job.note_id does not match its directory")
            except (ValueError, KeyError, TypeError) as e:
                log.error("ignoring invalid job.json for note %s: %s", note_id, e)
                doc = None
            self._job_docs[sha] = doc
        return self._job_docs[sha]

    def scan(self):
        tree = self.api.tree("inbox")
        self.last_scan_at = self.clock.now_iso()
        jobs = []
        for path, ent in (tree or {}).items():
            m = re.fullmatch(rf"jobs/({ULID})/job\.json", path)
            if not m:
                continue
            note = m.group(1)
            doc = self._job_doc(ent["sha"], note)
            if doc is None:
                continue
            files = {p: e for p, e in tree.items() if p.startswith(f"jobs/{note}/")}
            jobs.append(Job(note, doc["job_id"], doc, files, parse_iso(doc["created_at"])))
        jobs.sort(key=lambda j: j.sort_key)
        return jobs

    def _refresh_statuses(self, note_ids):
        if not note_ids:
            return
        main = self.api.tree("main") or {}
        for n in note_ids:
            ent = main.get(f"notes/{n}/status.json")
            cached = self._status.get(n)
            if ent is None:
                self._status[n] = (None, None, False)
            elif cached and cached[2]:
                continue          # we are the only writer of status.json: our own copy is authoritative (guards stale reads)
            elif not cached or cached[0] != ent["sha"]:
                try:
                    doc = json.loads(self.api.blob(ent["sha"]))
                except ValueError:
                    log.error("status.json of note %s is not valid JSON; treating as absent", n)
                    doc = None
                self._status[n] = (ent["sha"], doc, False)

    def status_of(self, note_id):
        return (self._status.get(note_id) or (None, None, False))[1]

    def plan(self):
        jobs = self.scan()
        self._refresh_statuses({j.note_id for j in jobs})
        p = Plan()
        for j in jobs:
            st = self.status_of(j.note_id)
            if st and (j.job_id in st.get("completed_job_ids", []) or (st.get("job_id") == j.job_id and st.get("state") == "done")):
                p.cleanup.append(j)
            elif st and st.get("job_id") == j.job_id and st.get("state") == "failed":
                p.failed.append(j)
            else:
                p.pending.append(j)
        self._last_queue = {
            "jobs_pending": len(p.pending), "bytes_pending": sum(j.bytes for j in p.pending),
            "oldest_job_created_at": iso(p.pending[0].created_at) if p.pending else None}
        return p

    # ------------------------------------------------------------------ status helpers
    def _path(self, note, name):
        return f"notes/{note}/{name}"

    def _write_status(self, st, message=None, throttle=False):
        if throttle and self.clock.mono() - self._last_status_write < 15:
            return
        st["updated_at"] = self.clock.now_iso()
        contract.check("status", st)
        data = contract.dumps(st)
        self.api.put_file(self._path(st["note_id"], "status.json"), data, message or f"note {st['note_id']}: status {st['state']}")
        self._status[st["note_id"]] = (blob_sha(data), st, True)
        self._last_status_write = self.clock.mono()

    def _new_status(self, job, state, prev=None, attempt=0, position=None):
        return {
            "schema_version": 2, "note_id": job.note_id, "job_id": job.job_id, "action": job.doc["action"], "state": state,
            "queue_position": position, "queued_at": job.doc["created_at"], "started_at": None, "finished_at": None,
            "updated_at": self.clock.now_iso(), "worker_id": self.cfg.worker_id, "attempt": attempt,
            "max_attempts": self.cfg.max_attempts,
            "parts": [{"part_id": p["id"], "state": "pending"} for p in job.doc["parts"]],
            "audio": {"state": "local" if job.location == "local" else "on_inbox", "purge_after": None},
            "completed_job_ids": list((prev or {}).get("completed_job_ids", [])),
        }

    def sync_queue(self, pending):
        """Make sure every waiting job has a `queued` status (with its position). One commit for all changes."""
        changes = {}
        for pos, j in enumerate(pending, 1):
            st = self.status_of(j.note_id)
            if st and st.get("job_id") == j.job_id:
                if st["state"] == "queued" and st.get("queue_position") != pos and j.job_id not in self.retry_after:
                    st = dict(st, queue_position=pos)
                else:
                    continue
            else:
                st = self._new_status(j, "queued", prev=st, position=pos)
            st["updated_at"] = self.clock.now_iso()
            contract.check("status", st)
            changes[j.note_id] = st
        if not changes:
            return
        if len(changes) == 1:
            self._write_status(next(iter(changes.values())))
            return
        files = {self._path(n, "status.json"): contract.dumps(s) for n, s in changes.items()}
        self.api.commit_files("main", files, "queue update")
        for n, s in changes.items():
            self._status[n] = (blob_sha(files[self._path(n, "status.json")]), s, True)

    # ------------------------------------------------------------------ heartbeat
    def heartbeat(self, state=None, force=True):
        if not force and self.clock.mono() - self._last_hb < self.cfg.heartbeat_interval_s:
            return
        doc = {
            "schema_version": 1, "worker_id": self.cfg.worker_id, "version": __version__,
            "state": state or ("busy" if self.busy_note else "idle"), "started_at": self.started_at,
            "last_seen": self.clock.now_iso(), "queue": dict(self._last_queue),
            "current_note_id": self.busy_note,
        }
        if self.last_scan_at:
            doc["last_scan_at"] = self.last_scan_at
        try:
            contract.check("worker", doc)
            self.api.put_file("worker/heartbeat.json", contract.dumps(doc), "worker heartbeat")
            self._last_hb = self.clock.mono()
        except (NetworkError, ApiError) as e:
            log.warning("heartbeat not written: %s", e)

    # ------------------------------------------------------------------ main cycle
    def cycle(self):
        """One scan + at most one job. Returns True when a job was processed (caller should rescan at once)."""
        plan = self.plan()
        for j in plan.cleanup:
            self.finish_cleanup(j)
        self.sync_queue(plan.pending)
        self.janitor(plan.failed)
        self.gc_images()
        runnable = [j for j in plan.pending if self.retry_after.get(j.job_id, 0) <= self.clock.mono()]
        if not runnable:
            self.heartbeat(force=False)
            return False
        self.run_job(runnable[0])
        return True

    def drain(self, max_jobs=10 ** 6):
        """Process everything that is runnable right now (`lesson-worker once`). Returns number of jobs run."""
        n = 0
        while not self.stop_requested and n < max_jobs and self.cycle():
            n += 1
        return n

    def run_forever(self):
        self.heartbeat("idle")
        self.invalidate()
        net_fail = 0
        while not self.stop_requested:
            try:
                if self.cycle():
                    net_fail = 0
                    continue
                net_fail = 0
                wait = self.cfg.poll_interval_s
            except (NetworkError, ApiError) as e:
                net_fail += 1
                wait = min(self.cfg.poll_interval_s * 2 ** min(net_fail, 5), 600)
                log.warning("GitHub unreachable or refused (%s); retrying in %.0fs", e, wait)
            except Exception:
                log.exception("unexpected error in scan loop")
                wait = 60
            if self.asr is not None and hasattr(self.asr, "release_if_idle"):
                self.asr.release_if_idle()
            if self.clock.wait(wait, lambda: self.stop_requested):
                self.resumes += 1
                log.info("resume from sleep detected: full catch-up scan")
                self.invalidate()
                self.retry_after.clear()
                self.clock.sleep(5)              # let the network come back
                self.heartbeat("idle")
        self.heartbeat("offline")

    # ------------------------------------------------------------------ job execution
    def run_job(self, job):
        self.busy_note = job.note_id
        self.heartbeat("busy")
        try:
            self._process(job)
        except Stopped:
            log.info("stopped between work units; job %s stays queued", job.job_id)
        except (NetworkError, ApiError):
            self._no_bump.add(job.job_id)
            raise
        finally:
            self.busy_note = None
            self.heartbeat("idle")

    def _check_stop(self, st):
        if self.stop_requested:
            clean = self._clean(st)
            clean.update(state="queued", attempt=max(st["attempt"] - 1, 0))     # a clean stop is not a failed attempt
            clean.pop("progress", None)
            self._write_status(clean)
            raise Stopped()

    def _process(self, job):
        doc, note = job.doc, job.note_id
        prev = self.status_of(note)
        same = bool(prev and prev.get("job_id") == job.job_id)
        attempt = prev["attempt"] if same else 0
        if job.job_id in self._no_bump:
            self._no_bump.discard(job.job_id)
        else:
            attempt += 1
        st = self._new_status(job, "queued", prev=prev, attempt=attempt)
        if same and prev.get("started_at"):
            st["started_at"] = prev["started_at"]
        st["started_at"] = st["started_at"] or self.clock.now_iso()
        st["_stage"] = "verify"
        try:
            if attempt > self.cfg.max_attempts:
                raise Fail("attempts_exhausted", f"gave up after {self.cfg.max_attempts} interrupted or failed attempts", True, "transcribe")
            self._load_images(job, st)
            if doc["action"] == "regenerate_notes":
                self._regenerate(job, st)
            else:
                self._transcribe_job(job, st)
        except Fail as f:
            self._handle_fail(job, st, f)
        except (Stopped, NetworkError, ApiError):
            raise
        except contract.InvalidDocument as e:
            self._handle_fail(job, st, Fail("output_invalid", str(e), False, "commit"))
        except Exception as e:           # bug or environment problem: bounded automatic retries
            log.exception("unexpected error in job %s", job.job_id)
            self._handle_fail(job, st, Fail("internal_error", f"{type(e).__name__}: {e}"[:500], True, st.get("_stage", "transcribe"), auto_retry=True))

    @staticmethod
    def _clean(st):
        return {k: v for k, v in st.items() if not k.startswith("_")}

    def _emit(self, st, state, part_index=None, part_total=None, part_id=None, percent=None, throttle=False):
        st["state"] = state
        st["queue_position"] = None
        if part_index is not None:
            st["progress"] = {"part_index": part_index, "part_total": part_total, "part_id": part_id, "percent": None if percent is None else round(percent, 1)}
        else:
            st.pop("progress", None)
        if state in ("transcribing", "diarizing"):
            st["_stage"] = "transcribe" if state == "transcribing" else "diarize"
        elif state == "reading_images":
            st["_stage"] = "read_images"
        elif state == "summarizing":
            st["_stage"] = "summarize"
        self._write_status(self._clean(st), throttle=throttle)

    def _handle_fail(self, job, st, f):
        now = self.clock.now_iso()
        clean = self._clean(st)
        if f.auto_retry and clean["attempt"] < self.cfg.max_attempts:
            back = self.cfg.retry_backoff_s
            wait = back[min(clean["attempt"] - 1, len(back) - 1)] if back else 0
            self.retry_after[job.job_id] = self.clock.mono() + wait
            log.warning("job %s failed (%s: %s); retry %d/%d in %.0fs", job.job_id, f.code, f.message, clean["attempt"], self.cfg.max_attempts, wait)
            clean["state"] = "queued"
            clean.pop("progress", None)
            self._write_status(clean)
            return
        log.error("job %s FAILED (%s: %s)", job.job_id, f.code, f.message)
        days = self._retention_days()
        purge = iso(parse_iso(now) + dt.timedelta(days=days))
        clean.update(state="failed", finished_at=now, queue_position=None,
                     error={"code": f.code, "message": f.message[:1000], "retryable": f.retryable, "stage": f.stage, "failed_at": now})
        clean["audio"] = {"state": "local" if job.location == "local" else "on_inbox", "purge_after": purge}
        self._write_status(clean)

    def _retention_days(self):
        try:
            got = self.api.get_file("settings/app.json")
            if got:
                return int(json.loads(got[0]).get("failed_audio_retention_days", self.cfg.failed_audio_retention_days))
        except (ValueError, ApiError, NetworkError):
            pass
        return self.cfg.failed_audio_retention_days

    # ---- audio acquisition ----------------------------------------------------------------------
    def _fetch_part(self, job, p):
        """Verified original audio of a part in the local cache. Returns its path."""
        import hashlib
        note, rel = job.note_id, p["file"]
        ext = rel.rsplit(".", 1)[1]
        d = self.cache.part_dir(p["sha256"])
        dest = d / f"audio.{ext}"

        def verify(data):
            if len(data) != p["bytes"] or hashlib.sha256(data).hexdigest() != p["sha256"]:
                raise Fail("sha_mismatch", f"part {p['id']} does not match job.json (bytes/sha256); re-upload the audio", True, "verify")

        if dest.exists():
            try:
                verify(dest.read_bytes())
                return dest
            except Fail:
                dest.unlink(missing_ok=True)
        if job.location == "local":
            src = self.cfg.local_audio_dir / note / rel
            if not src.is_file():
                raise Fail("part_missing", f"local audio {src} not found", False, "download")
            data = src.read_bytes()
        else:
            ent = job.files.get(f"jobs/{note}/{rel}")
            if ent is None:
                raise Fail("part_missing", f"part file {rel} is not on the inbox branch", True, "download")
            if ent.get("size") and ent["size"] != p["bytes"]:
                raise Fail("sha_mismatch", f"part {p['id']} has {ent['size']} bytes on inbox, job.json says {p['bytes']}", True, "verify")
            data = self.api.blob(ent["sha"])
        verify(data)
        tmp = d / (dest.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return dest

    def _wav(self, sha, orig):
        d = orig.parent
        wav = d / "audio.wav"
        if wav.exists():
            try:
                return wav, wav_duration(wav)
            except Exception:
                wav.unlink(missing_ok=True)
        try:
            return wav, to_wav16k(orig, wav)
        except AudioCorrupt as e:
            raise Fail("audio_corrupt", str(e), False, "transcribe")

    # ---- note/context helpers -------------------------------------------------------------------
    def _json_file(self, path):
        got = self.api.get_file(path)
        if not got:
            return None
        try:
            d = json.loads(got[0])
            return d if isinstance(d, dict) else None
        except ValueError:
            return None

    def _note_doc(self, note):
        """note.json (PWA-owned, read-only for us) or None when absent/unreadable."""
        return self._json_file(self._path(note, "note.json"))

    def _notebook_name(self, note):
        nd = self._note_doc(note) or {}
        if not nd.get("notebook_id"):
            return ""
        return (self._json_file(f"notebooks/{nd['notebook_id']}.json") or {}).get("name", "")

    def _context(self, job):
        """Context snapshot of the job; v1 jobs fall back to note.json (v2) or the notebook name (v1 note)."""
        if job.context:
            return dict(job.context)
        nd = self._note_doc(job.note_id) or {}
        if nd.get("context"):
            return dict(nd["context"])
        subject = ""
        if nd.get("notebook_id"):
            nb = self._json_file(f"notebooks/{nd['notebook_id']}.json") or {}
            subject = nb.get("name", "")
        return {"subject": subject, "topic": "", "details": ""}

    # ---- photos ---------------------------------------------------------------------------------
    @staticmethod
    def _listed_image_ids(note_doc):
        if note_doc is None or "images" not in note_doc:
            return None
        return {i["id"] for i in note_doc["images"]}

    def _image_bytes(self, job, im):
        """Verified original bytes of one photo (inbox, main or laptop cache). Mismatch -> sha_mismatch."""
        note, rel = job.note_id, im["file"]
        ext = rel.rsplit(".", 1)[1]

        def check(data, strict=True):
            if hashlib.sha256(data).hexdigest() == im["sha256"] and len(data) == im["bytes"]:
                return data
            if not strict:
                # the worker stores photos metadata-free; if that changed the bytes, accept what is already clean
                try:
                    if not imgmod.clean_image(data, ext).changed:
                        return data
                except imgmod.ImageError:
                    pass
            raise Fail("sha_mismatch", f"photo {im['id']} does not match job.json (bytes/sha256); re-upload the photo", True, "verify")

        loc = im["location"]
        if loc == "inbox":
            ent = job.files.get(f"jobs/{note}/{rel}")
            if ent is None:
                raise Fail("part_missing", f"photo file {rel} is not on the inbox branch", True, "download")
            if ent.get("size") and ent["size"] != im["bytes"]:
                raise Fail("sha_mismatch", f"photo {im['id']} has {ent['size']} bytes on inbox, job.json says {im['bytes']}", True, "verify")
            return check(self.api.blob(ent["sha"]))
        if loc == "main":
            got = self.api.get_file(f"notes/{note}/{rel}")
            if got is None:
                raise Fail("image_missing", f"photo {im['id']} is not stored with the note; remove or re-add it and regenerate", False, "download")
            return check(got[0], strict=False)
        src = self.cfg.local_audio_dir / note / rel
        if not src.is_file():
            raise Fail("image_missing", f"local photo {src} not found", False, "download")
        return check(src.read_bytes())

    def _load_images(self, job, st):
        """Fetch + verify every photo of the job (fail fast, before the long transcription). Photos the user already
        removed from note.json are skipped."""
        loaded = []
        if job.images:
            listed = self._listed_image_ids(self._note_doc(job.note_id))
            for im in sorted(job.images, key=lambda x: x["id"]):
                if listed is not None and im["id"] not in listed:
                    log.info("photo %s of note %s was removed from note.json; skipped", im["id"], job.note_id)
                    continue
                loaded.append({"spec": im, "raw": self._image_bytes(job, im)})
        st["_img"] = loaded

    def _prepare_images(self, job, st):
        """reading_images stage: validate + scrub every photo, write the scrubbed copies for the notes engine.
        Returns (shown, tmpdir) where shown = [{id,path,mime,width,height,bytes,caption?}]."""
        if not job.images:
            return [], None
        self._check_stop(st)
        self._emit(st, "reading_images")
        tmp = Path(self.cfg.cache_dir) / "images" / job.job_id
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        shown = []
        for it in st.get("_img", []):
            spec = it["spec"]
            ext = spec["file"].rsplit(".", 1)[1]
            try:
                ci = imgmod.clean_image(it["raw"], ext)
            except imgmod.ImageError as e:
                raise Fail("image_invalid", f"photo {spec['id']} cannot be used: {e}", False, "read_images")
            it["clean"] = ci.data
            path = tmp / f"{spec['id']}.{ext}"
            path.write_bytes(ci.data)
            path.chmod(0o600)
            d = {"id": spec["id"], "path": str(path), "mime": ci.mime, "width": ci.width, "height": ci.height, "bytes": len(ci.data)}
            if spec.get("caption"):
                d["caption"] = spec["caption"]
            shown.append(d)
        return shown, tmp

    # ---- the transcribe pipeline ----------------------------------------------------------------
    def _transcribe_job(self, job, st):
        doc, note = job.doc, job.note_id
        parts = sorted(doc["parts"], key=lambda p: p["order"])
        n = len(parts)
        mode = job.language_mode
        ctx = self._context(job)
        hint = asrmod.build_hint(self.cfg, ctx)
        old = self._get_transcript(note) if doc["append"] else None
        if doc["append"] and old is None:
            raise Fail("append_without_transcript", "job appends to a note that has no transcript yet", False, "verify")
        first = parts[0]["id"]
        self._emit(st, "transcribing", 1, n, first, 0.0)
        paths = {}
        for p in parts:
            self._check_stop(st)
            paths[p["id"]] = self._fetch_part(job, p)

        asr_key = {"model": self.cfg.whisper_model, "language": mode, "prompt": self.cfg.whisper_initial_prompt, "hint": hint,
                   "hint_mode": self.cfg.whisper_hint_mode if hint else "off", "mixed": asrmod.mixed_key(self.cfg) if mode == "mixed" else None}
        results = {}
        for i, p in enumerate(parts, 1):
            self._check_stop(st)
            self._emit(st, "transcribing", i, n, p["id"], 0.0)
            c = self.cache.read_json(p["sha256"], "asr.json")
            if not (c and c.get("key") == asr_key):
                wav, dur = self._wav(p["sha256"], paths[p["id"]])
                audio = load_f32(wav)

                def prog(pc, i=i, p=p):
                    self._check_stop(st)
                    self._emit(st, "transcribing", i, n, p["id"], pc, throttle=True)
                try:
                    segs = self.asr.transcribe(audio, dur, mode, prog, hint=hint) if hint else self.asr.transcribe(audio, dur, mode, prog)
                except (Stopped, ApiError, NetworkError):
                    raise
                except Exception as e:
                    raise Fail("transcribe_failed", f"{type(e).__name__}: {e}"[:500], True, "transcribe", auto_retry=True)
                c = {"key": asr_key, "duration": round(dur, 2), "segments": _with_lang(segs, mode), "transcribed_at": self.clock.now_iso()}
                self.cache.write_json(p["sha256"], "asr.json", c)
            results[p["id"]] = c
            sp = next(x for x in st["parts"] if x["part_id"] == p["id"])
            sp.update(state="transcribed", duration=c["duration"])

        diarized = False
        if doc["options"]["diarize"]:
            diarized = self._diarize_parts(job, st, parts, results)

        engine = self.asr.engine if hasattr(self.asr, "engine") else {"name": "faster-whisper", "model": self.cfg.whisper_model}
        new_parts = []
        for p in parts:
            r = results[p["id"]]
            segs = diarmod.label_segments(r["segments"]) if diarized and r.get("diarized") else [{k: v for k, v in s.items() if k != "spk"} for s in r["segments"]]
            new_parts.append({"part_id": p["id"], "order": p["order"], "duration": r["duration"], "offset": 0.0, "job_id": job.job_id,
                              "transcribed_at": r["transcribed_at"], "segments": segs})
        diar_info = {"enabled": bool(diarized or (old and old["diarization"]["enabled"]))}
        if diar_info["enabled"]:
            diar_info.update(engine="sherpa-onnx pyannote-segmentation-3.0 + 3D-Speaker CAM++", speaker_scope="per_part")
        tr = tmod.build(note, mode, engine, diar_info, old, new_parts, self.clock.now_iso())
        if not any(p["segments"] for p in tr["parts"]):
            raise Fail("no_speech", "no speech was recognised in the audio", False, "transcribe")
        self._summarize_and_commit(job, st, tr, ctx)

    def _diarize_parts(self, job, st, parts, results):
        n = len(parts)
        want = job.doc["options"].get("num_speakers")
        key = {"thr": self.cfg.diarize_threshold, "n": want}
        if not diarmod.models_present(self.cfg):
            log.warning("diarization requested but models are missing (run `lesson-worker download-models`); continuing without speakers")
            return False
        ok_any = False
        for i, p in enumerate(parts, 1):
            self._check_stop(st)
            self._emit(st, "diarizing", i, n, p["id"], 0.0)
            r = results[p["id"]]
            c = self.cache.read_json(p["sha256"], "diar.json")
            try:
                if not (c and c.get("key") == key):
                    wav, _ = self._wav(p["sha256"], self.cache.part_dir(p["sha256"]) / f"audio.{p['file'].rsplit('.', 1)[1]}")
                    dsegs = self.diarizer(self.cfg, load_f32(wav), want)
                    dsegs = diarmod.absorb_small_speakers(dsegs, self.cfg.diarize_min_speaker_s) if r["duration"] > 120 else dsegs
                    c = {"key": key, "segments": dsegs}
                    self.cache.write_json(p["sha256"], "diar.json", c)
                r["segments"] = diarmod.assign_speakers(r["segments"], c["segments"])
                r["diarized"] = True
                ok_any = True
                next(x for x in st["parts"] if x["part_id"] == p["id"])["state"] = "diarized"
            except (Stopped, ApiError, NetworkError):
                raise
            except Exception as e:      # best-effort: a diarization problem must never lose the transcript
                log.warning("diarization of %s failed (%s: %s); that part gets no speaker labels", p["id"], type(e).__name__, e)
        return ok_any

    def _get_transcript(self, note):
        got = self.api.get_file(self._path(note, "transcript.json"))
        if not got:
            return None
        try:
            return contract.check("transcript", json.loads(got[0]))
        except (ValueError, contract.InvalidDocument) as e:
            log.error("existing transcript of %s is unusable: %s", note, e)
            return None

    def _regenerate(self, job, st):
        old = self._get_transcript(job.note_id)
        if old is None:
            raise Fail("no_transcript", "regenerate_notes needs an existing transcript", False, "summarize")
        was_v1 = old.get("schema_version", 1) == 1
        # the transcript keeps the language mode it was decoded with; the audio is gone, so a new mode cannot change it
        tr = tmod.build(job.note_id, old.get("language_mode", "cs"), old["engine"], old["diarization"], old, [], old["updated_at"])
        self._summarize_and_commit(job, st, tr, self._context(job), write_transcript=was_v1)

    def _previous_notes(self, note):
        d = self._json_file(self._path(note, "notes.json"))
        return d if d and isinstance(d.get("content"), dict) else None

    def _summarize_and_commit(self, job, st, tr, ctx, write_transcript=True):
        note = job.note_id
        shown, tmp = self._prepare_images(job, st)
        try:
            self._check_stop(st)
            self._emit(st, "summarizing")
            usage = {}
            try:
                content, model = self.notes(self.cfg, transcript=tr, context=ctx, language_mode=job.language_mode, kind=job.doc["kind"],
                                            images=shown, previous_notes=self._previous_notes(note), notebook_name=self._notebook_name(note),
                                            usage=usage)
            except NotesError as e:
                raise Fail(e.code, e.message, e.retryable, "summarize", auto_retry=e.auto_retry or e.code in ("claude_timeout", "claude_error", "claude_bad_output"))
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        content = notes_final.finalize(content, job.doc["kind"])
        notes_doc = {"schema_version": 2, "note_id": note, "kind": job.doc["kind"], "job_id": job.job_id, "generated_at": self.clock.now_iso(),
                     "model": model, "based_on_parts": [p["part_id"] for p in tr["parts"]], "language": "cs",
                     "based_on_images": usage.get("images_used", [d["id"] for d in shown]), "content": content}
        if usage.get("images_skipped"):
            log.warning("notes engine could not read photos: %s", usage["images_skipped"])
        done = self._clean(st)
        done.pop("progress", None)
        now = self.clock.now_iso()
        done.update(state="done", queue_position=None, finished_at=now, updated_at=now)
        done["completed_job_ids"] = (done["completed_job_ids"] + [job.job_id])[-MAX_COMPLETED:]
        for sp in done["parts"]:
            if sp["state"] == "pending":
                sp["state"] = "transcribed"
        image_changes, kept = self._image_changes(job, st)
        done["images_kept"] = kept
        contract.check("transcript", tr)
        contract.check("notes", notes_doc)
        contract.check("status", done)
        files = {self._path(note, "notes.json"): contract.dumps(notes_doc), self._path(note, "status.json"): contract.dumps(done)}
        if write_transcript:
            files[self._path(note, "transcript.json")] = contract.dumps(tr)
        files.update(image_changes)
        self.api.commit_files("main", files, f"note {note}: done")       # THE commit point
        self._status[note] = (blob_sha(files[self._path(note, "status.json")]), done, True)
        log.info("job %s done", job.job_id)
        self.retry_after.pop(job.job_id, None)
        for p in job.doc["parts"]:
            self.cache.drop(p["sha256"])
        self.finish_cleanup(job)

    def _image_changes(self, job, st):
        """Files to add/delete under notes/<id>/images/ in the completion commit and the resulting images_kept ids.
        Copies `inbox` and `local` (laptop-recorded) photos that note.json still lists (re-read now: the user may have deleted one meanwhile),
        deletes orphans (files note.json no longer lists). Without a readable note.json nothing is deleted."""
        note = job.note_id
        prefix = f"notes/{note}/images/"
        main = self.api.tree("main") or {}
        present = {p: e for p, e in main.items() if p.startswith(prefix)}
        nd = self._note_doc(note)
        listed = self._listed_image_ids(nd)
        changes = {}
        for it in st.get("_img", []):
            spec = it["spec"]
            if spec["location"] not in ("inbox", "local") or (listed is not None and spec["id"] not in listed):
                continue
            path = f"notes/{note}/{spec['file']}"
            data = it.get("clean")
            if data is None:                                     # job without reading_images stage data (cannot happen) - be safe
                data = imgmod.clean_image(it["raw"], spec["file"].rsplit(".", 1)[1]).data
            if path not in present or present[path]["sha"] != blob_sha(data):
                changes[path] = data
        after = (set(present) | set(changes))
        if nd is not None and "images" in nd:
            listed_paths = {prefix + i["filename"] for i in nd["images"]}
            for p in sorted(after - listed_paths):
                if p in present:
                    changes[p] = None
                else:
                    changes.pop(p, None)
                after.discard(p)
        kept = sorted({f"i{p.rsplit('/', 1)[1].split('.')[0]}" for p in after})
        return changes, kept

    # ------------------------------------------------------------------ audio removal / janitor
    def _remove_job_audio(self, job):
        """Remove THIS job's audio from inbox; never touches a newer job.json or files it references. Orphan-recreate only if inbox becomes empty."""
        if job.location == "local":
            # only THIS job's parts: the recorder may already have stored the parts of a later job for the same note
            base = self.cfg.local_audio_dir / job.note_id
            for p in job.doc["parts"]:
                (base / p["file"]).unlink(missing_ok=True)
            for im in job.images:
                if im["location"] == "local":
                    (base / im["file"]).unlink(missing_ok=True)
            for d in (base / "images", base / "parts", base):
                try:
                    d.rmdir()                    # only if empty
                except OSError:
                    pass
        tip1 = self.api.get_ref("inbox")
        if tip1 is None:
            return True
        self.api.forget_etags()
        tree = self.api.tree("inbox") or {}
        jpath = f"jobs/{job.note_id}/job.json"
        cur = tree.get(jpath)
        mine = {jpath} | {f"jobs/{job.note_id}/{p['file']}" for p in job.doc["parts"]} \
            | {f"jobs/{job.note_id}/{i['file']}" for i in job.images if i["location"] == "inbox"}
        if cur is not None:
            try:
                cur_doc = json.loads(self.api.blob(cur["sha"]))
            except ValueError:
                cur_doc = {}
            if cur_doc.get("job_id") != job.job_id:
                # job.json now belongs to a newer job (pinned-job rule): keep it, and keep every file that job still
                # references; only our own, now unreferenced part files may go.
                log.info("job.json of note %s belongs to a newer job; removing only this job's unreferenced parts", job.note_id)
                theirs = {f"jobs/{job.note_id}/{p['file']}" for p in cur_doc.get("parts", [])} \
                    | {f"jobs/{job.note_id}/{i['file']}" for i in cur_doc.get("images", []) if i.get("location") == "inbox"}
                mine = (mine - {jpath}) - theirs
        gone = {p for p in tree if p in mine}
        if not gone:
            return True
        remaining = [p for p in tree if p not in gone and p != ".keep"]
        if not remaining and self.api.get_ref("inbox") == tip1:
            self.api.delete_ref("inbox")                    # nothing else queued: drop history, fresh orphan
            self.api.create_orphan_branch("inbox")
        else:
            self.api.commit_files("inbox", {p: None for p in gone}, "remove processed audio")
        self.api.forget_etags()
        return True

    def finish_cleanup(self, job):
        """Idempotent post-`done` cleanup: delete audio, mark status audio=purged."""
        try:
            if not self._remove_job_audio(job):
                return
        except ApiError as e:
            log.warning("inbox cleanup of %s postponed: %s", job.job_id, e)
            return
        st = self.status_of(job.note_id)
        if st and st.get("job_id") == job.job_id and st["audio"]["state"] != "purged":
            st = json.loads(json.dumps(st))
            st["audio"] = {"state": "purged", "purge_after": None}
            self._write_status(st)

    def janitor(self, failed_jobs):
        """Failed jobs keep their audio for failed_audio_retention_days (status.audio.purge_after), then it is purged."""
        now = self.clock.now()
        for j in failed_jobs:
            st = self.status_of(j.note_id)
            pa = (st or {}).get("audio", {}).get("purge_after")
            if not pa or st["audio"]["state"] == "purged" or parse_iso(pa) > now:
                continue
            log.info("purging audio of failed job %s (retention over)", j.job_id)
            if self._remove_job_audio(j):
                st = json.loads(json.dumps(st))
                st["audio"] = {"state": "purged", "purge_after": None}
                self._write_status(st)
                for p in j.doc["parts"]:
                    self.cache.drop(p["sha256"])

    def gc_images(self, force=False):
        """Photos on main that note.json no longer lists are deleted in every completion commit; this pass catches notes
        that are never regenerated. Only acts when note.json is readable and has an images list."""
        if not force and self.clock.mono() - self._last_gc < self.cfg.image_gc_interval_s:
            return
        self._last_gc = self.clock.mono()
        by_note = {}
        for path in (self.api.tree("main") or {}):
            m = re.fullmatch(rf"notes/({ULID})/images/\d\d\.(?:jpg|png|webp)", path)
            if m:
                by_note.setdefault(m.group(1), set()).add(path)
        for note, paths in by_note.items():
            nd = self._note_doc(note)
            if nd is None or "images" not in nd:
                continue
            listed = {f"notes/{note}/images/{i['filename']}" for i in nd["images"]}
            orphans = sorted(paths - listed)
            if orphans:
                log.info("removing %d unused photo(s) of note %s", len(orphans), note)
                self.api.commit_files("main", {p: None for p in orphans}, f"note {note}: remove unused photos")
