"use strict";
(function () {
  const $ = (id) => document.getElementById(id);
  const el = {
    pill: $("pill"), what: $("what"), clock: $("clock"), bar: $("bar"), peak: $("peak"), warnings: $("warnings"), capture: $("capture"),
    start: $("btn-start"), pause: $("btn-pause"), stop: $("btn-stop"), existing: $("existing"), notebook: $("notebook"), title: $("title"),
    banner: $("banner"), worker: $("worker"), notes: $("notes"), notesmsg: $("notesmsg"), outbox: $("outbox"), hint: $("meetinghint"),
    refresh: $("refresh"), langmode: $("langmode"), topic: $("topic"), details: $("details"), photofile: $("photofile"), photolist: $("photolist"),
  };
  let rec = { state: "idle" };
  let remote = { notes: [], notebooks: [] };
  let busy = false;
  let photos = [];          // {id, name, bytes} already uploaded to the local server, waiting for Start

  function fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    return Math.floor(s / 3600) + ":" + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }
  function banner(msg, isError) {
    el.banner.hidden = !msg;
    el.banner.textContent = msg || "";
    el.banner.className = "banner" + (isError ? " error" : "");
  }
  async function api(path, body) {
    const opt = body === undefined ? { method: "GET" } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(path, Object.assign({ credentials: "same-origin", cache: "no-store" }, opt));
    let data = {};
    try { data = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok || data.ok === false) throw new Error(data.error || ("HTTP " + r.status));
    return data;
  }
  function kind() { return document.querySelector("input[name=kind]:checked").value; }

  // ---- rendering ------------------------------------------------------------------------------
  function renderRec() {
    const s = rec.state || "idle";
    const active = ["starting", "recording", "paused", "stopping"].includes(s);
    el.pill.textContent = rec.orphan ? "interrupted" : s;
    el.pill.className = "pill " + (rec.orphan ? "paused" : s);
    el.what.textContent = active ? (rec.title || "") + (rec.mode ? "  -  " + rec.mode : "") : "";
    el.clock.textContent = fmt(rec.elapsed);
    el.start.disabled = busy || active;
    el.pause.disabled = busy || !(s === "recording" || s === "paused");
    el.pause.textContent = s === "paused" ? "Resume" : "Pause";
    el.stop.disabled = busy || !(s === "recording" || s === "paused");
    el.warnings.replaceChildren(...(rec.warnings || []).map((w) => { const d = document.createElement("div"); d.textContent = w; return d; }));
    const c = rec.capture || {};
    el.capture.textContent = active && (c.mic || c.monitor)
      ? "capturing: " + [c.mic && ("mic " + c.mic), c.monitor && ("system " + c.monitor)].filter(Boolean).join("  +  ") + (rec.capture_restarts ? "  (followed device changes: " + rec.capture_restarts + ")" : "")
      : "";
    document.querySelectorAll("#form input, #form select, #form textarea").forEach((x) => { x.disabled = active; });
    if (!active) lockForExisting();
    if (rec.last && !rec.last.ok && s === "idle" && rec.last.message) banner("Last recording was not saved: " + rec.last.message, true);
  }
  function renderLevel(live) {
    const db = live && rec.state === "recording" ? live.rms_db : -120;
    const pk = live && rec.state === "recording" ? live.peak_db : -120;
    const pct = (v) => Math.min(100, Math.max(0, ((v + 60) / 60) * 100));
    el.bar.style.width = pct(db) + "%";
    el.peak.style.left = "calc(" + pct(pk) + "% - 3px)";
    if (live && typeof live.elapsed === "number" && rec.state === "recording") el.clock.textContent = fmt(live.elapsed);
  }
  function renderRemote() {
    const nb = remote.notebooks || [];
    const keep = el.notebook.value;
    el.notebook.replaceChildren(...nb.map((n) => new Option(n.name, n.id)));
    if (keep && nb.some((n) => n.id === keep)) el.notebook.value = keep;
    else if (remote.default_notebook_id) el.notebook.value = remote.default_notebook_id;
    const keepEx = el.existing.value;
    el.existing.replaceChildren(new Option("a new note", ""), ...(remote.notes || []).slice(0, 25).map((n) => new Option(n.title + "  (" + n.kind + ", " + n.parts + " part" + (n.parts === 1 ? "" : "s") + ")", n.id)));
    el.existing.value = (remote.notes || []).some((n) => n.id === keepEx) ? keepEx : "";
    lockForExisting();
    const w = remote.worker;
    el.worker.className = "worker" + (w && w.offline ? " bad" : "");
    el.worker.textContent = !remote.online ? "GitHub unreachable (" + (remote.error || "offline") + ") - showing cached data"
      : !w ? "worker: no heartbeat yet" : "worker: " + (w.offline ? "OFFLINE" : w.state) + ", last seen " + w.age + (w.jobs_pending ? ", " + w.jobs_pending + " job(s) queued" : "");
    el.notes.replaceChildren(...(remote.notes || []).slice(0, 15).map((n) => {
      const li = document.createElement("li");
      const t = document.createElement("div"); t.className = "t"; t.textContent = n.title;
      const m = document.createElement("div"); m.className = "m"; m.textContent = [n.notebook, n.kind, n.created, n.parts + " part" + (n.parts === 1 ? "" : "s"), fmt(n.seconds)].join("  |  ");
      const st = document.createElement("div"); st.className = "s " + (n.state === "done" ? "done" : n.state === "failed" ? "failed" : n.state ? "busy" : "");
      st.textContent = n.state || "waiting"; st.title = n.error || "";
      li.append(t, m, st);
      return li;
    }));
    el.notesmsg.textContent = (remote.notes || []).length ? "" : "No notes yet.";
  }
  function renderOutbox() {
    const items = rec.outbox || [];
    el.outbox.replaceChildren(...items.map((o) => {
      const d = document.createElement("div"); d.className = "outbox";
      d.textContent = "Saved on this laptop, not yet announced to the worker (" + fmt(o.seconds) + "): " + (o.last_error || o.state);
      return d;
    }));
  }
  function lockForExisting() {
    if (rec.state && rec.state !== "idle") return;
    const ex = el.existing.value;
    const n = (remote.notes || []).find((x) => x.id === ex);
    const lock = !!n;
    el.notebook.disabled = lock; el.title.disabled = lock;
    document.querySelectorAll("input[name=kind]").forEach((x) => { x.disabled = lock; });
    if (n) {
      if (n.language_mode) el.langmode.value = n.language_mode;
      if (n.context && document.activeElement !== el.topic && document.activeElement !== el.details) { el.topic.value = n.context.topic || ""; el.details.value = n.context.details || ""; }
      el.notebook.value = n.notebook_id; el.title.value = n.title;
      document.getElementById("kind-" + n.kind).checked = true;
    }
    el.hint.hidden = kind() !== "meeting";
  }

  // ---- actions --------------------------------------------------------------------------------
  async function act(fn) {
    busy = true; renderRec(); banner("");
    try { await fn(); } catch (e) { banner(e.message, true); }
    busy = false; await loadState();
  }
  el.start.addEventListener("click", () => act(async () => {
    const body = { kind: kind(), notebook_id: el.notebook.value, title: el.title.value, note_id: el.existing.value || null,
      language_mode: el.langmode.value, topic: el.topic.value, details: el.details.value, photo_ids: photos.map((p) => p.id) };
    await api("/api/start", body);
    photos = []; renderPhotos(); el.topic.value = ""; el.details.value = "";
  }));
  el.stop.addEventListener("click", () => act(async () => {
    const r = await api("/api/stop", {});
    const last = r.last || {};
    if (last.ok) banner("Saved " + fmt(last.seconds) + " of audio" + (last.submit ? " - " + last.submit.message : "") + ".", false);
    await loadRemote(true);
  }));
  el.pause.addEventListener("click", () => act(() => api(rec.state === "paused" ? "/api/resume" : "/api/pause", {})));
  el.refresh.addEventListener("click", () => loadRemote(true));
  el.existing.addEventListener("change", lockForExisting);
  document.querySelectorAll("input[name=kind]").forEach((x) => x.addEventListener("change", lockForExisting));

  function renderPhotos() {
    el.photolist.replaceChildren(...photos.map((p) => {
      const li = document.createElement("li");
      li.textContent = p.name + " (" + Math.round(p.bytes / 1024) + " KiB) ";
      const b = document.createElement("button"); b.type = "button"; b.className = "small"; b.textContent = "remove";
      b.addEventListener("click", async () => { try { await api("/api/photo/remove", { id: p.id }); } catch (e) { /* gone already */ } photos = photos.filter((x) => x.id !== p.id); renderPhotos(); });
      li.append(b);
      return li;
    }));
  }
  function readB64(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] || "");
      r.onerror = () => reject(new Error("cannot read " + file.name));
      r.readAsDataURL(file);
    });
  }
  el.photofile.addEventListener("change", async () => {
    const files = Array.from(el.photofile.files || []);
    el.photofile.value = "";
    for (const f of files) {
      try { photos.push((await api("/api/photo", { name: f.name, data: await readB64(f) })).photo); renderPhotos(); }
      catch (e) { banner(f.name + ": " + e.message, true); }
    }
  });

  async function loadState() {
    try { rec = (await api("/api/state")).recorder; renderRec(); renderOutbox(); } catch (e) { banner("Lost contact with the local recorder UI server: " + e.message, true); }
  }
  async function loadRemote(force) {
    try { remote = (await api("/api/remote" + (force ? "?force=1" : ""))).remote; renderRemote(); } catch (e) { el.notesmsg.textContent = e.message; }
  }

  // ---- live updates over a websocket (level meter, clock) ------------------------------------------------
  function connect() {
    const ws = new WebSocket("ws://" + location.host + "/ws");
    ws.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.recorder) { const prev = rec.state; rec = Object.assign({}, rec, m.recorder); if (prev !== rec.state || m.tick) renderRec(); renderOutbox(); }
      renderLevel(m.live);
    };
    ws.onclose = () => { renderLevel(null); setTimeout(connect, 1500); };
  }
  loadState().then(() => loadRemote(false));
  connect();
  setInterval(loadState, 5000);
  setInterval(() => loadRemote(false), 20000);
})();
