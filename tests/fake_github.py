#!/usr/bin/env python3
"""Fake GitHub REST API (subset used by the PWA) + static server for the repo root + /__ctl control endpoints.
API (https, mapped from api.github.com by Chromium --host-resolver-rules): repo info, contents (GET/PUT/DELETE, dirs),
git blobs/trees/commits/refs, trees?recursive, ETag/304, CORS, optional fault injection."""
import base64, hashlib, json, os, re, ssl, subprocess, threading, time, mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

TOKEN = "test-token"
REPO = "EthEcho-UI/lesson-notes-data"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha1(b):
    return hashlib.sha1(b).hexdigest()


def blob_sha(data):
    return sha1(b"blob %d\0" % len(data) + data)


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.blobs = {}      # sha -> bytes
        self.trees = {}      # sha -> {path: blobsha}
        self.commits = {}    # sha -> {tree, parents}
        self.refs = {}       # branch -> commit sha
        self.faults = []     # {match, method, status|drop, n}
        self.log = []
        self.counter = 0
        self.commit_files("main", {"README.md": b"data repo\n"}, "init", parent_ok=False)

    def put_blob(self, data):
        s = blob_sha(data); self.blobs[s] = data; return s

    def make_tree(self, files):
        s = sha1(json.dumps(sorted(files.items())).encode()); self.trees[s] = dict(files); return s

    def make_commit(self, tree, parents, msg):
        self.counter += 1
        s = sha1(f"{tree}{parents}{msg}{self.counter}".encode()); self.commits[s] = {"tree": tree, "parents": parents, "message": msg}; return s

    def files(self, branch):
        c = self.refs.get(branch)
        return None if c is None else self.trees[self.commits[c]["tree"]]

    def commit_files(self, branch, changes, msg, parent_ok=True):
        """changes: {path: bytes|None}"""
        cur = dict(self.files(branch) or {})
        for p, d in changes.items():
            if d is None: cur.pop(p, None)
            else: cur[p] = self.put_blob(d)
        parent = self.refs.get(branch)
        c = self.make_commit(self.make_tree(cur), [parent] if parent else [], msg)
        self.refs[branch] = c
        return c


ST = State()


