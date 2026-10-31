# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Гашение экрана и защита пробуждения (sheregesh_console.screen, REQ-UI-005, TEST-UI-005): после тайм-аута без
ввода экран гаснет, монитор и связь продолжают работать; первое касание или нажатие только будит экран и не доходит до
элементов (20/20); конфигурация и состояние диагностики до и после совпадают. Без дисплея, offscreen."""
import hashlib
import json

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent

from sheregesh_console.app import Ui, compile_translations
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.screen import ScreenGuard


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def click(target, kind=QEvent.Type.MouseButtonPress):
    pos = QPointF(10, 10)
    return QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                       Qt.MouseButton.LeftButton if kind == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)


def test_blanks_after_timeout_and_first_press_only_wakes(qapp):
    clock, busy = Clock(), {"v": False}
    g = ScreenGuard(timeout_s=lambda: 60, busy=lambda: busy["v"], clock=clock, tick_ms=10 ** 6)
    obj = qapp
    assert g.eventFilter(obj, click(obj)) is False                 # обычный ввод проходит
    clock.t = 59
    g.check()
    assert not g.blanked
    clock.t = 120
    g.check()
    assert g.blanked
    assert g.eventFilter(obj, click(obj)) is True                  # будит — поглощено
    assert not g.blanked
    assert g.eventFilter(obj, click(obj, QEvent.Type.MouseButtonRelease)) is True   # отпускание того же жеста
    assert g.eventFilter(obj, click(obj)) is False                 # дальше — как обычно
    assert g.wakes == 1


def test_key_wake_swallows_press_and_release(qapp):
    clock = Clock()
    g = ScreenGuard(timeout_s=lambda: 10, clock=clock, tick_ms=10 ** 6)
    clock.t = 11
    g.check()
    press = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    release = QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
    assert g.eventFilter(qapp, press) is True and g.eventFilter(qapp, release) is True
    assert g.eventFilter(qapp, press) is False


def test_zero_timeout_and_active_control_keep_screen_on(qapp):
    clock = Clock()
    g = ScreenGuard(timeout_s=lambda: 0, clock=clock, tick_ms=10 ** 6)
    clock.t = 10 ** 6
    g.check()
    assert not g.blanked
    busy = {"v": True}
    g = ScreenGuard(timeout_s=lambda: 30, busy=lambda: busy["v"], clock=clock, tick_ms=10 ** 6)
    clock.t += 100
    g.check()
    assert not g.blanked                                             # передача управления включена
    busy["v"] = False
    clock.t += 29
    g.check()
    assert not g.blanked                                             # отсчёт — с момента выключения передачи
    clock.t += 2
    g.check()
    assert g.blanked


@pytest.fixture
def ui(qapp, tmp_path):
    compile_translations()
    b = Backend(tmp_path)
    u = Ui(b, layout="minimal")
    u.window.resize(1280, 800)
    pump(500)
    yield u
    u.close()


def _config_hash(b):
    return hashlib.sha256(json.dumps(b.config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.mark.test_id("TEST-UI-005")
def test_wake_touch_never_reaches_controls_20_of_20(ui):
    """Окно Minimal, настоящая доставка событий мыши окну: 20 раз — погасить, нажать на кнопку навигации «Система»:
    экран проснулся, раздел не открыт; второе нажатие — открыт. Монитор пульта идёт во время гашения; конфигурация и
    активные события до и после совпадают."""
    b, g, win = ui.backend, ui.screen, ui.window
    def walk(item):
        yield item
        for c in item.childItems():
            yield from walk(c)
    root = win.contentItem()
    while root.parentItem() is not None:                               # Overlay — сосед содержимого окна
        root = root.parentItem()
    items = {o.objectName(): o for o in walk(root) if o.objectName()}
    blank, nav = items["screenBlank"], items["nav_system"]
    p = nav.mapToScene(QPointF(nav.width() / 2, nav.height() / 2))
    cfg0, ev0 = _config_hash(b), {e.code for e in b.bench.monitor.log.active()}

    def tap():
        for kind, btns in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                           (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
            QGuiApplication.sendEvent(win, QMouseEvent(kind, p, win.mapToGlobal(p), Qt.MouseButton.LeftButton, btns,
                                                       Qt.KeyboardModifier.NoModifier))
        pump(150)
    for i in range(20):
        win.setProperty("section", "")
        pump(100)
        g._last_input -= 10 ** 6                                       # бездействие дольше тайм-аута
        g.check()
        assert g.blanked and wait_until(lambda: blank.isVisible(), 1)
        t0 = b.bench.monitor.clock()
        pump(600)
        assert b.bench.monitor.clock() > t0                            # монитор и связь работают при гашении
        tap()
        assert not g.blanked and not blank.isVisible() and win.property("section") == "", (i, win.property("section"))
        tap()
        assert win.property("section") == "system", (i, win.property("section"))
    assert g.wakes == 20
    assert _config_hash(b) == cfg0 and {e.code for e in b.bench.monitor.log.active()} == ev0
