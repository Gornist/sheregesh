# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Снимки интерфейса Minimal без дисплея и без плат: удалённый блок — процесс-эмулятор с синтетическим видео.

  python3 tools/dev/minimal_shots.py DIR [--size 1280x800] [--size 1920x1200] [--quick]

Кадры: видео без панели, пять разделов, «Подробно» (страница видео), потеря связи с блоком, восстановление.
Цикл правки интерфейса: правка QML → этот скрипт (секунды) → смотреть PNG; плата не нужна.
"""
import argparse
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in ("software", "software/emulators", "software/console", "software/remote", "tools/dev"):
    sys.path.insert(0, str(ROOT / p))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

SECTIONS = ("video", "control", "module", "diagnostics", "system")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--size", action="append", help="ШxВ (по умолчанию 1280x800 и 1920x1200)")
    ap.add_argument("--quick", action="store_true", help="только видео и раздел «Видео» на первом размере")
    a = ap.parse_args()
    sizes = [tuple(int(x) for x in s.split("x")) for s in (a.size or ["1280x800", "1920x1200"])]

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle

    from gallery import start_agent
    from sheregesh_console.app import Ui, compile_translations
    from sheregesh_console.backend import Backend
    from sheregesh_console.qtwait import pump

    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication([sys.argv[0]])  # noqa: F841
    compile_translations()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def wait(pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            pump(50)
        return pred()

    agent, port = start_agent()
    ui = Ui(Backend(tempfile.mkdtemp(prefix="sheregesh-minimal-"), autostart=True, peer=("127.0.0.1", port)),
            layout="minimal")
    w = ui.window
    shots = []

    def shot(name):
        pump(300)
        shots.append(ui.grab(out / f"{name}.png"))
        print(shots[-1], flush=True)

    try:
        ok = wait(lambda: ui.backend.videoSource.get("live"), 30)
        print(f"видео: {'идёт' if ok else 'нет'}", flush=True)
        for i, (wd, ht) in enumerate(sizes):
            w.setProperty("width", wd)
            w.setProperty("height", ht)
            tag = f"{wd}x{ht}"
            w.setProperty("section", "")
            shot(f"{tag}-01-video")
            for n, sec in enumerate(SECTIONS[:1] if a.quick else SECTIONS):
                w.setProperty("section", sec)
                shot(f"{tag}-{n + 2:02d}-{sec}")
            if a.quick or i > 0:
                continue
            w.setProperty("section", "video")
            w.setProperty("detailsSection", "video")
            w.setProperty("section", "")
            w.setProperty("details", "VideoPage")
            shot(f"{tag}-07-details-video")
            w.setProperty("details", "")
            os.killpg(agent.pid, signal.SIGTERM)
            agent.wait(5)
            wait(lambda: ui.backend.status.get("linkView") != "CONNECTED", 15)
            pump(1500)
            shot(f"{tag}-08-link-lost")
            agent, port = start_agent("--port", str(port))
            wait(lambda: ui.backend.videoSource.get("live"), 30)
            shot(f"{tag}-09-recovered")
    finally:
        ui.close()
        try:
            os.killpg(agent.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
