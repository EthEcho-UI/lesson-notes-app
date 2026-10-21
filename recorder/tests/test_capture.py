"""REAL capture (ffmpeg -> PipeWire's pulse server) in a private, hardware-free audio server (see audiosandbox.py):
mic path (a virtual microphone fed with a tone), monitor path (an application playing a tone to the default sink),
following the default sink when it changes or disappears, silent-mic warning, missing microphone."""
import time

import pytest

import audiosandbox as sbx
from conftest import run_cli, wait_until
from rh import probe, state

pytestmark = pytest.mark.audio
MIC_HZ, SINK_A_HZ, SINK_B_HZ = 600, 1500, 2300


def part(ctx):
    (f,) = list(ctx.wcfg.local_audio_dir.glob("*/parts/01.opus"))
    return f


def start(*args):
    return run_cli("start", "--notebook", "Biologie", "--title", "Capture", *args)


def test_monitor_and_mic_paths_are_both_captured_in_meeting_mode(ctx, gh, pwenv):
    pwenv.play_tone(MIC_HZ, 12, "lr_mic_feed")                  # what the microphone hears (virtual mic = monitor of this sink)
    start("--kind", "meeting")                                  # mode meeting: mic + system audio
    assert state(ctx.paths)["capture"] == {"mic": "lr_mic", "monitor": "lr_out_a.monitor"}
    time.sleep(1.0)
    pwenv.play_tone(SINK_A_HZ, 4, "lr_out_a")                   # "the other side of the call"
    time.sleep(5.0)
    run_cli("stop")
    x = sbx.decode(part(ctx))
    assert len(x) / 16000 > 5
    whole = {f: sbx.tone_power(x, f) for f in (MIC_HZ, SINK_A_HZ, SINK_B_HZ)}
    assert whole[MIC_HZ] > 0.02 and whole[SINK_A_HZ] > 0.02, whole
    assert whole[SINK_B_HZ] < whole[SINK_A_HZ] / 5                                    # a tone nobody played is absent
    # the system-audio tone only exists while it played (not the whole recording): first second has no 1500 Hz
    assert sbx.tone_power(x[: 14000], SINK_A_HZ) < whole[SINK_A_HZ] / 3


def test_mic_mode_records_only_the_microphone(ctx, gh, pwenv):
    pwenv.play_tone(MIC_HZ, 8, "lr_mic_feed")
    pwenv.play_tone(SINK_A_HZ, 8, "lr_out_a")
    start("--kind", "lesson")                                   # lesson => mic only
    assert state(ctx.paths)["capture"]["monitor"] is None
    time.sleep(4.0)
    run_cli("stop")
    x = sbx.decode(part(ctx))
    assert sbx.tone_power(x, MIC_HZ) > 0.02
    assert sbx.tone_power(x, SINK_A_HZ) < sbx.tone_power(x, MIC_HZ) / 20


def test_follows_default_sink_change(ctx, gh, pwenv):
    """Headphones plugged in / HDMI connected: the default sink changes and the app's audio moves with it."""
    t_begin = time.time()
    start("--kind", "meeting")
    a = pwenv.play_tone(SINK_A_HZ, 30, "lr_out_a")
    time.sleep(3.0)
    a.terminate()
    pwenv.set_default_sink("lr_out_b")
    pwenv.play_tone(SINK_B_HZ, 30, "lr_out_b")
    wait_until(lambda: state(ctx.paths).get("capture", {}).get("monitor") == "lr_out_b.monitor", 8)
    time.sleep(3.0)
    st = state(ctx.paths)
    assert st["capture_restarts"] == 1 and any("default device changed" in e["msg"] for e in st["events"])
    run_cli("stop")
    t_end = time.time()
    x = sbx.decode(part(ctx))
    n = len(x)
    early, late = x[int(1.0 * 16000): int(2.8 * 16000)], x[n - 2 * 16000: n - 4000]
    assert sbx.dominant(early, (SINK_A_HZ, SINK_B_HZ))[0] == SINK_A_HZ
    assert sbx.dominant(late, (SINK_A_HZ, SINK_B_HZ))[0] == SINK_B_HZ
    assert sbx.tone_power(late, SINK_B_HZ) > 0.03
    # gap caused by the restart is small: total length close to the wall-clock time
    assert n / 16000 > (t_end - t_begin) - 2.5


