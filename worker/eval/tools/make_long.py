"""Build eval input 07: a ~3 hour Czech lecture (~25-30k words) from 6 independently drafted ~4500-word parts (Claude, spec-driven,
realistic ASR noise), joined with [Část N] markers. Stress test for length/cost. Usage: python tools/make_long.py"""
import json, os, subprocess, tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
HERE = Path(__file__).resolve().parents[1]
TOPICS = ["Francouzská revoluce 1789-1792: příčiny, generální stavy, stržení Bastilly, Deklarace práv člověka a občana",
          "Radikalizace revoluce 1792-1794: monarchie padá, jakobíni, Robespierre, teror, gilotina, Direktorium",
          "Napoleon Bonaparte: vzestup, státní převrat 1799, Code civil, korunovace 1804, Austerlitz 3. prosince 1805",
          "Napoleonské války: kontinentální blokáda, Španělsko, tažení do Ruska 1812, bitva národů u Lipska 1813",
          "Pád Napoleona: Elba, Waterloo 18. června 1815, Svatá Helena, Vídeňský kongres, Metternich",
          "Důsledky: Evropa po roce 1815, restaurace, nacionalismus a liberalismus, vliv na české země, shrnutí a opakování před písemkou"]
def part(i):
    prompt = ("Write a realistic ASR (Whisper) transcript of a Czech history teacher lecturing (part %d of 6 of a 3 hour double-lesson block), about 4500 words, topic: %s. Spoken style, digressions, student questions, "
              "~3 percent ASR errors (misheard names, split words), sloppy punctuation, no markdown, no speaker labels. Write all ~4500 words out in full; do not abbreviate or summarise. Plain text only, no preface." % (i + 1, TOPICS[i]))
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([os.path.expanduser("~/.local/bin/claude"), "-p", "--model", "claude-sonnet-5-5", "--effort", "low", "--output-format", "json", "--tools", "", "--setting-sources", "project",
                            "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--system-prompt", "You write realistic test data."],
                           input=prompt, capture_output=True, text=True, cwd=cwd, timeout=1800, env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"})
    return json.loads(r.stdout)["result"]
with ThreadPoolExecutor(3) as ex:
    parts = list(ex.map(part, range(6)))
text = "\n".join(f"[Část {i + 1}]\n{p.strip()}" for i, p in enumerate(parts))
inp = {"name": "07_long_cs_history_3h", "kind": "lesson", "language_mode": "cs", "notebook": "Dějepis", "context": {"subject": "Dějepis", "topic": "Francouzská revoluce a Napoleon", "details": "Dvojhodinovka + blok, 6 nahrávek."},
       "transcript": text, "photos": [], "synthetic": True, "traps": []}
(HERE / "inputs_long").mkdir(exist_ok=True)
(HERE / "inputs_long" / "07_long_cs_history_3h.json").write_text(json.dumps(inp, ensure_ascii=False))
print(len(text.split()), "words")
