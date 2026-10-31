#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Снимок раздела «Система» реального GUI пульта без дисплея (TEST-UPD-004: событие OTA видно в GUI). Запускается НА
пульте (pi1) от sheregesh-ui рядом с работающей службой: тот же код выпуска (Backend, MainMinimal.qml, SystemInfo с
`sheregesh-ota-client status`), программный рендерер Qt; связь не нужна (адрес блока — заведомо пустой порт).

  PYTHONPATH=<выпуск>/software:<выпуск>/software/console:… python3 gui_system_panel.py --out DIR [--timeout 30]

В DIR: system.png и system.json — значения строк OTA, слотов и образа, как их рисует QML (свойство value), и сведения
SystemInfo в тот же момент. Ограничение: путь cage/Wayland/DRM/HDMI не проверяется.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def walk(item):
    """Визуальное дерево QML: элементы панелей — дети contentItem (childItems), а не QObject.children."""
    yield item
    for c in item.childItems():
        yield from walk(c)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=30.0)
    a = ap.parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    from PySide6.QtCore import Q_ARG, QMetaObject
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from sheregesh_console import app as capp
    from sheregesh_console.backend import Backend

    a.out.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Fusion")
    qapp = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    qapp.setApplicationName("sheregesh-console")
    capp.compile_translations()
    backend = Backend(Path(tempfile.mkdtemp(prefix="gui-system-")), peer=("127.0.0.1", 9))
    ui = capp.Ui(backend, layout="minimal")
    try:
        ui.window.resize(1280, 800)
        QMetaObject.invokeMethod(ui.window, "open", Q_ARG("QVariant", "system"))   # как кнопка нижнего меню
        t0 = time.monotonic()
        row = None
        while time.monotonic() - t0 < a.timeout:
            qapp.processEvents()
            row = next((i for i in walk(ui.window.contentItem()) if i.objectName() == "miniSysOta"), None)
            if row is not None and row.property("value") not in (None, "", "…", "—"):
                break
            time.sleep(0.05)
        qapp.processEvents()
        time.sleep(0.3)
        qapp.processEvents()
        ui.window.grabWindow().save(str(a.out / "system.png"))
        rows = {}
        for o in walk(ui.window.contentItem()):
            label = o.property("label") if o.metaObject().indexOfProperty("label") >= 0 else None
            value = o.property("value") if o.metaObject().indexOfProperty("value") >= 0 else None
            if isinstance(label, str) and isinstance(value, str) and label:
                rows[label] = value
        out = {"ota_row": row.property("value") if row is not None else None, "rows": rows,
               "system_info": dict(ui.system_info.info), "elapsed_s": round(time.monotonic() - t0, 2)}
        (a.out / "system.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["ota_row"] not in (None, "", "…", "—") else 1
    finally:
        ui.close()


if __name__ == "__main__":
    sys.exit(main())
