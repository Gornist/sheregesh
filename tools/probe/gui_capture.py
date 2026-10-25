#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Снимки реального GUI пульта без дисплея (E2E, стенд без экрана). Запускается НА пульте (pi1) от sheregesh-ui при
остановленной службе sheregesh-console: тот же код выпуска (Backend, QML, канал mTLS, видеоприёмник) с программным
рендерером Qt (QT_QPA_PLATFORM=offscreen, QT_QUICK_BACKEND=software) и настоящей связью с удалённым блоком.
Окно снимается grabWindow() по фактическим переходам состояния, а не по таймеру.

--by link (по умолчанию) — фазы по состоянию связи:
  01/02 связь есть (CONNECTED и видео playing ≥ 3 с)   → главная, видео (два кадра видео с интервалом 1 с)
  03/04 связь потеряна (не CONNECTED ≥ 2 с)            → главная, видео
  05/06 связь восстановлена (снова CONNECTED + playing) → главная, видео

--by video — фазы по видео: video.state приёмника и состояние источника (backend.videoSource, display/live):
  01/02 поток (live ≥ 3 с)                                         → главная, видео (пара кадров)
  затем для каждого состояния из --video-states (по умолчанию no-signal,no-device; также connect-error, error,
  no-video, reconnecting — канал управления восстанавливается) в том порядке, в каком оно фактически наступило и держится ≥ 2 с:
  NN    <состояние>                                                 → главная, видео (пара кадров)
  NN    восстановлено после него (снова live ≥ 3 с)                 → главная, видео (пара кадров)
  Отказы вносятся снаружи (сигнал источника, отключение устройства, адрес RTSP); метки phase-* — для этого.

Доказательство смены кадров: пара снимков страницы видео с интервалом 1 с; frame_changed — сравнение только области
кадра (videoView), а не всего окна (подписи частоты/счётчиков меняются и без видео): при потоке должно быть true,
при «нет сигнала» / «нет устройства» поле чёрное (прежний кадр не показывается): frame_dark — доля почти чёрных
пикселей области кадра (подпись «no frames for N s» меняет хеш области, поэтому frame_changed здесь не критерий).
video_changed — прежнее сравнение всего окна (режим link).

  PYTHONPATH=<выпуск>/software:… python3 gui_capture.py --out DIR [--by video] [--timeout 240]

В DIR: PNG, states.json (состояние бэкенда, метрики видео и источник в момент каждого снимка — сведения, не
доказательство рендеринга), phase-* (метки фаз). Ограничение: путь cage/Wayland/DRM/HDMI не проверяется — рендерится
та же сцена QML программным рендерером Qt.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

FAULT_STATES = ("no-signal", "no-device", "connect-error", "error", "no-video", "reconnecting")
STABLE_OK_S, STABLE_FAULT_S = 3.0, 2.0


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--link-config", default=str(Path("~/.config/sheregesh/link.json").expanduser()))
    ap.add_argument("--peer", default="127.0.0.1:7700")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--by", choices=("link", "video"), default="link", help="по чему снимать фазы")
    ap.add_argument("--video-states", default="no-signal,no-device",
                    help=f"для --by video: ожидаемые состояния отказа через запятую ({', '.join(FAULT_STATES)})")
    a = ap.parse_args(argv)
    a.video_states = [s.strip() for s in a.video_states.split(",") if s.strip()]
    bad = [s for s in a.video_states if s not in FAULT_STATES]
    if bad or not a.video_states:
        ap.error(f"--video-states: неизвестные состояния {bad}")
    return a


def region(img, item):
    from PySide6.QtCore import QRectF
    return img.copy(item.mapRectToScene(QRectF(0, 0, item.width(), item.height())).toRect())


def region_hash(img, item):
    """Хеш пикселей области элемента (кадр видео) на снимке окна."""
    return hashlib.sha256(bytes(region(img, item).constBits())).hexdigest()


def dark_fraction(img, item, step=4, level=24):
    """Доля почти чёрных пикселей области кадра (выборка через step). При отказе источника поле чёрное, а подпись
    «no frames for N s» в нём меняется — сравнение хешей тогда ложно говорит «кадр обновляется»."""
    sub = region(img, item)
    n = dark = 0
    for y in range(0, sub.height(), step):
        for x in range(0, sub.width(), step):
            c = sub.pixelColor(x, y)
            n += 1
            dark += max(c.red(), c.green(), c.blue()) <= level
    return round(dark / n, 3) if n else None


