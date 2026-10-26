# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сведения о системе для раздела «Система» (MainMinimal): OTA и слоты (sheregesh-ota-client status), DEV-выпуск
(sheregesh-deployd status). Только чтение; команды выполняются в фоновом потоке по запросу (открытие раздела),
не в потоке интерфейса. Где команд нет (рабочая станция, CI) — поля пустые, available=false."""
import json
import shutil
import subprocess
import threading
import time

from PySide6.QtCore import Property, QObject, Signal, Slot

COMMANDS = {"ota": ["sheregesh-ota-client", "status"], "deploy": ["sheregesh-deployd", "status"]}


REFRESH_S = 30.0      # состояние OTA и выпуска меняется редко; команды — Python-процессы, не чаще раза в 30 с


def _run(argv, timeout=5.0):
    if shutil.which(argv[0]) is None:
        return None
    if shutil.which("nice"):                     # запуск команд не отнимает процессор у видео (GUI-12)
        argv = ["nice", "-n", "10", *argv]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def summarize(ota, deploy):
    """Плоский вид для QML: слоты, состояние OTA, версия образа, текущий и предыдущий DEV-выпуск."""
    ota, deploy = ota or {}, deploy or {}
    slots, state, build = ota.get("slots") or {}, ota.get("state") or {}, ota.get("build") or {}
    return {"available": bool(ota or deploy),
            "otaState": str(state.get("state") or ""), "slotActive": str(slots.get("active") or ""),
            "slotOther": str(slots.get("other") or ""), "tryboot": bool(slots.get("tryboot_now")),
            "imageVersion": str(build.get("ota_version") or ""), "imageCommit": str(build.get("commit") or "")[:12],
            "consistent": ota.get("consistent") if isinstance(ota.get("consistent"), bool) else None,
            "switching": str(ota.get("switching") or ""),
            "release": str(deploy.get("current") or ""), "previous": str(deploy.get("previous") or ""),
            "role": str(deploy.get("role") or "")}


class SystemInfo(QObject):
    changed = Signal()
    _done = Signal(object)

    def __init__(self, parent=None, run=_run):
        super().__init__(parent)
        self._run = run
        self._info = summarize(None, None)
        self._busy = False
        self._at = 0.0
        self._done.connect(self._apply)

    @Property(dict, notify=changed)
    def info(self):
        return self._info

    @Property(bool, notify=changed)
    def busy(self):
        return self._busy

    @Slot()
    def refresh(self):
        """Запросить сведения (не чаще раза в REFRESH_S); результат придёт сигналом changed."""
        if self._busy or (self._at and time.monotonic() - self._at < REFRESH_S):
            return
        self._busy = True
        self.changed.emit()
        threading.Thread(target=lambda: self._done.emit(summarize(self._run(COMMANDS["ota"]),
                                                                  self._run(COMMANDS["deploy"]))),
                         name="sysinfo", daemon=True).start()

    def _apply(self, info):
        self._info, self._busy, self._at = info, False, time.monotonic()
        self.changed.emit()