class Api(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "ETag, X-RateLimit-Remaining, Retry-After")

    def out(self, status, obj=None, etag=None, headers=None):
        body = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        self.cors()
        if etag: self.send_header("ETag", etag)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204); self.cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, If-None-Match, X-GitHub-Api-Version, Accept")
        self.send_header("Content-Length", "0"); self.end_headers()

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def fault(self, method, path):
        with ST.lock:
            for f in ST.faults:
                if f["n"] > 0 and (not f.get("method") or f["method"] == method) and f["match"] in path:
                    if f.get("skip", 0) > 0: f["skip"] -= 1; continue
                    f["n"] -= 1
                    return f
        return None

    def handle_any(self):
        u = urlparse(self.path); path = unquote(u.path); q = parse_qs(u.query)
        body = self.body() if self.command in ("POST", "PUT", "PATCH", "DELETE") else {}
        with ST.lock: ST.log.append([self.command, path + ("?" + u.query if u.query else "")])
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self.out(401, {"message": "Bad credentials"})
        f = self.fault(self.command, path)
        if f:
            if f.get("drop"):
                self.close_connection = True
                try: self.connection.shutdown(2)
                except OSError: pass
                return
            hdr = {"X-RateLimit-Remaining": "0"} if f["status"] == 403 else {}
            return self.out(f["status"], {"message": f.get("message", "injected")}, headers=hdr)
        m = re.match(rf"^/repos/{REPO}(/.*)?$", path)
        if not m: return self.out(404, {"message": "Not Found"})
        sub = (m.group(1) or "").lstrip("/")
        with ST.lock:
            try: return self.route(sub, q, body)
            except KeyError as e: return self.out(404, {"message": f"Not Found {e}"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

    def route(self, sub, q, body):
        meth = self.command
        if sub == "" and meth == "GET":
            return self.out(200, {"full_name": REPO, "private": True, "permissions": {"push": True, "pull": True}})
        m = re.match(r"^contents/(.+)$", sub)
        if m: return self.contents(m.group(1), q, body)
        if sub.startswith("git/trees/") and meth == "GET":
            br = sub[len("git/trees/"):]
            files = ST.files(br)
            if files is None: return self.out(404, {"message": "Not Found"})
            et = f'"{ST.refs[br]}"'
            if self.headers.get("If-None-Match") == et: return self.out(304, None, et)
            tree = [{"path": p, "mode": "100644", "type": "blob", "sha": s, "size": len(ST.blobs[s])} for p, s in sorted(files.items())]
            return self.out(200, {"sha": ST.commits[ST.refs[br]]["tree"], "tree": tree, "truncated": False}, et)
        if sub.startswith("git/ref/heads/") and meth == "GET":
            br = sub[len("git/ref/heads/"):]
            if br not in ST.refs: return self.out(404, {"message": "Not Found"})
            return self.out(200, {"ref": f"refs/heads/{br}", "object": {"sha": ST.refs[br], "type": "commit"}})
        if sub == "git/refs" and meth == "POST":
            br = body["ref"].replace("refs/heads/", "")
            if br in ST.refs: return self.out(422, {"message": "Reference already exists"})
            ST.refs[br] = body["sha"]; return self.out(201, {"ref": body["ref"], "object": {"sha": body["sha"]}})
        if sub.startswith("git/refs/heads/") and meth == "PATCH":
            br = sub[len("git/refs/heads/"):]
            if br not in ST.refs: return self.out(404, {"message": "Not Found"})
            if not body.get("force") and ST.refs[br] not in ST.commits[body["sha"]]["parents"] + [body["sha"]]:
                return self.out(422, {"message": "Update is not a fast forward"})
            ST.refs[br] = body["sha"]; return self.out(200, {"object": {"sha": body["sha"]}})
        if sub.startswith("git/refs/heads/") and meth == "DELETE":
            ST.refs.pop(sub[len("git/refs/heads/"):], None); return self.out(204)
        if sub == "git/blobs" and meth == "POST":
            data = body["content"].encode() if body.get("encoding") == "utf-8" else base64.b64decode(body["content"])
            return self.out(201, {"sha": ST.put_blob(data)})
        m = re.match(r"^git/blobs/([0-9a-f]+)$", sub)
        if m and meth == "GET":
            return self.out(200, {"sha": m.group(1), "encoding": "base64", "content": base64.b64encode(ST.blobs[m.group(1)]).decode(), "size": len(ST.blobs[m.group(1)])})
        if sub == "git/trees" and meth == "POST":
            files = dict(ST.trees[body["base_tree"]]) if body.get("base_tree") else {}
            for e in body["tree"]:
                if e.get("sha") is None and "content" not in e: files.pop(e["path"], None)
                elif "content" in e: files[e["path"]] = ST.put_blob(e["content"].encode())
                else: files[e["path"]] = e["sha"]
            return self.out(201, {"sha": ST.make_tree(files)})
        if sub == "git/commits" and meth == "POST":
            return self.out(201, {"sha": ST.make_commit(body["tree"], body.get("parents", []), body["message"])})
        m = re.match(r"^git/commits/([0-9a-f]+)$", sub)
        if m and meth == "GET": return self.out(200, {"sha": m.group(1), "tree": {"sha": ST.commits[m.group(1)]["tree"]}})
        return self.out(404, {"message": f"unhandled {meth} {sub}"})

    def contents(self, path, q, body):
        branch = (q.get("ref") or [body.get("branch", "main")])[0]
        files = ST.files(branch)
        if self.command == "GET":
            if files is None: return self.out(404, {"message": "No commit found for the ref"})
            if path in files:
                sha = files[path]; data = ST.blobs[sha]; et = f'"{sha}"'
                if self.headers.get("If-None-Match") == et: return self.out(304, None, et)
                big = len(data) > 1000000
                return self.out(200, {"type": "file", "name": path.split("/")[-1], "path": path, "sha": sha, "size": len(data),
                                      "encoding": "none" if big else "base64", "content": "" if big else base64.b64encode(data).decode()}, et)
            pre = path.rstrip("/") + "/"
            ents = [(p, s) for p, s in files.items() if p.startswith(pre) and "/" not in p[len(pre):]]
            if not ents: return self.out(404, {"message": "Not Found"})
            return self.out(200, [{"type": "file", "name": p.split("/")[-1], "path": p, "sha": s, "size": len(ST.blobs[s])} for p, s in sorted(ents)])
        if files is None: return self.out(404, {"message": "Branch not found"})
        if self.command == "PUT":
            exist = files.get(path)
            if exist and body.get("sha") != exist:
                return self.out(409 if body.get("sha") else 422, {"message": f"{path} does not match" if body.get("sha") else "\"sha\" wasn't supplied."})
            if not exist and body.get("sha"): return self.out(422, {"message": "sha given for missing file"})
            c = ST.commit_files(branch, {path: base64.b64decode(body["content"])}, body["message"])
            return self.out(201 if not exist else 200, {"content": {"sha": ST.files(branch)[path], "path": path}, "commit": {"sha": c}})
        if self.command == "DELETE":
            if path not in files: return self.out(404, {"message": "Not Found"})
            if body.get("sha") != files[path]: return self.out(409, {"message": "does not match"})
            return self.out(200, {"commit": {"sha": ST.commit_files(branch, {path: None}, body["message"])}})


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address): pass


class Ctl(BaseHTTPRequestHandler):
    """plain http: static files from repo root + /__ctl/*"""
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def send(self, status, data, ctype="application/json", extra=None):
        if not isinstance(data, bytes): data = json.dumps(data).encode()
        self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); b = json.loads(self.rfile.read(n) or b"{}")
        p = self.path
        with ST.lock:
            if p == "/__ctl/reset": ST.reset()
            elif p == "/__ctl/fault": ST.faults.append({"n": 1, **b})
            elif p == "/__ctl/clear_faults": ST.faults.clear()
            elif p == "/__ctl/put":   # simulate the worker / any writer: commit text files
                ch = {k: (v.encode() if isinstance(v, str) else None) for k, v in b["files"].items()}
                if b["branch"] not in ST.refs: ST.refs.pop(b["branch"], None)
                ST.commit_files(b["branch"], ch, b.get("message", "worker"))
            elif p == "/__ctl/delete_branch": ST.refs.pop(b["branch"], None)
            elif p == "/__ctl/orphan_inbox":
                ST.refs.pop("inbox", None); ST.commit_files("inbox", {".keep": b"inbox"}, "init")
            else: return self.send(404, {})
        self.send(200, {"ok": True})

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/__ctl/state":
            with ST.lock:
                st = {}
                for br in ST.refs:
                    st[br] = {}
                    for path, s in ST.files(br).items():
                        d = ST.blobs[s]
                        st[br][path] = d.decode() if path.endswith(".json") or path in ("README.md", ".keep") else {"bytes": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                return self.send(200, {"branches": st, "log": ST.log})
        fp = os.path.normpath(os.path.join(ROOT, p.lstrip("/")))
        if p.endswith("/"): fp = os.path.join(fp, "index.html")
        if not fp.startswith(ROOT) or not os.path.isfile(fp): return self.send(404, b"nf", "text/plain")
        ct = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if fp.endswith(".webmanifest"): ct = "application/manifest+json"
        self.send(200, open(fp, "rb").read(), ct, {"Cache-Control": "no-cache"})


def start(api_port, web_port, certdir):
    cert, key = os.path.join(certdir, "c.pem"), os.path.join(certdir, "k.pem")
    if not os.path.exists(cert):
        os.makedirs(certdir, exist_ok=True)
        subprocess.check_call(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "30",
                               "-subj", "/CN=api.github.com", "-addext", "subjectAltName=DNS:api.github.com"], stderr=subprocess.DEVNULL)
    api = QuietServer(("127.0.0.1", api_port), Api); api.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cert, key)
    api.socket = ctx.wrap_socket(api.socket, server_side=True)
    web = QuietServer(("127.0.0.1", web_port), Ctl); web.daemon_threads = True
    for s in (api, web): threading.Thread(target=s.serve_forever, daemon=True).start()
    return api, web


if __name__ == "__main__":
    start(8443, 8080, os.path.join(ROOT, "tests", ".certs")); print("api https://127.0.0.1:8443  web http://127.0.0.1:8080/web/"); time.sleep(10 ** 7)
