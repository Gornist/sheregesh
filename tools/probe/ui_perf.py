#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Нагрузка пульта на pi1: CPU и RSS процессов пульта, gst-декодера и cage, FPS видео — за окно замера.

  python3 tools/probe/ui_perf.py --host 192.168.1.92 [--seconds 60] [--label before] [--out FILE.json]

CPU — доля одного ядра (100 % = одно ядро), из /proc/PID/stat за всё окно (с учётом потоков). FPS — из
ui-state.json пульта (SHEREGESH_UI_STATE): decoded_fps_5s декодера и, если пульт сообщает, rendered_fps_5s
(кадры окна, frameSwapped). Платы не трогает: только чтение /proc и файла состояния.
"""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
import time

REMOTE = r'''
import json, os, time
HZ = os.sysconf("SC_CLK_TCK")
def procs():
    out = {}
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        a = b" ".join(args).decode(errors="replace")
        name = ("console" if "-m sheregesh_console" in a else "gst" if a.startswith("gst-launch-1.0")
                else "cage" if a.startswith("/usr/bin/cage") else None)
        if name and name not in out:
            out[name] = int(pid)
    return out
def ticks(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return int(f[11]) + int(f[12])
def rss(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) // 1024
def state():
    try:
        d = json.load(open("/run/sheregesh-ui/ui-state.json"))
    except (OSError, ValueError):
        return {}
    v = d.get("video") or {}
    return {"decoded_fps_5s": v.get("decoded_fps_5s"), "perf": d.get("perf")}
P = procs()
t0, c0 = time.monotonic(), {n: ticks(p) for n, p in P.items()}
fps, rs = [], {n: [] for n in P}
while time.monotonic() - t0 < SECONDS:
    time.sleep(5)
    s = state()
    fps.append(s)
    for n, p in P.items():
        rs[n].append(rss(p))
dt = time.monotonic() - t0
cpu = {n: round(100.0 * (ticks(p) - c0[n]) / HZ / dt, 1) for n, p in P.items()}
print(json.dumps({"seconds": round(dt, 1), "pids": P, "cpu_pct_core": cpu,
                  "rss_mb_max": {n: max(v) for n, v in rs.items() if v},
                  "load": open("/proc/loadavg").read().split()[:3], "samples": fps}))
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="auto")
    ap.add_argument("--user", default="sheregesh-ui")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--label", default="")
    ap.add_argument("--out")
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tools/: адреса плат по серийному номеру
    from stand import boards
    boards.resolve_args(a, {'host': 'pi1'})
    code = REMOTE.replace("SECONDS", str(a.seconds))
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", f"{a.user}@{a.host}", "python3 -c " + shlex.quote(code)],
                       capture_output=True, text=True, timeout=a.seconds + 60)
    if r.returncode:
        print(r.stderr, file=sys.stderr)
        return 1
    d = json.loads(r.stdout)
    d.update(label=a.label, host=a.host, time=time.strftime("%Y-%m-%dT%H:%M:%S"))
    dec = [s["decoded_fps_5s"] for s in d["samples"] if s.get("decoded_fps_5s") is not None]
    ren = [s["perf"]["rendered_fps_5s"] for s in d["samples"] if (s.get("perf") or {}).get("rendered_fps_5s") is not None]
    d["fps"] = {"decoded_min": min(dec) if dec else None, "rendered_min": min(ren) if ren else None,
                "rendered_max": max(ren) if ren else None}
    print(json.dumps({k: d[k] for k in ("label", "seconds", "cpu_pct_core", "rss_mb_max", "load", "fps")},
                     ensure_ascii=False))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
