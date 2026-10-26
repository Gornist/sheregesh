# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запуск GUI пульта на эмуляторах.

  python3 -m sheregesh_console [--demo] [--speed N] [--data-dir DIR] [--peer 127.0.0.1:PORT] [--local-video]
  python3 -m sheregesh_console --demo --screenshots DIR     # снимки экранов и выход (без дисплея)
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QTimer, QTranslator, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide6.QtQuick import QQuickWindow  # noqa: F401 — регистрирует тип окна для rootObjects()
from PySide6.QtQuickControls2 import QQuickStyle

from sheregesh import sdnotify
from sheregesh.notify_relay import adopt_pdeathsig

from .backend import Backend
from .control import ControlServer, server_path
from .video import VideoView

qmlRegisterType(VideoView, "Sheregesh", 1, 0, "VideoView")

PKG = Path(__file__).resolve().parent
QML = PKG / "qml"
I18N = PKG / "i18n"
LANGS = ("ru",)
PAGE_NAMES = ["dashboard", "video", "diagnostics", "power", "logs", "settings", "about", "module", "sticks"]   # порядок вкладок Main.qml   # исходные строки QML — английские; переводы — файлы .ts/.qm


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
            if not qm.exists():
                raise RuntimeError("не найден lrelease для сборки переводов")
            # На устройстве lrelease нет, .qm собран вместе с пакетом из того же коммита; .ts может оказаться
            # «новее» лишь по времени записи файлов (выпуск DEV deploy, pi1 57e8fe4) — пульт не должен падать.
            print(f"перевод {qm.name}: lrelease нет, используется готовый .qm", file=sys.stderr, flush=True)
            out.append(qm)
    return out


def module_ui_state(ui):
    """Что показывает страница «Модуль» (ADR-0012, 10.4): тексты QML и состояние ModuleView для ui-state.json."""
    b = ui.backend
    mv = b.module

    def text(name):                      # видимость — по условию элемента (shown), а не по текущей вкладке
        o = ui.find(name)
        return o.property("text") if o is not None and o.property("shown") is not False else ""
    banner = ui.find("moduleLabBanner")
    return {"support": mv.support, "state": mv.state, "seq": mv.seq, "stale": mv.stale,
            "stateText": text("moduleStateText"), "stateDetail": text("moduleStateDetail"),
            "supportText": text("moduleSupportText"), "profileText": text("moduleProfileText"),
            "errorText": text("moduleErrorText"), "emulatorText": text("moduleEmulatorText"),
            "changedText": text("moduleChangedText"), "outputText": text("moduleOutputText"),
            "labBanner": bool(banner is not None and banner.property("shown")),
            "faultEvent": b.bench.monitor.log.is_active("MODULE-FAULT"), "command": dict(mv.command)}


