# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Длительный прогон программного стенда (S1.5): GUI пульта + отдельный процесс удалённого блока + видео.

Циклически вносятся неисправности (молчание, повреждённые сообщения, пропадание видео, события блока,
аварийное завершение блока с перезапуском). Отслеживаются память обоих процессов, паузы событийного
цикла GUI, предупреждения QML, оставшиеся процессы GStreamer и итоговое восстановление.

  python3 tools/dev/soak.py SECONDS [--out DIR]
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("software", "software/emulators", "software/console", "software/remote"):
    sys.path.insert(0, str(ROOT / p))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

CYCLE_S = 60
CRASH_EVERY_S = 180
CYCLE = ["10:silence:6", "25:corrupt:garbage", "26:corrupt:json", "30:video-off:8", "40:alert:EMU-TEST",
         "50:alert-clear"]


def rss_mb(pid="self"):
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gst_count():
    r = subprocess.run(["pgrep", "-c", "-x", "gst-launch-1.0"], capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def start_agent(port, vport, seconds, offset):
    faults = []
    for k in range(int(seconds // CYCLE_S) + 1):
        for f in CYCLE:
            at, rest = f.split(":", 1)
            t = k * CYCLE_S + float(at) - offset
            if t > 0 and offset + t < seconds - 20:         # последние 20 с — без неисправностей (проверка восстановления)
                faults += ["--fault", f"{t}:{rest}"]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in
                                                       ("software", "software/emulators", "software/remote"))}
    proc = subprocess.Popen([sys.executable, "-m", "sheregesh_remote", "--port", str(port), "--video",
                             "--video-port", str(vport), "--video-width", "640", "--video-height", "360",
                             "--video-fps", "15", *faults], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    proc.stdout.readline()
    return proc


def run(seconds, out_dir=None):
    from PySide6.QtCore import QElapsedTimer, QTimer, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest

    from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
    from sheregesh_console.backend import Backend

    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    qml_problems = []

    def on_msg(mode, ctx, text):
        if "qml" in text.lower() or "TypeError" in text or "ReferenceError" in text:
            qml_problems.append(text[:300])
    qInstallMessageHandler(on_msg)
    compile_translations()
    gst_before = gst_count()
    port, vport = free_port(), free_port(socket.SOCK_DGRAM)
    t0 = time.monotonic()
    agent = start_agent(port, vport, seconds, 0)
    ui = Ui(Backend(tempfile.mkdtemp(prefix="sheregesh-soak-"), autostart=True, peer=("127.0.0.1", port)))
    lat_timer, lat_clock = QTimer(), QElapsedTimer()
    worst = {"ms": 0}
    lat_timer.setInterval(20)
    lat_timer.timeout.connect(lambda: worst.__setitem__("ms", max(worst["ms"], lat_clock.restart())))
    lat_clock.start()
    lat_timer.start()
    samples, crashes, page = [], 0, 0
    next_crash = CRASH_EVERY_S
    try:
        while time.monotonic() - t0 < seconds:
            QTest.qWait(1000)
            el = time.monotonic() - t0
            page = (page + 1) % len(PAGE_NAMES)
            ui.window.setProperty("currentPage", page)              # нагрузка на все страницы
            if el >= next_crash and el < seconds - 30:
                os.killpg(agent.pid, signal.SIGKILL)                 # аварийное завершение блока
                agent.wait()
                crashes += 1
                QTest.qWait(3000)
                agent = start_agent(port, vport, seconds, time.monotonic() - t0)
                next_crash += CRASH_EVERY_S
            if int(el) % 5 == 0:
                st = ui.backend.status
                samples.append({"t": round(el), "console_rss_mb": rss_mb(), "agent_rss_mb": rss_mb(agent.pid),
                                 "loop_worst_ms": worst["ms"], "peer": st["peerState"],
                                 "video": ui.backend.video.state, "events": len(ui.backend.events),
                                 "logs": len(ui.backend.logs), "gst": gst_count()})
                worst["ms"] = 0
        QTest.qWait(500)                                            # последние 20 с прошли без неисправностей
        final = {"peer": ui.backend.status["peerState"], "video": ui.backend.video.state}
    finally:
        lat_timer.stop()
        ui.close()
        os.killpg(agent.pid, signal.SIGKILL)
        agent.wait()
    time.sleep(1.5)
    leftover = gst_count() - gst_before
    warm = [s for s in samples if s["t"] >= max(30, seconds * 0.2)]
    def growth(key):
        vals = [s[key] for s in warm if s[key] is not None]
        return round(max(vals) - vals[0], 1) if len(vals) >= 2 else 0.0
    report = {"seconds": seconds, "agent_crashes": crashes, "samples": samples,
              "console_rss_growth_mb": growth("console_rss_mb"), "agent_rss_growth_mb": growth("agent_rss_mb"),
              "loop_worst_ms": max((s["loop_worst_ms"] for s in samples), default=0),
              "qml_problems": qml_problems[:20], "final": final, "leftover_gst": leftover}
    limit = max(15.0, 0.10 * (warm[0]["console_rss_mb"] if warm and warm[0]["console_rss_mb"] else 0))
    report["verdict"] = {
        "memory": report["console_rss_growth_mb"] <= limit and report["agent_rss_growth_mb"] <= 15,
        "responsive": report["loop_worst_ms"] < 1000,
        "no_qml_errors": not qml_problems,
        "no_orphans": leftover <= 0,
        "recovered": final["peer"] == "connected",
    }
    report["passed"] = all(report["verdict"].values())
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "soak.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("seconds", type=float)
    ap.add_argument("--out", default=str(ROOT / "reports" / "soak"))
    a = ap.parse_args()
    r = run(a.seconds, a.out)
    print(json.dumps({k: v for k, v in r.items() if k != "samples"}, ensure_ascii=False, indent=1))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
