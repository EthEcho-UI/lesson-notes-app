"""Local web UI: `lesson-record ui`. Bound to 127.0.0.1 only, on a free port, with a random per-launch token.

Why the checks: ANY web page you visit can send requests to http://127.0.0.1:<port> (and DNS rebinding can make a hostile name
resolve to it). So every request, including the websocket upgrade, must pass ALL of:
  * Host header is exactly 127.0.0.1:<port> or localhost:<port>        (stops DNS rebinding)
  * Origin, when present, is http://127.0.0.1:<port> or http://localhost:<port>; POST and /ws REQUIRE it   (stops cross-site requests)
  * Sec-Fetch-Site, when present, is same-origin or none
  * the launch token: HttpOnly SameSite=Strict cookie (set when you open the printed URL ?t=<token>) or an X-LR-Token header
POST bodies must be application/json (a cross-origin page cannot send that without a preflight, which we never answer).
The UI is static files only (no external assets), strict CSP, no inline script or style. The recording itself runs in a separate
detached process: closing this server or the browser never stops it."""
import base64
import hashlib
import json
import logging
import os
import secrets
import select
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import session
from .common import read_json, write_json
from .recorder import lock_is_held
from .remote import Remote
from .submit import process_outbox

log = logging.getLogger("lesson_record.ui")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_BODY = 16 * 1024


