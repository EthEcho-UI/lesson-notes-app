"""The local UI: authentication (token, Host, Origin, Sec-Fetch-Site), CSP, websocket, and driving a recording through it."""
import base64
import http.client
import json
import os
import socket
import struct
import time

import pytest

from conftest import run_cli, wait_until
from rh import inbox_job, state

from lesson_record.ui import UIApp


@pytest.fixture
def ui(ctx, gh):
    app = UIApp(ctx.wcfg, ctx.rcfg, ctx.paths)
    app.start_thread()
    yield app
    app.shutdown()


class Client:
    def __init__(self, app, cookie=True):
        self.app, self.cookie = app, None
        if cookie:
            self.cookie = f"{app.cookie_name}={app.token}"   # what the single-use exchange hands out (tested separately)

    def req(self, method, path, body=None, headers=None, host=None, origin="default", ctype="application/json"):
        h = {"Host": f"127.0.0.1:{self.app.port}" if host is None else host}
        if self.cookie:
            h["Cookie"] = self.cookie
        if origin == "default":
            origin = f"http://127.0.0.1:{self.app.port}" if method == "POST" else None
        if origin:
            h["Origin"] = origin
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h["Content-Type"] = ctype
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        r.body = r.read()
        return r

    def json(self, method, path, body=None, **kw):
        r = self.req(method, path, body, **kw)
        return r.status, json.loads(r.body or b"{}")


# ------------------------------------------------------------------------------------------ authentication
def test_binds_to_loopback_only_on_a_free_port(ui):
    host, port = ui.server.server_address
    assert host == "127.0.0.1" and port > 1024 and ui.url.startswith(f"http://127.0.0.1:{port}/?t=")
    assert len(ui.token) >= 40


def test_no_token_is_403_everywhere(ui):
    c = Client(ui, cookie=False)
    for method, path in (("GET", "/"), ("GET", "/app.js"), ("GET", "/api/state"), ("GET", "/api/remote"), ("POST", "/api/stop"),
                         ("POST", "/api/start"), ("GET", "/ws")):
        r = c.req(method, path, {} if method == "POST" else None)
        assert r.status == 403, (method, path, r.status)
        assert b"token" not in r.body.lower() and ui.token.encode() not in r.body


def test_wrong_token_is_403(ui):
    c = Client(ui, cookie=False)
    assert c.req("GET", "/?t=" + "x" * 43).status == 403
    assert c.req("GET", "/api/state", headers={"X-LR-Token": "nope"}).status == 403
    assert c.req("GET", "/api/state", headers={"Cookie": f"lr_{ui.port}=nope"}).status == 403


def test_token_header_and_cookie_work(ui):
    c = Client(ui, cookie=False)
    assert c.req("GET", "/api/state", headers={"X-LR-Token": ui.token}).status == 200
    c2 = Client(ui)
    r = c2.req("GET", "/")
    assert r.status == 200
    set_cookie = Client(ui, cookie=False).req("GET", f"/?t={ui.launch_token}").getheader("Set-Cookie")
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie and "Path=/" in set_cookie


def test_launch_token_is_single_use_and_not_the_session_token(ui, tmp_path):
    """Security review: the opened URL (visible in argv/browser history) must stop working after the first exchange."""
    assert ui.launch_token != ui.token and ui.launch_token in ui.url and ui.token not in ui.url
    c = Client(ui, cookie=False)
    r = c.req("GET", f"/?t={ui.launch_token}")
    assert r.status == 303 and ui.token in r.getheader("Set-Cookie") and ui.launch_token not in r.getheader("Set-Cookie")
    assert c.req("GET", f"/?t={ui.launch_token}").status == 403        # second use of the link: dead
    assert c.req("GET", f"/?t={ui.token}").status == 403               # the session token is not a valid URL token
    assert Client(ui).req("GET", "/api/state").status == 200           # the cookie session keeps working


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1", "127.0.0.1.evil.example:%PORT%", "localhost", "[::1]:%PORT%", ""])
def test_wrong_host_is_403_dns_rebinding(ui, host):
    c = Client(ui)
    host = host.replace("%PORT%", str(ui.port))
    for path in ("/", "/api/state"):
        assert c.req("GET", path, host=host).status == 403
    assert c.req("GET", f"/?t={ui.token}", host=host).status == 403


