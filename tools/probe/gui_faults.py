#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-UI-003 на пульте: каждая запись каталога неисправностей, вызванная эмулятором, видна в GUI (страница «События»:
код, текст, действия) не позднее 3 с; после снятия — устранена без перезапуска. Запускается НА пульте (pi1) от
sheregesh-ui рядом с работающей службой: код выпуска, интерфейс Minimal, эмулятор стенда внутри процесса (без связи),
программный рендерер Qt.

  PYTHONPATH=<выпуск>/software:<выпуск>/software/console:<выпуск>/software/emulators python3 gui_faults.py --out DIR [--lang ru]

В DIR: faults.json (по коду: время до показа, тексты строки списка и каталога, снятие), faults.png (последний кадр).
Ограничение: путь cage/Wayland/DRM/HDMI не проверяется.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SHOWN_MAX_S = 3.0
PARAMS = {"sensor": "soc", "value": 71.2, "limit": 70, "rpm": 5000, "duty": 40, "percent": 15, "source": "battery",
          "timeout": 3, "peer": "2.0", "local": "1.5", "reason": "major", "state": "no-video", "age": 2.0, "count": 1,
          "code": "TEMP-HIGH", "codes": "TEMP-HIGH", "detail": "probe", "revision": "r000002", "active": "r000001",
          "service": "video-rx", "restarts": 1, "services": "video-tx (failed)"}


def walk(item):
    yield item
    for c in item.childItems():
        yield from walk(c)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--timeout", type=float, default=10.0)
    a = ap.parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    from PySide6.QtCore import Q_ARG, QMetaObject
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from sheregesh.faults import CATALOG
    from sheregesh_console import app as capp
    from sheregesh_console.backend import Backend

    a.out.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Fusion")
    qapp = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    qapp.setApplicationName("sheregesh-console")
    capp.compile_translations()
    backend = Backend(Path(tempfile.mkdtemp(prefix="gui-faults-")))          # эмулятор стенда в процессе
    ui = capp.Ui(backend, layout="minimal")
    results = {}
    try:
        ui.window.resize(1280, 800)
        QMetaObject.invokeMethod(ui.window, "open", Q_ARG("QVariant", "diagnostics"))
        QMetaObject.invokeMethod(ui.window, "showDetails", Q_ARG("QVariant", "DiagnosticsPage"))

        def pump(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                qapp.processEvents()
                time.sleep(0.02)
        pump(1.5)

        def row_texts():
            lst = next((i for i in walk(ui.window.contentItem()) if i.objectName() == "eventsList"), None)
            return [] if lst is None else [i.property("text") for i in walk(lst) if isinstance(i.property("text"), str)]

        lang = backend.lang
        for code in sorted(CATALOG):
            params = {k: v for k, v in PARAMS.items()}
            log, clock = backend.bench.monitor.log, backend.bench.clock   # код — позиционно (у REMOTE-EVENT свой «code»)
            t0 = time.monotonic()
            ev = log.raise_(clock(), code, **params) or log._active.get(code)
            row = backend._render_event(ev)          # как пульт готовит строку: каталог + названия на языке GUI
            text, action = row["text"], row["action"]
            shown = None
            while time.monotonic() - t0 < a.timeout:
                qapp.processEvents()
                texts = row_texts()
                if code in texts and any(t.startswith(text[:24]) for t in texts) and action in texts:
                    shown = round(time.monotonic() - t0, 2)
                    break
                time.sleep(0.02)
            log.resolve(clock(), code)
            t1 = time.monotonic()
            cleared = None
            while CATALOG[code].latched and time.monotonic() - t1 < a.timeout:
                qapp.processEvents()
                if not backend.bench.monitor.log.is_active(code) and \
                        all(e["code"] != code for e in backend.activeEvents):
                    cleared = round(time.monotonic() - t1, 2)
                    break
                time.sleep(0.02)
            ok = shown is not None and shown <= SHOWN_MAX_S and (cleared is not None or not CATALOG[code].latched)
            results[code] = {"verdict": "PASS" if ok else "FAIL", "shown_s": shown, "latched": CATALOG[code].latched,
                             "cleared_s": cleared, "text": text, "action": action}
            print(code, results[code]["verdict"], shown, cleared, flush=True)
        ui.window.grabWindow().save(str(a.out / "faults.png"))
    finally:
        ui.close()
    summary = {"lang": lang, "total": len(results), "pass": sum(r["verdict"] == "PASS" for r in results.values()),
               "max_shown_s": max((r["shown_s"] or 99) for r in results.values()), "results": results}
    (a.out / "faults.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps({k: summary[k] for k in ("lang", "total", "pass", "max_shown_s")}, ensure_ascii=False))
    return 0 if summary["pass"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