class UIApp:
    def __init__(self, wcfg, rcfg, paths, port=0, token=None):
        self.wcfg, self.rcfg, self.paths = wcfg, rcfg, paths
        self.token = token or secrets.token_urlsafe(32)          # session token: lives in the HttpOnly cookie / X-LR-Token header only
        self.launch_token = secrets.token_urlsafe(32)            # in the opened URL only; single use, then dead (the URL shows up in argv/history)
        self._launch_lock = threading.Lock()
        self._launch_used = False
        self.remote = Remote(wcfg, paths)
        self.cookie_name = None
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.cookie_name = f"lr_{self.port}"
        self.hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self._stop = threading.Event()
        self.last_activity = time.monotonic()
        self.idle_exit_s = float(os.environ.get("LESSON_RECORD_UI_IDLE_MINUTES", "30")) * 60
        self.idle_exited = threading.Event()
        self._static = {p: (resources.files("lesson_record").joinpath("static", f).read_bytes(), ct) for p, (f, ct) in STATIC.items()}
        self.csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                    f"connect-src 'self' ws://127.0.0.1:{self.port} ws://localhost:{self.port}; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?t={self.launch_token}"

    def serve_forever(self):
        threading.Thread(target=self._background, daemon=True).start()
        self.server.serve_forever(poll_interval=0.3)

    def start_thread(self):
        t = threading.Thread(target=self.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self):
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()

    def _background(self):
        """Retries the hand-off of recordings made while offline and keeps the remote cache warm."""
        while not self._stop.wait(min(30, max(0.2, self.idle_exit_s / 2))):
            if time.monotonic() - self.last_activity > self.idle_exit_s and not lock_is_held(self.paths):
                log.info("no browser activity for %.0f min and nothing recording: closing the UI server", self.idle_exit_s / 60)
                self.idle_exited.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            try:
                if any(self.paths.outbox.glob("*.json")):
                    process_outbox(self.remote, self.paths)
            except Exception:
                log.exception("outbox retry")

    # ---- data for the page -----------------------------------------------------------------------------
    def recorder_state(self, recover=True):
        return session.status(self.wcfg, self.rcfg, self.paths, auto_recover=recover)

    def remote_view(self, force=False):
        c = self.remote.refresh(max_age=0 if force else 10)
        nb = {n["id"]: n["name"] for n in c.get("notebooks", [])}
        notes = []
        for n in c.get("notes", [])[:40]:
            notes.append({"id": n["id"], "title": n["title"], "kind": n["kind"], "notebook": nb.get(n["notebook_id"], "?"), "notebook_id": n["notebook_id"],
                          "created": (n["created_at"] or "")[:16].replace("T", " "), "parts": len(n["parts"]),
                          "seconds": sum(p.get("duration") or 0 for p in n["parts"]), "state": n["state"], "error": n.get("error")})
        hb = c.get("heartbeat")
        w = None
        if hb:
            try:
                age = (datetime.now(timezone.utc) - datetime.strptime(hb["last_seen"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)).total_seconds()
            except ValueError:
                age = 1e9
            w = {"state": hb.get("state"), "offline": age > 1800 or hb.get("state") == "offline",
                 "age": "just now" if age < 90 else f"{int(age // 60)} min ago" if age < 7200 else f"{int(age // 3600)} h ago",
                 "jobs_pending": (hb.get("queue") or {}).get("jobs_pending", 0)}
        return {"online": bool(c.get("online")), "error": c.get("error"),
                "notebooks": [{"id": n["id"], "name": n["name"]} for n in c.get("notebooks", []) if not n.get("archived")],
                "default_notebook_id": (c.get("settings") or {}).get("default_notebook_id"), "notes": notes, "worker": w}

    # ---- HTTP ----------------------------------------------------------------------------------------
    def _handler(app):
        class H(BaseHTTPRequestHandler):
            server_version = "lesson-record"
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            # -- plumbing
            def _send(self, status, body=b"", ctype="application/json", extra=None):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("Cross-Origin-Opener-Policy", "same-origin")
                self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                self.send_header("Content-Security-Policy", app.csp)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _deny(self, why):
                log.info("403 %s %s: %s", self.command, self.path.split("?")[0], why)
                self._send(403, {"ok": False, "error": "forbidden"}, extra={"Connection": "close"})
                self.close_connection = True

            def _token_ok(self):
                supplied = self.headers.get("X-LR-Token", "")
                if not supplied:
                    c = SimpleCookie()
                    try:
                        c.load(self.headers.get("Cookie", ""))
                    except Exception:
                        return False
                    m = c.get(app.cookie_name)
                    supplied = m.value if m else ""
                return bool(supplied) and secrets.compare_digest(supplied.encode(), app.token.encode())

            def _guard(self, need_origin):
                """True if the request may proceed; otherwise a 403 was already sent."""
                app.last_activity = time.monotonic()
                if self.headers.get("Host", "") not in app.hosts:
                    return self._deny("bad Host") or False
                origin = self.headers.get("Origin")
                if origin is not None and origin not in app.origins:
                    return self._deny("bad Origin") or False
                if need_origin and origin is None:
                    return self._deny("Origin required") or False
                if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                    return self._deny("cross-site fetch") or False
                if not self._token_ok():
                    return self._deny("missing/invalid token") or False
                return True

            # -- verbs
            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/" and "t" in parse_qs(u.query):
                    # first visit: exchange the URL token for the cookie, then drop it from the address bar
                    if self.headers.get("Host", "") not in app.hosts:
                        return self._deny("bad Host")
                    with app._launch_lock:
                        if app._launch_used or not secrets.compare_digest(parse_qs(u.query)["t"][0].encode(), app.launch_token.encode()):
                            return self._deny("bad or already used launch token")
                        app._launch_used = True
                    return self._send(303, b"", "text/plain", {"Location": "/", "Set-Cookie":
                                      f"{app.cookie_name}={app.token}; HttpOnly; SameSite=Strict; Path=/"})
                if u.path == "/ws":
                    return self._websocket()
                if not self._guard(need_origin=False):
                    return
                if u.path in app._static:
                    body, ct = app._static[u.path]
                    return self._send(200, body, ct)
                if u.path == "/api/state":
                    return self._send(200, {"ok": True, "recorder": app.recorder_state()})
                if u.path == "/api/remote":
                    return self._send(200, {"ok": True, "remote": app.remote_view(force="force" in parse_qs(u.query))})
                if u.path == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                self._send(404, {"ok": False, "error": "not found"})

            do_HEAD = do_GET

            def do_POST(self):
                u = urlparse(self.path)
                if not self._guard(need_origin=True):
                    return
                if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                    return self._send(415, {"ok": False, "error": "application/json required"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    return self._send(413, {"ok": False, "error": "body too large"})
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, {"ok": False, "error": "invalid JSON"})
                try:
                    out = app.action(u.path, body)
                except session.SessionError as e:
                    return self._send(409, {"ok": False, "error": str(e)})
                except KeyError:
                    return self._send(404, {"ok": False, "error": "not found"})
                except ValueError as e:
                    return self._send(400, {"ok": False, "error": str(e)})
                except Exception as e:
                    log.exception("action %s", u.path)
                    return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
                self._send(200, {"ok": True, **out})

            # -- websocket (RFC 6455, server side, text frames only)
            def _websocket(self):
                if not self._guard(need_origin=True):
                    return
                key = self.headers.get("Sec-WebSocket-Key", "")
                if self.headers.get("Upgrade", "").lower() != "websocket" or self.headers.get("Sec-WebSocket-Version") != "13" or not key:
                    return self._send(400, {"ok": False, "error": "websocket upgrade expected"})
                accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.close_connection = True
                sock = self.connection
                sock.settimeout(5)
                tick = 0
                try:
                    while not app._stop.is_set():
                        st = session.read_state(app.paths)
                        st["orphan"] = session.is_orphan(app.paths, st)
                        live = read_json(app.paths.live_file) if st.get("state") in ("recording", "paused") else None
                        msg = {"recorder": st, "live": live, "tick": tick % 4 == 0}
                        tick += 1
                        app.last_activity = time.monotonic()
                        self._ws_send(sock, json.dumps(msg).encode())
                        r, _, _ = select.select([sock], [], [], 0.25)
                        if r and not self._ws_read(sock):
                            break
                except (OSError, ValueError):
                    pass

            @staticmethod
            def _ws_send(sock, payload, opcode=0x1):
                n = len(payload)
                hdr = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else b"\x7e" + struct.pack(">H", n) if n < 65536 else b"\x7f" + struct.pack(">Q", n))
                sock.sendall(hdr + payload)

            def _ws_read(self, sock):
                """Handle one client frame. False = close the connection."""
                def rd(n):
                    b = b""
                    while len(b) < n:
                        c = sock.recv(n - len(b))
                        if not c:
                            raise OSError("closed")
                        b += c
                    return b
                b0, b1 = rd(2)
                opcode, masked, n = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", rd(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", rd(8))[0]
                if not masked or n > 65536:                       # clients must mask; we never expect big frames
                    return False
                mask = rd(4)
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(rd(n)))
                if opcode == 0x8:
                    return False
                if opcode == 0x9:
                    self._ws_send(sock, data, 0xA)
                return True
        return H

    # ---- POST actions ----------------------------------------------------------------------------------
    def action(self, path, body):
        if path == "/api/start":
            for k, mx in (("title", 200), ("notebook_id", 64), ("note_id", 64), ("kind", 16)):
                if body.get(k) is not None and (not isinstance(body[k], str) or len(body[k]) > mx):
                    raise ValueError(f"bad {k}")
            st = session.start(self.wcfg, self.rcfg, self.paths, kind=body.get("kind") or "lesson", notebook=body.get("notebook_id") or None,
                               title=body.get("title") or None, note=body.get("note_id") or None, remote=self.remote,
                               diarize=body.get("diarize") if isinstance(body.get("diarize"), bool) else None)
            return {"recorder": st}
        if path == "/api/stop":
            return {"last": session.stop(self.wcfg, self.rcfg, self.paths)}
        if path == "/api/pause":
            return {"recorder": session.pause(self.paths)}
        if path == "/api/resume":
            return {"recorder": session.resume(self.paths)}
        if path == "/api/submit":
            return {"results": process_outbox(self.remote, self.paths)}
        raise KeyError(path)


