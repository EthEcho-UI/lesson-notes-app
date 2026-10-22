"""Stub `claude` for the notes-engine tests: speaks stream-json (stdin: one user message, stdout: events + result).
Behaviour via <dir>/mode; every call is appended to <dir>/calls.jsonl (argv, env, cwd listing, stdin)."""
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def rd(name, default=""):
    try: return open(os.path.join(here, name)).read().strip()
    except OSError: return default
mode = rd("mode", "ok")
stdin = sys.stdin.read()
n = int(rd("count", "0")) + 1
open(os.path.join(here, "count"), "w").write(str(n))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "cwd_entries": os.listdir(os.getcwd()), "stdin": stdin}) + "\n")
def emit(result_obj):
    print(json.dumps({"type": "system", "subtype": "init", "tools": []}))
    print(json.dumps(result_obj))
if mode == "hang":
    import time; time.sleep(30)
if mode == "garbage":
    print("this is not json"); sys.exit(0)
if mode == "not_logged_in":
    emit({"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}); sys.exit(1)
if mode == "is_error":
    emit({"type": "result", "is_error": True, "result": "API Error: rate limit reached"}); sys.exit(1)
if "--json-schema" not in sys.argv:           # free-text call (condense step)
    emit({"type": "result", "is_error": False, "result": "- pracovni poznamky", "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}}); sys.exit(0)
schema = json.loads(sys.argv[sys.argv.index("--json-schema") + 1])
meeting = "decisions" in schema["properties"]
SVG_OK = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 500"><rect x="100" y="200" width="200" height="80" fill="#e3f2fd" stroke="#455a64"/>'
          '<text x="200" y="250" text-anchor="middle" font-family="sans-serif" font-size="18">Vstup</text>'
          '<rect x="500" y="200" width="200" height="80" fill="#e8f5e9" stroke="#455a64"/>'
          '<text x="600" y="250" text-anchor="middle" font-family="sans-serif" font-size="18">Výstup</text>'
          '<line x1="300" y1="240" x2="498" y2="240" stroke="#455a64" stroke-width="2"/></svg>')
c = {"title": "Testovací poznámky", "description": "První věta o hodině. Druhá věta o hodině.",
     "outline": [{"heading": "Úvod", "points": ["bod 1", {"text": "bod 2", "children": ["pod 2a"]}]}],
     "key_points": [{"text": "pojem je věc", "kind": "definition", "term": "pojem"}],
     "figures": [{"id": "f01", "title": "Schéma", "caption": "Vstup vede na výstup.", "svg": SVG_OK, "refs": "Úvod"}],
     "open_questions": [], "uncertainties": []}
if meeting:
    c["decisions"] = ["rozhodnutí A"]; c["action_items"] = [{"task": "udělat B", "owner": "Petra", "due": None}]
else:
    c["study_materials"] = [{"kind": "flashcard", "title": "Pojem", "text": "Co je pojem?", "answer": "Věc."}]
    c["homework"] = [{"task": "Doporučeno: zopakovat", "source": "suggested"}, {"task": "Strana 5", "source": "lesson", "due": "pondělí"}]
if mode == "evil_figure":
    c["figures"][0]["svg"] = SVG_OK.replace("<rect", '<script>alert(1)</script><rect', 1)
if mode == "bad_figure_xml":
    c["figures"][0]["svg"] = "<svg viewBox='0 0 800 500'><rect"
if mode == "urls":
    c["study_materials"][0]["text"] = "Viz https://example.com/x a www.seznam.cz pro víc."
if mode == "bad_schema":
    c.pop("outline")
if mode == "seven_figures":
    c["figures"] = [dict(c["figures"][0], id=f"f{i:02d}") for i in range(1, 8)]
if mode == "bad_order":
    c = dict(reversed(list(c.items())))
if mode == "review_fail" and "<draft_notes>" in stdin:
    emit({"type": "result", "is_error": True, "result": "overloaded"}); sys.exit(1)
if "<draft_notes>" in stdin:
    c["title"] = "Po revizi"
emit({"type": "result", "is_error": False, "result": json.dumps(c, ensure_ascii=False), "structured_output": c, "total_cost_usd": 0.05,
      "usage": {"input_tokens": 100, "output_tokens": 200}, "duration_ms": 1000, "modelUsage": {"claude-sonnet-5-5": {}}})
