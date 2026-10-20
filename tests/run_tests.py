#!/usr/bin/env python3
"""End-to-end tests: Chromium (Playwright) + fake GitHub API. Run: tests/.venv/bin/python tests/run_tests.py [name-filter]
Serial on purpose (one browser, one context at a time)."""
import json, os, pathlib, re, subprocess, sys, time, traceback, urllib.request, copy
from jsonschema import Draft202012Validator
from playwright.sync_api import sync_playwright
sys.path.insert(0, os.path.dirname(__file__))
import fake_github as fg

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
CONTRACT = pathlib.Path(os.environ.get("CONTRACT_DIR", REPO.parent / "contract"))
SHOTS = pathlib.Path(os.environ.get("SHOTS_DIR", REPO.parent / "b4-screens"))
API_PORT, WEB_PORT = 18443, 18080
BASE = f"http://127.0.0.1:{WEB_PORT}/web/"
TOKEN = fg.TOKEN

# ---- contract validation (schemas + semantic rules from contract/validate.py) -------------------------
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

def login(c, token=TOKEN):
    p = c.page
    p.goto(BASE + "#/settings")
    p.fill("#tokenIn", token); p.click("#saveTokenBtn")
    wait_for(lambda: "Ulo" in p.inner_text("#connMsg"), 5, "token saved")

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
    c.page.goto(BASE + "#/settings"); c.page.click("#syncBtn")
    time.sleep(0.8)

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
        p.goto(BASE + f"#/n/{nid}"); p.check("#withTr"); p.click("#copyBtn")
        md = wait_for(lambda: p.evaluate("navigator.clipboard.readText()"), 5, "clipboard")
        assert md.startswith("# Fotosyntéza hodina 14") and "## Shrnutí" in md and "Učitel" in md and "## Přepis" in md, md[:300]
        with p.expect_download() as dl: p.click("#exportBtn")
        assert dl.value.suggested_filename.endswith(".md")
        # regenerate
        p.click("#regenBtn"); wait_for(lambda: json.loads(inbox_files()[f"jobs/{nid}/job.json"])["action"] == "regenerate_notes", 10, "regenerate job")
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
