# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Матрица снимков экранов для визуальной приёмки S1.5 (1920×1200, без дисплея).

Штатное состояние: все страницы × 2 языка × 2 темы × 3 масштаба (отдельный процесс удалённого блока, видео).
Нештатные состояния: предупреждение, критическая ошибка, нет второго процесса, нет видео — главная панель
и профильная страница × 2 языка × 2 темы × масштабы 100 и 150 %.

  python3 tools/dev/gallery.py DIR [--quick]
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

LANGS, THEMES, SCALES = ("ru", "en"), ("dark", "light"), (100, 125, 150)
STATES = {"warning": ["dashboard", "diagnostics"], "critical": ["dashboard", "diagnostics", "power"],
          "no-peer": ["dashboard", "about"], "no-video": ["dashboard", "video"]}


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_agent(*extra):
    port, vport = free_port(), free_port(socket.SOCK_DGRAM)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in
                                                       ("software", "software/emulators", "software/remote"))}
    proc = subprocess.Popen([sys.executable, "-m", "sheregesh_remote", "--port", str(port), "--video",
                             "--video-port", str(vport), *extra], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    proc.stdout.readline()
    return proc, port


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("--quick", action="store_true", help="только RU/EN × тёмная × 100 % (для CI)")
    a = ap.parse_args()

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from PySide6.QtTest import QTest

    from sheregesh_console.qtwait import pump, wait_until
    from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
    from sheregesh_console.backend import Backend

    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication([sys.argv[0]])  # noqa: F841
    compile_translations()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    index = []
    langs, themes = (LANGS, ("dark",)) if a.quick else (LANGS, THEMES)

    def wait(pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end and not pred():
            pump(100)
        return pred()

    def session(agent_args=()):
        agent, port = start_agent(*agent_args)
        ui = Ui(Backend(tempfile.mkdtemp(prefix="sheregesh-gallery-"), autostart=True, peer=("127.0.0.1", port)))
        wait(lambda: ui.backend.status["peerState"] == "connected", 10)
        return agent, ui

    def appearance(ui, lang, theme, scale):
        form = dict(ui.backend.config)
        form.update(language=lang, theme=theme, ui_scale=scale)
        if (form["language"], form["theme"], form["ui_scale"]) != (ui.backend.language, ui.backend.theme, ui.backend.uiScale):
            assert ui.backend.applyForm(form, True), ui.backend.lastResult
        pump(250)

    def shot(ui, state, lang, theme, scale, page):
        ui.window.setProperty("currentPage", PAGE_NAMES.index(page))
        pump(350)
        name = f"{state}_{lang}_{theme}_{scale}_{PAGE_NAMES.index(page) + 1:02d}-{page}.png"
        ui.grab(out / name)
        index.append({"file": name, "state": state, "lang": lang, "theme": theme, "scale": scale, "page": page,
                      "peer": ui.backend.status["peerState"], "video": ui.backend.video.state,
                      "overall": ui.backend.status["overall"]})

    def close(agent, ui):
        ui.close()
        try:
            os.killpg(agent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        agent.wait()

    # ---- штатное состояние ----
    agent, ui = session()
    wait(lambda: ui.backend.video.state == "playing", 15)
    for lang in langs:
        for theme in themes:
            for scale in (SCALES if not a.quick else (100,)):
                appearance(ui, lang, theme, scale)
                for page in PAGE_NAMES:
                    shot(ui, "normal", lang, theme, scale, page)
    close(agent, ui)

    # ---- нештатные состояния ----
    for state, pages in STATES.items():
        agent, ui = session(("--fault", "0.5:video-off:3600") if state == "no-video" else ())
        b = ui.backend
        if state in ("warning", "critical"):
            b.bench.thermal.set_ambient(40.0)
            b.bench.thermal.set_load(12.0 if state == "warning" else 20.0)
            if state == "critical":
                b.bench.fan.set_fault("stall")
                b.bench.power.switch_source("battery")
                b.bench.power.set_percent(8)
            target = "TEMP-HIGH" if state == "warning" else "TEMP-CRIT"
            for _ in range(600):
                b.tick()
                if any(e["code"] == target for e in b.activeEvents):
                    break
            if state == "warning":
                b.bench.thermal.set_load(10.5)       # ≈ 72 °C: между порогами 70 и 80 — предупреждение держится
        elif state == "no-peer":
            os.killpg(agent.pid, signal.SIGKILL)
            agent.wait()
            wait(lambda: b.status["peerState"] == "lost" and b.status["peerDetail"] == "refused", 15)
        elif state == "no-video":
            wait(lambda: b.video.state == "no-video", 15)
        for lang in langs:
            for theme in themes:
                for scale in ((100, 150) if not a.quick else (100,)):
                    appearance(ui, lang, theme, scale)
                    for page in pages:
                        shot(ui, state, lang, theme, scale, page)
        close(agent, ui)

    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1))
    print(f"{len(index)} снимков в {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
