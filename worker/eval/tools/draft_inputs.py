"""Draft the synthetic eval transcripts. They are written by Claude (claude-sonnet-5-5) to a detailed spec with PLANTED
facts and ASR errors (so hallucination / correction can be checked against `traps`), then stored in eval/inputs/.
Usage: python tools/draft_inputs.py [name ...]   (needs a logged-in claude; ~0.1 USD each)"""
import json, subprocess, sys, tempfile, os
from pathlib import Path
HERE = Path(__file__).resolve().parents[1]
SCHEMA = {"type": "object", "properties": {"transcript": {"type": "string"}, "traps": {"type": "array", "items": {"type": "string"}}},
          "required": ["transcript", "traps"]}
COMMON = ("You write TEST DATA for a note-taking app: a realistic automatic-speech-recognition (Whisper) transcript of a recorded class or meeting. "
          "Requirements: spoken style (hesitations, repetitions, digressions, jokes, classroom management such as 'tak pojďme dál', students interrupting with questions), "
          "realistic ASR noise: ~2-4 percent of words wrong (homophones, split/merged words, misspelled names and technical terms, numbers written as words or misheard, "
          "missing diacritics sometimes), sloppy punctuation, NO speaker labels unless stated, NO markdown. The recording is split in recording parts: start each part with a line "
          "'[Část N]'. Do not mention that it is test data. Return JSON: transcript (the full text; it must reach the requested length - write it all out, do not abbreviate) and "
          "traps (a list of short strings describing the PLANTED facts, errors and pitfalls, so a reviewer can check whether notes got them right, e.g. 'teacher says test on Friday 24.10. covering chapters 3 and 4', "
          "'ASR wrote Nyuton instead of Newton', 'teacher misspeaks X then corrects to Y', 'number 9,81 heard as devět osmdesát jedna').\n\nSPEC:\n")
