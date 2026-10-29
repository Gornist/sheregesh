# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выход CRSF на пульте (S8, ADR-0014, 3.3; offscreen, L3): страницы «Модуль» и «Управление».

status.module приходит так же, как из телеметрии блока (PeerStatus.telemetry["module"] после wire.sanitize_module
агента H): output none | neutral | live, блок crsf (снимок CrsfOutput + emu — принятое эмулятором ELRS TX).
Клиент канала подменный: module_command (S6) + send_rc/snapshot с rc_state (S7). Проверяется: строка выхода и блок
CRSF страницы «Модуль», плашка лабораторного канала, выбор backend через configure только после подтверждения в
диалоге, плашка «Управления» по фактическому выходу с пометкой «эмулятор модуля» и третья полоса эха (каналы
эмулятора), ui-state, геометрия на 1280×800 (Waveshare 10.1, сенсорный) и 1920×1200 со шрифтом устройства.
Снимки: SHEREGESH_S8_GUI_OUT=docs/acceptance/s8-crsf/screenshots-offscreen — туда; иначе во временный каталог.

Это не проверка физического дисплея, модуля и блока: только экран пульта на подменных данных.
"""
import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest

from sheregesh.module import wire
from sheregesh_console import module as module_mod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations, module_ui_state, sticks_ui_state
from sheregesh_console.backend import Backend
from sheregesh_console.journal import MemoryJournal
from sheregesh_console.qtwait import finish, pump, wait_until
from test_e2e_gui_screens import header_problems, outside_window, save_small
from test_gui_module import READY as S6_READY
from test_gui_module import center, find, text, visible
from test_gui_module import page_problems as module_problems
from test_gui_sticks import AXES, CONNECTED, LOST, FakeClient, FakeSource, rc_state
from test_gui_sticks import page_problems as sticks_problems
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта с метриками устройства

QQuickStyle.setStyle("Fusion")
SIZES = [(1280, 800), (1920, 1200)]
EMU_NAME = "RM Nomad X-Band (emu)"
UART_NOT_FOUND = "UART не найден (uart-not-found): нет устройства UART3 (serial@7e201600) — dtoverlay=uart3 в образе"


def crsf_block(mode, frames=4000, emu_ch=None, **kw):
    """status.module.crsf (ADR-0014, 3.1–3.2): снимок CrsfOutput эмулятора 50 Гц и принятое эмулятором."""
    c = {"mode": mode, "port": "pty", "period_us": 20000, "synced": True, "sync_age_ms": 80, "offset_us": 120.0,
         "frames_sent": frames, "jitter_us_p99": 310, "crc_errors": 0, "collisions": 0,
         "link": {"rssi": -42, "lq": 100, "snr": 9, "rf_mode": 4, "tx_power": 100},
         "device": {"name": EMU_NAME, "sw": "4.1.0", "serial": "EMU-ELRS-0001"}, "last_rx_ms": 12,
         "emu": {"state": "ok", "ch": list(emu_ch if emu_ch is not None else [0] * 8) + [0] * 8,
                 "armed": mode == "live", "frames": frames - 10, "rate_hz": 50.0, "jitter_us_p99": 280,
                 "crc_errors": 0}}
    c.update(kw)
    return c


def crsf_tel(output="neutral", state="READY", backend="crsf-pty-lab", crsf=None, **kw):
    """status.module с бэкендом CRSF (до sanitize_module)."""
    t = copy.deepcopy(S6_READY)
    lab = backend == "crsf-pty-lab"
    t.update(state=state, seq=41, since_s=8.2, output=output,
             backend={"kind": backend, "lab": lab, "state": "running" if lab else "stopped", "pid": 2211 if lab else 0,
                      "restarts": 0, "restart_limit": 3, "last_exit": None},
             identity={"vendor": "RadioMaster", "model": "Nomad", "firmware": "4.1.0", "serial": "EMU-ELRS-0001"},
             settings={**S6_READY["settings"], "backend": backend, "revision": "r000005",
                       "uart": {"device": "auto", "baud": 400000, "half_duplex": True}, "failsafe_ch": [0] * 8},
             crsf=crsf if crsf is not None else crsf_block(output),
             history=[{"seq": 41, "from": "INITIALIZING", "state": "READY", "reason": "init-ok", "code": "",
                       "ago_s": 8.2},
                      {"seq": 40, "from": "DISABLED", "state": "DISCONNECTED", "reason": "settings-backend",
                       "code": "", "ago_s": 10.0}])
    t.update(kw)
    return t


SILENT = crsf_tel("none", "FAULT", reason="no-response",
                  error={"code": "MOD-NO-RESPONSE", "text": "no valid CRSF frames for 3.0 s", "since_s": 1.2,
                         "state": "READY", "active": True},
                  crsf=crsf_block("none", frames=4000, period_us=4000, synced=False, sync_age_ms=4300, link=None,
                                  last_rx_ms=4200, emu={"state": "silent", "ch": [0] * 16, "armed": False,
                                                        "frames": 8990, "rate_hz": 0.0}))
UART_MISSING = crsf_tel("none", "FAULT", backend="crsf-uart", reason="start-failed", identity=None,
                        error={"code": "MOD-IFACE-ERROR", "text": UART_NOT_FOUND, "since_s": 2.0,
                               "state": "DISCONNECTED", "active": True},
                        crsf={"mode": "none", "port": ""})


class Clock:
    """Часы пульта для ModuleView (время перехода, частота кадров по frames_sent)."""

    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


class Peer(FakeClient):
    """Клиент канала: rc/rc-state (S7, test_gui_sticks.FakeClient) и module_command (S6, ADR-0012, 9.2)."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def module_command(self, op, args=None, wait_s=5.0):
        self.calls.append((op, dict(args or {}), wait_s))
        return {"ok": True, "result": "ok", "error": "", "req": len(self.calls)}


