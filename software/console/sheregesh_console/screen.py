# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Гашение экрана пульта по бездействию и защита пробуждения (REQ-UI-005).

После screen_timeout_s секунд без ввода (касание, кнопки мыши, клавиши, колесо) окно закрывается чёрным слоем
(QML: screenGuard.blanked) — GUI, приём видео и службы продолжают работать, гасится только изображение.
Первое нажатие после гашения только будит экран: оно и его отпускание (касание целиком) не доходят до элементов.
0 — не гасить. Пока включена передача управления (стики), экран не гаснет: оператор ведёт устройство.
Подсветку дисплея не выключает — это свойство выбранного дисплея (железо отложено).
"""
import time

from PySide6.QtCore import Property, QEvent, QObject, QTimer, Signal, Slot

WAKE = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.TouchBegin,
        QEvent.Type.KeyPress, QEvent.Type.Wheel, QEvent.Type.TabletPress}
GESTURE_END = {QEvent.Type.MouseButtonRelease: QEvent.Type.MouseButtonPress,
               QEvent.Type.TouchEnd: QEvent.Type.TouchBegin, QEvent.Type.TouchCancel: QEvent.Type.TouchBegin,
               QEvent.Type.KeyRelease: QEvent.Type.KeyPress, QEvent.Type.TabletRelease: QEvent.Type.TabletPress}
SWALLOW = WAKE | set(GESTURE_END) | {QEvent.Type.TouchUpdate, QEvent.Type.MouseMove, QEvent.Type.TabletMove}


class ScreenGuard(QObject):
    """Фильтр событий приложения: учёт бездействия, гашение, поглощение будящего жеста."""
    changed = Signal()

    def __init__(self, timeout_s=lambda: 300, busy=lambda: False, clock=time.monotonic, parent=None, tick_ms=500):
        super().__init__(parent)
        self.timeout_s, self.busy, self.clock = timeout_s, busy, clock
        self._last_input = clock()
        self._blanked = False
        self._swallow = None          # тип начала будящего жеста, пока он не закончился
        self.wakes = 0
        self.swallowed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.check)
        self._timer.start(tick_ms)

    @Property(bool, notify=changed)
    def blanked(self):
        return self._blanked

    def idle_s(self):
        return self.clock() - self._last_input

    @Slot()
    def check(self):
        t = self.timeout_s() or 0
        if self._blanked or t <= 0:
            return
        if self.busy():
            self._last_input = self.clock()                    # передача управления — экран остаётся включён
            return
        if self.idle_s() >= t:
            self._blanked = True
            self.changed.emit()

    def wake(self):
        self._last_input = self.clock()
        if self._blanked:
            self._blanked = False
            self.wakes += 1
            self.changed.emit()

    def eventFilter(self, obj, ev):
        et = ev.type()
        if self._swallow is not None and et in SWALLOW:        # продолжение будящего жеста
            self.swallowed += 1
            if GESTURE_END.get(et) == self._swallow:
                self._swallow = None
            return True
        if et in WAKE:
            if self._blanked:
                self.wake()
                self._swallow = {QEvent.Type.MouseButtonDblClick: QEvent.Type.MouseButtonPress,
                                 QEvent.Type.Wheel: None}.get(et, et)
                self.swallowed += 1
                return True
            self._last_input = self.clock()
        return False

    def snapshot(self):
        return {"blanked": self._blanked, "idle_s": round(self.idle_s(), 1), "timeout_s": self.timeout_s(),
                "wakes": self.wakes, "swallowed": self.swallowed}