def test_localhost_host_is_accepted(ui):
    c = Client(ui)
    assert c.req("GET", "/api/state", host=f"localhost:{ui.port}", origin=f"http://localhost:{ui.port}").status == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://evil.example", "null", "http://127.0.0.1", f"http://127.0.0.1:1",
                                    "http://localhost:%PORT%.evil.example"])
def test_wrong_origin_is_403(ui, origin):
    c = Client(ui)
    origin = origin.replace("%PORT%", str(ui.port))
    assert c.req("POST", "/api/submit", {}, origin=origin).status == 403
    assert c.req("GET", "/api/state", origin=origin).status == 403
    assert c.req("POST", "/api/stop", {}, origin=origin).status == 403


def test_post_without_origin_or_json_is_rejected(ui):
    c = Client(ui)
    assert c.req("POST", "/api/submit", {}, origin=None).status == 403
    assert c.req("POST", "/api/submit", b"{}", ctype="text/plain").status == 415
    assert c.req("POST", "/api/submit", b"{}", ctype="application/x-www-form-urlencoded").status == 415
    assert c.req("POST", "/api/submit", b"not json").status == 400
    assert c.req("POST", "/api/submit", b"[1]").status == 400
    assert c.req("POST", "/api/submit", b"x" * 20000).status == 413
    assert c.req("POST", "/api/submit", {}).status == 200


