# Notes engine evaluation (block b6)

Engine: `src/lesson_worker/notes.py`, prompt `prompts/notes_system_v2.md` (+ `notes_review_v1.md`), model claude-sonnet-5-5, `--effort high`.
Run: `python eval/run_eval.py --variant NAME [--passes 1|2] [prefix...]`, judge: `python eval/judge.py NAME`. Opt-in (real quota). Outputs in `eval/out/<variant>/` (git-ignored; small sample in `eval/out/sample/`).

## Inputs (eval/inputs, 7 + 1 long)
01 Czech physics ~2400 words (2 parts, planted: test date, homework, teacher slip, chatter, postponed question); 02 English history ~1900; 03 mixed cs/en informatics ~1500; 03b English class with Czech asides ~1600; 04 Czech meeting with speaker labels ~1400 (planted: a REJECTED proposal, an action item without owner/date); 05 maths lecture + 2 generated whiteboard photos (the example numbers exist only in the photos); 06 REAL faster-whisper (small, noisy) output on 24 FLEURS Czech clips (449 words, unrelated sentences, heavily garbled); 07 long ~30.7k words (~3 h) Czech history.
HONEST CAVEAT: 01-05 and 07 were drafted by Claude to my spec (with planted traps and ASR-style errors), not recorded by real teachers; they are cleaner and denser than real classes. Only 06 is real ASR. The `traps` lists in the input files were produced by the same drafting call and are only a review aid.

## Method and rubric
My own reading of every output (faithfulness, structure per contract, usefulness, Czech quality, figure quality, homework/study usefulness, hallucination check) plus an independent LLM judge (claude-opus-5-5, different model than the writer; `judge.py`) that compares notes with transcript/photos and lists unsupported claims, omissions, Czech problems. The judge is lenient on scores (almost all 5/5) so I use its concrete findings, not the scores. Contract validity is checked mechanically (schema + contract `validate.py` rules) on every run: 0 contract failures in all runs.

## Round 1 (prompt v1, single pass, $0.74 for 7 inputs; $0.07-0.15 each)
Verdict: already good. Real failures found (me + judge):
1. Off-topic chatter in the notes (projector, school trip in the middle of the physics outline, "projektor nikdo nemá zapínat..." as a point and as an open question). 
2. Teacher's corrected slip written into the outline ("Přeřek učitele ...").
3. Embellishment (minor, 3-5 per lecture): "třídní učitelce" (gender invented), "v učebnici", plural "zákazníci" for singular, "všichni souhlasí" when one person agreed, conditional promise stated as certain, date attached to an undated event inside a figure (Russian mobilisation on 28 July).
4. Test announcement filed as homework with source "lesson" (not assigned).
5. Garbage-in confidence (real ASR, 06): garbled words repaired into confident sentences without a flag ("obrovské čelisti", "vyrovnaná týmová spolupráce", "pacient", two unrelated sentences merged).
6. Paranoid/unneeded uncertainties (rounded numbers, "who is S1", clear numbers flagged "ASR may have garbled"), first-person uncertainties ("četl jsem").
7. Czech: English labels in English-lecture notes ("Teacher:"), needless English glosses ("fotosyntéza (photosynthesis)"), invented "ask the teacher" open questions, notes 90-150% of transcript length (outline 35-50%).
Fixes in prompt v2: rules for off-topic/organisational content (one short section at the end), no recording of corrected slips, no unfounded attributions, rules 3b (garbled ASR: leave out or mark "(nejasné)" + uncertainty, never add words) and 3c (no embellishment, conditional stays conditional, test is not homework), English glosses only where the lecturer used them/field standard, impersonal uncertainty wording, open_questions only if actually raised, uncertainties 0-6 and what not to list, outline 20-35% (max 40%) of transcript words, figure facts must be supported.

## Round 2 (prompt v2): single pass $0.05-0.16 each (mean ~0.11), 2-pass (draft + review) $0.28-0.30 for the three long inputs
Judge findings (minor only, 0 major in all runs): 1-pass 01: 4, 02: 3, 03: 2, 05: 2, 06: 4 (garbled real ASR, now flagged in most places); 2-pass 01: 2, 02: 1, 03: 1. Chatter and slip problems from round 1 are gone; the remaining ones are small paraphrase drifts ("nikoho" for "nothing", dropped "probably"), one derived number not marked supplementary, "dotek"/Czech phrasing nits.
Two-pass vs single pass: the review pass removed roughly half of the remaining minor unsupported claims but doubles the cost and time (~2x; no new major issues found by the judge in either), so **single pass is the default** (`DEFAULT_PASSES = 1`); `cfg.claude_notes_passes = 2` enables it (recommended for exam-important notes). Limitation: only 3 inputs were compared (the other 2-pass runs were cut by a usage limit and fell back to the draft, as designed), so the difference is indicative, not statistically significant.

## Figures (rendered to PNG and looked at)
Eval produced 1 figure each for 01, 02, 03, 03b, 05 and 0 for 04/06 (sensible: no diagram helps a meeting; the unrelated sentences). Quality: clean, readable, correct and genuinely useful in 5 of 5: action/reaction two boxes with arrows; WWI July crisis timeline; under/overfitting comparison; second conditional scheme; three parabolas for D>0/D=0/D<0 (best one). Weak points: simple layouts (boxes and arrows, no data plots), occasional wasted space (timeline), one unsupported date in round 1 (fixed by the rule). The layout lint (`figcheck.py`, estimated text boxes) never fired on these; it exists to drop figures with text off canvas or heavy label overlap. The long lecture gave 3 figures, all plausible. I did not see a broken figure in 8 samples, so the drop-rate cannot be estimated; the safety net is tested with stub data only.

## Photos
Photos are passed as base64 image blocks over `--input-format stream-json` with `--tools ""` (works on 2.1.306; no Read tool, no add-dir, no filesystem access). Input 05: the model read a=2,b=-5,c=3, D=25-24=1 and the roots from the whiteboard photo although the transcript never says them, noticed that the two photos used a different root order (my generator's inconsistency, fixed after round 1) and flagged it. 

## Long input (30.7k words ~3 h, single pass)
Works in one call (97k input tokens, 96k output tokens, 630 s, $1.35); 41 outline sections (36% of transcript words), 15 study items, 3 figures, valid. Because it is slow the engine raises the per-call timeout to 180 s + 0.05 s/word above 3000 words (cfg default 900 s would be borderline). A map-reduce condense step exists for > 80k words (tested with the stub only, not with real claude). Quality of that output was only spot-checked (not judged).

## Cost per lecture (list-price equivalent from `total_cost_usd`; on a subscription it consumes quota)
5-25 min: $0.05-0.16 (single pass), ~$0.3 with review pass; 3 h: ~$1.35. Wall time 35-120 s (single), up to ~10 min for 3 h.

## Scores (mine, 1-5; round 2 single pass, mean over 01-06)
faithfulness 4.5, structure per contract 5, usefulness 4.5, Czech quality 4 (occasionally stiff or colloquial phrasing), figure quality 4, homework/study usefulness 4.5, hallucination control 4.5 on clean transcripts and 3.5 on the garbled real ASR (06: flags most but not all guesses).
Not verified: behaviour on real classroom recordings, on non-Czech-speaker accents, handwriting photos (my photos are rendered fonts), prompt-injection beyond one opt-in real test and stub plumbing tests.