# ---------------------------------------------------------------------------------------------------
def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def serve(wcfg, rcfg, paths, port=0, open_browser=True):
    paths.ensure()
    old = read_json(paths.ui_file)
    if old and isinstance(old.get("pid"), int) and _pid_alive(old["pid"]) and old.get("url"):
        # the launch link was single-use and is gone; the browser that used it already holds the session cookie
        print(f"UI already running (pid {old['pid']}): {old['url']}\n"
              f"Open it in the browser you used before. If it answers 403, stop it (kill {old['pid']}) and run `lesson-record ui` again.", flush=True)
        if open_browser:
            webbrowser.open(old["url"])
        return 0
    app = UIApp(wcfg, rcfg, paths, port)
    write_json(paths.ui_file, {"pid": os.getpid(), "port": app.port, "url": f"http://127.0.0.1:{app.port}/"})  # no token on disk
    print(f"lesson-record UI: {app.url}", flush=True)
    print("Bound to 127.0.0.1 only; the link contains a single-use launch token (valid for the first browser visit only). Ctrl+C closes the UI - a running recording keeps going.")
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=app.server.shutdown, daemon=True).start())
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(app.url)).start()
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            paths.ui_file.unlink()
        except OSError:
            pass
        app.server.server_close()
    return 0


def stop_running(paths):
    old = read_json(paths.ui_file)
    if old and isinstance(old.get("pid"), int) and _pid_alive(old["pid"]):
        os.kill(old["pid"], signal.SIGTERM)
        print("UI server stopped (a running recording is not affected)")
        return 0
    print("no UI server running")
    return 0
