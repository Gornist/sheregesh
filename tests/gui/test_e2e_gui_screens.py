# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DUAL-DEVICE E2E, экраны пульта на 1920×1200 (эталонный дисплей, REQ-P-002) и 1280×800 в состояниях
CONNECTED + видео и DISCONNECTED: снимки offscreen и программная проверка геометрии ключевых элементов
(ничего не выходит за окно и свою плитку, индикаторы заголовка не перекрываются и не обрезаны).

Это L3 (offscreen, программный рендерер) — НЕ PASS на физическом дисплее (L4: Waveshare 10.1EP-CAPLCD).
Снимки: SHEREGESH_E2E_GUI_OUT=docs/acceptance/e2e/gui — туда; иначе во временный каталог.
"""
import json
import os
import signal
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, QRectF, Qt
from PySide6.QtGui import QImage
from PySide6.QtQuickControls2 import QQuickStyle

from procs import Agent, free_port
from sheregesh import video as gst
from sheregesh_console import backend as backend_mod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump, wait_until

QQuickStyle.setStyle("Fusion")
SIZES = [(1920, 1200), (1280, 800)]
PAGES = ["dashboard", "video", "diagnostics"]
TILES = ["tileState", "tilePower", "tileLink", "tileTemp", "tileEvents"]
HEADER = ["linkBadge", "stateBadge"]
needs_gst = pytest.mark.skipif(gst.choose_codec() is None, reason="нет работающего кодека GStreamer")


def find(u, name):
    o = u.window.findChild(QObject, name)
    assert o is not None, name
    return o


def rect(item):
    return item.mapRectToScene(QRectF(0, 0, item.width(), item.height()))


def outside_window(u):
    """Видимые элементы за границей окна (кроме содержимого прокручиваемых областей)."""
    win = QRectF(0, 0, u.window.width(), u.window.height()).adjusted(-1, -1, 1, 1)
    bad = []

    def walk(item, flick):
        flick = flick or item.inherits("QQuickFlickable")
        for ch in item.childItems():
            if not ch.isVisible() or ch.width() <= 0 or ch.height() <= 0:
                continue
            if not flick and not win.contains(rect(ch)):
                bad.append(ch.objectName() or ch.metaObject().className())
            walk(ch, flick)
    walk(u.window.contentItem(), False)
    walk(u.window.property("header"), False)
    return bad


def tile_overflow(u):
    """Текст плитки главного экрана не выходит за её рамку; плитки не перекрываются."""
    bad = []
    rects = {n: rect(find(u, n)) for n in TILES}
    for name, r in rects.items():
        tile = find(u, name)

        def walk(item):
            for ch in item.childItems():
                if not ch.isVisible() or ch.width() <= 0 or ch.height() <= 0:
                    continue
                if ch.inherits("QQuickText") and not r.adjusted(-1, -1, 1, 1).contains(rect(ch)):
                    bad.append(f"{name}/{ch.objectName() or ch.property('text')[:30]}")
                walk(ch)
        walk(tile)
    names = list(rects)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if rects[a].adjusted(1, 1, -1, -1).intersects(rects[b]):
                bad.append(f"{a}∩{b}")
    return bad


def header_problems(u):
    bad = []
    rs = {n: rect(find(u, n)) for n in HEADER}
    if rs["linkBadge"].adjusted(1, 1, -1, -1).intersects(rs["stateBadge"]):
        bad.append("linkBadge∩stateBadge")
    for n in ("linkBadgeText", "stateBadgeText"):
        t = find(u, n)
        if t.property("truncated") or t.property("implicitWidth") > t.parentItem().width():
            bad.append(f"{n} обрезан")
    note = find(u, "emulationNote")              # может сокращаться, но не наезжать на индикаторы
    if rect(note).right() > rs["linkBadge"].left() + 1:
        bad.append("emulationNote∩linkBadge")
    return bad


def video_problems(u):
    bad = []
    frame = rect(find(u, "videoView")).adjusted(-4, -4, 4, 4)
    for n in ("videoPlaceholder", "videoReason"):
        o = find(u, n)
        if o.property("visible") and not frame.contains(rect(o)):
            bad.append(f"{n} вне кадра")
    if rect(find(u, "videoStateBadge")).intersects(frame.adjusted(4, 4, -4, -4)):
        bad.append("videoStateBadge∩videoView")
    return bad


def save_small(u, path):
    """PNG с палитрой 256 цветов: экран интерфейса без потери читаемости, в несколько раз меньше по объёму."""
    pump(50)
    img = u.window.grabWindow().convertToFormat(QImage.Format_Indexed8, Qt.ThresholdDither | Qt.AvoidDither)
    assert img.save(str(path), "PNG", 9)
    return path


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_E2E_GUI_OUT") or tmp_path / "e2e-gui")
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_all(u, state, out_dir, report):
    for w, h in SIZES:
        u.window.resize(w, h)
        pump(250)
        for page in PAGES:
            u.window.setProperty("currentPage", PAGE_NAMES.index(page))
            pump(1200 if state == "connected" else 150)     # частота кадров в подписи — за последнюю секунду
            problems = outside_window(u) + header_problems(u)
            problems += tile_overflow(u) if page == "dashboard" else video_problems(u) if page == "video" else []
            shot = save_small(u, out_dir / f"{w}x{h}-{state}-{page}.png")
            report.append({"size": f"{w}x{h}", "state": state, "page": page, "png": shot.name,
                           "link_badge": find(u, "linkBadgeText").property("text"),
                           "video_state": u.backend.video.state, "problems": problems})
            assert problems == [], f"{w}×{h} {state} {page}: {problems}"


@needs_gst
@pytest.mark.test_id("TEST-UI-002")
def test_screens_connected_with_video_and_disconnected_at_both_resolutions(qapp, tmp_path, out_dir, monkeypatch):
    monkeypatch.setattr(backend_mod, "RECONNECT_WINDOW_S", 2.0)
    compile_translations()
    vport = free_port(kind=2)
    agent = Agent("--heartbeat-interval", "0.2", "--video", "--video-port", str(vport), "--video-width", "640",
                  "--video-height", "360", "--video-fps", "25")
    u = Ui(Backend(tmp_path / "data", autostart=True, peer=("127.0.0.1", agent.port)))
    report = []
    try:
        assert wait_until(lambda: u.backend.status["linkView"] == "CONNECTED", 10)
        assert wait_until(lambda: u.backend.video.state == "playing", 15), u.backend.video.metrics()
        pump(1500)                                      # частота и телеметрия установились
        assert u.backend.video.state == "playing"
        check_all(u, "connected", out_dir, report)
        agent.signal(signal.SIGKILL)
        agent.proc.wait(5)
        assert wait_until(lambda: u.backend.status["linkView"] == "DISCONNECTED", 15)
        assert wait_until(lambda: u.backend.video.state == "no-video", 6)
        check_all(u, "disconnected", out_dir, report)
    finally:
        agent.stop()
        u.close()
        (out_dir / "geometry.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software (L3) — не проверка физического дисплея",
             "checks": ["элементы в пределах окна", "текст плиток в пределах плитки, плитки не перекрываются",
                        "индикаторы заголовка не перекрываются и не обрезаны", "подписи видео в пределах кадра"],
             "shots": report}, ensure_ascii=False, indent=1), encoding="utf-8")
