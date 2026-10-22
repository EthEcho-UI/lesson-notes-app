"""Read side of the data repo (notebooks, notes + their status, settings, worker heartbeat) with an on-disk cache so the
recorder still works (start/stop/list) when the laptop is offline."""
import json
import logging
import re
import threading
import time

from lesson_worker.ghapi import ApiError, GitHub, NetworkError

from .common import LANGUAGE_MODES, ConfigError, now_iso, read_json, write_json

log = logging.getLogger("lesson_record.remote")
MAX_NOTES = 60


class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, wcfg, paths, timeout=15, retries=1):
        self.wcfg, self.paths, self.timeout, self.retries = wcfg, paths, timeout, retries
        self._lock = threading.Lock()

    def api(self, timeout=None, retries=None) -> GitHub:
        try:
            token = self.wcfg.read_token()
        except ConfigError as e:
            raise RemoteError(str(e))
        return GitHub(self.wcfg.repo, token, self.wcfg.api_base, retries=self.retries if retries is None else retries,
                      timeout=timeout or self.timeout)

    # ------------------------------------------------------------------------------------------
    def cached(self):
        return read_json(self.paths.cache_file, {}) or {}

    def refresh(self, max_age=0.0):
        """Fetch what changed since the last refresh. Never raises: returns the cache with `online`/`error` set."""
        with self._lock:
            cache = self.cached()
            if max_age and cache.get("online") and time.time() - cache.get("fetched_ts", 0) < max_age:
                return cache
            try:
                cache = self._fetch(cache)
                cache.update(online=True, error=None)
            except (NetworkError, ApiError, RemoteError) as e:
                cache = dict(cache)
                cache.update(online=False, error=str(e))
                log.info("remote refresh failed: %s", e)
            cache["checked_at"] = now_iso()
            try:
                write_json(self.paths.cache_file, cache)
            except OSError:
                pass
            return cache

    def _fetch(self, cache):
        api = self.api()
        tree = api.tree("main")
        if tree is None:
            raise RemoteError("branch main of the data repo does not exist yet")
        blobs = cache.get("blobs", {})
        used = {}

        def doc(path):
            ent = tree.get(path)
            if ent is None:
                return None
            sha = ent["sha"]
            if sha not in blobs:
                try:
                    blobs[sha] = json.loads(api.blob(sha))
                except ValueError:
                    blobs[sha] = None
            used[sha] = blobs[sha]
            return blobs[sha]

        notebooks = [d for p in sorted(tree) if re.fullmatch(r"notebooks/[0-9A-Z]{26}\.json", p) and (d := doc(p))]
        note_ids = sorted({p.split("/")[1] for p in tree if re.fullmatch(r"notes/[0-9A-Z]{26}/note\.json", p)}, reverse=True)[:MAX_NOTES]
        notes = []
        for nid in note_ids:
            n = doc(f"notes/{nid}/note.json")
            if not n:
                continue
            st = doc(f"notes/{nid}/status.json") or {}
            notes.append({"id": nid, "kind": n.get("kind"), "title": n.get("title"), "notebook_id": n.get("notebook_id"),
                          "created_at": n.get("created_at"), "source": n.get("source"), "schema_version": n.get("schema_version"),
                          "language_mode": n.get("language_mode"), "context": n.get("context"), "images": n.get("images", []),
                          "parts": n.get("parts", []), "state": st.get("state"), "progress": st.get("progress"),
                          "error": (st.get("error") or {}).get("message") if st.get("state") == "failed" else None,
                          "status_job_id": st.get("job_id")})
        return {"fetched_ts": time.time(), "fetched_at": now_iso(), "notebooks": notebooks, "notes": notes,
                "settings": doc("settings/app.json"), "heartbeat": doc("worker/heartbeat.json"), "blobs": used}

    # ------------------------------------------------------------------------------------------
    def find_note(self, note_id, refresh=True):
        c = self.refresh(max_age=30) if refresh else self.cached()
        hit = next((n for n in c.get("notes", []) if n["id"] == note_id), None)
        if hit is None and refresh:
            c = self.refresh()                       # maybe created a moment ago
            hit = next((n for n in c.get("notes", []) if n["id"] == note_id), None)
        return hit

    def resolve_notebook(self, name_or_id):
        c = self.refresh(max_age=30)
        nbs = [n for n in c.get("notebooks", []) if not n.get("archived")]
        for n in nbs:
            if n["id"] == name_or_id:
                return n
        hits = [n for n in nbs if n["name"].casefold() == name_or_id.casefold()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            hits = [n for n in nbs if name_or_id.casefold() in n["name"].casefold()]
            if len(hits) == 1:
                return hits[0]
        names = ", ".join(sorted(n["name"] for n in nbs)) or "none (create one in the app first)"
        if len(hits) > 1:
            raise RemoteError(f"notebook '{name_or_id}' is ambiguous; available: {names}")
        why = "" if c.get("online") else f" (offline, using cached list: {c.get('error') or 'no cache yet'})"
        raise RemoteError(f"unknown notebook '{name_or_id}'{why}; available: {names}")

    def diarize_default(self, kind):
        s = (self.cached().get("settings") or {}).get("diarize_default") or {}
        return bool(s.get(kind, kind == "meeting"))

    def default_language_mode(self, rcfg):
        """settings/app.json `default_language_mode` (if the app ever sets one), else recorder.toml, else mixed."""
        m = (self.cached().get("settings") or {}).get("default_language_mode")
        return m if m in LANGUAGE_MODES else rcfg.language_mode