def test_cross_site_fetch_metadata_is_403(ui):
    c = Client(ui)
    assert c.req("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status == 403
    assert c.req("GET", "/api/state", headers={"Sec-Fetch-Site": "same-site"}).status == 403
    assert c.req("GET", "/api/state", headers={"Sec-Fetch-Site": "same-origin"}).status == 200


def test_unknown_paths_and_methods(ui):
    c = Client(ui)
    assert c.req("GET", "/etc/passwd").status == 404
    assert c.req("GET", "/../../etc/passwd").status == 404
    assert c.req("POST", "/api/nothing", {}).status == 404
    assert c.req("PUT", "/api/stop", {}).status in (501, 405)


# ------------------------------------------------------------------------------------------ CSP / static
def test_strict_csp_and_no_inline_or_external_assets(ui):
    c = Client(ui)
    for path in ("/", "/app.js", "/app.css", "/api/state"):
        r = c.req("GET", path)
        csp = r.getheader("Content-Security-Policy")
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "style-src 'self'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "*" not in csp
        assert "frame-ancestors 'none'" in csp and f"ws://127.0.0.1:{ui.port}" in csp
        assert r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("Cache-Control") == "no-store"
    html = c.req("GET", "/").body.decode()
    import re
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "style=" not in html
    assert not re.search(r"\son[a-z]+=", html)
    assert not re.search(r"(src|href)=\"(https?:)?//", html)
    js = c.req("GET", "/app.js").body.decode()
    assert "innerHTML" not in js and "eval(" not in js and "document.write" not in js and "http://" not in js.replace("ws://", "")


# ------------------------------------------------------------------------------------------ websocket
def ws_connect(app, headers):
    s = socket.create_connection(("127.0.0.1", app.port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET /ws HTTP/1.1", "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head, _, rest = buf.partition(b"\r\n\r\n")
    return s, head.decode(), rest


def ws_frame(s, rest=b""):
    def rd(n):
        nonlocal rest
        while len(rest) < n:
            rest += s.recv(4096)
        out, rest = rest[:n], rest[n:]
        return out
    b0, b1 = rd(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", rd(2))[0]
    return b0 & 0x0F, rd(n), rest


def test_websocket_requires_token_and_origin(ui):
    good = {"Host": f"127.0.0.1:{ui.port}", "Origin": f"http://127.0.0.1:{ui.port}"}
    cookie = Client(ui).cookie
    for hdrs in ({**good}, {**good, "Cookie": f"lr_{ui.port}=bad"}, {**good, "Origin": "https://evil.example", "Cookie": cookie},
                 {"Host": f"127.0.0.1:{ui.port}", "Cookie": cookie}, {**good, "Host": "evil.example", "Cookie": cookie}):
        s, head, _ = ws_connect(ui, hdrs)
        assert " 403 " in head.split("\r\n")[0], head
        s.close()
    s, head, rest = ws_connect(ui, {**good, "Cookie": cookie})
    assert " 101 " in head.split("\r\n")[0]
    op, payload, rest = ws_frame(s, rest)
    msg = json.loads(payload)
    assert op == 1 and msg["recorder"]["state"] == "idle" and "live" in msg
    s.close()


# ------------------------------------------------------------------------------------------ driving a recording
def test_record_through_the_ui_api(ui, ctx, fake_tone):
    c = Client(ui)
    code, rem = c.json("GET", "/api/remote?force=1")
    assert code == 200 and rem["remote"]["online"] and rem["remote"]["notebooks"][0]["name"] == "Biologie 2.A"
    nb = rem["remote"]["notebooks"][0]["id"]
    code, r = c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": nb, "title": "Z UI"})
    assert code == 200 and r["recorder"]["state"] == "recording", r
    code, r = c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": nb})
    assert code == 409 and "already recording" in r["error"]
    # live level over the websocket while recording
    s, head, rest = ws_connect(ui, {"Host": f"127.0.0.1:{ui.port}", "Origin": f"http://127.0.0.1:{ui.port}", "Cookie": c.cookie})
    seen = []
    t_end = time.time() + 3
    while time.time() < t_end and not seen:
        op, payload, rest = ws_frame(s, rest)
        m = json.loads(payload)
        if m.get("live") and m["live"]["peak_db"] > -40:
            seen.append(m)
    s.close()
    assert seen and seen[0]["recorder"]["state"] == "recording" and seen[0]["live"]["elapsed"] > 0
    code, r = c.json("POST", "/api/pause", {})
    assert code == 200 and r["recorder"]["state"] == "paused"
    code, r = c.json("POST", "/api/resume", {})
    assert code == 200 and r["recorder"]["state"] == "recording"
    time.sleep(1.5)
    code, r = c.json("POST", "/api/stop", {})
    assert code == 200 and r["last"]["ok"] and r["last"]["submit"]["status"] == "submitted"
    assert inbox_job(r["last"]["note_id"])["parts"]
    code, st = c.json("GET", "/api/state")
    assert st["recorder"]["state"] == "idle" and st["recorder"]["outbox"] == []
    code, rem = c.json("GET", "/api/remote?force=1")
    assert [n["title"] for n in rem["remote"]["notes"]] == ["Z UI"]
    code, r = c.json("POST", "/api/stop", {})
    assert code == 409


def test_ui_server_stopping_does_not_stop_the_recording(ctx, gh, fake_tone):
    app = UIApp(ctx.wcfg, ctx.rcfg, ctx.paths)
    app.start_thread()
    c = Client(app)
    nb = c.json("GET", "/api/remote?force=1")[1]["remote"]["notebooks"][0]["id"]
    assert c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": nb})[0] == 200
    app.shutdown()
    time.sleep(1.0)
    assert state(ctx.paths)["state"] == "recording"
    run_cli("stop")


def test_ui_command_picks_free_port_reuses_itself_and_leaves_recording_alone(ctx, gh, fake_tone):
    import re
    import signal
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-m", "lesson_record.cli", "ui", "--no-browser"], stdout=subprocess.PIPE, text=True)
    try:
        line = p.stdout.readline()
        m = re.search(r"http://127\.0\.0\.1:(\d+)/\?t=([\w-]+)", line)
        assert m, line
        port, token = int(m.group(1)), m.group(2)
        assert json.loads(ctx.paths.ui_file.read_text())["port"] == port and oct(ctx.paths.ui_file.stat().st_mode & 0o777) == "0o600"
        again = subprocess.run([sys.executable, "-m", "lesson_record.cli", "ui", "--no-browser"], capture_output=True, text=True, timeout=20)
        assert "already running" in again.stdout and token not in again.stdout       # the single-use launch link is never re-printed or stored
        app = type("A", (), {"port": port, "token": token, "cookie_name": f"lr_{port}"})
        r = Client(app, cookie=False).req("GET", f"/?t={token}")                    # first browser visit: exchange the link for the session cookie
        assert r.status == 303
        c = Client(app, cookie=False)
        c.cookie = r.getheader("Set-Cookie").split(";")[0]
        nb = c.json("GET", "/api/remote?force=1")[1]["remote"]["notebooks"][0]["id"]
        assert c.json("POST", "/api/start", {"kind": "lesson", "notebook_id": nb})[0] == 200
        p.send_signal(signal.SIGTERM)                                  # the UI goes away (terminal closed, Ctrl+C ...)
        p.wait(timeout=10)
        time.sleep(1.0)
        assert state(ctx.paths)["state"] == "recording"
        assert not ctx.paths.ui_file.exists()
    finally:
        if p.poll() is None:
            p.kill()
        if state(ctx.paths).get("state") == "recording":
            run_cli("stop")


def test_playwright_walkthrough_and_screenshots(ui, ctx, fake_tone):
    """Real clicks in headless Chromium (needs the app's tests/.venv with Playwright; skipped when absent)."""
    import subprocess
    import sys
    from pathlib import Path
    import fake_github as fg
    from conftest import NOTEBOOK_ID, jb
    py = Path(__file__).resolve().parents[2] / "tests" / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("Playwright venv (lesson-notes-app/tests/.venv) not present")
    note = {"schema_version": 1, "id": "01K8G0M1N2P3Q4R5S6T7V8W9XY", "kind": "meeting", "title": "Tydenni porada tymu", "notebook_id": NOTEBOOK_ID,
            "created_at": "2026-10-20T10:20:00Z", "updated_at": "2026-10-20T10:35:00Z", "source": "laptop", "language": "cs",
            "parts": [{"id": "p01", "order": 1, "filename": "01.opus", "bytes": 3780000, "sha256": "0" * 64, "duration": 1260.0,
                       "uploaded_at": "2026-10-20T10:35:00Z", "status": "uploaded"}]}
    status = {"schema_version": 1, "state": "done", "job_id": "01K8G0M1N2P3Q4R5S6T7V8W9XZ"}
    hb = {"schema_version": 1, "worker_id": "laptop", "state": "idle", "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
          "queue": {"jobs_pending": 0, "bytes_pending": 0, "oldest_job_created_at": None}}
    fg.ST.commit_files("main", {f"notes/{note['id']}/note.json": jb(note), f"notes/{note['id']}/status.json": jb(status),
                                "worker/heartbeat.json": jb(hb)}, "seed notes")
    out = Path(__file__).resolve().parents[1] / "docs"
    import os
    import pwd
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)               # HOME is isolated in this test; the browsers live in the real one
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(real_home / ".cache/ms-playwright"))
    r = subprocess.run([str(py), str(Path(__file__).parent / "shoot.py"), ui.url, str(out)], capture_output=True, text=True, timeout=180, env=env)
    assert r.returncode == 0 and "ui ok" in r.stdout, r.stdout + r.stderr


def test_idle_ui_server_closes_itself_but_not_while_recording(ctx, gh, fake_tone, monkeypatch):
    monkeypatch.setenv("LESSON_RECORD_UI_IDLE_MINUTES", "0.02")           # 1.2 s
    app = UIApp(ctx.wcfg, ctx.rcfg, ctx.paths)
    t = app.start_thread()
    c = Client(app)
    assert c.req("GET", "/api/state").status == 200
    t.join(timeout=8)
    assert not t.is_alive() and app.idle_exited.is_set()
    app.server.server_close()
    # while a recording runs the server stays up
    app2 = UIApp(ctx.wcfg, ctx.rcfg, ctx.paths)
    t2 = app2.start_thread()
    c2 = Client(app2)
    nb = c2.json("GET", "/api/remote?force=1")[1]["remote"]["notebooks"][0]["id"]
    assert c2.json("POST", "/api/start", {"kind": "lesson", "notebook_id": nb})[0] == 200
    time.sleep(4)
    assert t2.is_alive() and not app2.idle_exited.is_set()
    run_cli("stop")
    app2.shutdown()
