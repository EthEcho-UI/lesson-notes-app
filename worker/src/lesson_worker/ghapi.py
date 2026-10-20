"""Minimal GitHub REST client (stdlib only) for the private data repo: contents, git data, trees with ETag.
The token is only ever sent in the Authorization header and never logged."""
import base64
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("lesson_worker.api")


class ApiError(Exception):
    def __init__(self, status, message, path=""):
        super().__init__(f"HTTP {status} {path}: {message}")
        self.status = status
        self.path = path


class NetworkError(Exception):
    """Transport-level failure or persistent 5xx/rate limit after retries."""


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GitHub:
    def __init__(self, repo, token, api_base="https://api.github.com", sleep=time.sleep, retries=4, timeout=120):
        self.repo = repo
        self._token = token
        self.base = api_base.rstrip("/")
        self.sleep = sleep
        self.retries = retries
        self.timeout = timeout
        self._etags = {}       # url -> (etag, parsed body)
        self._file_sha = {}    # (branch, path) -> sha we last wrote/read (for conditional PUT)

    # ---- transport -------------------------------------------------------------------------------
    def request(self, method, sub, body=None, etag_cache=False):
        """Returns (status, parsed_json_or_None). Raises ApiError for 4xx (except 304 handled), NetworkError otherwise."""
        url = f"{self.base}/repos/{self.repo}" + (f"/{sub}" if sub else "")
        data = None if body is None else json.dumps(body).encode()
        delay = 1.0
        last = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("User-Agent", "lesson-notes-worker")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            if etag_cache and url in self._etags:
                req.add_header("If-None-Match", self._etags[url][0])
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    obj = json.loads(raw) if raw else None
                    if etag_cache and r.headers.get("ETag"):
                        self._etags[url] = (r.headers["ETag"], obj)
                    return r.status, obj
            except urllib.error.HTTPError as e:
                raw = e.read()
                msg = ""
                try:
                    msg = json.loads(raw).get("message", "")
                except Exception:
                    msg = raw[:200].decode("utf-8", "replace")
                if e.code == 304 and url in self._etags:
                    return 304, self._etags[url][1]
                rate = e.code == 429 or (e.code == 403 and (e.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in msg.lower()))
                if e.code >= 500 or rate:
                    last = f"HTTP {e.code} {msg}"
                    wait = delay
                    ra = e.headers.get("Retry-After")
                    if ra and ra.isdigit():
                        wait = min(int(ra), 120)
                    log.warning("GitHub %s %s -> %s, retry in %.0fs", method, sub.split("?")[0], e.code, wait)
                    if attempt < self.retries:
                        self.sleep(wait)
                        delay = min(delay * 2, 60)
                        continue
                    raise NetworkError(last)
                raise ApiError(e.code, msg, sub)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = str(getattr(e, "reason", e))
                log.warning("GitHub %s %s failed: %s", method, sub.split("?")[0], last)
                if attempt < self.retries:
                    self.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise NetworkError(last)
        raise NetworkError(last or "unknown")

    def forget_etags(self):
        self._etags.clear()

    # ---- reads ------------------------------------------------------------------------------------
    def repo_info(self):
        return self.request("GET", "")[1]

    def get_ref(self, branch):
        try:
            return self.request("GET", f"git/ref/heads/{branch}")[1]["object"]["sha"]
        except ApiError as e:
            if e.status == 404:
                return None
            raise

    def tree(self, branch):
        """Recursive tree {path: {sha,size}} of a branch, or None if the branch does not exist. Uses ETag (304)."""
        try:
            st, obj = self.request("GET", f"git/trees/{branch}?recursive=1", etag_cache=True)
        except ApiError as e:
            if e.status in (404, 409):      # 409 = empty repository
                return None
            raise
        if obj.get("truncated"):
            log.warning("tree of %s is truncated; queue may be incomplete", branch)
        return {t["path"]: {"sha": t["sha"], "size": t.get("size", 0)} for t in obj["tree"] if t["type"] == "blob"}

    def blob(self, sha):
        _, obj = self.request("GET", f"git/blobs/{sha}")
        return base64.b64decode(obj["content"])

    def get_file(self, path, branch="main"):
        """(bytes, sha) or None. Small JSON files only (contents API, <1 MB; larger fall back to the blob API)."""
        try:
            _, obj = self.request("GET", f"contents/{path}?ref={branch}")
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        if isinstance(obj, list):
            return None
        data = base64.b64decode(obj["content"]) if obj.get("encoding") == "base64" and obj.get("content") else self.blob(obj["sha"])
        self._file_sha[(branch, path)] = obj["sha"]
        return data, obj["sha"]

    # ---- writes -----------------------------------------------------------------------------------
    def put_file(self, path, data: bytes, message, branch="main"):
        """Create/update one file with a sha-conditional PUT (single-writer files: status, heartbeat)."""
        key = (branch, path)
        for _ in range(5):
            body = {"message": message, "branch": branch, "content": base64.b64encode(data).decode()}
            if key in self._file_sha:
                body["sha"] = self._file_sha[key]
            try:
                _, obj = self.request("PUT", f"contents/{path}", body)
                self._file_sha[key] = obj["content"]["sha"]
                return
            except ApiError as e:
                if e.status in (409, 422):
                    cur = self.get_file(path, branch)      # refreshes the cached sha
                    if cur is None:
                        self._file_sha.pop(key, None)
                    elif cur[0] == data:
                        return
                    self.sleep(0.5)
                    continue
                raise
        raise ApiError(409, "could not update after 5 attempts", path)

    def commit_files(self, branch, changes, message):
        """ONE commit with several file changes ({path: bytes | None=delete}) via the Git Data API.
        Retries from a fresh tip when the ref moved (409/422)."""
        for attempt in range(5):
            tip = self.get_ref(branch)
            if tip is None:
                raise ApiError(404, f"branch {branch} missing", "git/ref")
            base_tree = self.request("GET", f"git/commits/{tip}")[1]["tree"]["sha"]
            entries = []
            for p, d in changes.items():
                if d is None:
                    entries.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
                else:
                    sha = self.request("POST", "git/blobs", {"content": base64.b64encode(d).decode(), "encoding": "base64"})[1]["sha"]
                    entries.append({"path": p, "mode": "100644", "type": "blob", "sha": sha})
            tree = self.request("POST", "git/trees", {"base_tree": base_tree, "tree": entries})[1]["sha"]
            commit = self.request("POST", "git/commits", {"message": message, "tree": tree, "parents": [tip]})[1]["sha"]
            try:
                self.request("PATCH", f"git/refs/heads/{branch}", {"sha": commit, "force": False})
            except ApiError as e:
                if e.status in (409, 422):
                    self.sleep(0.5 * (attempt + 1))
                    continue
                raise
            for p, d in changes.items():
                if d is None:
                    self._file_sha.pop((branch, p), None)
                else:
                    self._file_sha[(branch, p)] = blob_sha(d)
            return commit
        raise ApiError(409, "ref kept moving", branch)

    def delete_ref(self, branch):
        try:
            self.request("DELETE", f"git/refs/heads/{branch}")
        except ApiError as e:
            if e.status not in (404, 422):
                raise

    def create_orphan_branch(self, branch, keep_text="inbox"):
        """Fresh orphan branch holding only .keep (b0c recipe). Tolerates a concurrent creator (422)."""
        b = self.request("POST", "git/blobs", {"content": keep_text, "encoding": "utf-8"})[1]["sha"]
        t = self.request("POST", "git/trees", {"tree": [{"path": ".keep", "mode": "100644", "type": "blob", "sha": b}]})[1]["sha"]
        c = self.request("POST", "git/commits", {"message": "init", "tree": t, "parents": []})[1]["sha"]
        try:
            self.request("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": c})
        except ApiError as e:
            if e.status != 422:
                raise
        self.forget_etags()