class Capture:
    """Съёмка по переходам. backend/ui — готовые (на стенде — с настоящей связью, в испытаниях — с подменами)."""

    def __init__(self, a, backend, ui, page_names):
        self.a, self.backend, self.ui, self.pages = a, backend, ui, page_names
        self.states, self.t0 = [], time.monotonic()
        self.phase, self.since, self.rc, self.n = "wait-ok", None, 1, 0
        self.pending = list(a.video_states) if a.by == "video" else []
        self.last_fault = None
        self.busy = False

    # ---------- снимки ----------
    def page(self, name):
        self.ui.window.setProperty("currentPage", self.pages.index(name))

    def grab(self, name):
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.processEvents()
        img = self.ui.window.grabWindow()
        path = self.a.out / f"{name}.png"
        img.save(str(path))
        b, s = self.backend, self.backend.status
        m = b.video.metrics()
        self.states.append({"shot": path.name, "t": round(time.monotonic() - self.t0, 2), "linkView": s.get("linkView"),
                            "peerLink": s.get("peerLink"), "peerDetail": s.get("peerDetail"),
                            "video": {k: m.get(k) for k in ("state", "decoded_fps_5s", "sessions", "mode", "error")},
                            "videoSource": dict(b.videoSource),
                            # S4: источники блока и ход выбора (пусто/no-list — блок списка не сообщает)
                            "videoSources": list(getattr(b, "videoSources", [])),
                            "videoSelection": dict(getattr(b, "videoSelection", {}))})
        return path, img

    def video_pair(self, prefix, done):
        """Два снимка страницы видео с интервалом 1 с: смена байтов окна и смена области кадра."""
        from PySide6.QtCore import QObject, QTimer
        self.page("video")
        view = self.ui.window.findChild(QObject, "videoView")
        p1, img1 = self.grab(f"{prefix}-video")
        h1, f1 = hashlib.sha256(p1.read_bytes()).hexdigest(), region_hash(img1, view)

        def second():
            p2, img2 = self.grab(f"{prefix}-video-t+1s")
            self.states[-1]["video_changed"] = hashlib.sha256(p2.read_bytes()).hexdigest() != h1
            self.states[-1]["frame_changed"] = region_hash(img2, view) != f1
            self.states[-1]["frame_dark"] = dark_fraction(img2, view)
            self.page("dashboard")
            done()
        QTimer.singleShot(1000, second)

    def shots(self, prefix, after):
        """Главная, затем пара кадров видео; по окончании — метка фазы и переход."""
        from PySide6.QtCore import QTimer
        self.busy = True
        self.page("dashboard")

        def finish_phase():
            (self.a.out / f"phase-{prefix}").write_text(str(round(time.monotonic() - self.t0, 2)))
            self.busy = False
            after()
        QTimer.singleShot(300, lambda: (self.grab(f"{prefix}-dashboard"),
                                        QTimer.singleShot(300, lambda: self.video_pair(prefix, finish_phase))))

    def prefix(self, label):
        a, b = self.n * 2 + 1, self.n * 2 + 2
        self.n += 1
        return f"{a:02d}-{b:02d}-{label}"

    # ---------- переходы ----------
    def held(self, cond, seconds, now):
        if not cond:
            self.since = None
            return False
        self.since = self.since or now
        return now - self.since >= seconds

    def tick(self):
        now = time.monotonic()
        if now - self.t0 > self.a.timeout:
            (self.a.out / "phase-timeout").write_text(self.phase)
            return self.finish()
        if self.busy:
            return
        (self.tick_video if self.a.by == "video" else self.tick_link)(now)

    def tick_link(self, now):
        link, video = self.backend.status.get("linkView"), self.backend.video.state
        ok = link == "CONNECTED" and video == "playing"
        if self.phase in ("wait-ok", "wait-recovered"):
            if self.held(ok, STABLE_OK_S, now):
                first = self.phase == "wait-ok"
                self.since = None
                self.shots("01-02-connected" if first else "05-06-recovered",
                           lambda: self.go("wait-lost" if first else "done"))
        elif self.phase == "wait-lost":
            if self.held(link != "CONNECTED", STABLE_FAULT_S, now):
                self.since = None
                self.shots("03-04-lost", lambda: self.go("wait-recovered"))

    def tick_video(self, now):
        src = self.backend.videoSource
        live, display = bool(src.get("live")), src.get("display", self.backend.video.state)
        if self.phase in ("wait-ok", "wait-recovered"):
            if self.held(live, STABLE_OK_S, now):
                self.since = None
                label = "stream" if self.phase == "wait-ok" else f"recovered-{self.last_fault}"
                self.shots(self.prefix(label), lambda: self.go("wait-fault" if self.pending else "done"))
        elif self.phase == "wait-fault":
            fault = display if display in self.pending else None
            if fault is not None and fault != getattr(self, "_fault_seen", None):
                self.since = None                        # другое состояние отказа — отсчёт заново
            self._fault_seen = fault
            if self.held(fault is not None, STABLE_FAULT_S, now):
                self.since = None
                self.pending.remove(fault)
                self.last_fault = fault
                self.shots(self.prefix(fault), lambda: self.go("wait-recovered"))

    def go(self, phase):
        self.phase = phase
        if phase == "done":
            self.rc = 0
            self.finish()

    def finish(self):
        (self.a.out / "states.json").write_text(json.dumps(self.states, ensure_ascii=False, indent=1))
        self.timer.stop()
        self.loop.quit()

    def run(self):
        from PySide6.QtCore import QEventLoop, QTimer
        self.ui.window.resize(1280, 800)
        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(200)
        self.loop = QEventLoop()
        self.loop.exec()
        return self.rc


def main(argv=None):
    a = parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from sheregesh.link.tls import LinkConfig
    from sheregesh_console import app as capp
    from sheregesh_console.backend import Backend

    a.out.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Fusion")
    qapp = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    qapp.setApplicationName("sheregesh-console")
    capp.compile_translations()
    host, _, port = a.peer.rpartition(":")
    backend = Backend(Path(tempfile.mkdtemp(prefix="gui-capture-")), peer=(host, int(port)),
                      link_cfg=LinkConfig.load(a.link_config), link_status_file=str(a.out / "link-status.json"))
    ui = capp.Ui(backend)
    try:
        return Capture(a, backend, ui, capp.PAGE_NAMES).run()
    finally:
        ui.close()


if __name__ == "__main__":
    sys.exit(main())
