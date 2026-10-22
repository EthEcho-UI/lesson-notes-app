#!/usr/bin/env python3
"""End-to-end tests: Chromium (Playwright) + fake GitHub API. Run: tests/.venv/bin/python tests/run_tests.py [name-filter]
Serial on purpose (one browser, one context at a time)."""
import json, os, pathlib, re, subprocess, sys, time, traceback, urllib.request, copy, io, random, zipfile, shutil, base64, hashlib
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw
from jsonschema import Draft202012Validator
from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(__file__))
import fake_github as fg

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CONTRACT = pathlib.Path(os.environ.get("CONTRACT_DIR", REPO.parent / "contract"))
SHOTS = pathlib.Path(os.environ.get("SHOTS_DIR", REPO.parent / "b8-screens"))
API_PORT, WEB_PORT = 18443, 18080
BASE = f"http://127.0.0.1:{WEB_PORT}/web/"
TOKEN = fg.TOKEN

# ---- contract validation (schemas + semantic rules from contract/validate.py) -------------------------
sys.path.insert(0, str(CONTRACT))
_ns = {"__file__": str(CONTRACT / "validate.py")}
exec(open(CONTRACT / "validate.py").read().split("bad = 0")[0], _ns)
def contract_errors(kind, d):
    errs = [f"{'/'.join(map(str, x.absolute_path))}: {x.message[:100]}" for x in Draft202012Validator(_ns["SCHEMAS"][kind]).iter_errors(d)]
    return errs or _ns["semantic"](kind, d)

# ---- helpers -------------------------------------------------------------------------------------------
def ctl(path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{WEB_PORT}/__ctl/{path}", data=json.dumps(body or {}).encode(), method="POST")
    return json.load(urllib.request.urlopen(req))
def state():
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/__ctl/state"))
def wait_for(fn, timeout=20, msg="condition"):
    end = time.time() + timeout; last = None
    while time.time() < end:
        try:
            last = fn()
            if last: return last
        except Exception as e: last = e
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {msg} (last={last!r})")
def main_files(): return state()["branches"]["main"]
def inbox_files(): return state()["branches"].get("inbox", {})
def note_ids(): return sorted({m.group(1) for p in main_files() if (m := re.match(r"notes/([^/]+)/note\.json", p))})
def audio(name, size=3000, seed=1):
    return {"name": name, "mimeType": "audio/ogg", "buffer": bytes((i * seed) % 251 for i in range(size))}

class Ctx:
    def __init__(self, pw, offline=False, width=412, height=900):
        self.browser = pw.chromium.launch(args=[f"--host-resolver-rules=MAP api.github.com 127.0.0.1:{API_PORT}", "--ignore-certificate-errors",
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        self.context = self.browser.new_context(viewport={"width": width, "height": height}, permissions=["microphone", "clipboard-read", "clipboard-write"], accept_downloads=True)
        self.context.add_init_script("try{localStorage.setItem('lessonnotes.backoff','0.01')}catch(e){}")
        self.problems = []
        self.page = self.new_page()
    def new_page(self):
        p = self.context.new_page()
        p.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        p.on("console", lambda m: self.problems.append(f"console: {m.text}") if "Content Security Policy" in m.text or "Refused" in m.text else None)
        return p
    def js(self, expr, arg=None):
        return self.page.evaluate(expr, arg) if arg is not None else self.page.evaluate(expr)
    def close(self): self.browser.close()

def login(c, token=TOKEN, plain=True, passphrase="correct horse battery"):
    """plain=True uses the explicit 'no passphrase' mode so tests can reload freely; test_vault covers the locked default."""
    p = c.page
    p.goto(BASE + "#/settings")
    p.fill("#tokenIn", token)
    if plain:
        p.check("#plainChk"); p.click("#saveTokenBtn"); p.click("dialog .btn.danger")
    else:
        p.fill("#passIn", passphrase); p.fill("#passIn2", passphrase); p.click("#saveTokenBtn")
    wait_for(lambda: "Uloženo" in p.inner_text("#connMsg"), 8, "token saved")

SCAN_JS = """async (needle) => {
  const hits = [];
  const has = (v) => { try { return JSON.stringify(v).includes(needle); } catch (e) { return false; } };
  try { for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k.includes(needle) || (localStorage.getItem(k) || '').includes(needle)) hits.push('localStorage:' + k); } } catch (e) {}
  try { for (let i = 0; i < sessionStorage.length; i++) { const k = sessionStorage.key(i); if ((sessionStorage.getItem(k) || '').includes(needle)) hits.push('sessionStorage:' + k); } } catch (e) {}
  for (const d of await indexedDB.databases()) {
    const db = await new Promise((res, rej) => { const q = indexedDB.open(d.name); q.onsuccess = () => res(q.result); q.onerror = () => rej(q.error); });
    for (const name of db.objectStoreNames) {
      const rows = await new Promise((res) => { const g = db.transaction(name).objectStore(name).getAll(); g.onsuccess = () => res(g.result); g.onerror = () => res([]); });
      for (const r of rows) {
        if (has(r)) hits.push('idb:' + d.name + '/' + name);
        if (r && r.blob instanceof Blob && r.blob.size < 2e6 && (await r.blob.text()).includes(needle)) hits.push('idb-blob:' + name);
      }
    }
    db.close();
  }
  for (const k of await caches.keys()) { const c = await caches.open(k); for (const req of await c.keys()) { if (req.url.includes(needle)) hits.push('cache-url:' + k); const t = await (await c.match(req)).text(); if (t.includes(needle)) hits.push('cache:' + k + ':' + req.url); } }
  return hits;
}"""
def scan_token(c, needle=TOKEN): return c.page.evaluate(SCAN_JS, needle)

def create_notebook(c, name):
    p = c.page; p.goto(BASE + "#/")
    p.click("text=Nový sešit"); p.fill("dialog input[type=text]", name); p.press("dialog input[type=text]", "Enter")
    wait_for(lambda: name in p.inner_text("main"), 5, "notebook visible")

def new_note(c, title, files, kind="lesson", submit=True):
    p = c.page; p.goto(BASE + "#/new")
    p.fill("#titleIn", title)
    if kind == "meeting": p.click("#kind-meeting")
    p.set_input_files("#fileIn", files)
    wait_for(lambda: p.locator(".plist li").count() == len(files), 5, "parts listed")
    if submit: p.click("#submitBtn")

def sim_worker(note_id, state_name="done", job_id=None, speakers=False, progress=None, extra_status=None):
    """Write what the laptop worker would write, based on the note/job on the fake server."""
    mf = main_files(); note = json.loads(mf[f"notes/{note_id}/note.json"])
    job = json.loads(inbox_files().get(f"jobs/{note_id}/job.json", "null") or "null")
    job_id = job_id or (job and job["job_id"]) or "01K8F4A9M3N5P7Q9R1S3T5V7W9"
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st = {"schema_version": 1, "note_id": note_id, "job_id": job_id, "action": "transcribe", "state": state_name, "queue_position": None, "queued_at": now,
          "started_at": now, "finished_at": now if state_name in ("done", "failed") else None, "updated_at": now, "worker_id": "fake-laptop", "attempt": 1, "max_attempts": 3,
          "audio": {"state": "purged" if state_name == "done" else "on_inbox", "purge_after": None}, "completed_job_ids": [job_id] if state_name == "done" else []}
    if state_name == "queued": st["started_at"] = None
    if state_name in ("transcribing", "diarizing"): st["progress"] = progress or {"part_index": 1, "part_total": len(note["parts"]), "part_id": "p01", "percent": 40.0}
    if state_name == "failed": st["error"] = {"code": "whisper_oom", "message": "out of memory", "retryable": True, "stage": "transcribe", "failed_at": now}; st["audio"]["purge_after"] = now
    st.update(extra_status or {})
    files = {f"notes/{note_id}/status.json": json.dumps(st, indent=2) + "\n"}
    if state_name == "done":
        off, parts, texts = 0.0, [], []
        for p in note["parts"]:
            dur = 60.0 * p["order"]
            segs = [{"start": 0.0, "end": 6.0, "text": "Dobrý den, dnes si povíme o fotosyntéze."}, {"start": 6.0, "end": 20.0, "text": f"Toto je část {p['order']} přednášky o chloroplastech."}]
            if speakers: segs = [dict(s, speaker="SPEAKER_00" if i % 2 == 0 else "SPEAKER_01") for i, s in enumerate(segs)]
            parts.append({"part_id": p["id"], "order": p["order"], "duration": dur, "offset": off, "job_id": job_id, "transcribed_at": now, "segments": segs})
            texts.append(f"[Část {p['order']}]\n" + " ".join(s["text"] for s in segs)); off += dur
        tr = {"schema_version": 1, "note_id": note_id, "language": "cs", "engine": {"name": "fake", "model": "x"}, "diarization": {"enabled": speakers, **({"engine": "x", "speaker_scope": "per_part"} if speakers else {})},
              "updated_at": now, "parts": parts, "merged": {"duration": off, "text": "\n".join(texts)}}
        content = {"title": "Fotosyntéza: světelná fáze", "summary": "Hodina vysvětlila fotosyntézu v chloroplastech.", "key_points": ["Probíhá v chloroplastech", "Potřebuje světlo"],
                   "definitions": [{"term": "Fotosyntéza", "definition": "Přeměna světla na chemickou energii."}], "todo": ["Referát o Calvinově cyklu"], "questions_open": []}
        if note["kind"] == "meeting": content.update(decisions=["Nasadit ve čtvrtek"], action_items=[{"task": "Připravit poznámky", "owner": "Petra"}])
        nt = {"schema_version": 1, "note_id": note_id, "kind": note["kind"], "job_id": job_id, "generated_at": now, "model": "fake", "based_on_parts": [p["id"] for p in note["parts"]], "content": content}
        for k, d in (("transcript", tr), ("notes", nt), ("status", st)):
            e = contract_errors(k, d); assert not e, (k, e)
        files[f"notes/{note_id}/transcript.json"] = json.dumps(tr, indent=2) + "\n"; files[f"notes/{note_id}/notes.json"] = json.dumps(nt, indent=2) + "\n"
    else:
        e = contract_errors("status", st); assert not e, e
    ctl("put", {"branch": "main", "files": files, "message": f"note {note_id}: status {state_name}"})

def heartbeat(offline_minutes=0, jobs=0):
    t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - offline_minutes * 60))
    d = {"schema_version": 1, "worker_id": "fake-laptop", "version": "0", "state": "idle", "started_at": t, "last_seen": t, "queue": {"jobs_pending": jobs, "bytes_pending": 0}}
    assert not contract_errors("worker", d)
    ctl("put", {"branch": "main", "files": {"worker/heartbeat.json": json.dumps(d)}})

def sync_now(c):
    c.page.goto(BASE + "#/settings"); time.sleep(1.5)   # let any in-flight refresh finish, otherwise the click coalesces with it
    c.page.click("#syncBtn"); time.sleep(1.0)

def check_upload_contract(note_id, n_parts):
    note = json.loads(main_files()[f"notes/{note_id}/note.json"])
    e = contract_errors("note", note); assert not e, e
    ib = inbox_files()
    job = json.loads(ib[f"jobs/{note_id}/job.json"]); e = contract_errors("job", job); assert not e, e
    for p in job["parts"]:
        f = ib[f"jobs/{note_id}/{p['file']}"]
        assert f["bytes"] == p["bytes"] and f["sha256"] == p["sha256"], ("sha/bytes mismatch", p, f)
    return note, job

# ---- tests ---------------------------------------------------------------------------------------------------
def test_flow_two_files(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c)
        p.click("#testBtn"); wait_for(lambda: "Připojeno" in p.inner_text("#connMsg"), 8, "connection test")
        assert "zápis je povolen" in p.inner_text("#connMsg")
        create_notebook(c, "Biologie 2.A")
        new_note(c, "Fotosyntéza hodina 14", [audio("b.ogg", 4000, 3), audio("a.ogg", 3000, 5)])
        # reorder: move first part down so a.ogg becomes part 1? (submitted order is what counts)
        nid = wait_for(lambda: note_ids(), 15, "note created")[0]
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 20, "job.json on inbox")
        note, job = check_upload_contract(nid, 2)
        assert [x["order"] for x in job["parts"]] == [1, 2] and job["append"] is False and job["action"] == "transcribe"
        assert "jobs/%s/parts/01.ogg" % nid in inbox_files() and "jobs/%s/parts/02.ogg" % nid in inbox_files()
        wait_for(lambda: all(x["status"] == "uploaded" for x in json.loads(main_files()[f"notes/{nid}/note.json"])["parts"]), 10, "parts marked uploaded")
        log = [l for l in state()["log"] if l[0] == "PUT"]
        idx = lambda s: next(i for i, l in enumerate(log) if s in l[1])
        assert idx("parts/01") < idx("parts/02") < idx("job.json"), "job.json must be committed last"
        # no personal data in paths/commit paths
        assert not any("Foto" in k for k in list(main_files()) + list(inbox_files()))
        # waiting for laptop
        heartbeat(offline_minutes=90, jobs=1)
        p.goto(BASE + f"#/n/{nid}"); sync_now(c); p.goto(BASE + f"#/n/{nid}")
        wait_for(lambda: "čeká na notebook" in p.inner_text("#statusCard"), 10, "waiting state")
        assert "laptop" in p.inner_text("#statusCard").lower() or "notebook" in p.inner_text("#statusCard").lower()
        # worker: transcribing (polled every 5 s for the open note), then done
        sim_worker(nid, "queued"); sim_worker(nid, "transcribing", progress={"part_index": 1, "part_total": 2, "part_id": "p01", "percent": 40.0})
        wait_for(lambda: "Přepisuji" in p.inner_text("#statusCard") and "1/2" in p.inner_text("#statusCard"), 15, "transcribing 1/2")
        sim_worker(nid, "diarizing"); wait_for(lambda: "Rozpoznávám" in p.inner_text("#statusCard"), 15, "diarizing")
        sim_worker(nid, "done", speakers=True)
        wait_for(lambda: "Hotovo" in p.inner_text("#statusCard"), 15, "done")
        wait_for(lambda: "Hodina vysvětlila" in p.inner_text(".tabpanel"), 10, "summary rendered")
        assert "Referát o Calvinově cyklu" in p.inner_text(".tabpanel")
        p.click("#tab-transcript"); assert "Dobrý den" in p.inner_text(".tabpanel") and "Část 2" in p.inner_text(".tabpanel")
        # rename speaker -> speakers.json on main
        p.locator(".spk").first.click(); p.fill("dialog input[type=text]", "Učitel"); p.check("dialog input[type=checkbox]"); p.press("dialog input[type=text]", "Enter")
        wait_for(lambda: f"notes/{nid}/speakers.json" in main_files(), 10, "speakers.json written")
        sp = json.loads(main_files()[f"notes/{nid}/speakers.json"]); assert sp["names"]["p01:SPEAKER_00"] == "Učitel" and sp["names"]["p02:SPEAKER_00"] == "Učitel"
        assert "Učitel" in p.inner_text(".tabpanel")
        # audio tab, search (diacritics-insensitive), markdown copy/export
        p.click("#tab-audio"); assert "01" in p.inner_text(".tabpanel") or "1" in p.inner_text(".tabpanel")
        p.goto(BASE + "#/search"); p.fill("#searchIn", "fotosynteze chloroplast"); wait_for(lambda: "Fotosyntéza hodina 14" in p.inner_text("main"), 5, "search hit")
        p.goto(BASE + f"#/n/{nid}"); p.click("#exportBtn"); p.check("#exTr"); p.click("#exCopy")
        md = wait_for(lambda: p.evaluate("navigator.clipboard.readText()"), 5, "clipboard")
        assert md.startswith("# Fotosyntéza: světelná fáze") and "Fotosyntéza hodina 14" in md and "## Shrnutí" in md and "Učitel" in md and "## Přepis" in md, md[:300]
        with p.expect_download() as dl: p.click("#exMd")
        assert dl.value.suggested_filename.endswith(".md")
        p.click("#exClose")
        # regenerate (asks for confirmation because it costs AI quota)
        p.click("#regenBtn"); p.click("dialog .btn.danger"); wait_for(lambda: json.loads(inbox_files()[f"jobs/{nid}/job.json"])["action"] == "regenerate_notes", 10, "regenerate job")
        j = json.loads(inbox_files()[f"jobs/{nid}/job.json"]); e = contract_errors("job", j); assert not e, e
        assert not c.problems, c.problems
    finally: c.close()

