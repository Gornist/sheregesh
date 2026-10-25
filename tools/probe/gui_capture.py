#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Снимки реального GUI пульта без дисплея (E2E, стенд без экрана). Запускается НА пульте (pi1) от sheregesh-ui при
остановленной службе sheregesh-console: тот же код выпуска (Backend, QML, канал mTLS, видеоприёмник) с программным
рендерером Qt (QT_QPA_PLATFORM=offscreen, QT_QUICK_BACKEND=software) и настоящей связью с удалённым блоком.
Окно снимается grabWindow() по фактическим переходам состояния, а не по таймеру:

  01/02 связь есть (CONNECTED и видео playing ≥ 3 с)   → главная, видео (два кадра видео с интервалом 1 с)
  03/04 связь потеряна (не CONNECTED ≥ 2 с)            → главная, видео
  05/06 связь восстановлена (снова CONNECTED + playing) → главная, видео

  PYTHONPATH=<выпуск>/software:… python3 gui_capture.py --out DIR [--timeout 240]

В DIR: PNG, states.json (состояние бэкенда и метрики видео в момент каждого снимка — сведения, не доказательство
рендеринга), phase-* (метки фаз для внешнего внесения отказа). Ограничение: путь cage/Wayland/DRM/HDMI не
проверяется — рендерится та же сцена QML программным рендерером Qt.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--link-config", default=str(Path("~/.config/sheregesh/link.json").expanduser()))
    ap.add_argument("--peer", default="127.0.0.1:7700")
    ap.add_argument("--timeout", type=float, default=240.0)
    a = ap.parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from sheregesh.link.tls import LinkConfig
    from sheregesh_console import app as capp
    from sheregesh_console.backend import Backend

    a.out.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Fusion")
    qapp = QGuiApplication(sys.argv[:1])
    qapp.setApplicationName("sheregesh-console")
    capp.compile_translations()
    host, _, port = a.peer.rpartition(":")
    backend = Backend(Path(tempfile.mkdtemp(prefix="gui-capture-")), peer=(host, int(port)),
                      link_cfg=LinkConfig.load(a.link_config), link_status_file=str(a.out / "link-status.json"))
    ui = capp.Ui(backend)
    ui.window.resize(1280, 800)
    states, t0 = [], time.monotonic()
    st = {"phase": "wait-connected", "since": None, "rc": 1}

    def view():
        s = backend.status
        return s.get("linkView"), backend.video.state

    def page(name):
        ui.window.setProperty("currentPage", capp.PAGE_NAMES.index(name))

    def grab(name):
        path = ui.grab(a.out / f"{name}.png")
        s = backend.status
        states.append({"shot": path.name, "t": round(time.monotonic() - t0, 2), "linkView": s.get("linkView"),
                       "peerLink": s.get("peerLink"), "peerDetail": s.get("peerDetail"),
                       "video": {k: backend.video.metrics().get(k) for k in ("state", "decoded_fps_5s", "sessions", "mode")}})
        return path

    def video_pair(prefix):
        """Два снимка страницы видео с интервалом 1 с: разные байты — кадры действительно сменяются в окне."""
        page("video")
        first = grab(f"{prefix}-video")
        h1 = hashlib.sha256(first.read_bytes()).hexdigest()

        def second():
            p2 = grab(f"{prefix}-video-t+1s")
            states[-1]["video_changed"] = hashlib.sha256(p2.read_bytes()).hexdigest() != h1
            page("dashboard")
        QTimer.singleShot(1000, second)

    def shots(prefix):
        page("dashboard")
        QTimer.singleShot(300, lambda: (grab(f"{prefix}-dashboard"), QTimer.singleShot(300, lambda: video_pair(prefix))))

    def tick():
        link, video = view()
        now = time.monotonic()
        ok = link == "CONNECTED" and video == "playing"
        ph = st["phase"]
        if now - t0 > a.timeout:
            (a.out / "phase-timeout").write_text(ph)
            return finish()
        if ph in ("wait-connected", "wait-recovered"):
            if ok:
                st["since"] = st["since"] or now
                if now - st["since"] >= 3.0:
                    prefix = "01-02-connected" if ph == "wait-connected" else "05-06-recovered"
                    shots(prefix)
                    st.update(phase="shooting", next="wait-lost" if ph == "wait-connected" else "done", since=None)
                    QTimer.singleShot(2500, lambda: _after(prefix))
            else:
                st["since"] = None
        elif ph == "wait-lost":
            if link != "CONNECTED":
                st["since"] = st["since"] or now
                if now - st["since"] >= 2.0:
                    shots("03-04-lost")
                    st.update(phase="shooting", next="wait-recovered", since=None)
                    QTimer.singleShot(2500, lambda: _after("03-04-lost"))
            else:
                st["since"] = None

    def _after(prefix):
        (a.out / f"phase-{prefix}").write_text(str(round(time.monotonic() - t0, 2)))
        st["phase"] = st["next"]
        if st["phase"] == "done":
            st["rc"] = 0
            finish()

    def finish():
        (a.out / "states.json").write_text(json.dumps(states, ensure_ascii=False, indent=1))
        timer.stop()
        ui.close()
        qapp.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    qapp.exec()
    return st["rc"]


if __name__ == "__main__":
    sys.exit(main())
