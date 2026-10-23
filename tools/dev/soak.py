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
import threading
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
    """RSS процесса в МБ: /proc в Linux, иначе ps (macOS); None — если процесс не найден."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    r = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid() if pid == "self" else pid)],
                       capture_output=True, text=True)
    return int(r.stdout.split()[0]) / 1024 if r.stdout.split() else None


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gst_count():
    """Число процессов gst-launch-1.0 (pgrep -c есть только в Linux — считаем строки)."""
    r = subprocess.run(["pgrep", "-x", "gst-launch-1.0"], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"pgrep: {r.stderr.strip()}")
    return len(r.stdout.split())


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
    # Строку готовности читает поток: синхронный readline останавливал событийный цикл пульта на время старта
    # блока (≈ 1,6 с в macOS), и стенд засчитывал это как паузу GUI при каждом перезапуске.
    proc.ready = threading.Event()
    threading.Thread(target=lambda: (proc.stdout.readline(), proc.ready.set()), daemon=True).start()
    return proc


def run(seconds, out_dir=None):
    from PySide6.QtCore import QElapsedTimer, QTimer, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle

    from sheregesh_console.qtwait import pump, wait_until
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
    agent.ready.wait(30)
    ui = Ui(Backend(tempfile.mkdtemp(prefix="sheregesh-soak-"), autostart=True, peer=("127.0.0.1", port)))
    lat_timer, lat_clock = QTimer(), QElapsedTimer()
    worst = {"ms": 0}
    lat_timer.setInterval(20)
    lat_timer.timeout.connect(lambda: worst.__setitem__("ms", max(worst["ms"], lat_clock.restart())))
    lat_clock.start()
    lat_timer.start()
    samples, crashes, page = [], 0, 0
    seen = {"peer": set(), "video": set()}
    next_crash = CRASH_EVERY_S
    try:
        while time.monotonic() - t0 < seconds:
            pump(1000)
            el = time.monotonic() - t0
            seen["peer"].add(ui.backend.status["peerState"])
            seen["video"].add(ui.backend.video.state)
            page = (page + 1) % len(PAGE_NAMES)
            ui.window.setProperty("currentPage", page)              # нагрузка на все страницы
            if el >= next_crash and el < seconds - 30:
                os.killpg(agent.pid, signal.SIGKILL)                 # аварийное завершение блока
                agent.wait()
                crashes += 1
                pump(3000)
                agent = start_agent(port, vport, seconds, time.monotonic() - t0)
                wait_until(agent.ready.is_set, 30)            # цикл Qt продолжает работать
                next_crash += CRASH_EVERY_S
            if int(el) % 5 == 0:
                st = ui.backend.status
                samples.append({"t": round(el), "console_rss_mb": rss_mb(), "agent_rss_mb": rss_mb(agent.pid),
                                 "loop_worst_ms": worst["ms"], "peer": st["peerState"],
                                 "video": ui.backend.video.state, "events": len(ui.backend.events),
                                 "logs": len(ui.backend.logs), "gst": gst_count()})
                worst["ms"] = 0
        pump(500)                                            # последние 20 с прошли без неисправностей
        final = {"peer": ui.backend.status["peerState"], "video": ui.backend.video.state}
        codes = {}
        for e in ui.backend.bench.monitor.log.events:
            codes[e.code] = codes.get(e.code, 0) + 1
    finally:
        lat_timer.stop()
        ui.close()
        os.killpg(agent.pid, signal.SIGKILL)
        agent.wait()
    end = time.monotonic() + 5                       # дочерним процессам — до 5 с на завершение
    while gst_count() > gst_before and time.monotonic() < end:
        time.sleep(0.2)
    leftover = gst_count() - gst_before
    warm = [s for s in samples if s["t"] >= max(30, seconds * 0.2)]
    def growth(key):
        vals = [s[key] for s in warm if s[key] is not None]
        return round(max(vals) - vals[0], 1) if len(vals) >= 2 else 0.0
    report = {"seconds": seconds, "agent_crashes": crashes, "samples": samples,
              "console_rss_growth_mb": growth("console_rss_mb"), "agent_rss_growth_mb": growth("agent_rss_mb"),
              "loop_worst_ms": max((s["loop_worst_ms"] for s in samples), default=0),
              "qml_problems": qml_problems[:20], "final": final, "leftover_gst": leftover,
              "event_counts": codes, "states_seen": {k: sorted(v) for k, v in seen.items()}}
    limit = max(15.0, 0.10 * (warm[0]["console_rss_mb"] if warm and warm[0]["console_rss_mb"] else 0))
    measured = sum(1 for s in warm if s["console_rss_mb"] is not None and s["agent_rss_mb"] is not None)
    report["memory_samples"] = measured
    report["verdict"] = {                            # без замеров памяти вердикт отрицательный, а не «рост 0»
        "memory": measured >= 2 and report["console_rss_growth_mb"] <= limit and report["agent_rss_growth_mb"] <= 15,
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