def test_add_part_and_delete(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Dějepis")
        new_note(c, "Revoluce", [audio("1.mp3", 2500)], kind="meeting")
        nid = wait_for(lambda: note_ids(), 15, "note")[0]
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 15, "job")
        sim_worker(nid, "done")
        # worker cleans the inbox job after done (single commit removing job dir)
        ctl("put", {"branch": "inbox", "files": {f"jobs/{nid}/job.json": None, f"jobs/{nid}/parts/01.mp3": None}})
        p.goto(BASE + f"#/n/{nid}"); sync_now(c); p.goto(BASE + f"#/n/{nid}")
        wait_for(lambda: "Hotovo" in p.inner_text("#statusCard"), 10, "done")
        assert "Nasadit ve čtvrtek" in p.inner_text(".tabpanel"), "meeting decisions shown"
        p.click("#addPartsBtn"); p.set_input_files("#fileIn", [audio("2.mp3", 2000, 7)]); wait_for(lambda: p.locator(".plist li").count() == 1, 5, "part")
        p.click("#submitBtn")
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 15, "append job")
        note, job = check_upload_contract(nid, 1)
        assert job["append"] is True and [x["order"] for x in job["parts"]] == [2] and job["parts"][0]["file"] == "parts/02.mp3", job
        assert [x["id"] for x in note["parts"]] == ["p01", "p02"]
        # while the new job is unprocessed, adding more parts is blocked
        sync_now(c); p.goto(BASE + f"#/n/{nid}")
        wait_for(lambda: p.locator("#addPartsBtn").is_disabled(), 10, "add blocked while job pending")
        assert "aktualizuje" in p.inner_text("#statusCard") or "čeká" in p.inner_text("#statusCard")
        # delete note
        p.click("#deleteBtn"); p.click("dialog .btn.danger")
        wait_for(lambda: not any(k.startswith(f"notes/{nid}/") for k in main_files()), 10, "note removed from main")
        wait_for(lambda: not any(k.startswith(f"jobs/{nid}/") for k in inbox_files()), 10, "job removed from inbox")
        assert not c.problems, c.problems
    finally: c.close()

def test_failed_retry(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Fyzika"); new_note(c, "Optika", [audio("x.ogg")])
        nid = wait_for(lambda: note_ids(), 15, "note")[0]; wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 15, "job")
        first = json.loads(inbox_files()[f"jobs/{nid}/job.json"])["job_id"]
        sim_worker(nid, "failed"); p.goto(BASE + f"#/n/{nid}"); sync_now(c); p.goto(BASE + f"#/n/{nid}")
        wait_for(lambda: p.locator("#retryBtn").count() == 1, 10, "retry button")
        assert "whisper_oom" in p.inner_text("#statusCard")
        p.click("#retryBtn")
        wait_for(lambda: json.loads(inbox_files()[f"jobs/{nid}/job.json"])["job_id"] != first, 10, "new job_id committed")
        j = json.loads(inbox_files()[f"jobs/{nid}/job.json"]); assert not contract_errors("job", j) and len(j["parts"]) == 1
    finally: c.close()

