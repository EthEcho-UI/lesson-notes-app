from lesson_worker import diar, transcript


def test_merged_text_markers_and_speakers():
    parts = [{"order": 1, "segments": [{"start": 0, "end": 1, "text": "Ahoj.", "speaker": "S1"}, {"start": 1, "end": 2, "text": "Jak se mas?", "speaker": "S1"},
                                       {"start": 2, "end": 3, "text": "Dobre.", "speaker": "S2"}]},
             {"order": 2, "segments": [{"start": 0, "end": 1, "text": "Dalsi cast."}]},
             {"order": 3, "segments": []}]
    assert transcript.merged_text(parts) == "[Část 1]\n[S1] Ahoj. Jak se mas? [S2] Dobre.\n[Část 2]\nDalsi cast.\n[Část 3]"


def test_build_replaces_same_part_and_recomputes_offsets():
    old = {"parts": [{"part_id": "p01", "order": 1, "duration": 10.0, "offset": 0, "segments": [], "job_id": "x", "transcribed_at": "t"},
                     {"part_id": "p02", "order": 2, "duration": 5.0, "offset": 10.0, "segments": [], "job_id": "x", "transcribed_at": "t"}],
           "diarization": {"enabled": False}}
    new = [{"part_id": "p02", "order": 2, "duration": 7.0, "offset": 0, "segments": [{"start": 0, "end": 1, "text": "n"}], "job_id": "y", "transcribed_at": "t"}]
    d = transcript.build("N", "cs", {"name": "e", "model": "m"}, {"enabled": False}, old, new, "2026-10-01T00:00:00Z")
    assert [p["offset"] for p in d["parts"]] == [0, 10.0] and d["merged"]["duration"] == 17.0 and d["parts"][1]["job_id"] == "y"


def test_assign_speakers_max_overlap_and_labels():
    segs = [{"start": 0, "end": 5, "text": "a"}, {"start": 5, "end": 9, "text": "b"}, {"start": 20, "end": 21, "text": "c"}]
    d = [{"start": 0, "end": 4, "speaker": "SPK_3"}, {"start": 4, "end": 10, "speaker": "SPK_0"}]
    out = diar.label_segments(diar.assign_speakers(segs, d))
    assert [s.get("speaker") for s in out] == ["S1", "S2", None]          # no overlap -> no label
    assert all("spk" not in s for s in out)


def test_absorb_small_speakers():
    d = [{"start": 0, "end": 60, "speaker": "A"}, {"start": 60, "end": 63, "speaker": "B"}, {"start": 63, "end": 120, "speaker": "C"}]
    out = diar.absorb_small_speakers(d, 20)
    assert [x["speaker"] for x in out] == ["A", "A", "C"] or [x["speaker"] for x in out] == ["A", "C", "C"]
    only_small = [{"start": 0, "end": 5, "speaker": "A"}, {"start": 5, "end": 9, "speaker": "B"}]
    assert diar.absorb_small_speakers(only_small, 20) == only_small