SPECS = {
 "01_cs_physics": dict(kind="lesson", language_mode="cs", notebook="Fyzika", context={"subject": "Fyzika", "topic": "Newtonovy pohybové zákony a síla", "details": "1. ročník gymnázia, kapitola 3-4. Učitel pan Novák."},
   spec="Czech physics lecture, about 3000 words (~25 minutes), 2 recording parts. Content: setrvačnost (1st law), F = m a (2nd law, unit newton), akce a reakce (3rd law), tíhová síla F = m g with g = 9,81 m/s2 (one worked example: 5 kg object -> about 49 N; ASR garbles one number), "
        "a trolley example with a=2 m/s2 and m=3 kg -> 6 N, a car braking example. Planted: teacher states the test is on Friday 24. 10. (chapters 3 and 4, 'to bude určitě u testu: třetí Newtonův zákon'); homework: page 57 exercises 3, 5 and 8 in the textbook, due Monday; "
        "the teacher misspeaks once ('síla je hmotnost děleno zrychlení') and corrects himself; 8 minutes total off-topic chatter (school trip, broken projector, a student arriving late); one student question ('a jak to je s vesmírem, tam není tření?') that the teacher postpones to next lesson; "
        "ASR errors: 'Nyuton', 'zrychlení' as 'zrychlení', 'setrvačnost' as 'setrvačnost' sometimes 'sedr vačnost', 'newton' as 'nyuton', 'kilogram' split, 'gé' for g."),
 "02_en_history": dict(kind="lesson", language_mode="en", notebook="History", context={"subject": "History", "topic": "Causes of World War I", "details": "Year 11 class, Ms Clarke. Class taught in English."},
   spec="English history lecture, about 1600 words, 1 part. Content: the MAIN causes of WWI as in the classic acronym (militarism, alliances, imperialism, nationalism), the alliance system (Triple Entente vs Triple Alliance), assassination of Archduke Franz Ferdinand in Sarajevo on 28 June 1914, the July Crisis, German 'blank cheque', Schlieffen Plan, Britain entering after the invasion of Belgium on 4 August 1914. "
        "Planted: homework = 800-word essay 'Which cause was the most important?' due Monday 3 November; a quiz on the alliance system next Tuesday; teacher says 'remember, the date of the assassination will definitely be in the exam'; teacher misspeaks 'Franz Ferdinand was the Kaiser's nephew' then corrects (he was the heir to the Austro-Hungarian throne); "
        "ASR errors: 'Serbia' as 'Siberia' once, 'Gavrilo Princip' as 'Gabriel Principe', 'Schlieffen' as 'Schlifen', 'entente' as 'on-tont'; chatter about the school cafeteria."),
 "03_mixed_cs_informatics": dict(kind="lesson", language_mode="mixed", notebook="Informatika", context={"subject": "Informatika", "topic": "Úvod do strojového učení (machine learning)", "details": "Učitelka mluví česky a přeskakuje do angličtiny u termínů a občas celé věty z prezentace."},
   spec="Mixed Czech/English computer-science lecture, about 1800 words, 1 part: Czech teacher, many English terms embedded in Czech sentences and some full English sentences read from slides. Content: supervised vs unsupervised learning, training set / test set split (80/20), overfitting vs underfitting, loss function, gradient descent and learning rate, a simple example: predicting house prices (linear regression), accuracy vs precision/recall briefly. "
        "Planted: homework 'projít notebook linear_regression.ipynb a změnit learning rate na 0.01 a 0.1 a porovnat'; project deadline 15. listopadu; ASR errors on English terms spoken inside Czech ('overfitting' as 'over fitting', 'gradient descent' as 'gradient the sent', 'learning rate' as 'learning rate'); some English segments are transcribed with Czech phonetics ('trejnink set'); the teacher says '80 procent trénovací, 20 procent testovací'; she misspeaks once that more data always means no overfitting, then clarifies it helps but does not eliminate it."),
 "03b_mixed_english_class": dict(kind="lesson", language_mode="mixed", notebook="Angličtina", context={"subject": "Angličtina", "topic": "Second conditional and wishes", "details": "English-language class, the teacher teaches in English with Czech asides to explain. Students are B1."},
   spec="English-class lecture, about 1300 words, 1 part. Mostly English teaching (second conditional: If + past simple, would + infinitive; examples: 'If I had a million dollars, I would travel the world', 'If I were you, I would study more'; 'were' for all persons; wish + past simple), with Czech asides explaining (e.g. 'to je jako kdyby v češtině: kdybych byl bohatý, koupil bych...') and a Czech warning about a common mistake ('nikdy neříkáme would v if-části'). "
        "Planted: homework: workbook page 64 exercise 3 and write 6 sentences with 'If I were...'; vocabulary test next Thursday on unit 5 words (list: reluctant, afford, behaviour, nowadays); ASR errors: 'conditional' as 'condition all', Czech words with wrong diacritics, 'would' as 'wood' twice."),
 "04_meeting_cs": dict(kind="meeting", language_mode="cs", notebook="Porady", context={"subject": "Týdenní porada vývojového týmu", "topic": "Plán vydání verze 2.4 a oprava chyb", "details": "Účastníci: Petra (vedoucí), Honza (backend), Lucie (testerka). Řečníci jsou označeni [S1]..[S3] automaticky."},
   spec="Czech software-team meeting, about 1400 words, 1 part, WITH speaker labels like '[S1] ...' at the start of turns (automatic, so occasionally a turn is attributed to the wrong speaker). Participants speak by first names (Petra, Honza, Lucie) in conversation. Content: release 2.4 date, blocker bug in login, decision to postpone the new dashboard to 2.5, decision to use PostgreSQL 16 for staging, budget question left open, Honza to fix the login bug by Wednesday 29. 10., Lucie to prepare regression tests by Friday, Petra to ask the customer about the contract extension (no date given). "
        "Planted: one decision is first proposed then REJECTED (so it must NOT appear as decided: 'přepsat modul plateb v Rustu' rejected); one action item has no owner and no date ('někdo by měl aktualizovat dokumentaci'); chatter about lunch; ASR errors: 'PostgreSQL' as 'postgres kvéél', 'staging' as 'stejdžing', names 'Honza' as 'Honza' ok, 'Lucie' as 'Lucie'."),
 "05_photos_math": dict(kind="lesson", language_mode="cs", notebook="Matematika", context={"subject": "Matematika", "topic": "Kvadratické rovnice a parabola", "details": "Učitel píše na tabuli. Dvě fotky tabule jsou přiložené."},
   spec="Czech maths lecture, about 1100 words, 1 part. Teacher solves a quadratic equation on the board and constantly says 'jak vidíte tady' and 'tenhle příklad' WITHOUT speaking the numbers of the example (they are only on the whiteboard photo), explains the discriminant D = b^2 - 4ac, the formula x1,2 = (-b +- sqrt(D)) / 2a, the three cases D>0, D=0, D<0 (two, one, no real roots), then sketches a parabola with vertex and roots and says the graph is on the second photo. "
        "Planted: the numbers 2x^2 - 5x + 3 = 0, D = 1, roots 1.5 and 1 exist ONLY in the photo (the transcript never says them, at most 'dvě x na druhou...' garbled); homework: workbook exercises 12 and 14 on page 31, due next Wednesday; the teacher says 'diskriminant se na písemce ptá vždycky'; ASR errors: 'diskriminant' as 'diskriminat', 'parabola' as 'para bola', 'odmocnina' as 'od možnina'."),
}

def run(name):
    sp = SPECS[name]
    prompt = COMMON + sp["spec"]
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run([os.path.expanduser("~/.local/bin/claude"), "-p", "--model", "claude-sonnet-5-5", "--effort", "medium", "--output-format", "json",
                            "--json-schema", json.dumps(SCHEMA), "--tools", "", "--setting-sources", "project", "--strict-mcp-config",
                            "--disable-slash-commands", "--no-session-persistence", "--system-prompt", "You write realistic test data."],
                           input=prompt, capture_output=True, text=True, cwd=cwd, timeout=1500, env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"})
    d = json.loads(r.stdout)
    out = d["structured_output"]
    inp = {"name": name, "kind": sp["kind"], "language_mode": sp["language_mode"], "notebook": sp["notebook"], "context": sp["context"],
           "transcript": out["transcript"], "photos": [], "traps": out["traps"], "synthetic": True}
    (HERE / "inputs" / f"{name}.json").write_text(json.dumps(inp, ensure_ascii=False, indent=1))
    print(name, len(out["transcript"].split()), "words", round(d["total_cost_usd"], 3), "USD")

if __name__ == "__main__":
    from concurrent.futures import ThreadPoolExecutor
    names = sys.argv[1:] or list(SPECS)
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(run, names))