def test_follows_when_the_default_sink_disappears(ctx, gh, pwenv):
    """Bluetooth headset switched off / HDMI unplugged: the sink vanishes and the session manager picks another default."""
    start("--kind", "meeting")
    time.sleep(1.5)
    pwenv.unload_sink("lr_out_a")
    wait_until(lambda: pwenv.default_sink() != "lr_out_a", 8)
    new = pwenv.default_sink()
    assert new
    wait_until(lambda: state(ctx.paths).get("capture", {}).get("monitor") == new + ".monitor", 10)
    pwenv.play_tone(SINK_B_HZ, 5, new)
    time.sleep(3.5)
    run_cli("stop")
    x = sbx.decode(part(ctx))
    assert sbx.tone_power(x[-3 * 16000:], SINK_B_HZ) > 0.03


def test_silent_microphone_warning(ctx, gh, pwenv, home):
    (home / ".config/lesson-notes/recorder.toml").write_text("silent_warn_s = 3.0\n")
    start("--kind", "lesson")                                   # the virtual mic hears nothing (digital silence)
    time.sleep(1.0)
    assert not state(ctx.paths)["warnings"]
    wait_until(lambda: state(ctx.paths).get("warnings"), 6)
    assert "is the microphone muted" in state(ctx.paths)["warnings"][0]
    pwenv.play_tone(MIC_HZ, 5, "lr_mic_feed")                   # sound comes back -> warning goes away
    wait_until(lambda: not state(ctx.paths).get("warnings"), 6)
    run_cli("stop")


def test_missing_microphone_fails_clearly(ctx, gh, pwenv, monkeypatch):
    monkeypatch.setenv("LESSON_RECORD_MIC", "no_such_source")
    monkeypatch.setenv("LESSON_RECORD_START_TIMEOUT", "4")
    r = run_cli("start", "--notebook", "Biologie", check=False)
    assert r.returncode == 1 and "not found" in r.stderr
    wait_until(lambda: state(ctx.paths).get("state") == "idle", 15)
    assert state(ctx.paths)["last"]["code"] == "capture_failed"
    assert not list(ctx.wcfg.local_audio_dir.glob("**/*.opus")) and not list(ctx.paths.outbox.glob("*.json"))


def test_only_a_monitor_as_default_source_is_no_microphone(ctx, gh, pwenv):
    pwenv.pactl("set-default-source", "lr_out_a.monitor")
    try:
        wait_until(lambda: pwenv.pactl("get-default-source") == "lr_out_a.monitor", 5)
        r = run_cli("start", "--notebook", "Biologie", "--kind", "lesson", check=False)
        assert r.returncode == 1 and "no microphone" in r.stderr
        # meeting mode still works: system audio only, with a note in the log
        pwenv.play_tone(SINK_A_HZ, 5, "lr_out_a")
        start("--kind", "meeting")
        assert state(ctx.paths)["capture"]["mic"] is None
        time.sleep(2.5)
        run_cli("stop")
        assert sbx.tone_power(sbx.decode(part(ctx)), SINK_A_HZ) > 0.02
    finally:
        pwenv.pactl("set-default-source", "lr_mic")


def test_pause_releases_the_microphone_and_resume_continues(ctx, gh, pwenv):
    from rh import children
    pwenv.play_tone(MIC_HZ, 14, "lr_mic_feed")
    start("--kind", "lesson")
    time.sleep(2.0)
    pid = state(ctx.paths)["pid"]
    n_running = len(children(pid))
    run_cli("pause")
    time.sleep(1.0)
    assert len(children(pid)) < n_running                       # the ffmpeg that holds the microphone is gone while paused
    time.sleep(2.0)
    run_cli("resume")
    time.sleep(2.5)
    run_cli("stop")
    x = sbx.decode(part(ctx))
    assert 3.5 < len(x) / 16000 < 7.5                           # ~4.5 s recorded, the 3 s pause is not in the file
    assert sbx.tone_power(x[-2 * 16000:], MIC_HZ) > 0.03