@pytest.fixture
def out_dir(tmp_path):
    d = Path(os.environ.get("SHEREGESH_S8_GUI_OUT") or tmp_path / "s8-gui")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def ui(qapp, tmp_path, wide_font):
    compile_translations()
    src = FakeSource(present=True)
    b = Backend(tmp_path / "data", autostart=False, journal=MemoryJournal(), stick_source=src)
    b.peer, b.src = Peer(), src
    b._module_peer = lambda: b.peer
    b._sticks_peer = lambda: b.peer
    b.module._wall = Clock()
    u = Ui(b)
    u.window.resize(1280, 800)
    u.window.setProperty("currentPage", PAGE_NAMES.index("module"))
    pump(100)
    yield u
    u.close()


def feed(u, module, rc=None, steps=0, session="1.4", status=None, dt=1.0):
    """Как Backend._refresh: поля канала в status, status.module (после wire.sanitize_module) → ModuleView,
    rc-state — в snapshot клиента → SticksView. dt — шаг часов пульта (частота кадров по frames_sent)."""
    b = u.backend
    st = status or CONNECTED
    b.module._wall.t += dt
    b._status.update(st)
    b.peer.phase = "connected" if st["peerLink"] == "CONNECTED" else "connecting"
    b.peer.session = session
    if rc is not None:
        b.peer.rc_state = rc
    for _ in range(steps):                       # такты передачи 50 Гц
        b.sticks.sender.step(b.sticks.sender._clock())
    tel = {"state": "normal", "module": wire.sanitize_module(module)} if module is not None else {"state": "normal"}
    b._update_module(SimpleNamespace(peer=SimpleNamespace(telemetry=tel, session=session)))
    b.sticks.update_link(st["peerLink"], session if st["peerLink"] == "CONNECTED" else None)
    b.statusChanged.emit()
    pump(60)


def settle(u, module, **kw):
    """Два status подряд через 1 с: frames_sent +50 → частота кадров 50 Гц на экране."""
    c = module.get("crsf") or {}
    if "frames_sent" in c:
        first = copy.deepcopy(module)
        first["crsf"]["frames_sent"] = c["frames_sent"] - (50 if c.get("mode") in ("neutral", "live") else 0)
        feed(u, first, **kw)
    feed(u, module, **kw)


def page(u, name):
    u.window.setProperty("currentPage", PAGE_NAMES.index(name))
    pump(60)