def test_offline_queue(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Chemie"); sync_now(c)
        c.context.set_offline(True)
        p.goto(BASE + "#/"); time.sleep(0.5)
        new_note(c, "Soli offline", [audio("o.ogg", 2000, 9)])
        wait_for(lambda: "Čeká na připojení" in p.inner_text("#statusCard"), 10, "queued offline")
        time.sleep(1.5)
        assert not note_ids() and not inbox_files().get("jobs") and not any(k.startswith("jobs/") for k in inbox_files()), "nothing may reach the server while offline"
        assert "offline" in p.inner_text("#statusCard").lower()
        # cached data readable offline after reload (shell from SW)
        wait_for(lambda: p.evaluate("navigator.serviceWorker.getRegistration().then(r=>!!(r&&r.active))"), 10, "sw active")
        p.reload(); wait_for(lambda: "Soli offline" in p.inner_text("main"), 10, "note visible offline after reload")
        c.context.set_offline(False)
        wait_for(lambda: note_ids() and any(k.endswith("job.json") for k in inbox_files()), 25, "queued upload completes when back online")
        nid = note_ids()[0]; check_upload_contract(nid, 1)
        assert not [x for x in c.problems if "pageerror" in x], c.problems
    finally: c.close()

def test_resume_interrupted(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Mat")
        ctl("fault", {"method": "PUT", "match": "parts/02", "drop": True, "n": 500})
        new_note(c, "Přerušeno", [audio("1.ogg", 5000, 3), audio("2.ogg", 6000, 5)])
        nid = wait_for(lambda: note_ids(), 15, "note")[0]
        wait_for(lambda: f"jobs/{nid}/parts/01.ogg" in inbox_files(), 15, "part 1 uploaded")
        wait_for(lambda: p.locator("#resumeBtn").count() == 1, 25, "error state with resume button")
        assert f"jobs/{nid}/parts/02.ogg" not in inbox_files() and f"jobs/{nid}/job.json" not in inbox_files()
        ctl("clear_faults")
        p.reload()    # "tab closed and reopened": the queue continues on its own
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 30, "resumed upload finished")
        check_upload_contract(nid, 2)
        puts01 = [l for l in state()["log"] if l[0] == "PUT" and "parts/01" in l[1]]
        assert len(puts01) == 1, f"part 1 must not be re-uploaded, got {len(puts01)} PUTs"
    finally: c.close()

def test_retries_401_409(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Zeměpis")
        ctl("fault", {"method": "PUT", "match": "parts/01", "status": 401, "n": 2})
        ctl("fault", {"method": "PUT", "match": "note.json", "status": 409, "n": 2})
        ctl("fault", {"method": "GET", "match": "contents/jobs", "status": 502, "n": 1})
        new_note(c, "Retry", [audio("r.ogg", 3000, 11)])
        nid = wait_for(lambda: note_ids(), 20, "note despite 409s")[0]
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 30, "job despite spurious 401")
        check_upload_contract(nid, 1)
        wait_for(lambda: json.loads(main_files()[f"notes/{nid}/note.json"])["parts"][0]["status"] == "uploaded", 10, "uploaded flag")
        # persistent 401 -> clear message (let the post-upload refresh finish first so the click starts a new one)
        time.sleep(2.5)
        ctl("fault", {"method": "GET", "match": "git/trees/main", "status": 401, "n": 100})
        sync_now(c); p.goto(BASE + "#/")
        wait_for(lambda: "Token nebyl přijat" in p.inner_text("main"), 20, "auth banner")
    finally: c.close()

def test_recording_and_crash_recovery(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Rec")
        p.goto(BASE + "#/new"); p.fill("#titleIn", "Nahrávka")
        p.click("#recStart"); wait_for(lambda: p.locator("#recStop").count() == 1, 5, "recording UI")
        assert "zamknutí" in p.inner_text("main")
        wait_for(lambda: p.evaluate("""() => new Promise(r => { const q = indexedDB.open('lessonnotes'); q.onsuccess = () => { const t = q.result.transaction('chunks'); const g = t.objectStore('chunks').count(); g.onsuccess = () => r(g.result >= 1); }; })"""), 12, "chunks persisted to IndexedDB while recording")
        p.click("#recPause"); assert p.locator("#recResume").count() == 1; p.click("#recResume"); time.sleep(1.2)
        p.click("#recStop"); wait_for(lambda: p.locator(".plist li").count() == 1, 8, "recorded part listed")
        p.click("#submitBtn")
        nid = wait_for(lambda: note_ids(), 15, "note")[0]; wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 20, "uploaded")
        note, job = check_upload_contract(nid, 1); assert job["parts"][0]["file"].endswith(".webm") and job["parts"][0]["bytes"] > 500, job
        # crash: start recording, kill the tab, reopen -> recording recovered
        p.goto(BASE + "#/new"); p.click("#recStart")
        wait_for(lambda: p.evaluate("""() => new Promise(r => { const q = indexedDB.open('lessonnotes'); q.onsuccess = () => { const g = q.result.transaction('chunks').objectStore('chunks').count(); g.onsuccess = () => r(g.result >= 1); }; })"""), 12, "chunk saved")
        p.close(); p2 = c.new_page(); c.page = p2
        p2.goto(BASE + "#/new")
        wait_for(lambda: "Neuložené nahrávky" in p2.inner_text("main"), 10, "recovered recording offered")
        p2.click("text=Přidat >> nth=-1") if False else p2.locator(".tray .btn", has_text="Přidat").first.click()
        wait_for(lambda: p2.locator(".plist li").count() == 1, 5, "recovered part in draft")
    finally: c.close()

def test_sw_isolation_and_shell(pw):
    c = Ctx(pw); p = c.page
    try:
        p.goto(BASE); wait_for(lambda: p.evaluate("navigator.serviceWorker.getRegistration().then(r=>!!(r&&r.active))"), 15, "sw active")
        p.evaluate("navigator.serviceWorker.getRegistration().then(r=>r.unregister())")
        p.evaluate("""async () => { for (const n of ['otherapp-shell-v1','docket-v3','lessonnotes-shell-OLD']) { const c = await caches.open(n); await c.put('/x', new Response('keep')); }
            localStorage.setItem('other.key','1'); await new Promise(r => { const q = indexedDB.open('otherapp'); q.onsuccess = () => { q.result.close(); r(); }; }); }""")
        p.reload(); wait_for(lambda: p.evaluate("navigator.serviceWorker.getRegistration().then(r=>!!(r&&r.active))"), 15, "sw reactivated")
        names = wait_for(lambda: (lambda n: n if any(x.startswith("lessonnotes-shell-") and x != "lessonnotes-shell-OLD" for x in n) else None)(p.evaluate("caches.keys()")), 15, "own cache created")
        assert "otherapp-shell-v1" in names and "docket-v3" in names, f"foreign caches must survive: {names}"
        assert "lessonnotes-shell-OLD" not in names, "own stale cache must be removed"
        assert p.evaluate("localStorage.getItem('other.key')") == "1"
        assert p.evaluate("indexedDB.databases().then(d=>d.map(x=>x.name))").count("otherapp") == 1
        # precache list == git ls-files, all cached
        n = p.evaluate("""async () => { const k = (await caches.keys()).find(x => x.startsWith('lessonnotes-shell-') && x !== 'lessonnotes-shell-OLD'); return (await (await caches.open(k)).keys()).length; }""")
        want = len(subprocess.check_output(["git", "ls-files", "web"], cwd=REPO, text=True).split()) - 2 + 1  # minus sw.js, precache.js; plus './'
        assert n == want, f"cache has {n} entries, expected {want}"
        # offline shell
        c.context.set_offline(True); p.reload(); wait_for(lambda: p.locator("#nav a").count() == 4, 8, "shell loads offline"); c.context.set_offline(False)
        assert not c.problems, c.problems
    finally: c.close()

def test_vault_locked_token(pw):
    # the real token is long and random-looking; use a distinctive one so a scan cannot false-positive
    secret = "github_pat_TESTSECRET_0123456789abcdefABCDEF"
    ctl("reset")
    fg.TOKEN_OVERRIDE = secret
    c = Ctx(pw); p = c.page
    try:
        # sanity: the scanner really finds a plainly stored token
        login(c, token=secret, plain=True)
        assert scan_token(c, secret), "scanner must find the token in plain mode (otherwise the locked-mode check is meaningless)"
        p.click("#rmTokenBtn"); time.sleep(0.5)
        assert not scan_token(c, secret), "removing the token must erase it"
        # locked (default): nothing readable at rest
        p.fill("#tokenIn", secret); p.fill("#passIn", "short"); p.fill("#passIn2", "short"); p.click("#saveTokenBtn")
        wait_for(lambda: "8 znaků" in p.inner_text("#connMsg"), 5, "short passphrase rejected")
        p.fill("#passIn", "correct horse battery"); p.fill("#passIn2", "other thing entirely"); p.click("#saveTokenBtn")
        wait_for(lambda: "neshodují" in p.inner_text("#connMsg"), 5, "mismatch rejected")
        p.fill("#passIn", "correct horse battery"); p.fill("#passIn2", "correct horse battery"); p.click("#saveTokenBtn")
        wait_for(lambda: "zamčeno" in p.inner_text("#connMsg"), 15, "saved locked")
        assert scan_token(c, secret) == [], scan_token(c, secret)
        vault = p.evaluate("""() => new Promise(r => { const q = indexedDB.open('lessonnotes'); q.onsuccess = () => { const g = q.result.transaction('kv').objectStore('kv').get('vault'); g.onsuccess = () => r(g.result.v); }; })""")
        assert vault["mode"] == "locked" and vault["kdf"] == "PBKDF2-SHA256" and vault["iter"] >= 600000 and {"salt", "iv", "ct"} <= set(vault) and "token" not in vault, vault
        cfg = p.evaluate("""() => new Promise(r => { const q = indexedDB.open('lessonnotes'); q.onsuccess = () => { const g = q.result.transaction('kv').objectStore('kv').get('cfg'); g.onsuccess = () => r(g.result.v); }; })""")
        assert "token" not in cfg, cfg
        create_notebook(c, "Zamčeno")
        # reload -> unlock screen, nothing works before unlocking
        p.reload(); wait_for(lambda: p.locator("#unlockIn").count() == 1, 10, "unlock screen")
        assert "Zamčeno" not in p.inner_text("main")
        p.fill("#unlockIn", "wrong passphrase"); p.click("#unlockBtn")
        wait_for(lambda: "Špatné heslo" in p.inner_text("#unlockMsg") or "Počkej" in p.inner_text("#unlockMsg"), 10, "wrong passphrase rejected")
        assert p.locator("#unlockIn").count() == 1, "must stay locked"
        assert p.locator("#unlockBtn").is_disabled(), "retry backoff after a wrong passphrase"
        time.sleep(1.3)
        p.fill("#unlockIn", "correct horse battery"); p.click("#unlockBtn")
        wait_for(lambda: p.locator("#unlockIn").count() == 0 and "Zamčeno" in p.inner_text("main"), 15, "unlocked, app visible")
        # unlocked session works against the API and still nothing readable at rest
        sync_now(c); create_notebook(c, "Po odemčení")
        wait_for(lambda: any("notebooks/" in k for k in main_files()), 10, "write works after unlock")
        assert scan_token(c, secret) == [], scan_token(c, secret)
        # lock now
        p.goto(BASE + "#/settings"); p.click("#lockBtn"); wait_for(lambda: p.locator("#unlockIn").count() == 1, 5, "locked again")
        assert not c.problems, c.problems
    finally:
        fg.TOKEN_OVERRIDE = None; c.close()


IDB_KV = """(k) => new Promise(r => { const q = indexedDB.open('lessonnotes'); q.onsuccess = () => { const g = q.result.transaction('kv').objectStore('kv').get(k); g.onsuccess = () => { r(g.result ? g.result.v : null); q.result.close(); }; }; })"""
def kv(c, k): return c.page.evaluate(IDB_KV, k)
SECRET = "github_pat_TESTSECRET_0123456789abcdefABCDEF"
def unlock(c, passphrase):
    p = c.page
    wait_for(lambda: p.locator("#unlockIn").count() == 1, 10, "unlock screen")
    p.fill("#unlockIn", passphrase); p.click("#unlockBtn")
    wait_for(lambda: p.locator("#unlockIn").count() == 0, 15, "unlocked")
def save_locked(c, passphrase):
    p = c.page
    p.fill("#passIn", passphrase); p.fill("#passIn2", passphrase); p.click("#saveTokenBtn")

def test_cfg_never_persists_token(pw):
    """Regression (errors.md finding 1): changing settings while UNLOCKED must not write the token anywhere at rest."""
    ctl("reset"); fg.TOKEN_OVERRIDE = SECRET
    c = Ctx(pw); p = c.page
    try:
        login(c, token=SECRET, plain=False)
        wait_for(lambda: (kv(c, "vault") or {}).get("mode") == "locked", 15, "locked vault saved")
        create_notebook(c, "Cfg")                       # settings/app.json can only be written once a notebook exists
        p.reload(); unlock(c, "correct horse battery")  # fresh session: token only in memory
        p.goto(BASE + "#/settings")
        p.click("#dz-lesson"); p.click("#dz-meeting")   # setDiarize -> persisted cfg + settings/app.json
        p.select_option("#langSel", "en"); p.select_option("#themeSel", "dark")
        wait_for(lambda: "settings/app.json" in main_files(), 10, "settings written")
        wait_for(lambda: json.loads(main_files()["settings/app.json"])["diarize_default"]["lesson"] is True, 10, "diarize change synced")
        time.sleep(1.0)
        cfg = kv(c, "cfg"); assert cfg and "token" not in cfg and cfg.get("diarize"), cfg
        assert cfg["diarize"]["lesson"] is True, cfg
        assert scan_token(c, SECRET) == [], scan_token(c, SECRET)
        assert not any(SECRET in v for v in main_files().values()), "token must never reach the data repo"
        assert not c.problems, c.problems
    finally:
        fg.TOKEN_OVERRIDE = None; c.close()

def test_vault_mode_switch(pw):
    """Regression (errors.md finding 2): locked -> plain -> locked and passphrase change must reseal, not early-return."""
    ctl("reset"); fg.TOKEN_OVERRIDE = SECRET
    c = Ctx(pw); p = c.page
    try:
        login(c, token=SECRET, plain=False, passphrase="first passphrase")
        wait_for(lambda: (kv(c, "vault") or {}).get("mode") == "locked", 15, "locked")
        v1 = kv(c, "vault"); assert scan_token(c, SECRET) == []
        # locked -> plain (same token, nothing else changed)
        p.goto(BASE + "#/settings"); p.check("#plainChk"); p.click("#saveTokenBtn"); p.click("dialog .btn.danger")
        wait_for(lambda: (kv(c, "vault") or {}).get("mode") == "plain", 10, "switched to plain")
        assert scan_token(c, SECRET), "plain mode stores the token (by explicit choice)"
        # plain -> locked
        p.goto(BASE + "#/settings"); p.uncheck("#plainChk"); save_locked(c, "second passphrase")
        wait_for(lambda: (kv(c, "vault") or {}).get("mode") == "locked", 10, "switched back to locked")
        assert scan_token(c, SECRET) == [], scan_token(c, SECRET)
        v2 = kv(c, "vault"); assert v2["ct"] != v1["ct"]
        # passphrase change with the SAME token and mode
        p.goto(BASE + "#/settings"); save_locked(c, "third passphrase")
        wait_for(lambda: (kv(c, "vault") or {}).get("ct") not in (None, v2["ct"]), 10, "resealed with the new passphrase")
        assert scan_token(c, SECRET) == []
        # a click on Save with nothing changed keeps the vault as-is (early return still works)
        v3 = kv(c, "vault"); p.goto(BASE + "#/settings"); p.click("#saveTokenBtn"); time.sleep(1.0)
        assert kv(c, "vault")["ct"] == v3["ct"]
        # reload: old passphrases fail, the new one works
        p.reload(); wait_for(lambda: p.locator("#unlockIn").count() == 1, 10, "unlock screen")
        for old in ("first passphrase", "second passphrase"):
            p.fill("#unlockIn", old); p.click("#unlockBtn")
            wait_for(lambda: p.locator("#unlockBtn").is_disabled(), 5, "backoff after wrong passphrase"); time.sleep(2.2)
            assert p.locator("#unlockIn").count() == 1
        unlock(c, "third passphrase")
        assert scan_token(c, SECRET) == []
        assert not c.problems, c.problems
    finally:
        fg.TOKEN_OVERRIDE = None; c.close()

def test_speaker_merge(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Merge"); new_note(c, "Přeštěpení", [audio("a.ogg", 3000, 3), audio("b.ogg", 2000, 5)])
        nid = wait_for(lambda: note_ids(), 15, "note")[0]; wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 20, "job")
        sim_worker(nid, "done", speakers=True)
        p.goto(BASE + f"#/n/{nid}"); sync_now(c); p.goto(BASE + f"#/n/{nid}")
        wait_for(lambda: "Hotovo" in p.inner_text("#statusCard"), 15, "done"); p.click("#tab-transcript")
        spk = lambda: p.locator(".spk").all_inner_texts()
        assert spk() == ["SPEAKER_00", "SPEAKER_01"] * 2, spk()
        # merge SPEAKER_01 into SPEAKER_00 in ALL parts
        p.locator(".spk").nth(1).click(); p.select_option("#spkMerge", "SPEAKER_00"); p.check("#spkAll"); p.click("#spkSave")
        wait_for(lambda: f"notes/{nid}/speakers.json" in main_files(), 10, "speakers.json written")
        sp = json.loads(main_files()[f"notes/{nid}/speakers.json"])
        assert sp["merges"] == {"p01:SPEAKER_01": "SPEAKER_00", "p02:SPEAKER_01": "SPEAKER_00"}, sp
        assert spk() == ["SPEAKER_00"] * 4, spk()
        # raw transcript untouched (single-writer rule): only the user file changed
        tr = json.loads(main_files()[f"notes/{nid}/transcript.json"]); assert {s["speaker"] for pt in tr["parts"] for s in pt["segments"]} == {"SPEAKER_00", "SPEAKER_01"}
        # rename the merged speaker (all parts) and check the Markdown export uses it
        p.locator(".spk").first.click(); p.fill("#spkName", "Učitel"); p.check("#spkAll"); p.click("#spkSave")
        wait_for(lambda: spk() == ["Učitel"] * 4, 10, "renamed merged speaker")
        p.click("#exportBtn"); p.check("#exTr"); p.click("#exCopy")
        md = wait_for(lambda: p.evaluate("navigator.clipboard.readText()"), 5, "clipboard")
        p.click("#exClose")
        assert "SPEAKER_01" not in md and "**Učitel**" in md, md
        # undo the merge
        p.locator(".spk").first.click(); p.check("#spkAll"); p.click("#spkUnmerge")
        wait_for(lambda: json.loads(main_files()[f"notes/{nid}/speakers.json"])["merges"] == {}, 10, "merge removed")
        assert spk() == ["Učitel", "SPEAKER_01"] * 2, spk()
        assert not c.problems, c.problems
    finally: c.close()

def test_csp(pw):
    html = (REPO / "web" / "index.html").read_text()
    csp = re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1)
    assert "unsafe-" not in csp, csp
    assert "connect-src https://api.github.com;" in csp and "script-src 'self'" in csp and "default-src 'none'" in csp
    for f in (REPO / "web").rglob("*.js"):
        src = f.read_text(); assert not re.search(r"\binnerHTML\b|\beval\(|new Function\(|\.outerHTML\s*=|insertAdjacentHTML", src), f"unsafe DOM/eval API in {f}"
    assert not re.search(r"\son[a-z]+=|style=", html)