def sticks_ui_state(ui):
    """Что показывает страница «Управление» (ADR-0013, 4.2): тексты QML и состояние SticksView для ui-state.json."""
    sv = ui.backend.sticks

    def text(name):                      # видимость — по условию элемента (shown), а не по текущей вкладке
        o = ui.find(name)
        return o.property("text") if o is not None and o.property("shown") is not False else ""
    banner, switch = ui.find("sticksNoOutputBanner"), ui.find("sticksEnableSwitch")
    snd = sv.sender.status()
    return {"support": sv.support, "present": sv.present, "deviceName": sv.deviceName, "enabled": sv.enabled,
            "axisCount": sv.axisCount, "buttonCount": sv.buttonCount, "axes": list(sv.axes), "buttons": sv.buttons,
            "remoteState": sv.remoteState, "remoteReason": sv.remoteReason, "remoteAxes": list(sv.remoteAxes),
            "remoteButtons": sv.remoteButtons, "remoteOutput": sv.remoteOutput, "rateHz": sv.rateHz,
            "latencyMs": sv.latencyMs, "gaps": sv.gaps, "failsafes": sv.failsafes, "sent": sv.sent,
            "frame": snd["n"], "latency": dict(snd["latency"]), "result": sv.result, "notice": sv.notice,
            "stateText": text("sticksRemoteStateText"), "reasonText": text("sticksRemoteReasonText"),
            "noticeText": text("sticksNoticeText"), "deviceText": text("sticksDeviceText"),
            "rateText": text("sticksRateText"), "latencyText": text("sticksLatencyText"),
            "switchChecked": bool(switch is not None and switch.property("checked")),
            "noOutputBanner": bool(banner is not None and banner.property("shown")),
            "noOutputText": text("sticksNoOutputText")}


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
        ui.window.setProperty("currentPage", PAGE_NAMES.index(page) if isinstance(page, str) else page)
        shots.append(ui.grab(out_dir / f"{name}.png"))

    b.run_until(DEMO.checkpoints["start"]);     shot("01-dashboard-normal", 0)
    b.run_until(DEMO.checkpoints["heating"]);   shot("02-dashboard-heating", 0)
    b.run_until(DEMO.checkpoints["warning"]);   shot("03-dashboard-warning", 0)
    b.run_until(DEMO.checkpoints["peer-lost"]); shot("04-dashboard-peer-lost", 0); shot("05-diagnostics-events", "diagnostics")
    b.run_until(DEMO.checkpoints["recovered"]); shot("06-dashboard-recovered", 0); shot("07-diagnostics-history", "diagnostics")
    shot("08-settings-ru", "settings")
    form = dict(b.config)
    form.update(language="en", theme="light", ui_scale=125)
    b.applyForm(form, True)
    b.reload()                                   # повторная загрузка с диска
    shot("09-settings-en-light-125", "settings")
    shot("10-dashboard-en-light-125", 0)
    return shots


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheregesh_console", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="демонстрационный сценарий S1")
    ap.add_argument("--speed", type=float, default=4.0, help="ускорение модельного времени (по умолчанию 4)")
    ap.add_argument("--data-dir", help="каталог данных эмулятора (по умолчанию временный)")
    ap.add_argument("--screenshots", metavar="DIR", help="снять экраны сценария в DIR и выйти")
    ap.add_argument("--peer", metavar="HOST:PORT",
                    help="отдельный процесс удалённого блока (только 127.0.0.1); без параметра блок эмулируется в процессе")
    ap.add_argument("--local-video", action="store_true", help="локальный синтетический видеогенератор (без удалённого блока)")
    ap.add_argument("--link-config", metavar="FILE",
                    help="сетевой лабораторный профиль (mTLS, S3.1); если файла нет — канал через --peer (loopback)")
    ap.add_argument("--link-status", metavar="FILE", help="состояние канала в JSON для sheregesh-health")
    ap.add_argument("--control-socket", metavar="PATH", default=None,
                    help="локальный канал управления для sheregesh.link.videoselect (S5 GUI-02); по умолчанию "
                         "$XDG_RUNTIME_DIR/sheregesh-console.sock, \"\" — выключен")
    args = ap.parse_args(argv)
    adopt_pdeathsig()                                  # под cage в службе: не переживать композитор (S3.1)
    peer = None
    if args.peer:
        host, _, port = args.peer.rpartition(":")
        if not host.startswith("127.") or not port.isdigit():
            ap.error("--peer: только 127.0.0.1:ПОРТ")
        peer = (host, int(port))

    if args.screenshots:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")
    QQuickStyle.setStyle("Fusion")
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("sheregesh-console")
    compile_translations()
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="sheregesh-emu-"))
    link_cfg = None
    if args.link_config and Path(args.link_config).expanduser().exists():
        from sheregesh.link.tls import LinkConfig, LinkConfigError
        try:
            link_cfg = LinkConfig.load(args.link_config)
            print(f"Сетевой профиль: {link_cfg.own_uri} → {link_cfg.peer_uri}, порт {link_cfg.port}", flush=True)
        except LinkConfigError as e:              # без проверенных ключей сеть не используется
            print(f"Сетевой профиль не принят: {e}; канал через {args.peer or 'эмулятор в процессе'}", flush=True)
    backend = Backend(data_dir, demo=args.demo or bool(args.screenshots), speed=args.speed,
                      autostart=not args.screenshots, peer=peer, local_video=args.local_video, link_cfg=link_cfg,
                      link_status_file=args.link_status)
    ui = Ui(backend)
    print(f"Данные эмулятора: {data_dir}", flush=True)
    control = None
    ctl_path = args.control_socket if args.control_socket is not None else server_path()
    if ctl_path and not args.screenshots:
        try:
            control = ControlServer(backend, ctl_path).start()
            print(f"Локальный канал управления: {ctl_path}", flush=True)
        except OSError as e:                      # без канала пульт работает; videoselect — прямым сеансом
            print(f"Локальный канал управления не открыт: {e}", flush=True)
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
    # systemd (ADR-0002): READY — после загрузки QML и запуска бэкенда, а не после старта композитора cage;
    # WATCHDOG — из цикла Qt и только пока таймер бэкенда действительно тикает.
    sdnotify.ready(f"console ready, peer {backend.status.get('peerMode', '?')}")
    wd = sdnotify.watchdog_interval()
    wd_timer = QTimer()
    if wd:
        def ping():
            if time.monotonic() - getattr(backend, "last_tick", time.monotonic()) < max(5.0, wd):
                sdnotify.ping()
        wd_timer.timeout.connect(ping)
        wd_timer.start(int(wd * 1000))
    # Без дисплея: что показывает плитка связи (текст QML), раз в секунду — доказательство состояний, не визуальный PASS.
    ui_state = os.environ.get("SHEREGESH_UI_STATE")
    ui_timer = QTimer()
    if ui_state:
        def dump_ui():
            st = backend.status
            label = ui.find("peerValue")
            detail = ui.find("peerDetail")
            badge = ui.find("linkBadgeText")
            data = {"time": time.time(), "linkView": st.get("linkView"),
                    "linkBadgeText": badge.property("text") if badge else None, "peerLink": st.get("peerLink"),
                    "peerState": st.get("peerState"),
                    "peerDetail": st.get("peerDetail"), "linkTls": st.get("linkTls"), "linkHost": st.get("linkHost"),
                    "linkRttMs": st.get("linkRttMs"), "peerValueText": label.property("text") if label else None,
                    "peerDetailText": detail.property("text") if detail and detail.property("visible") else "",
                    "video": backend.video.metrics(),   # фактически декодированные кадры и видеоканал (S3.1-VIDEO)
                    "videoSource": backend.videoSource,  # вход/состояние источника и решение для кадра (display, live)
                    "module": module_ui_state(ui),       # страница «Модуль» (ADR-0012, S6)
                    "sticks": sticks_ui_state(ui)}       # страница «Управление» (ADR-0013, S7)
            try:
                tmp = Path(ui_state + ".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False))
                os.replace(tmp, ui_state)
            except OSError:
                pass
        ui_timer.timeout.connect(dump_ui)
        ui_timer.start(1000)
        # Переходы — сразу, а не с периодом 1 с: иначе замер времени индикации по файлу (REQ-SW-002 «нет видео»
        # ≤ 3 с, REQ-SW-004) получает до 1 с запаздывания самого файла.
        last_key = {"k": None}

        def dump_on_change():
            st = backend.status
            mv, sv = backend.module, backend.sticks
            key = (st.get("linkView"), st.get("peerLink"), st.get("peerDetail"), backend.video.state,
                   mv.support, mv.state, mv.seq, sv.support, sv.present, sv.enabled, sv.remoteState, sv.remoteReason)
            if key != last_key["k"]:
                last_key["k"] = key
                dump_ui()
        backend.statusChanged.connect(dump_on_change)
        backend.video.stateChanged.connect(dump_on_change)
        backend.sticks.changed.connect(dump_on_change)  # передача и состояние команд на блоке — сразу (ST-03, ST-06)
    mark = os.environ.get("SHEREGESH_FIRST_FRAME_MARK")
    if mark:                                 # проба S2-2: время от загрузки ядра до первого показанного кадра
        def first_frame():
            ui.window.frameSwapped.disconnect(first_frame)
            clock = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
            Path(mark).write_text(json.dumps({"clock": "boottime" if clock != time.CLOCK_MONOTONIC else "monotonic",
                                              "seconds": time.clock_gettime(clock), "wall": time.time()}))
            sdnotify.notify("STATUS=first frame shown")
        ui.window.frameSwapped.connect(first_frame)
    # Ctrl+C / SIGTERM: штатное завершение. Обработчик Python срабатывает только между событиями Qt,
    # поэтому таймер периодически возвращает управление интерпретатору.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: app.quit())
    wake = QTimer()
    wake.start(200)
    wake.timeout.connect(lambda: None)
    rc = app.exec()
    if control is not None:
        control.stop()
    ui.close()
    return rc