def check_screen(u, name, out_dir, report):
    """Обе страницы проверяются по своим правилам; снимок — текущей страницы."""
    cur = PAGE_NAMES[u.window.property("currentPage")]
    problems_of = {"module": module_problems, "sticks": sticks_problems}[cur]
    for w, h in SIZES:
        u.window.resize(w, h)
        pump(200)
        problems = outside_window(u) + header_problems(u) + problems_of(u)
        shot = save_small(u, out_dir / f"{w}x{h}-{name}.png")
        report.append({"size": f"{w}x{h}", "state": name, "page": cur, "png": shot.name, "problems": problems,
                       "ui": {"module": module_ui_state(u), "sticks": sticks_ui_state(u)}})
        assert problems == [], f"{w}×{h} {name}: {problems}"
    u.window.resize(1280, 800)
    pump(60)


@pytest.fixture(scope="module")
def report():
    rows = []
    yield rows
    if rows and os.environ.get("SHEREGESH_S8_GUI_OUT"):
        (Path(os.environ["SHEREGESH_S8_GUI_OUT"]) / "screens.json").write_text(json.dumps(
            {"note": "offscreen, QT_QUICK_BACKEND=software, шрифт устройства (DejaVu Sans/Verdana) — L3, "
                     "не проверка физического дисплея; модуль, эмулятор ELRS TX и блок подменные "
                     "(ADR-0014, 3.2: status.module после wire.sanitize_module)",
             "checks": ["элементы в пределах окна и страницы", "текст вне списков не обрезан и не шире элемента",
                        "блоки страницы не перекрываются", "индикаторы заголовка не перекрываются",
                        "вкладка страницы видна на панели вкладок"],
             "shots": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def crsf_texts(u):
    return {n: text(u, f"moduleCrsf{n}Text") for n in ("Device", "Period", "Sync", "Rate", "Jitter", "Link", "Errors")}


# ---------------- lab-emulator: как в S6 ----------------
def test_lab_emulator_as_s6(ui, out_dir, report):
    feed(ui, S6_READY)
    mv = ui.backend.module
    assert mv.backendSetting == "lab-emulator" and not mv.isCrsf and mv.output == "none"
    assert text(ui, "moduleLabBannerText") == "Лабораторный эмулятор, не физический JR-модуль"
    assert text(ui, "moduleOutputText") == "Выход: нет" and visible(ui, "moduleOutputBadge")
    assert not visible(ui, "moduleCrsfBlock") and visible(ui, "moduleBackendButton")
    assert text(ui, "moduleEmulatorText").startswith("Эмулятор: работает")
    assert mv.labScenarios == list(module_mod.LAB_SCENARIOS)
    s = module_ui_state(ui)
    assert s["output"] == "none" and s["outputText"] == "Выход: нет" and s["backend"] == "lab-emulator"
    assert s["labBanner"] and not s["crsfShown"] and s["crsfDeviceText"] == ""
    check_screen(ui, "01-lab-emulator-module", out_dir, report)
    page(ui, "sticks")
    assert text(ui, "sticksOutputText") == "Радиовыхода нет: команды доходят до блока, модулю не передаются"
    assert not visible(ui, "sticksEmuAxisValue_0") and not visible(ui, "sticksEmuLegend")
    st = sticks_ui_state(ui)
    assert st["output"] == "none" and st["noOutputBanner"] and st["noOutputText"] == st["outputText"]
    assert st["outputEmulator"] is False and st["emuAxes"] == []
    check_screen(ui, "02-lab-emulator-sticks", out_dir, report)


# ---------------- crsf-pty-lab: READY, нейтраль ----------------
def test_crsf_pty_ready_neutral(ui, out_dir, report):
    settle(ui, crsf_tel("neutral"), rc=rc_state("IDLE", "start", 1.0, n=0, rate_hz=0.0, failsafes=0))
    mv = ui.backend.module
    assert mv.support == "ok" and mv.state == "READY" and mv.backendSetting == "crsf-pty-lab" and mv.isCrsf
    assert mv.output == "neutral" and text(ui, "moduleOutputText") == "Выход: нейтраль"
    assert text(ui, "moduleStateText") == "Готов"
    assert text(ui, "moduleLabBannerText") == "Эмулятор модуля CRSF, не физический модуль: радиопередачи нет"
    assert text(ui, "moduleEmulatorText") == "Эмулятор CRSF: работает · PID 2211"
    assert crsf_texts(ui) == {"Device": f"Устройство: {EMU_NAME} · ПО 4.1.0",
                              "Period": "Период: 20,0 мс", "Sync": "Синхронизация: есть",
                              "Rate": "Кадры: 50,0 Гц", "Jitter": "Джиттер p99: 0,31 мс",
                              "Link": "RSSI −42 дБм · LQ 100 %",
                              "Errors": "Ошибки CRC: 0 · коллизии 0"}
    assert mv.labScenarios == list(module_mod.CRSF_LAB_SCENARIOS)
    assert text(ui, "moduleLabButton_silent") == "Модуль молчит"
    assert mv.snapshot()["display"]["outputText"] == "Выход: нейтраль" and mv.snapshot()["backend"] == "crsf-pty-lab"
    s = module_ui_state(ui)
    assert s["crsfShown"] and s["crsf"]["rateHz"] == pytest.approx(50.0) and s["crsf"]["synced"]
    assert s["crsfPeriodText"].startswith("Период: 20,0") and s["emuChannels"] == [0] * 8
    check_screen(ui, "03-crsf-pty-neutral-module", out_dir, report)
    page(ui, "sticks")
    assert text(ui, "sticksOutputText") == "Выход CRSF: нейтраль · эмулятор модуля"
    assert visible(ui, "sticksEmuLegend") and text(ui, "sticksEmuAxisValue_2") == "0"
    st = sticks_ui_state(ui)
    assert st["output"] == "neutral" and st["outputEmulator"] and not st["noOutputBanner"] and st["noOutputText"] == ""
    assert st["emuAxes"] == [0] * 8
    check_screen(ui, "04-crsf-pty-neutral-sticks", out_dir, report)


# ---------------- live: команды пульта на модуль ----------------
def test_crsf_live_with_channels(ui, out_dir, report):
    b = ui.backend
    b.src.st.update(axes=list(AXES), buttons=5)
    feed(ui, crsf_tel("neutral"))
    assert b.sticks.setEnabled(True, True)
    emu = [0, -12, 999, 3, -1000, 640, -355, 1000]          # ±1 шаг 11 бит после CRSF (CR-04)
    settle(ui, crsf_tel("live", crsf=crsf_block("live", frames=6000, emu_ch=emu)),
           rc=rc_state("ACTIVE", "fresh", 2.0, ch=AXES, btn=5, failsafes=0), steps=5)
    mv = b.module
    assert mv.output == "live" and mv.emuChannels == emu
    page(ui, "sticks")
    assert text(ui, "sticksOutputText") == "Выход CRSF: команды идут на модуль · эмулятор модуля"
    assert text(ui, "sticksRemoteStateText") == "Команды идут"
    assert text(ui, "sticksEmuAxisValue_2") == "999" and text(ui, "sticksEmuAxisValue_6") == "−355"
    assert text(ui, "sticksRemoteAxisValue_2") == "1000"
    assert find(ui, "sticksChannelsTile").property("title") == "Каналы: пульт → удалённый блок → модуль"
    st = sticks_ui_state(ui)
    assert st["output"] == "live" and st["emuAxes"] == emu and st["remoteAxes"] == AXES
    check_screen(ui, "05-crsf-live-sticks", out_dir, report)
    page(ui, "module")
    assert text(ui, "moduleOutputText") == "Выход: команды"
    s = module_ui_state(ui)
    assert s["output"] == "live" and s["crsf"]["emu"]["armed"] is True
    check_screen(ui, "06-crsf-live-module", out_dir, report)


# ---------------- FAILSAFE → нейтраль ----------------
def test_failsafe_gives_neutral(ui, out_dir, report):
    b = ui.backend
    feed(ui, crsf_tel("neutral"))
    assert b.sticks.setEnabled(True, True)
    settle(ui, crsf_tel("neutral", crsf=crsf_block("neutral", frames=7000)),
           rc=rc_state("FAILSAFE", "timeout", 3.0, rate_hz=0.0, failsafes=2, gaps=3, age_ms=640), steps=1)
    page(ui, "sticks")
    assert text(ui, "sticksRemoteStateText") == "FAILSAFE: нет свежих команд"
    assert text(ui, "sticksOutputText") == "Выход CRSF: нейтраль · эмулятор модуля"
    assert all(text(ui, f"sticksEmuAxisValue_{i}") == "0" for i in range(8))
    assert all(text(ui, f"sticksRemoteAxisValue_{i}") == "0" for i in range(8))
    check_screen(ui, "07-failsafe-neutral-sticks", out_dir, report)


# ---------------- модуль молчит → FAULT, выхода нет ----------------
def test_module_silent_fault_none(ui, out_dir, report):
    settle(ui, crsf_tel("neutral"))
    feed(ui, SILENT)
    mv = ui.backend.module
    assert mv.state == "FAULT" and mv.output == "none"
    assert text(ui, "moduleStateText") == "Ошибка" and text(ui, "moduleStateDetail") == "Модуль не отвечает"
    assert text(ui, "moduleOutputText") == "Выход: нет"
    t = crsf_texts(ui)
    assert t["Period"] == "Период: 4,0 мс" and t["Sync"] == "Синхронизации нет" and t["Link"] == "RSSI / LQ: —"
    assert t["Rate"] == "Кадры: 0,0 Гц"
    assert ui.backend.bench.monitor.log.is_active("MODULE-FAULT")
    check_screen(ui, "08-silent-fault-module", out_dir, report)
    page(ui, "sticks")
    assert text(ui, "sticksOutputText") == "Радиовыхода нет: команды доходят до блока, модулю не передаются"
    check_screen(ui, "09-silent-none-sticks", out_dir, report)


# ---------------- crsf-uart: UART не найден ----------------
def test_crsf_uart_not_found(ui, out_dir, report):
    feed(ui, UART_MISSING)
    mv = ui.backend.module
    assert mv.backendSetting == "crsf-uart" and mv.uartMissing and not mv.isLab
    assert text(ui, "moduleStateText") == "Ошибка" and text(ui, "moduleStateDetail") == "UART не найден"
    assert text(ui, "moduleEmulatorText") == "CRSF на UART: UART не найден"
    assert not visible(ui, "moduleLabBanner") and not visible(ui, "moduleLabPanel") and mv.labScenarios == []
    assert text(ui, "moduleCrsfDeviceText") == "Устройство: нет ответа" and text(ui, "moduleOutputText") == "Выход: нет"
    s = module_ui_state(ui)
    assert s["uartMissing"] and s["backend"] == "crsf-uart" and s["stateDetail"] == "UART не найден"
    check_screen(ui, "10-uart-not-found-module", out_dir, report)


# ---------------- выбор backend ----------------
def test_backend_choice_requires_dialog(ui, out_dir, report):
    feed(ui, S6_READY)
    mv, peer = ui.backend.module, ui.backend.peer
    assert mv.setBackend("crsf-pty-lab", False) is False and peer.calls == []
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "moduleBackendButton")))
    pump(80)
    dlg = find(ui, "moduleBackendDialog")
    assert dlg.property("opened") or dlg.property("visible")
    assert dlg.property("title") == "Сменить канал к модулю?" and dlg.property("choice") == "lab-emulator"
    assert text(ui, "moduleBackendQuestion") == "Выберите другой канал и нажмите «ОК»."
    QTest.keyClick(ui.window, Qt.Key_Return)                # тот же канал — «ОК» не действует
    pump(60)
    assert dlg.property("visible") and peer.calls == []
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "moduleBackendOption_crsf-pty-lab")))
    pump(60)
    assert dlg.property("choice") == "crsf-pty-lab"
    assert text(ui, "moduleBackendQuestion").startswith("Удалённый блок сохранит настройку и перезапустит модуль "
                                                         "на канале «Эмулятор модуля CRSF»")
    check_screen(ui, "11-backend-dialog", out_dir, report)
    QTest.keyClick(ui.window, Qt.Key_Down)                  # клавиатура: следующий вариант
    pump(40)
    assert dlg.property("choice") == "crsf-uart"
    QTest.keyClick(ui.window, Qt.Key_Up)
    pump(40)
    QTest.keyClick(ui.window, Qt.Key_Return)
    assert wait_until(lambda: ("configure", {"backend": "crsf-pty-lab"}, module_mod.CONFIGURE_WAIT_S) in peer.calls, 3)
    assert wait_until(lambda: mv.command["state"] == "done", 3) and not dlg.property("visible")


