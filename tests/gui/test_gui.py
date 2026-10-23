# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""GUI пульта без физического дисплея (offscreen, программный рендерер): L3 на эмуляторах.

Проверяются базовые состояния интерфейса. Работа на эталонном дисплее Waveshare (L4) здесь НЕ проверяется.
"""
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest
from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.app import I18N, PAGE_NAMES, QML, Ui, compile_translations, qt_tool
from sheregesh_console.backend import Backend
from sheregesh_emulators.demo import DEMO

QQuickStyle.setStyle("Fusion")
PAGES = PAGE_NAMES


@pytest.fixture
def ui(qapp, tmp_path):
    compile_translations()
    backend = Backend(tmp_path / "data", demo=True, autostart=False)
    u = Ui(backend)
    qapp.processEvents()
    yield u
    u.close()


def obj(u, name):
    o = u.find(name)
    assert o is not None, name
    return o


def page(u):
    return PAGES[u.window.property("currentPage")]


def set_appearance(u, **kw):
    form = dict(u.backend.config)
    form.update(kw)
    assert u.backend.applyForm(form, True), u.backend.lastResult
    pump(50)


def center(item):
    p = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    return QPoint(int(p.x()), int(p.y()))


# ---------------- запуск и данные ----------------
def test_starts_with_three_screens_and_emulated_data(ui):
    assert ui.window.width() == 1920 and ui.window.height() == 1200
    for name in ("dashboardPage", "diagnosticsPage", "settingsPage", "tabDashboard", "tabDiagnostics", "tabSettings"):
        obj(ui, name)
    st = ui.backend.status
    assert st["peerDevice"].startswith("remote-emu") and st["scenario"] == DEMO.name


def test_demo_states_are_shown(ui):
    badge = obj(ui, "stateBadgeText")
    ui.backend.run_until(DEMO.checkpoints["start"])
    assert badge.property("text") == "Норма" and obj(ui, "noActiveLabel").property("visible")
    ui.backend.run_until(DEMO.checkpoints["warning"])
    pump(20)
    assert badge.property("text") == "Предупреждение"
    assert not obj(ui, "noActiveLabel").property("visible")
    assert "°C" in obj(ui, "tempValue").property("text")
    ui.backend.run_until(DEMO.checkpoints["recovered"])
    pump(20)
    assert badge.property("text") == "Норма"


@pytest.mark.test_id("TEST-PWR-007")
def test_power_without_telemetry_shows_no_data(ui):
    ui.backend.bench.power.set_available(False)
    ui.backend.tick()
    pump(20)
    assert obj(ui, "powerValue").property("text") == "Нет данных"


# ---------------- языки (REQ-UI-001) ----------------
@pytest.mark.test_id("TEST-UI-001")
def test_language_switch_without_restart(ui):
    tab = obj(ui, "tabSettings")
    assert tab.property("text") == "Настройки"
    set_appearance(ui, language="en")
    assert tab.property("text") == "Settings"
    assert obj(ui, "stateBadgeText").property("text") in ("Normal", "Warning", "Starting")
    ui.backend.run_until(DEMO.checkpoints["warning"])
    assert all(ord(c) < 0x400 for e in ui.backend.activeEvents for c in e["text"]), "текст события не на английском"
    set_appearance(ui, language="ru")
    assert tab.property("text") == "Настройки"


@pytest.mark.test_id("TEST-UI-001")
def test_translation_complete_for_all_qml_strings(tmp_path):
    """Повторное извлечение строк из QML не находит непереведённых или новых строк."""
    tools = qt_tool("lupdate")
    assert tools, "lupdate не найден — полноту перевода проверить нельзя"
    tool = tools[0]
    ts = tmp_path / "check.ts"
    shutil.copy(I18N / "sheregesh_ru.ts", ts)
    subprocess.run([tool, *map(str, sorted(QML.glob("*.qml"))), "-ts", str(ts), "-no-obsolete"],
                   check=True, capture_output=True)
    unfinished = [m.find("source").text for m in ET.parse(ts).iter("message")
                  if m.find("translation").get("type") == "unfinished" or not (m.find("translation").text or "").strip()]
    assert unfinished == []


@pytest.mark.test_id("TEST-UI-001")
def test_decimal_separator_follows_language(ui):
    """RU — десятичная запятая (75,3), EN — точка; в текстах событий так же."""
    ui.backend.run_until(DEMO.checkpoints["warning"])
    set_appearance(ui, language="ru")
    assert re.fullmatch(r"SoC \d+,\d\u00a0°C", obj(ui, "tempValue").property("text"))
    temp = [e["text"] for e in ui.backend.activeEvents if e["code"] == "TEMP-HIGH"]
    assert temp and all(re.search(r"\d,\d\u00a0°C", t) for t in temp), temp
    set_appearance(ui, language="en")
    assert re.fullmatch(r"SoC \d+\.\d\u00a0°C", obj(ui, "tempValue").property("text"))


@pytest.mark.test_id("TEST-UI-001")
def test_number_and_unit_never_split_across_lines():
    """Число и единица связаны неразрывным пробелом (дефект S1.5: «с» в плитке связи при 150 % на отдельной строке)."""
    unit = re.compile(r"%\d (s|ms|с|мс|°C|fps|к/с|%)(?=[^\w%]|$)")
    texts = [e.text or "" for e in ET.parse(I18N / "sheregesh_ru.ts").iter() if e.tag in ("source", "translation")]
    texts += [m for f in QML.glob("*.qml") for m in re.findall(r'qsTr\("((?:[^"\\]|\\.)*)"\)', f.read_text())]
    assert [t for t in texts if unit.search(t)] == []


# ---------------- ввод: клавиатура, мышь, сенсор (REQ-P-003, автоматическая часть) ----------------
@pytest.mark.test_id("TEST-P-003")
def test_keyboard_only_navigation(ui):
    ui.window.requestActivate()
    QTest.keyClick(ui.window, Qt.Key_3, Qt.ControlModifier)
    assert page(ui) == "diagnostics"
    QTest.keyClick(ui.window, Qt.Key_F6)
    assert page(ui) == "settings"
    for key, expected in [(Qt.Key_F7, "about"), (Qt.Key_F2, "video"), (Qt.Key_F4, "power"), (Qt.Key_F5, "logs")]:
        QTest.keyClick(ui.window, key)
        assert page(ui) == expected
    QTest.keyClick(ui.window, Qt.Key_Escape)
    QTest.keyClick(ui.window, Qt.Key_Left)
    assert page(ui) == "power"
    QTest.keyClick(ui.window, Qt.Key_Left)
    assert page(ui) == "diagnostics"
    QTest.keyClick(ui.window, Qt.Key_Right)
    QTest.keyClick(ui.window, Qt.Key_Right)
    QTest.keyClick(ui.window, Qt.Key_Right)
    assert page(ui) == "settings"
    # фокус перемещается по полям настроек клавишей Tab
    obj(ui, "nameField").forceActiveFocus()
    QTest.keyClick(ui.window, Qt.Key_Tab)
    assert obj(ui, "languageBox").property("activeFocus")


@pytest.mark.test_id("TEST-P-003")
def test_settings_apply_with_keyboard_only(ui):
    ui.window.requestActivate()
    QTest.keyClick(ui.window, Qt.Key_F6)
    spin = obj(ui, "timeoutSpin")
    spin.forceActiveFocus()
    QTest.keyClick(ui.window, Qt.Key_Up)          # 300 → 330
    assert spin.property("value") == 330
    apply = obj(ui, "applyButton")
    apply.forceActiveFocus()
    QTest.keyClick(ui.window, Qt.Key_Space)
    dialog = obj(ui, "confirmDialog")
    pump(50)
    assert dialog.property("opened") or dialog.property("visible")
    before = ui.backend.revision
    QTest.keyClick(ui.window, Qt.Key_Return)      # кнопка по умолчанию — «ОК»
    pump(50)
    assert ui.backend.revision != before
    assert ui.backend.config["screen_timeout_s"] == 330


def test_mouse_click_switches_tab(ui):
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(obj(ui, "tabDiagnostics")))
    assert page(ui) == "diagnostics"


def test_touch_tap_switches_tab(ui):
    device = QTest.createTouchDevice()
    pos = center(obj(ui, "tabSettings"))
    QTest.touchEvent(ui.window, device).press(0, pos, ui.window).commit()
    QTest.touchEvent(ui.window, device).release(0, pos, ui.window).commit()
    pump(20)
    assert page(ui) == "settings"


# ---------------- темы и масштаб (REQ-UI-002) ----------------
def _overflow(ui):
    """Видимые элементы вне окна (кроме содержимого прокручиваемых областей)."""
    win = QRectF(0, 0, ui.window.width(), ui.window.height())
    bad = []

    def walk(item, inside_flick):
        flick = inside_flick or item.inherits("QQuickFlickable")
        for child in item.childItems():
            if not child.isVisible() or child.width() <= 0 or child.height() <= 0:
                continue
            r = child.mapRectToScene(QRectF(0, 0, child.width(), child.height()))
            if not flick and not win.adjusted(-1, -1, 1, 1).contains(r):
                bad.append((child.objectName() or child.metaObject().className(), r))
            walk(child, flick)
    walk(ui.window.contentItem(), False)
    return bad


@pytest.mark.test_id("TEST-UI-002")
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("scale", [100, 125, 150])
def test_theme_and_scale_layout_fits(ui, theme, scale, tmp_path):
    set_appearance(ui, theme=theme, ui_scale=scale)
    assert ui.backend.uiScale == scale and ui.backend.theme == theme
    for idx in range(len(PAGES)):
        ui.window.setProperty("currentPage", idx)
        pump(30)
        assert _overflow(ui) == [], f"страница {idx}: элементы за границей окна"
        ui.grab(tmp_path / f"{theme}-{scale}-{idx}.png")
    assert obj(ui, "stateBadgeText").property("font").pixelSize() >= 20 * scale / 100 - 1


# ---------------- безопасное редактирование (REQ-SYS-009, CFG) ----------------
@pytest.mark.test_id("TEST-SYS-009")
def test_apply_requires_confirmation(ui):
    form = dict(ui.backend.config)
    form["language"] = "en"
    before = ui.backend.revision
    assert ui.backend.applyForm(form, False) is False
    assert ui.backend.revision == before and ui.backend.language == "ru"


def test_invalid_settings_are_reported_and_not_applied(ui):
    form = dict(ui.backend.config)
    form.update(temp_warning_c=85, temp_critical_c=80, peer_address="10.0.0.1")
    issues = ui.backend.validateForm(form)
    assert any("temp_warning_c" in i for i in issues) and any("peer_address" in i for i in issues)
    before = ui.backend.revision
    assert ui.backend.applyForm(form, True) is False
    assert ui.backend.revision == before
    assert "Не применено" in ui.backend.lastResult


def test_settings_persist_across_reload(ui):
    set_appearance(ui, language="en", theme="light", ui_scale=125)
    rev = ui.backend.revision
    ui.backend.reload()
    assert (ui.backend.revision, ui.backend.language, ui.backend.theme, ui.backend.uiScale) == (rev, "en", "light", 125)
    assert obj(ui, "revisionLabel").property("text") == f"Active revision: {rev}"


def test_backup_and_restore_through_ui(ui):
    name = ui.backend.backup()
    set_appearance(ui, theme="light")
    assert ui.backend.backups() == [name]
    assert ui.backend.restore(name, False) is False and ui.backend.theme == "light"
    assert ui.backend.restore(name, True) and ui.backend.theme == "dark"
