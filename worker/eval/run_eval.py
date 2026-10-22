#!/usr/bin/env python
"""Run the notes engine (REAL claude -p) on the eval inputs and save outputs + usage + rendered figures.

  python eval/run_eval.py [--variant NAME] [--passes 1|2] [--effort high] [--model ID] [input-name-prefix ...]

Outputs go to eval/out/<variant>/<input>.json (+ .usage.json, figures as <input>.fNN.svg/.png, and a Markdown view <input>.md
for reading). Opt-in: costs real quota (~0.05-0.4 USD per input)."""
import argparse, json, subprocess, sys, time, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from lesson_worker.config import Config          # noqa: E402
from lesson_worker import notes as N              # noqa: E402


def load_inputs(prefixes):
    files = sorted((HERE / "inputs").glob("*.json")) + (sorted((HERE / "inputs_long").glob("*.json")) if prefixes else [])
    return [json.loads(f.read_text()) for f in files if not prefixes or any(f.stem.startswith(p) for p in prefixes)]


def render_md(content, kind):
    L = [f"# {content['title']}", "", content["description"], ""]
    L.append("## Osnova")
    def pt(p, d=0):
        if isinstance(p, str):
            L.append("  " * d + "- " + p)
        else:
            L.append("  " * d + "- " + p["text"])
            for c in p["children"]:
                pt(c, d + 1)
    for s in content["outline"]:
        L.append(f"### {s['heading']}")
        for p in s["points"]:
            pt(p)
    L += ["", "## Klíčové body"]
    for k in content["key_points"]:
        L.append(f"- [{k['kind']}] " + (f"**{k['term']}**: " if "term" in k else "") + k["text"])
    for f in content.get("figures", []):
        L.append(f"\n![{f['title']}]({f['id']}.png) {f['caption']} (sekce: {f.get('refs', '-')})")
    for k in ("study_materials", "follow_up_materials"):
        if content.get(k):
            L += ["", "## Studijní materiály"]
            for m in content[k]:
                L.append(f"- [{m['kind']}] {m['title']}: {m['text']}" + (f"  -> {m['answer']}" if m.get("answer") else ""))
    if kind == "lesson":
        L += ["", "## Domácí úkoly"]
        for h in content["homework"]:
            L.append(f"- ({h['source']}) {h['task']}" + (f" - {h['details']}" if h.get("details") else "") + (f" [do: {h['due']}]" if h.get("due") else ""))
    else:
        L += ["", "## Rozhodnutí"] + [f"- {d}" for d in content["decisions"]]
        L += ["", "## Úkoly"] + [f"- {a['task']} (kdo: {a.get('owner')}, do: {a.get('due')})" for a in content["action_items"]]
    L += ["", "## Otevřené otázky"] + [f"- {q}" for q in content["open_questions"]]
    L += ["", "## Nejistoty"] + [f"- {u['text']} - {u['reason']}" for u in content["uncertainties"]]
    return "\n".join(L)


def one(inp, a, cfg, outdir):
    photos = [{"id": f"i{n:02d}", "path": HERE / "inputs" / "photos" / p, "caption": c} for n, (p, c) in enumerate(inp.get("photos", []), 1)]
    u = {}
    t = time.time()
    try:
        content, model = N.generate_notes(cfg, inp["kind"], inp["transcript"], context=inp["context"], language_mode=inp["language_mode"],
                                          notebook_name=inp["notebook"], images=photos, passes=a.passes, usage=u)
    except N.NotesError as e:
        print(f"{inp['name']}: FAILED {e.code} {e.message[:200]}")
        (outdir / f"{inp['name']}.error.txt").write_text(f"{e.code}: {e.message}")
        return None
    u["wall_s"] = round(time.time() - t, 1)
    u["words_in"] = len(inp["transcript"].split())
    (outdir / f"{inp['name']}.json").write_text(json.dumps(content, ensure_ascii=False, indent=1))
    (outdir / f"{inp['name']}.usage.json").write_text(json.dumps(u, ensure_ascii=False, indent=1))
    for f in content.get("figures", []):
        svg = outdir / f"{inp['name']}.{f['id']}.svg"
        svg.write_text(f["svg"])
        if shutil.which("rsvg-convert"):
            subprocess.run(["rsvg-convert", "-w", "1000", "-b", "white", "-o", str(svg.with_suffix(".png")), str(svg)], check=False)
    (outdir / f"{inp['name']}.md").write_text(render_md(content, inp["kind"]))
    print(f"{inp['name']}: ok {u['wall_s']}s ${u['cost_usd']} in={u['input_tokens']} out={u['output_tokens']} figs={len(content['figures'])} warn={len(u['warnings'])}")
    return u


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--variant", default="latest")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--effort", default="high")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()
    cfg = Config(claude_model=a.model, claude_timeout_s=1500, claude_attempts=2)
    cfg.claude_effort = a.effort
    cfg.validate_paths()
    cfg.cache_dir = Path("/tmp/claude-1000/scratch/eval-cache")
    outdir = HERE / "out" / a.variant
    outdir.mkdir(parents=True, exist_ok=True)
    ins = load_inputs(a.names)
    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(lambda i: one(i, a, cfg, outdir), ins))
    tot = sum(r["cost_usd"] for r in res if r)
    print(f"TOTAL ${tot:.3f} for {len([r for r in res if r])}/{len(ins)} inputs")
