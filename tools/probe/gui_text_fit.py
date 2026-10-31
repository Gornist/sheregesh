#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""TEST-UI-001 на пульте: язык RU/EN переключается без перезапуска; на критичных экранах интерфейса Minimal на обоих
языках нет обрезанного текста и элементов за краем окна. Запускается НА пульте (pi1) от sheregesh-ui рядом с работающей
службой: код выпуска, эмулятор стенда в процессе, программный рендерер Qt.

  PYTHONPATH=<выпуск>/software:<выпуск>/software/console:<выпуск>/software/emulators python3 gui_text_fit.py --out DIR

Экраны: главный; разделы video, control, module, diagnostics, system; «Подробно» — DiagnosticsPage («События»),
AboutPage, SettingsPage. Разрешения 1280×800 и 1920×1200 (эталонный дисплей). Проблема — видимая надпись с
truncated (сокращена многоточием) или шире своего места без переноса, либо видимый элемент за краем окна (кроме
содержимого прокручиваемых областей). Язык меняется штатно — ревизией конфигурации (applyForm), без перезапуска.
В DIR: text_fit.json, PNG экранов.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SECTIONS = ["video", "control", "module", "diagnostics", "system"]
DETAILS = [("system", "AboutPage"), ("diagnostics", "DiagnosticsPage"), ("system", "SettingsPage")]
SIZES = [(1280, 800), (1920, 1200)]


def walk(item):
    yield item
    for c in item.childItems():
        yield from walk(c)


def problems(window):
    from PySide6.QtCore import QRectF
    win = QRectF(0, 0, window.width(), window.height())
    bad = []

    def visit(item, in_flick):
        flick = in_flick or item.inherits("QQuickFlickable")
        for ch in item.childItems():
            if not ch.isVisible() or ch.width() <= 0 or ch.height() <= 0 or ch.opacity() == 0:
                continue
            r = ch.mapRectToScene(QRectF(0, 0, ch.width(), ch.height()))
            name = ch.objectName() or ch.metaObject().className()
            if not flick and not win.adjusted(-1, -1, 1, 1).contains(r):
                bad.append({"kind": "outside", "item": name, "rect": [round(r.x()), round(r.y()), round(r.width()),
                                                                        round(r.height())]})
            text = ch.property("text")
            if isinstance(text, str) and text.strip() and ch.metaObject().indexOfProperty("truncated") >= 0:
                if ch.property("truncated"):
                    bad.append({"kind": "truncated", "item": name, "text": text[:80]})
                elif ch.property("lineCount") == 1 and ch.property("implicitWidth") > ch.width() + 1:
                    # одна строка шире своего места: перенос не сработал или выключен — текст выходит за границу
                    bad.append({"kind": "too-wide", "item": name, "text": text[:80],
                                "need": round(ch.property("implicitWidth")), "have": round(ch.width())})
            visit(ch, flick)
    visit(window.contentItem(), False)
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QUICK_BACKEND"] = "software"
    from PySide6.QtCore import Q_ARG, QMetaObject
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuickControls2 import QQuickStyle
    from sheregesh_console import app as capp
    from sheregesh_console.backend import Backend
    from sheregesh_console.qtwait import finish

    a.out.mkdir(parents=True, exist_ok=True)
    QQuickStyle.setStyle("Fusion")
    qapp = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    qapp.setApplicationName("sheregesh-console")
    capp.compile_translations()
    backend = Backend(Path(tempfile.mkdtemp(prefix="gui-fit-")))
    ui = capp.Ui(backend, layout="minimal")
    win = ui.window

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.02)

    def call(name, arg):
        QMetaObject.invokeMethod(win, name, Q_ARG("QVariant", arg))

    report = {"screens": [], "language_switch": []}
    try:
        for lang in ("ru", "en"):
            if backend.language != lang:
                started = time.monotonic()
                form = dict(backend.config)
                form.update(language=lang)
                ok = finish(backend, backend.applyForm(form, True))
                pump(0.5)
                nav = next((i.property("text") for i in walk(win.contentItem()) if i.objectName() == "nav_system"), "")
                report["language_switch"].append({"to": lang, "applied": ok, "language": backend.language,
                                                  "seconds": round(time.monotonic() - started, 2),
                                                  "nav_system_label": nav})
            for w, h in SIZES:
                win.resize(w, h)
                screens = [("main", None, None)] + [(s, s, None) for s in SECTIONS] + \
                    [(f"{s}-{p}", s, p) for s, p in DETAILS]
                for name, section, page in screens:
                    win.setProperty("section", "")
                    win.setProperty("details", "")
                    pump(0.2)
                    if section:
                        call("open", section)
                    if page:
                        call("showDetails", page)
                    pump(1.0)
                    found = problems(win)
                    shot = f"{lang}-{w}x{h}-{name}.png"
                    win.grabWindow().save(str(a.out / shot))
                    report["screens"].append({"lang": lang, "size": f"{w}x{h}", "screen": name, "shot": shot,
                                              "problems": found})
                    print(lang, f"{w}x{h}", name, len(found), flush=True)
    finally:
        ui.close()
    report["total_screens"] = len(report["screens"])
    report["screens_with_problems"] = sum(1 for s in report["screens"] if s["problems"])
    (a.out / "text_fit.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(json.dumps({k: report[k] for k in ("total_screens", "screens_with_problems", "language_switch")},
                     ensure_ascii=False))
    return 0 if report["screens_with_problems"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