def test_precache_static(pw):
    out = subprocess.run([sys.executable, str(REPO / "tools" / "stamp-precache.py"), "--check"], cwd=REPO, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    tracked = set(subprocess.check_output(["git", "ls-files", "web"], cwd=REPO, text=True).split())
    js = (REPO / "web" / "precache.js").read_text()
    for f in re.findall(r"^  '([^']+)',$", js, re.M):
        if f != "./": assert f"web/{f}" in tracked and (REPO / "web" / f).exists(), f
    html = (REPO / "web" / "index.html").read_text() + (REPO / "web" / "manifest.webmanifest").read_text()
    for ref in re.findall(r'(?:href|src)="([^"#]+)"', html) + re.findall(r'"src": "([^"]+)"', html):
        assert f"web/{ref}" in tracked, f"referenced but not tracked: {ref}"
    for f in tracked:
        if f.endswith(".js"): assert not re.search(r"github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{20,}", (REPO / f).read_text()), f"token-like string in {f}"


# =====================================================================================================================
# v2 (CHANGE REQUEST 2): context form, photos, notes v2, export
# =====================================================================================================================
EX_V2 = CONTRACT / "examples" / "v2"
EX_V1 = CONTRACT / "examples" / "lesson-2part"
EVIL_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60" onload="window.__pwned=1"><script>window.__pwned=2</script>'
            '<rect x="5" y="5" width="50" height="25" fill="#e57373" stroke="black" onclick="window.__pwned=3"/>'
            '<foreignObject width="10" height="10"><body xmlns="http://www.w3.org/1999/xhtml" onload="window.__pwned=4"/></foreignObject>'
            '<text x="8" y="50" font-family="sans-serif" font-size="12">SAFE-TEXT</text></svg>')
