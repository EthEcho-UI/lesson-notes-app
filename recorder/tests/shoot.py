"""Drives the real UI in headless Chromium with real clicks and takes screenshots.
Run with a python that has Playwright (the app's tests/.venv): shoot.py <url-with-token> <outdir>. Exit code != 0 on failure."""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

url, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
csp_violations = []


def until(page, expr, timeout=15000):
    """Poll a JS expression with page.evaluate (CDP): wait_for_function evals a string in the page, which the strict CSP rightly forbids."""
    end = time.time() + timeout / 1000
    while time.time() < end:
        if page.evaluate(expr):
            return
        time.sleep(0.1)
    raise TimeoutError(f"timeout waiting for: {expr}")

with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 820, "height": 1180}, color_scheme="light")
    page = ctx.new_page()
    page.on("console", lambda m: csp_violations.append(m.text) if "Content Security Policy" in m.text or m.type == "error" else None)
    page.on("pageerror", lambda e: csp_violations.append(str(e)))
    page.on("response", lambda r: print("resp", r.status, r.url.split("?")[0], r.request.post_data or "") if "/api/" in r.url and r.request.method == "POST" else None)
    page.goto(url)
    assert "?t=" not in page.url, "token must be removed from the address bar by the redirect"
    page.wait_for_selector("#notes li")
    until(page, "document.getElementById('worker').textContent.includes('worker')")
    assert page.text_content("#pill") == "idle"
    page.screenshot(path=str(out / "ui-idle.png"), full_page=True)

    page.select_option("#notebook", label="Biologie 2.A")
    page.fill("#title", "Mitoza - cviceni")
    page.check("#kind-lesson", force=True)
    page.click("#btn-start")
    try:
        until(page, "document.getElementById('pill').textContent === 'recording'", 15000)
    except Exception:
        print("banner:", page.text_content("#banner"), "| pill:", page.text_content("#pill"), "| start disabled:", page.is_disabled("#btn-start"), "| console:", csp_violations)
        raise
    time.sleep(3.2)
    until(page, "parseFloat(document.getElementById('bar').style.width) > 20", 5000)     # live meter moves
    clock = page.inner_text("#clock")
    assert clock != "0:00:00", clock
    assert page.is_disabled("#btn-start") and page.is_enabled("#btn-stop") and page.is_disabled("#title")
    page.screenshot(path=str(out / "ui-recording.png"), full_page=True)

    page.click("#btn-pause")
    until(page, "document.getElementById('pill').textContent === 'paused'", 8000)
    assert page.inner_text("#btn-pause") == "Resume"
    page.screenshot(path=str(out / "ui-paused.png"), full_page=True)
    page.click("#btn-pause")
    until(page, "document.getElementById('pill').textContent === 'recording'", 8000)
    time.sleep(1.2)
    page.click("#btn-stop")
    until(page, "document.getElementById('pill').textContent === 'idle'", 30000)
    until(page, "document.getElementById('banner').textContent.startsWith('Saved')", 10000)
    until(page, "[...document.querySelectorAll('#notes .t')].some(e => e.textContent === 'Mitoza - cviceni')", 15000)
    page.screenshot(path=str(out / "ui-saved.png"), full_page=True)

    # a meeting, "add to existing note" and the small-screen / dark layouts
    page.select_option("#existing", label="Mitoza - cviceni  (lesson, 1 part)")
    assert page.is_disabled("#notebook") and page.input_value("#title") == "Mitoza - cviceni"
    page.set_viewport_size({"width": 390, "height": 900})
    ctx2 = b.new_context(viewport={"width": 390, "height": 900}, color_scheme="dark")
    m = ctx2.new_page()
    ctx2.add_cookies(ctx.cookies())   # the launch link is single-use: reuse the session the first context obtained
    m.goto(url.split("?")[0])
    m.wait_for_selector("#notes li")
    m.screenshot(path=str(out / "ui-mobile-dark.png"), full_page=True)
    b.close()

bad = [c for c in csp_violations if "favicon" not in c]
if bad:
    print("console errors:", bad)
    sys.exit(2)
print("ui ok")
