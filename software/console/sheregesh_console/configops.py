# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Блокирующие операции конфигурации и файлов пульта вне потока интерфейса (как sysinfo.py).

Применение, резервная копия и восстановление (ConfigStore: fsync, health-check), выгрузка журнала на «USB»
(logops.write_export) идут по одной в фоновом потоке. Пока операция идёт, новая не принимается (busy): изменения конфигурации не пересекаются, а
подтверждение пользователя остаётся за вызывающим. Итог доставляется сигналом finished в поток интерфейса: только там
владелец (Backend) меняет QObject, модели и монитор. Функция операции не обращается к QObject.

close() ждёт текущую операцию не дольше заданного времени: fsync не прерывается на середине, а зависшая операция
не держит закрытие приложения."""
import threading

from PySide6.QtCore import Property, QObject, Signal


class ConfigOps(QObject):
    busyChanged = Signal()
    finished = Signal(str, object)      # операция, итог (результат функции или исключение) — в потоке интерфейса
    _done = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._op = None
        self._thread = None
        self._closed = False
        self._done.connect(self._finish)

    @Property(bool, notify=busyChanged)
    def busy(self):
        return self._op is not None

    @property
    def op(self):
        return self._op

    def submit(self, op: str, fn) -> bool:
        """Запустить fn() в фоне; False — уже идёт другая операция или исполнитель закрыт."""
        if self._op is not None or self._closed:
            return False
        self._op = op

        def run():
            try:
                result = fn()
            except Exception as e:      # noqa: BLE001 — итог показывается пользователю текстом
                result = e
            self._done.emit(op, result)
        self._thread = threading.Thread(target=run, name=f"config-{op}", daemon=True)
        self._thread.start()
        self.busyChanged.emit()
        return True

    def _finish(self, op, result):
        self._op = None
        if not self._closed:                # сначала итог (lastOk, lastExport), затем «не занято»: QML читает итог
            self.finished.emit(op, result)  # по busyChanged
        self.busyChanged.emit()

    def close(self, timeout_s: float = 5.0) -> bool:
        """Новых операций нет; текущая — ждать до timeout_s. True — фоновых операций не осталось."""
        self._closed = True
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout_s)
        return t is None or not t.is_alive()

