# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запуск GUI пульта на эмуляторах.

  python3 -m sheregesh_console [--demo] [--speed N] [--data-dir DIR]
  python3 -m sheregesh_console --demo --screenshots DIR     # снимки экранов и выход (без дисплея)
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer, QTranslator, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow  # noqa: F401 — регистрирует тип окна для rootObjects()
from PySide6.QtQuickControls2 import QQuickStyle

from .backend import Backend

PKG = Path(__file__).resolve().parent
QML = PKG / "qml"
I18N = PKG / "i18n"
LANGS = ("ru",)   # исходные строки QML — английские; переводы — файлы .ts/.qm


def qt_tool(name):
    """Кандидаты инструмента Qt: рядом с интерпретатором (.venv/bin), в PATH, в каталоге Qt дистрибутива."""
    here = Path(sys.executable).parent
    out = [str(here / f"pyside6-{name}"), f"pyside6-{name}", name, f"/usr/lib/qt6/bin/{name}"]
    return [c for c in out if shutil.which(c)]


def compile_translations(out_dir: Path = I18N) -> list[Path]:
    """Собирает .qm из .ts (pyside6-lrelease или lrelease). На устройстве .qm входят в пакет."""
    out = []
    for lang in LANGS:
        ts, qm = I18N / f"sheregesh_{lang}.ts", out_dir / f"sheregesh_{lang}.qm"
        if qm.exists() and qm.stat().st_mtime >= ts.stat().st_mtime:
            out.append(qm)
            continue
        for tool in qt_tool("lrelease"):
            try:
                subprocess.run([tool, "-silent", str(ts), "-qm", str(qm)], check=True, capture_output=True)
                out.append(qm)
                break
            except (OSError, subprocess.CalledProcessError):
                continue
        else:
            raise RuntimeError("не найден lrelease для сборки переводов")
    return out


class Ui:
    """Движок QML + переводчик, следящий за языком из конфигурации."""

    def __init__(self, backend: Backend):
        self.backend = backend
        self.engine = QQmlApplicationEngine()
        self.translator = QTranslator()
        self._lang = None
        self.engine.rootContext().setContextProperty("backend", backend)
        self._apply_language()
        backend.appearanceChanged.connect(self._apply_language)
        self.engine.load(QUrl.fromLocalFile(str(QML / "Main.qml")))
        if not self.engine.rootObjects():
            raise RuntimeError("не удалось загрузить Main.qml")
        self.window = self.engine.rootObjects()[0]

    def _apply_language(self):
        lang = self.backend.language
        if lang == self._lang:
            return
        app = QGuiApplication.instance()
        app.removeTranslator(self.translator)
        if lang != "en":
            qm = I18N / f"sheregesh_{lang}.qm"
            if not self.translator.load(str(qm)):
                raise RuntimeError(f"нет перевода {qm}; выполните сборку переводов")
            app.installTranslator(self.translator)
        self._lang = lang
        self.engine.retranslate()

    def close(self):
        """Уничтожить QML раньше бэкенда, иначе привязки при закрытии обращаются к удалённому объекту."""
        self.backend.stop()
        QGuiApplication.instance().removeTranslator(self.translator)
        self.window = None
        engine, self.engine = self.engine, None
        del engine
        QGuiApplication.processEvents()

    def find(self, name):
        from PySide6.QtCore import QObject
        return self.window.findChild(QObject, name)

    def grab(self, path):
        QGuiApplication.processEvents()
        img = self.window.grabWindow()
        img.save(str(path))
        return path


def capture_demo(ui: Ui, out_dir: Path):
    """Детерминированный проход демонстрационного сценария со снимками экранов в контрольных точках."""
    from sheregesh_emulators.demo import DEMO
    out_dir.mkdir(parents=True, exist_ok=True)
    b = ui.backend
    shots = []

    def shot(name, page):
        ui.window.setProperty("currentPage", page)
        shots.append(ui.grab(out_dir / f"{name}.png"))

    b.run_until(DEMO.checkpoints["start"]);     shot("01-dashboard-normal", 0)
    b.run_until(DEMO.checkpoints["heating"]);   shot("02-dashboard-heating", 0)
    b.run_until(DEMO.checkpoints["warning"]);   shot("03-dashboard-warning", 0)
    b.run_until(DEMO.checkpoints["peer-lost"]); shot("04-dashboard-peer-lost", 0); shot("05-diagnostics-events", 1)
    b.run_until(DEMO.checkpoints["recovered"]); shot("06-dashboard-recovered", 0); shot("07-diagnostics-history", 1)
    shot("08-settings-ru", 2)
    form = dict(b.config)
    form.update(language="en", theme="light", ui_scale=125)
    b.applyForm(form, True)
    b.reload()                                   # повторная загрузка с диска
    shot("09-settings-en-light-125", 2)
    shot("10-dashboard-en-light-125", 0)
    return shots


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheregesh_console", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="демонстрационный сценарий S1")
    ap.add_argument("--speed", type=float, default=4.0, help="ускорение модельного времени (по умолчанию 4)")
    ap.add_argument("--data-dir", help="каталог данных эмулятора (по умолчанию временный)")
    ap.add_argument("--screenshots", metavar="DIR", help="снять экраны сценария в DIR и выйти")
    args = ap.parse_args(argv)

    if args.screenshots:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("sheregesh-console")
    compile_translations()
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="sheregesh-emu-"))
    backend = Backend(data_dir, demo=args.demo or bool(args.screenshots), speed=args.speed,
                      autostart=not args.screenshots)
    ui = Ui(backend)
    print(f"Данные эмулятора: {data_dir}", flush=True)
    if args.screenshots:
        code = {"rc": 1}

        def run():
            try:
                for p in capture_demo(ui, Path(args.screenshots)):
                    print(p, flush=True)
                code["rc"] = 0
            finally:
                app.quit()   # выход даже при ошибке, иначе процесс без дисплея не завершится
        QTimer.singleShot(0, run)
        app.exec()
        ui.close()
        return code["rc"]
    rc = app.exec()
    ui.close()
    return rc