def test_backend_dialog_cancel_sends_nothing(ui):
    feed(ui, crsf_tel("neutral"))
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "moduleBackendButton")))
    pump(80)
    QTest.mouseClick(ui.window, Qt.LeftButton, Qt.NoModifier, center(find(ui, "moduleBackendOption_lab-emulator")))
    pump(40)
    QTest.keyClick(ui.window, Qt.Key_Escape)
    pump(60)
    assert not find(ui, "moduleBackendDialog").property("visible") and ui.backend.peer.calls == []


def test_backend_button_inactive_without_link(ui):
    feed(ui, crsf_tel("neutral"))
    feed(ui, crsf_tel("neutral"), status=LOST)
    assert not find(ui, "moduleBackendButton").property("enabled")
    assert ui.backend.module.setBackend("crsf-uart", True) is False
    assert ui.backend.module.command["result"] == "no-link"


# ---------------- диагностика, связь, английский ----------------
def test_crsf_diagnostics_rows(ui, out_dir, report):
    settle(ui, crsf_tel("live", crsf=crsf_block("live", frames=6000, emu_ch=AXES)))
    model = find(ui, "moduleDiagList").property("model")
    rows = {r["k"]: r["v"] for r in (model.toVariant() if hasattr(model, "toVariant") else model)}
    assert rows["Кадры CRSF"] == "команды · отправлено 6000"
    assert rows["Синхронизация"] == "80 мс назад" and rows["Последний ответ модуля"] == "12 мс назад"
    assert rows["Радиолинк"] == "SNR 9 дБ · режим 4 · мощность 100 мВт"
    assert rows["Эмулятор модуля"] == "ok · взведён · 50,0 Гц"
    diag = find(ui, "moduleDiagList")
    for w, h in SIZES:                                       # строки CRSF — в конце списка: прокрутка к ним
        ui.window.resize(w, h)
        pump(100)
        diag.setProperty("contentY", max(0.0, diag.property("contentHeight") - diag.property("height")))
        pump(60)
        problems = outside_window(ui) + header_problems(ui) + module_problems(ui)
        shot = save_small(ui, out_dir / f"{w}x{h}-12-crsf-diagnostics.png")
        report.append({"size": f"{w}x{h}", "state": "12-crsf-diagnostics", "page": "module", "png": shot.name,
                       "problems": problems, "ui": {"module": module_ui_state(ui), "sticks": sticks_ui_state(ui)}})
        assert problems == [], f"{w}×{h}: {problems}"
    ui.window.resize(1280, 800)
    pump(60)


