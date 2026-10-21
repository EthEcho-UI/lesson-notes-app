"""Helpers for the recorder tests."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import fake_github as fg


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels,sample_rate:format=duration,format_name",
                        "-of", "json", str(path)], capture_output=True, text=True)
    d = json.loads(r.stdout or "{}")
    return {"codec": d["streams"][0]["codec_name"], "channels": d["streams"][0]["channels"], "duration": float(d["format"]["duration"]),
            "format": d["format"]["format_name"]}


def state(paths):
    try:
        return json.loads(paths.state_file.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}


def main_json(path):
    f = fg.ST.files("main").get(path)
    return None if f is None else json.loads(fg.ST.blobs[f])


def inbox_job(note_id):
    f = (fg.ST.files("inbox") or {}).get(f"jobs/{note_id}/job.json")
    return None if f is None else json.loads(fg.ST.blobs[f])


def daemon_pids(home):
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                cmd = (p / "cmdline").read_bytes()
                if b"lesson_record.recorder" in cmd and f"HOME={home}".encode() in (p / "environ").read_bytes():
                    out.append(int(p.name))
            except OSError:
                pass
    return out


def children(pid):
    out = []
    for p in Path("/proc").iterdir():
        if p.name.isdigit():
            try:
                st = (p / "stat").read_text().rsplit(")", 1)[1].split()
                if int(st[1]) == pid:
                    out.append(int(p.name))
            except (OSError, IndexError, ValueError):
                pass
    return out


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False