UNFIXABLE_SVG = '<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text x="1" y="5">&x;</text></svg>'

def jpeg(w, h, orientation=1, noise=False, seed=1, name=None):
    """Test photo: a gradient with a red square in the top-left corner of the RAW pixels (+ optional EXIF orientation) or random noise."""
    if noise:
        im = Image.frombytes("RGB", (w, h), random.Random(seed).randbytes(w * h * 3))
    else:
        im = Image.new("RGB", (w, h), (30, 90 + seed * 20 % 120, 160))
        d = ImageDraw.Draw(im); d.rectangle([0, 0, w // 5, h // 5], fill=(255, 0, 0)); d.line([0, h, w, 0], fill=(255, 255, 255), width=max(2, w // 100))
    exif = Image.Exif(); exif[0x0112] = orientation
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=90, exif=exif)
    return {"name": name or f"photo{seed}.jpg", "mimeType": "image/jpeg", "buffer": buf.getvalue()}

def raw(branch, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{WEB_PORT}/__ctl/raw?branch={branch}&path={urllib.parse.quote(path)}").read()
import urllib.parse

def new_note_v2(c, title, files, photos=(), topic="", details="", mode=None, kind="lesson", submit=True):
    p = c.page; p.goto(BASE + "#/new")
    p.fill("#titleIn", title)
    if kind == "meeting": p.click("#kind-meeting")
    if mode: p.click(f"#lm-{mode}")
    if topic: p.fill("#topicIn", topic)
    if details: p.fill("#detailsIn", details)
    p.set_input_files("#fileIn", files)
    wait_for(lambda: p.locator(".plist li").count() == len(files), 5, "parts listed")
    if photos:
        p.set_input_files("#photoIn", list(photos))
        wait_for(lambda: p.locator("#phList li").count() == len(photos) and not p.locator("#photoBusy").count(), 20, "photos processed")
    if submit: p.click("#submitBtn")

def svg_noise_ok(c):
    """Hostile SVG parsed by DOMParser makes Chromium log CSP *blocks* (inline handler / style): expected, they prove the CSP holds."""
    return [x for x in c.problems if not re.search(r"Executing inline event handler|Applying inline style", x)]

def now_z(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def sim_worker_v2(note_id, evil=False, meeting=False, state_name="done", with_images=True, topic_title=None):
    """What the v2 worker commits: transcript v2 + notes v2 (from the contract examples) + status v2 + kept photos on main."""
    mf = main_files(); note = json.loads(mf[f"notes/{note_id}/note.json"])
    job = json.loads(inbox_files().get(f"jobs/{note_id}/job.json", "null") or "null")
    job_id = (job and job["job_id"]) or "01K8F4A9M3N5P7Q9R1S3T5V7W9"
    now = now_z()
    st = json.load(open(EX_V2 / "lecture-mixed-images" / ("status.json" if state_name == "done" else "status.reading_images.json")))
    st.update(note_id=note_id, job_id=job_id, updated_at=now, finished_at=now if state_name == "done" else None, completed_job_ids=[job_id] if state_name == "done" else [],
              parts=[{"part_id": p["id"], "state": "transcribed", "duration": 60.0 * p["order"]} for p in note["parts"]])
    img_ids = [i["id"] for i in note.get("images", []) if i.get("status") != "failed"]
    if state_name == "done": st["images_kept"] = img_ids
    else: st.pop("images_kept", None)
    files, b64 = {f"notes/{note_id}/status.json": json.dumps(st, indent=2) + "\n"}, {}
    if state_name == "done":
        off, parts = 0.0, []
        for pt in note["parts"]:
            dur = 60.0 * pt["order"]
            segs = [{"start": 0.0, "end": 6.0, "text": "Dnes se podíváme na light-dependent reactions.", "lang": "cs", "speaker": "SPEAKER_00"}, {"start": 6.0, "end": 20.0, "text": f"The light reactions happen in the thylakoid membrane, část {pt['order']}.", "lang": "en", "speaker": "SPEAKER_01"}]
            if note.get("language_mode", "mixed") != "mixed":
                for sg in segs: sg["lang"] = note["language_mode"]
            parts.append({"part_id": pt["id"], "order": pt["order"], "duration": dur, "offset": off, "job_id": job_id, "transcribed_at": now,
                          "detected_languages": [{"lang": "cs", "share": 0.6}, {"lang": "en", "share": 0.4}], "segments": segs}); off += dur
        tr = {"schema_version": 2, "note_id": note_id, "language_mode": note.get("language_mode", "mixed"), "language": "cs", "engine": {"name": "fake", "model": "x"},
              "diarization": {"enabled": True, "engine": "x", "speaker_scope": "per_part"}, "updated_at": now, "parts": parts,
              "merged": {"duration": off, "text": "\n".join(f"[Část {p['order']}]\n" + " ".join(s["text"] for s in p["segments"]) for p in parts)}}
        nt = json.load(open(EX_V2 / ("meeting" if meeting else "lecture-mixed-images") / "notes.json"))
        nt.update(note_id=note_id, job_id=job_id, generated_at=now, based_on_parts=[p["id"] for p in note["parts"]])
        if with_images and img_ids and not meeting: nt["based_on_images"] = img_ids
        else: nt.pop("based_on_images", None)
        nt["kind"] = note["kind"]
        for k, d in (("transcript", tr), ("notes", nt), ("status", st)):
            e = contract_errors(k, d); assert not e, (k, e)
        if evil:   # injected AFTER validation: the PWA must not trust figures even though the worker should have sanitised them
            nt["content"]["figures"][1]["svg"] = EVIL_SVG
            nt["content"]["figures"].append({"id": "f03", "title": "Neopravitelný", "caption": "x", "svg": UNFIXABLE_SVG})
        if topic_title: nt["content"]["title"] = topic_title
        files[f"notes/{note_id}/transcript.json"] = json.dumps(tr, indent=2) + "\n"; files[f"notes/{note_id}/notes.json"] = json.dumps(nt, indent=2) + "\n"
        if job:   # worker copies photos from the inbox to main (completion commit)
            for im in job.get("images", []):
                if im["location"] == "inbox": b64[f"notes/{note_id}/{im['file']}"] = base64.b64encode(raw("inbox", f"jobs/{note_id}/{im['file']}")).decode()
    ctl("put", {"branch": "main", "files": files, "b64files": b64, "message": f"note {note_id}: status {state_name}"})

def upload_order(note_id):
    """indices of the PUTs for audio parts, images and job.json in the fake server's request log"""
    log = state()["log"]
    idx = lambda pat: [i for i, (m, pth) in enumerate(log) if m == "PUT" and re.search(pat, pth)]
    return idx(rf"/contents/jobs/{note_id}/parts/"), idx(rf"/contents/jobs/{note_id}/images/"), idx(rf"/contents/jobs/{note_id}/job\.json")

def open_note(c, nid):
    c.page.goto(BASE + f"#/n/{nid}"); time.sleep(0.5)

def setup_done_note(c, photos=2, evil=False, meeting=False, mode=None, title="Fotosyntéza v2", topic="Světelná fáze fotosyntézy"):
    login(c); create_notebook(c, "Biologie 2.A")
    ph = [jpeg(1200 + 100 * i, 900, seed=i + 1) for i in range(photos)]
    new_note_v2(c, title, [audio("a.ogg", 3000)], ph, topic=topic, details="Učitelka střídá češtinu a angličtinu.", mode=mode, kind="meeting" if meeting else "lesson")
    nid = wait_for(lambda: note_ids(), 15, "note")[0]
    wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 20, "job")
    wait_for(lambda: json.loads(main_files()[f"notes/{nid}/note.json"]).get("parts", [{}])[0].get("status") == "uploaded", 20, "parts marked uploaded")
    heartbeat(1, 0); sim_worker_v2(nid, evil=evil, meeting=meeting); sync_now(c)
    return nid

# ---- SVG safety port ----------------------------------------------------------------------------------------------
def test_svg_vectors(pw):
    c = Ctx(pw); p = c.page
    try:
        p.goto(BASE + "#/search")
        vec = json.load(open(CONTRACT / "svg_vectors.json"))
        res = p.evaluate("""async (vec) => {
          const m = await import('./js/svgsafe.js'); const out = [];
          for (const v of vec) {
            const viol = m.violations(v.svg); const s = m.sanitize(v.svg);
            out.push({name: v.name, valid: viol.length === 0, san: s.svg, sanViol: s.svg ? m.violations(s.svg) : null, again: s.svg ? m.sanitize(s.svg).svg : null, safe: m.safeSvg(v.svg)});
          }
          return out; }""", vec)
        assert len(res) == len(vec) >= 31
        for v, r in zip(vec, res):
            assert r["valid"] == v["valid"], (v["name"], r)
            if r["san"] is not None:
                assert r["sanViol"] == [], (v["name"], r["sanViol"])
                assert not re.search(r"<script|<foreignObject|\son[a-z]+=|style=|href=", r["san"], re.I), (v["name"], r["san"])
                assert r["again"] == r["san"], ("not idempotent", v["name"])
            if v["valid"]: assert r["san"] is not None and r["safe"] == v["svg"], v["name"]
            else: assert r["safe"] is None or not re.search(r"<script|onload|onclick|foreignObject", r["safe"], re.I), (v["name"], r["safe"])
        for n in ("not_xml", "doctype_entity", "wrong_root", "no_viewbox", "too_big"):
            assert next(r for r in res if r["name"] == n)["san"] is None, n
        assert not svg_noise_ok(c), c.problems
    finally: c.close()

# ---- new-note form, photo resize, upload order -----------------------------------------------------------------------
def test_new_note_v2_photos_upload(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Biologie 2.A")
        p.goto(BASE + "#/new")
        assert p.get_attribute("#lm-mixed", "aria-checked") == "true", "default language mode is mixed"
        assert p.get_attribute("#camIn", "capture") is not None and p.get_attribute("#photoIn", "multiple") is not None
        assert "AI" in p.inner_text(".ctxform") and "O čem hodina byla" in p.inner_text(".ctxform")
        p.fill("#titleIn", "Fotosyntéza v2")
        p.fill("#topicIn", "Světelná fáze"); p.fill("#detailsIn", "Učitelka používá anglické termíny.")
        p.set_input_files("#fileIn", [audio("a.ogg", 3000)])
        # photo 1: 3000x2000 raw pixels with EXIF orientation 6 (rotate 90 CW) -> displayed portrait 2000x3000 -> resized to 1067x1600
        # photo 2: random noise 2400x1800: cannot fit 600 KiB at 1600 px / q 0.8, so the encoder must shrink
        p.set_input_files("#photoIn", [jpeg(3000, 2000, orientation=6, seed=1), jpeg(2400, 1800, noise=True, seed=2)])
        wait_for(lambda: p.locator("#phList li").count() == 2 and not p.locator("#photoBusy").count(), 30, "2 photos processed")
        th = p.evaluate("[...document.querySelectorAll('#phList img.thumb')].map(i => [i.naturalWidth, i.naturalHeight])")
        assert all(0 < w <= 240 and 0 < h <= 240 for w, h in th), th
        # add a third, then remove it again (caption + remove before upload)
        p.set_input_files("#photoIn", [jpeg(800, 600, seed=3)]); wait_for(lambda: p.locator("#phList li").count() == 3, 10, "third photo")
        p.locator("#phList li").nth(2).locator(".ibtn").click(); wait_for(lambda: p.locator("#phList li").count() == 2, 5, "photo removed")
        p.locator("#phList li").first.locator("input.cap").fill("Tabule: schéma")
        p.screenshot(path=str(SHOTS / "new-form-photos-412.png"), full_page=True)
        p.click("#submitBtn")
        nid = wait_for(lambda: note_ids(), 15, "note created")[0]
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 30, "job on inbox")
        wait_for(lambda: all(i["status"] == "uploaded" for i in json.loads(main_files()[f"notes/{nid}/note.json"]).get("images", [{"status": "x"}])), 15, "images marked uploaded")
        note = json.loads(main_files()[f"notes/{nid}/note.json"]); e = contract_errors("note", note); assert not e, e
        assert note["schema_version"] == 2 and note["language_mode"] == "mixed"
        assert note["context"] == {"subject": "Biologie 2.A", "topic": "Světelná fáze", "details": "Učitelka používá anglické termíny."}, note["context"]
        assert note["inputs_updated_at"] and [i["id"] for i in note["images"]] == ["i01", "i02"] and note["images"][0]["caption"] == "Tabule: schéma"
        i1, i2 = note["images"]
        assert (i1["width"], i1["height"]) == (1067, 1600), (i1["width"], i1["height"])         # EXIF orientation respected, long edge 1600
        assert i2["bytes"] <= 614400 and max(i2["width"], i2["height"]) <= 1600, i2
        ib = inbox_files()
        for im in note["images"]:
            f = ib[f"jobs/{nid}/images/{im['filename']}"]
            assert f["bytes"] == im["bytes"] and f["sha256"] == im["sha256"], (im, f)
        pic = Image.open(io.BytesIO(raw("inbox", f"jobs/{nid}/images/01.jpg"))); assert pic.size == (1067, 1600) and pic.format == "JPEG"
        # orientation really applied: the red square sat top-left in RAW pixels, after a 90 deg CW rotation it is top-RIGHT
        tl, tr_ = pic.getpixel((20, 20)), pic.getpixel((1067 - 20, 20))
        assert tr_[0] > 200 and tr_[1] < 80 and not (tl[0] > 200 and tl[1] < 80), (tl, tr_)
        assert Image.open(io.BytesIO(raw("inbox", f"jobs/{nid}/images/02.jpg"))).size == (i2["width"], i2["height"])
        job = json.loads(ib[f"jobs/{nid}/job.json"]); e = contract_errors("job", job); assert not e, e
        assert job["schema_version"] == 2 and job["language_mode"] == "mixed" and job["context"] == note["context"] and "language" not in job
        assert [(i["id"], i["location"]) for i in job["images"]] == [("i01", "inbox"), ("i02", "inbox")] and job["images"][0]["caption"] == "Tabule: schéma"
        parts_i, img_i, job_i = upload_order(nid)
        assert parts_i and len(img_i) == 2 and len(job_i) == 1 and max(parts_i) < min(img_i) and max(img_i) < job_i[0], (parts_i, img_i, job_i)
        assert not c.problems, c.problems
    finally: c.close()

def test_photo_limit_and_errors(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Limit"); p.goto(BASE + "#/new")
        p.set_input_files("#photoIn", [jpeg(200, 150, seed=i % 5 + 1, name=f"p{i}.jpg") for i in range(12)])
        wait_for(lambda: p.locator("#phList li").count() == 12 and not p.locator("#photoBusy").count(), 40, "12 photos")
        p.set_input_files("#photoIn", [jpeg(200, 150, seed=2)]); time.sleep(1.0)
        assert p.locator("#phList li").count() == 12, "13th photo must be refused"
        assert "12" in p.inner_text(".toast")
        p.set_input_files("#photoIn", [{"name": "broken.jpg", "mimeType": "image/jpeg", "buffer": b"not an image"}]); time.sleep(1.0)
        assert p.locator("#phList li").count() == 12
        assert not [x for x in c.problems if "pageerror" in x], c.problems
    finally: c.close()

# ---- notes v2 view ----------------------------------------------------------------------------------------------------------
def test_notes_v2_render_and_figure_safety(pw):
    c = Ctx(pw); p = c.page
    try:
        nid = setup_done_note(c, photos=2, evil=True)
        open_note(c, nid)
        wait_for(lambda: p.locator("#nTitle").count() == 1, 10, "v2 notes rendered")
        assert "Fotosyntéza v praxi" in p.inner_text("#nTitle") and "střídala češtinu" in p.inner_text("#nDesc")
        assert "Česky + anglicky" in p.inner_text("#langChip")
        # outline: collapsible, nested
        assert p.locator("#outline details.ol").count() == 2
        assert "Probíhají v thylakoidní membráně" in p.inner_text("#outline")
        p.click("#olCollapse"); assert p.locator("#outline details.ol[open]").count() == 0
        p.click("text=Rozbalit vše"); assert p.locator("#outline details.ol[open]").count() == 2
        # key points with kind badges
        kinds = p.evaluate("[...document.querySelectorAll('#keypoints li')].map(li => li.dataset.kind)")
        assert kinds == ["key", "definition", "formula", "important"], kinds
        assert "definice" in p.inner_text("#keypoints").lower() and "vzorec" in p.inner_text("#keypoints").lower()
        # figures: img data URI only, no svg element from notes in the DOM, evil one sanitised, unfixable one hidden
        figs = p.evaluate("[...document.querySelectorAll('#figures figure img')].map(i => decodeURIComponent(i.src.split(',')[1]))")
        assert len(figs) == 2 and all(f.startswith("<svg") for f in figs), figs
        assert not re.search(r"script|onload|onclick|foreignObject", figs[1], re.I) and "SAFE-TEXT" in figs[1], figs[1]
        assert p.evaluate("document.querySelectorAll('#figures svg, #view script').length") == 0
        assert "nelze bezpečně zobrazit" in p.inner_text("#figures")
        assert p.evaluate("window.__pwned") is None, "script in a figure executed"
        p.locator("#figures .figbtn").first.click(); wait_for(lambda: p.locator("dialog .bigimg").count() == 1, 5, "enlarged figure")
        p.screenshot(path=str(SHOTS / "figure-enlarged-412.png")); p.click("#bigClose"); wait_for(lambda: p.locator("dialog").count() == 0, 5, "closed")
        # study materials: tap to reveal, flashcards flip
        qa = p.locator("#study details.qa").first
        assert not qa.evaluate("e => e.open") and not qa.locator(".ans").is_visible()
        qa.locator("summary").click(); assert qa.locator(".ans").is_visible() and "ATP, NADPH" in qa.inner_text() or "thylakoidech" in qa.inner_text()
        fc = p.locator("#study .flash").first
        before = fc.inner_text(); fc.click(); after = fc.inner_text()
        assert before != after and "Štěpení molekuly vody" in after and fc.get_attribute("aria-pressed") == "true", (before, after)
        fc.click(); assert fc.inner_text() == before
        # homework: split lesson / suggested, state saved locally
        assert p.locator("#hwSec .hw-lesson").count() == 1
        cb = p.locator("#hwSec input[type=checkbox]").first; cb.check()
        keys = p.evaluate("Object.keys(localStorage).filter(k => k.startsWith('lessonnotes.hw.'))"); assert keys == [f"lessonnotes.hw.{nid}"], keys
        p.reload(); wait_for(lambda: p.locator("#hwSec").count() == 1, 10, "reloaded")
        assert p.locator("#hwSec input[type=checkbox]").first.is_checked(), "homework state must survive reload"
        # uncertainties box
        assert "Zkontroluj" in p.inner_text("#uncertain") and "neostrá" in p.inner_text("#uncertain")
        # photo gallery: loaded from the local copy
        wait_for(lambda: p.evaluate("[...document.querySelectorAll('#gallery img')].filter(i => i.naturalWidth > 0).length") == 2, 15, "gallery images")
        # ... and from main when the local copy is gone (other device / cleared storage)
        p.evaluate("""() => new Promise(r => { const q = indexedDB.open('lessonnotes'); q.onsuccess = () => { const db = q.result; const tx = db.transaction('blobs', 'readwrite'); const os = tx.objectStore('blobs');
            const k = os.getAllKeys(); k.onsuccess = () => { for (const key of k.result) if (String(key).startsWith('img:')) os.delete(key); }; tx.oncomplete = () => { db.close(); r(); }; }; })""")
        p.reload()
        wait_for(lambda: p.evaluate("[...document.querySelectorAll('#gallery img')].filter(i => i.naturalWidth > 0).length") == 2, 20, "gallery images from main")
        p.locator("#gallery .gbtn").first.click(); wait_for(lambda: p.locator("dialog .bigimg").count() == 1, 5, "photo enlarged"); p.click("#bigClose")
        assert not svg_noise_ok(c), c.problems
    finally: c.close()

def test_meeting_v2_render(pw):
    c = Ctx(pw); p = c.page
    try:
        nid = setup_done_note(c, photos=0, meeting=True, title="Porada v2")
        open_note(c, nid); wait_for(lambda: p.locator("#nTitle").count() == 1, 10, "meeting notes")
        txt = p.inner_text(".notes")
        assert "Porada: stav releasu" in txt and "Rozhodnutí" in txt and "Nasazení proběhne" in txt and "Úkoly ze schůzky" in txt and "Petra" in txt and "Co dál" in txt, txt[:500]
        assert p.locator("#hwSec, #studySec").count() == 0, "meeting has no homework / study materials"
        assert p.locator("#aiSec input[type=checkbox]").count() == 1
        assert not c.problems, c.problems
    finally: c.close()

def test_v1_compat_and_bump(pw):
    """A v1 note (no language_mode/context/images, v1 notes.json) still renders, exports, and becomes v2 only when touched."""
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Staré")
        nbid = [k for k in main_files() if k.startswith("notebooks/")][0].split("/")[1][:-5]
        note = json.load(open(EX_V1 / "note.json")); nid = note["id"]; note["notebook_id"] = nbid
        files = {f"notes/{nid}/note.json": json.dumps(note, indent=2) + "\n"}
        for k in ("transcript", "notes", "status"): files[f"notes/{nid}/{k}.json"] = (EX_V1 / f"{k}.json").read_text()
        ctl("put", {"branch": "main", "files": files}); heartbeat(1, 0); sync_now(c)
        assert json.loads(main_files()[f"notes/{nid}/note.json"])["schema_version"] == 1
        open_note(c, nid)
        wait_for(lambda: p.locator(".notes").count() == 1 and "Hlavní body" in p.inner_text(".notes"), 10, "v1 notes rendered")
        assert p.locator("#nTitle").count() == 0
        # export works for v1 too
        p.click("#exportBtn")
        with p.expect_download() as dl: p.click("#exDocx")
        path = dl.value.path(); z = zipfile.ZipFile(path); assert z.testzip() is None; ET.fromstring(z.read("word/document.xml"))
        p.click("#exClose")
        # touching it (regenerate via edit form) upgrades note.json to v2 with defaults filled in
        p.click("#editCtxBtn"); wait_for(lambda: p.locator("#topicIn").count() == 1, 10, "edit form")
        assert p.get_attribute("#lm-mixed", "aria-checked") == "true"
        p.fill("#topicIn", "Dodatečné téma"); p.click("#saveEditBtn"); p.click("dialog .btn.danger")
        wait_for(lambda: json.loads(main_files()[f"notes/{nid}/note.json"]).get("schema_version") == 2, 15, "note bumped to v2")
        n2 = json.loads(main_files()[f"notes/{nid}/note.json"]); e = contract_errors("note", n2); assert not e, e
        assert n2["context"]["topic"] == "Dodatečné téma" and n2["context"]["subject"] == "Staré" and n2["images"] == [] and n2["language_mode"] == "mixed", n2
        wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 15, "regenerate job")
        job = json.loads(inbox_files()[f"jobs/{nid}/job.json"]); e = contract_errors("job", job); assert not e, e
        assert job["action"] == "regenerate_notes" and job["parts"] == [] and job["images"] == []
        assert not c.problems, c.problems
    finally: c.close()

def test_edit_context_photos_regenerate(pw):
    c = Ctx(pw); p = c.page
    try:
        nid = setup_done_note(c, photos=2)
        i0 = json.loads(main_files()[f"notes/{nid}/note.json"])
        assert f"notes/{nid}/images/01.jpg" in main_files() and f"notes/{nid}/images/02.jpg" in main_files(), "worker kept the photos"
        open_note(c, nid)
        assert p.locator("#outdatedBanner").count() == 0
        p.click("#editCtxBtn"); wait_for(lambda: p.locator("#topicIn").count() == 1, 10, "edit form")
        assert p.input_value("#topicIn") == "Světelná fáze fotosyntézy" and "střídá" in p.input_value("#detailsIn")
        wait_for(lambda: p.evaluate("[...document.querySelectorAll('#phList img.thumb')].filter(i => i.naturalWidth > 0).length") == 2, 10, "existing thumbs")
        p.click("#lm-en"); p.fill("#topicIn", "Calvinův cyklus"); p.fill("#detailsIn", "Test příští týden.")
        p.locator("#phList li[data-id=i01] .ibtn").click()
        p.locator("#phList li[data-id=i02] input.cap").fill("Slide 2")
        p.set_input_files("#photoIn", [jpeg(1000, 700, seed=4)]); wait_for(lambda: p.locator("#phList li").count() == 2 and not p.locator("#photoBusy").count(), 10, "new photo")
        p.screenshot(path=str(SHOTS / "edit-form-412.png"), full_page=True)
        p.click("#saveEditBtn"); assert "AI" in p.inner_text("dialog") or "Claude" in p.inner_text("dialog")   # cost warning
        p.click("dialog .btn.danger")
        wait_for(lambda: json.loads(inbox_files().get(f"jobs/{nid}/job.json", "{}") or "{}").get("action") == "regenerate_notes", 20, "regenerate job")
        n = json.loads(main_files()[f"notes/{nid}/note.json"]); e = contract_errors("note", n); assert not e, e
        assert n["language_mode"] == "en" and n["context"]["topic"] == "Calvinův cyklus" and n["context"]["details"] == "Test příští týden."
        assert [i["id"] for i in n["images"]] == ["i02", "i03"] and n["images"][0]["caption"] == "Slide 2" and n["inputs_updated_at"] > i0["inputs_updated_at"] or n["inputs_updated_at"] >= i0["inputs_updated_at"]
        job = json.loads(inbox_files()[f"jobs/{nid}/job.json"]); e = contract_errors("job", job); assert not e, e
        assert job["parts"] == [] and job["append"] is True and job["language_mode"] == "en" and job["context"] == n["context"]
        assert [(i["id"], i["location"]) for i in job["images"]] == [("i02", "main"), ("i03", "inbox")], job["images"]
        assert job["images"][0]["caption"] == "Slide 2"
        _, img_i, job_i = upload_order(nid)
        assert len(img_i) >= 2 and max(img_i) < job_i[-1], (img_i, job_i)   # new photo uploaded before job.json (>=2: the original two + new)
        wait_for(lambda: all(i["status"] == "uploaded" for i in json.loads(main_files()[f"notes/{nid}/note.json"])["images"]), 15, "new image uploaded")
        # while the job is pending the note cannot be edited again
        open_note(c, nid); assert p.is_disabled("#editCtxBtn")
        # worker finishes: kept photos = i02 + i03 ; the deleted i01 is orphaned on main (worker GC), PWA never shows it
        sim_worker_v2(nid); sync_now(c); open_note(c, nid)
        wait_for(lambda: p.evaluate("[...document.querySelectorAll('#gallery img')].filter(i => i.naturalWidth > 0).length") == 2, 15, "gallery has 2 photos")
        assert "Slide 2" in p.inner_text("#gallery")
        # outdated banner: context edited after the notes were generated and no job pending
        n["inputs_updated_at"] = "2099-01-01T00:00:00Z"; n["images"] = [dict(i, status="uploaded") for i in n["images"]]
        ctl("put", {"branch": "main", "files": {f"notes/{nid}/note.json": json.dumps(n, indent=2) + "\n"}}); sync_now(c); open_note(c, nid)
        wait_for(lambda: p.locator("#outdatedBanner").count() == 1, 10, "outdated banner")
        assert not c.problems, c.problems
    finally: c.close()

def test_regenerate_confirmation(pw):
    c = Ctx(pw); p = c.page
    try:
        nid = setup_done_note(c, photos=1)
        open_note(c, nid)
        p.click("#regenBtn"); p.click("dialog .btn.ghost")     # cancel: nothing sent
        time.sleep(1.0); job0 = json.loads(inbox_files().get(f"jobs/{nid}/job.json", "{}"))
        assert job0.get("action") == "transcribe", "cancelled regenerate must not create a job"
        p.click("#regenBtn"); p.click("dialog .btn.danger")
        wait_for(lambda: json.loads(inbox_files()[f"jobs/{nid}/job.json"])["action"] == "regenerate_notes", 10, "regenerate job")
        job = json.loads(inbox_files()[f"jobs/{nid}/job.json"]); e = contract_errors("job", job); assert not e, e
        assert [(i["id"], i["location"]) for i in job["images"]] == [("i01", "main")] and job["language_mode"] == "mixed"
        assert not c.problems, c.problems
    finally: c.close()

def test_status_reading_images(pw):
    c = Ctx(pw); p = c.page
    try:
        login(c); create_notebook(c, "Stav")
        new_note_v2(c, "Čtení fotek", [audio("a.ogg", 3000)], [jpeg(600, 400, seed=1)])
        nid = wait_for(lambda: note_ids(), 15, "note")[0]; wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 20, "job")
        wait_for(lambda: json.loads(main_files()[f"notes/{nid}/note.json"])["parts"][0]["status"] == "uploaded", 15, "uploaded")
        sim_worker_v2(nid, state_name="reading_images"); heartbeat(0, 1); sync_now(c); open_note(c, nid)
        wait_for(lambda: "Čtu fotky" in p.inner_text("#statusCard"), 10, "reading_images badge")
        assert "Ve frontě" not in p.inner_text("#statusCard")
        assert not c.problems, c.problems
    finally: c.close()

# ---- export ---------------------------------------------------------------------------------------------------------------------
def docx_report(path, expect_images):
    z = zipfile.ZipFile(path); assert z.testzip() is None, "corrupt zip"
    names = z.namelist(); assert names[0] == "[Content_Types].xml", names
    for n in names:
        if n.endswith((".xml", ".rels")): ET.fromstring(z.read(n))          # well-formed
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(z.read("word/document.xml"))
    paras = ["".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p")]
    styles = [(p.find(f"{W}pPr/{W}pStyle").get(W + "val") if p.find(f"{W}pPr/{W}pStyle") is not None else "") for p in root.iter(W + "p")]
    media = [n for n in names if n.startswith("word/media/")]
    assert len(media) == expect_images, (media, expect_images)
    rels = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    rid = {r.get("Id"): r.get("Target") for r in rels}
    RN = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
    embeds = [b.get(RN) for b in root.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}blip")]
    assert len(embeds) == expect_images and all(("word/" + rid[e]) in names for e in embeds), (embeds, rid)
    for n in media:
        if n.endswith(".png"): assert z.read(n)[:8] == b"\x89PNG\r\n\x1a\n"
        else: assert z.read(n)[:2] == b"\xff\xd8"
    return paras, styles

def lo_convert(docx_path, outdir):
    soffice = shutil.which("soffice")
    if not soffice: return None
    prof = HERE / ".lo-profile"
    r = subprocess.run([soffice, f"-env:UserInstallation=file://{prof}", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(docx_path)], capture_output=True, text=True, timeout=180)
    pdf = pathlib.Path(outdir) / (pathlib.Path(docx_path).stem + ".pdf")
    assert pdf.exists(), (r.stdout, r.stderr)
    return pdf

def pdf_pages(pdf): return int(re.search(r"Pages:\s+(\d+)", subprocess.check_output(["pdfinfo", str(pdf)], text=True)).group(1))
def pdf_text(pdf): return subprocess.check_output(["pdftotext", "-layout", str(pdf), "-"], text=True)

def test_export_docx_pdf_md(pw):
    out = SHOTS / "export"; out.mkdir(parents=True, exist_ok=True)
    c = Ctx(pw); p = c.page
    c.context.add_init_script("window.__printed = 0; window.print = () => { window.__printed++; };")
    try:
        nid = setup_done_note(c, photos=2, title="Export test")
        open_note(c, nid); wait_for(lambda: p.locator("#nTitle").count() == 1, 10, "notes")
        p.check(f"#hwSec input[type=checkbox] >> nth=0")
        # --- DOCX with transcript
        p.click("#exportBtn"); p.check("#exTr")
        with p.expect_download() as dl: p.click("#exDocx")
        d1 = out / "with-transcript.docx"; dl.value.save_as(d1); assert dl.value.suggested_filename.endswith(".docx")
        paras, styles = docx_report(d1, expect_images=2 + 2)       # 2 figures (PNG) + 2 photos
        text = "\n".join(paras)
        for must in ("Fotosyntéza v praxi: světelné reakce a Calvinův cyklus", "Osnova", "Světelné reakce (light-dependent reactions)", "Hlavní body", "Obrázky", "Dvě fáze fotosyntézy", "Materiály na učení",
                     "Domácí úkoly a práce", "Zadáno v hodině", "Zkontroluj", "Fotky", "Přepis", "SPEAKER_00", "[0:06] SPEAKER_01", "The light reactions happen in the thylakoid membrane"):
            assert must in text, f"missing in docx: {must}"
        assert "Heading1" in styles and "Heading2" in styles and "Title" in styles
        assert "☑" in text, ("homework checkbox state in export", text[-1500:])
        # --- DOCX without transcript
        p.uncheck("#exTr")
        with p.expect_download() as dl: p.click("#exDocx")
        d2 = out / "no-transcript.docx"; dl.value.save_as(d2)
        paras2, _ = docx_report(d2, expect_images=4)
        assert "Přepis" not in "\n".join(paras2) and "thylakoid membrane, část" not in "\n".join(paras2) and len(paras2) < len(paras)
        # --- LibreOffice opens it and renders pages
        pdf = lo_convert(d1, out)
        if pdf:
            n_pages = pdf_pages(pdf); t = pdf_text(pdf)
            assert n_pages >= 2 and "Osnova" in t and "Hlavní body" in t and "Dvě fáze" in t, (n_pages, t[:300])
            subprocess.run(["pdftoppm", "-r", "50", "-png", str(pdf), str(out / "docx-lo")], check=True)
            print(f"      docx -> LibreOffice -> {n_pages} pages ({out})")
        else: print("      LibreOffice not available: DOCX only structurally validated")
        # --- Markdown
        with p.expect_download() as dl: p.click("#exMd")
        md = pathlib.Path(dl.value.path()).read_text()
        assert md.startswith("# Fotosyntéza v praxi") and "## Osnova" in md and "## Hlavní body" in md and "## Obrázky" in md and "## Materiály na učení" in md and "## Domácí úkoly a práce" in md and "## Přepis" not in md, md[:200]
        b64s = re.findall(r"\]\(data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)\)", md); assert len(b64s) == 2
        for b in b64s:
            svg = base64.b64decode(b).decode(); assert svg.startswith("<svg") and "<script" not in svg and "onload" not in svg
        assert "- [x]" in md and "Fotka i01" in md
        (out / "export.md").write_text(md)
        p.click("#exClose")
        # --- PDF: dedicated print stylesheet; window.print() is stubbed, we print the prepared #printRoot ourselves with page.pdf()
        p.click("#exportBtn"); p.check("#exTr"); p.click("#exPdf")
        wait_for(lambda: p.evaluate("window.__printed") == 1, 15, "window.print called")
        assert p.locator("#printRoot").count() == 1 and not p.locator("#printRoot").is_visible(), "print root hidden on screen"
        pdfp = out / "print.pdf"
        p.pdf(path=str(pdfp), format="A4", print_background=True, prefer_css_page_size=True)
        n_pages = pdf_pages(pdfp); t = pdf_text(pdfp)
        info = subprocess.check_output(["pdfinfo", str(pdfp)], text=True)
        assert n_pages >= 2 and "A4" in info, info                              # A4 portrait
        for must in ("Fotosyntéza v praxi", "Osnova", "Hlavní body", "Dvě fáze fotosyntézy", "Materiály na učení", "Odpověď", "Domácí úkoly", "Zkontroluj", "Přepis", "[0:06]"):
            assert must in t, f"missing in pdf: {must}"
        assert "Zápisky z hodin" not in t and "Nastavení" not in t, "app chrome must not be printed"
        subprocess.run(["pdftoppm", "-r", "50", "-png", str(pdfp), str(out / "print")], check=True)
        print(f"      print.pdf: {n_pages} pages")
        assert not c.problems, c.problems
    finally: c.close()

def test_screenshots_and_overflow(pw):
    SHOTS.mkdir(exist_ok=True, parents=True)
    c = Ctx(pw, width=360, height=800); p = c.page
    try:
        login(c); create_notebook(c, "Biologie 2.A"); new_note(c, "Fotosyntéza, hodina 14 s hodně dlouhým názvem pro kontrolu zalamování", [audio("a.ogg", 3000)])
        nid = wait_for(lambda: note_ids(), 15, "note")[0]; wait_for(lambda: f"jobs/{nid}/job.json" in inbox_files(), 15, "job")
        heartbeat(5, 1); sim_worker(nid, "done", speakers=True); sync_now(c)
        for w, h in ((360, 800), (768, 1000), (1280, 900)):
            p.set_viewport_size({"width": w, "height": h})
            for name, hash_ in (("home", "#/"), ("note", f"#/n/{nid}"), ("new", "#/new"), ("settings", "#/settings"), ("search", "#/search")):
                p.goto(BASE + hash_); time.sleep(0.4)
                sw = p.evaluate("document.documentElement.scrollWidth")
                assert sw <= w, f"horizontal overflow at {w}px on {name}: scrollWidth={sw}"
                p.screenshot(path=str(SHOTS / f"{name}-{w}.png"), full_page=True)
        p.emulate_media(color_scheme="dark"); p.goto(BASE + f"#/n/{nid}"); time.sleep(0.4); p.screenshot(path=str(SHOTS / "note-dark-360.png"), full_page=True)
        assert not c.problems, c.problems
    finally: c.close()

def test_screenshots_v2_overflow(pw):
    SHOTS.mkdir(exist_ok=True, parents=True)
    c = Ctx(pw, width=360, height=800); p = c.page
    try:
        nid = setup_done_note(c, photos=2, evil=True)
        for w, h in ((360, 800), (768, 1000), (1280, 900)):
            p.set_viewport_size({"width": w, "height": h})
            for name, hash_ in (("note-v2", f"#/n/{nid}"), ("edit", f"#/n/{nid}/edit"), ("new-v2", "#/new")):
                p.goto(BASE + hash_); time.sleep(0.8)
                sw = p.evaluate("document.documentElement.scrollWidth")
                assert sw <= w, f"horizontal overflow at {w}px on {name}: {sw}"
                p.screenshot(path=str(SHOTS / f"{name}-{w}.png"), full_page=True)
            p.goto(BASE + f"#/n/{nid}"); p.click("#exportBtn"); time.sleep(0.3)
            assert p.evaluate("document.documentElement.scrollWidth") <= w
            p.screenshot(path=str(SHOTS / f"export-dialog-{w}.png")); p.click("#exClose")
        assert not svg_noise_ok(c), c.problems
    finally: c.close()

def main():
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    fg.start(API_PORT, WEB_PORT, str(HERE / ".certs"))
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and flt in n]
    fails = 0
    with sync_playwright() as pw:
        for name, fn in tests:
            ctl("reset"); t0 = time.time()
            try: fn(pw); print(f"PASS {name} ({time.time() - t0:.1f}s)", flush=True)
            except Exception: fails += 1; print(f"FAIL {name} ({time.time() - t0:.1f}s)\n{traceback.format_exc()}", flush=True)
    print(f"\n{len(tests) - fails}/{len(tests)} passed"); sys.exit(1 if fails else 0)

if __name__ == "__main__": main()
