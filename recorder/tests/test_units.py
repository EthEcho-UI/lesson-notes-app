import math
import re
import threading
import time

import numpy as np
import pytest

from lesson_record import audio, finalize as fin, media
from lesson_record.common import ulid
from lesson_record.encoder import Encoder
from lesson_record.recorder import acquire_lock, lock_is_held


def test_ulid_is_valid_and_sortable():
    a = ulid()
    time.sleep(0.01)
    b = ulid()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", a) and a < b


def test_levels():
    t = np.arange(16000) / 16000
    full = (32767 * np.sin(2 * math.pi * 440 * t)).astype("<i2").tobytes()
    half = (16384 * np.sin(2 * math.pi * 440 * t)).astype("<i2").tobytes()
    rms, peak = audio.levels(full)
    assert -0.1 < peak <= 0 and -3.3 < rms < -2.8
    assert audio.levels(half)[1] == pytest.approx(-6.0, abs=0.1)
    assert audio.levels(bytes(32000)) == (-120.0, -120.0) and audio.levels(b"") == (-120.0, -120.0)


def test_fake_source_paces_in_realtime_and_signals_eof():
    s = audio.FakeSource("tone:440")
    s.start()
    t0 = time.monotonic()
    got = 0
    while got < 2 * 32000:
        c = s.read(0.5)
        got += len(c or b"")
    assert 1.5 < time.monotonic() - t0 < 3.0
    none = audio.FakeSource("none")
    with pytest.raises(audio.SourceEnded):
        none.read(0.1)


def test_pulse_command_for_both_modes():
    s = audio.PulseSource("meeting")
    cmd = s.build_cmd("MIC", "SINK.monitor")
    assert cmd.count("-f") >= 3 and "MIC" in cmd and "SINK.monitor" in cmd
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "amix=inputs=2" in fc and "normalize=0" in fc and "alimiter" in fc and "channel_layouts=mono" in fc
    assert cmd[-6:] == ["-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"][-6:] or cmd[-1] == "pipe:1"
    mic_only = audio.PulseSource("mic").build_cmd("MIC", None)
    assert "-filter_complex" not in mic_only and "SINK" not in " ".join(mic_only)
    sys_only = audio.PulseSource("meeting").build_cmd(None, "SINK.monitor")
    assert "SINK.monitor" in sys_only and sys_only.count("pulse") == 1


def test_encoder_writes_valid_opus_even_when_killed(tmp_path):
    out = tmp_path / "x.opus"
    e = Encoder(out)
    t = np.arange(8000) / 16000
    for _ in range(6):
        e.write((8000 * np.sin(2 * math.pi * 300 * t)).astype("<i2").tobytes())
        time.sleep(0.5)
    e.proc.kill()
    e.proc.wait()
    assert 2.0 < media.duration(out) <= 3.1                       # 3 s fed, at most a fraction of a second lost


def test_lock_is_released_when_holder_dies(ctx):
    fd = acquire_lock(ctx.paths)
    assert fd is not None and lock_is_held(ctx.paths) and acquire_lock(ctx.paths) is None
    import os
    os.close(fd)
    assert not lock_is_held(ctx.paths)


def test_next_order_considers_remote_outbox_and_disk(ctx):
    nid = ulid()
    assert fin.next_order(ctx.paths, None, nid) == 1
    assert fin.next_order(ctx.paths, {"parts": [{"order": 1}, {"order": 2}]}, nid) == 3
    d = ctx.wcfg.local_audio_dir / nid / "parts"
    d.mkdir(parents=True)
    (d / "04.opus").write_bytes(b"x")
    assert fin.next_order(ctx.paths, {"parts": [{"order": 1}]}, nid) == 5


def test_start_refuses_without_notebook_and_with_ambiguous_one(ctx, gh, fake_tone):
    from lesson_record import session
    with pytest.raises(session.SessionError, match="--notebook is required"):
        session.start(*ctx.args(), kind="lesson")
    import fake_github as fg
    from conftest import NOTEBOOK, jb
    nb2 = dict(NOTEBOOK, id="01K8F3Z6N7Q2W9XJ4M5R8TBVCB", name="Biologie 3.B")
    fg.ST.commit_files("main", {"notebooks/01K8F3Z6N7Q2W9XJ4M5R8TBVCB.json": jb(nb2)}, "nb2")
    with pytest.raises(session.SessionError, match="ambiguous"):
        session.start(*ctx.args(), kind="lesson", notebook="Biologie")


def test_offline_without_cache_explains(ctx, gh, fake_tone, home):
    from lesson_record import session
    toml = home / ".config/lesson-notes/worker.toml"
    toml.write_text(toml.read_text().replace("127.0.0.1:", "127.0.0.1:1"))
    from lesson_record.common import load_worker_config
    w = load_worker_config()
    with pytest.raises(session.SessionError, match="offline"):
        session.start(w, ctx.rcfg, ctx.paths, kind="lesson", notebook="Biologie")