def test_sticks_banner_without_link_and_old_remote(ui):
    page(ui, "sticks")
    feed(ui, crsf_tel("live"))
    feed(ui, crsf_tel("live"), status=LOST)
    assert text(ui, "sticksOutputText") == "Радиовыход: нет данных, нет связи с удалённым блоком"
    assert sticks_ui_state(ui)["output"] == "" and not visible(ui, "sticksEmuLegend")
    feed(ui, None, session="1.2")                         # блок без status.module — выхода нет
    assert text(ui, "sticksOutputText") == "Радиовыхода нет: команды доходят до блока, модулю не передаются"
    assert sticks_problems(ui) == []


def test_unknown_output_is_warning(ui):
    feed(ui, crsf_tel("armed-everything"))
    assert ui.backend.module.output == "unknown"
    assert text(ui, "moduleOutputText") == "Выход: неизвестное значение от блока"
    page(ui, "sticks")
    assert text(ui, "sticksOutputText") == "Радиовыход: неизвестное значение от блока"


def test_english_texts(ui):
    form = dict(ui.backend.config)
    form.update(language="en")
    assert finish(ui.backend, ui.backend.applyForm(form, True))
    pump(60)
    settle(ui, crsf_tel("neutral"))
    assert text(ui, "moduleOutputText") == "Output: neutral"
    assert text(ui, "moduleCrsfPeriodText") == "Period: 20.0 ms" and text(ui, "moduleCrsfSyncText") == "Synchronization: yes"
    assert ui.backend.module.snapshot()["display"]["outputText"] == "Output: neutral"
    assert module_problems(ui) == []
    page(ui, "sticks")
    assert text(ui, "sticksOutputText") == "CRSF output: neutral · module emulator"
    assert sticks_problems(ui) == []
