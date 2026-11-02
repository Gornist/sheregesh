# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Связь QML ↔ монитор и конфигурация (ADR-0003). GUI не пишет файлы и не обращается к оборудованию:
все данные — от эмуляторов, все изменения конфигурации — через ConfigStore с подтверждением."""
import collections
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot, qVersion

from sheregesh import PROTOCOL_VERSION, __semver__, __version__, semver_or_raw, timepeer
from sheregesh import video as gst
from sheregesh.config import BackupError, ConfigStore, Schema, console_defaults
from sheregesh.faults import CATALOG, render
from sheregesh.link.client import PeerClient, RemotePeer
from sheregesh_emulators import Bench
from sheregesh_emulators.demo import DEMO
from sheregesh_emulators.scenario import TICK_S

from . import configops
from . import journal as jr
from . import logops
from . import videoops
from .module import ModuleView
from .sticks import INPUT_DIR, StickSource, SticksView
from .video import VideoReceiver
from . import videorx
from .videoops import VIDEO_DEFAULTS

POWER_HISTORY = 240

PALETTES = {
    "dark": {"bg": "#15181c", "panel": "#22272e", "text": "#e8eaed", "muted": "#9aa0a6", "accent": "#4f9cf9",
             "ok": "#3fb950", "warning": "#d29922", "fault": "#f85149", "info": "#58a6ff", "border": "#3a414a",
             "boot": "#4f9cf9"},
    "light": {"bg": "#f6f7f9", "panel": "#ffffff", "text": "#1b1f24", "muted": "#57606a", "accent": "#0b63ce",
              "ok": "#1a7f37", "warning": "#9a6700", "fault": "#cf222e", "info": "#0969da", "border": "#d0d7de",
              "boot": "#0b63ce"},
}
SOURCE_NAMES = {"ru": {"external": "внешнее", "battery": "батарея"}, "en": {"external": "external", "battery": "battery"}}
SENSOR_NAMES = {"ru": {"soc": "процессора", "board": "платы"}, "en": {"soc": "SoC", "board": "board"}}
ISSUE_TEXT = {
    "yaml": {"ru": "файл не разбирается как YAML", "en": "file is not valid YAML"},
    "type": {"ru": "неверный тип значения", "en": "wrong value type"},
    "enum": {"ru": "недопустимое значение", "en": "value not allowed"},
    "range": {"ru": "значение вне допустимого диапазона", "en": "value out of range"},
    "pattern": {"ru": "недопустимый формат", "en": "invalid format"},
    "required": {"ru": "обязательное поле отсутствует", "en": "required field missing"},
    "extra": {"ru": "поле не описано схемой", "en": "field not defined by the schema"},
    "missing-file": {"ru": "файл отсутствует", "en": "file missing"},
    "unknown-file": {"ru": "файл не описан схемой", "en": "file not defined by the schema"},
    "semantic": {"ru": "противоречие между порогами", "en": "thresholds contradict each other"},
}
RESULTS = {
    "applied": {"ru": "Применено и подтверждено: ревизия {revision}", "en": "Applied and confirmed: revision {revision}"},
    "restored": {"ru": "Восстановлено из резервной копии: ревизия {revision}", "en": "Restored from backup: revision {revision}"},
    "rolled-back": {"ru": "Проверка служб не пройдена; восстановлена ревизия {active}",
                    "en": "Service check failed; revision {active} restored"},
    "invalid": {"ru": "Не применено: {issues}", "en": "Not applied: {issues}"},
    "busy": {"ru": "Не применено: предыдущая проверка служб ещё идёт; активна ревизия {active}",
             "en": "Not applied: the previous service check is still running; revision {active} is active"},
    "backup": {"ru": "Резервная копия записана: {name}", "en": "Backup written: {name}"},
    "backup-bad": {"ru": "Резервная копия отклонена: {reason}", "en": "Backup rejected: {reason}"},
    "logs": {"ru": "Журнал выгружен: {name} ({count} записей)", "en": "Log exported: {name} ({count} records)"},
    "config-busy": {"ru": "Не выполнено: ещё идёт предыдущая операция с конфигурацией или файлами",
                    "en": "Not done: a previous configuration or file operation is still running"},
    "op-failed": {"ru": "Операция не выполнена: {reason}", "en": "Operation failed: {reason}"},
    "remote-logs": {"ru": "Журнал удалённого блока выгружен: {name} ({size} байт)",
                    "en": "Remote unit log exported: {name} ({size} bytes)"},
    "remote-off": {"ru": "Удалённый блок выключается по команде пульта", "en": "The remote unit is shutting down on command"},
    "remote-dev": {"ru": "Удалённый блок: разрешена установка одного собственного образа ({reason})",
                   "en": "Remote unit: one own image may be installed ({reason})"},
    "remote-failed": {"ru": "Удалённый блок: не выполнено — {result}{reason}",
                      "en": "Remote unit: not done — {result}{reason}"},
}
# Коды итога команды блока (клиент канала, протокол 1.6) — текстом для оператора; неизвестный код — как есть
REMOTE_CODES = {
    "not-connected": {"ru": "нет связи с удалённым блоком", "en": "no link to the remote unit"},
    "unsupported": {"ru": "блок не поддерживает команду (старая версия ПО)",
                    "en": "the remote unit does not support the command (older software)"},
    "busy": {"ru": "блок выполняет другую команду, повторите позже", "en": "the remote unit is busy, try again later"},
    "timeout": {"ru": "блок не ответил вовремя", "en": "the remote unit did not answer in time"},
    "bad-request": {"ru": "команда отклонена блоком", "en": "the remote unit rejected the command"},
    "error": {"ru": "ошибка на блоке", "en": "error on the remote unit"},
}
REMOTE_GENERIC_ERRORS = {"no link to remote unit"}   # тексты клиента, которые код уже объясняет
# Обслуживание блока (протокол 1.6): ожидание итога выгрузки журнала (journalctl + до 2 МБ по каналу) и выключения
REMOTE_LOG_WAIT_S = 45.0
REMOTE_OFF_WAIT_S = 10.0


def _about_static():
    """Неизменные за время работы сведения о сборке; внешние команды — только здесь, в фоновом потоке."""
    def run(argv, **kw):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=2, **kw).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    from sheregesh import info as devinfo
    img, rel = devinfo.image(), devinfo.release()     # REQ-SYS-007: сборка образа и DEV-выпуск (на плате git нет)
    build = " · ".join(x for x in (f"{img['ota_version']} {img['commit']}".strip(), rel) if x) or \
        run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent).strip() or "—"
    try:
        gst_version = run(["gst-launch-1.0", "--version"]).splitlines()[0].split()[-1]
    except IndexError:
        gst_version = "—"
    try:
        codec = gst.describe(gst.choose_codec())   # с фактическими элементами (проба S2-4)
    except Exception:                              # noqa: BLE001 — сведения, не работа видео
        codec = "—"
    return {"build": build, "gstreamer": gst_version, "videoCodec": codec, "hardware": devinfo.hardware(),
            "serial": devinfo.board()["serial"] or "—"}


def _yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


RECONNECT_WINDOW_S = 30.0


def peer_link(peer_state, snap):
    """Состояние канала для интерфейса: CONNECTED | RECONNECTING | DISCONNECTED | AUTH_FAILED | INCOMPATIBLE."""
    if peer_state == "connected":
        return "CONNECTED"
    if peer_state == "auth-failed":
        return "AUTH_FAILED"
    if peer_state == "incompatible":
        return "INCOMPATIBLE"
    lost = snap.get("lost_s")
    if snap.get("phase") == "connected":
        return "RECONNECTING"                      # монитор уже видит потерю heartbeat, клиент ещё не разорвал
    if lost is not None and lost <= RECONNECT_WINDOW_S:
        return "RECONNECTING"                      # связь была и только что потеряна — идут попытки
    return "DISCONNECTED"


def peer_role(snap):
    """Роль второй стороны: из hello, если канал её передаёт, иначе из проверенного URI сертификата (mTLS)
    urn:sheregesh:<роль>:<device_id>. В loopback без TLS роль не известна — пусто."""
    role = (snap.get("peer") or {}).get("role")
    if role:
        return str(role)
    uri = snap.get("peer_uri") or ""
    parts = uri.split(":")
    return parts[2] if snap.get("secure") and uri.startswith("urn:sheregesh:") and len(parts) >= 4 else ""


def link_view(peer_state, link):
    """Обобщённое состояние канала для индикатора на всех экранах (E2E): CONNECTING | CONNECTED | DISCONNECTED | ERROR.
    Только отображение состояний ADR-0007 (peer_link), новых состояний протокола нет:
    CONNECTING — первое подключение после старта (монитор: connecting) или RECONNECTING;
    ERROR — AUTH_FAILED или INCOMPATIBLE (связь не восстановится без вмешательства)."""
    if link == "CONNECTED":
        return "CONNECTED"
    if link in ("AUTH_FAILED", "INCOMPATIBLE"):
        return "ERROR"
    if link == "RECONNECTING" or peer_state == "connecting":
        return "CONNECTING"
    return "DISCONNECTED"


class Backend(QObject):
    statusChanged = Signal()
    eventsChanged = Signal()
    configChanged = Signal()
    appearanceChanged = Signal()
    logsChanged = Signal()
    videoSourceChanged = Signal()
    videoSourcesChanged = Signal()      # список источников блока и ход выбора (videoSources, videoSelection)
    _aboutDone = Signal(object)         # сведения о сборке (git, GStreamer) — из фонового потока
    aboutChanged = Signal()             # свои сигналы: configChanged перечитывает форму настроек (SettingsPage),
    configBusyChanged = Signal()        # а фоновая проба и ход операции не должны сбрасывать несохранённые правки
    message = Signal(str, str)          # уровень (info/warning/error), текст
    _timeDone = Signal(str, str)        # итог sheregesh-time-peer@ (поток) → журнал в потоке интерфейса

    def __init__(self, data_dir, demo=False, speed=1.0, autostart=True, peer=None, local_video=False,
                 video_command=None, link_cfg=None, link_status_file=None, video_scan=None, journal=None,
                 stick_source=None):
        """peer=None — удалённый блок эмулируется в процессе пульта; peer=(host, port) — внешний процесс-эмулятор.
        local_video — локальный синтетический генератор видео (без удалённого блока).
        video_scan — для испытаний: корни sysfs/dev и ioctl обнаружения устройств UVC (sheregesh.videoinput.scan).
        stick_source — для испытаний: источник стиков вместо USB-джойстика из /dev/input (ADR-0013)."""
        super().__init__()
        self.data_dir = Path(data_dir)
        self.usb_dir = self.data_dir / "usb"
        self.schema = Schema.load()
        self.store = ConfigStore(self.data_dir / "config", self.schema)
        if not self.store.is_initialized():
            self.store.initialize(console_defaults())
        report = self.store.recover()
        self._rev = self.store.active()
        data = self._rev.data
        self._log = logops.AppLog()                 # журнал приложения (logops.py): записи, ревизия, выгрузка
        self._last_phase = None
        self.journal = journal if journal is not None else jr.Journal()
        self._journal_link = None                   # последнее записанное в системный журнал (только переходы)
        self._auth_episodes = 0                     # переходы канала в auth-failed (если клиент не ведёт счётчик)
        self._power_history = collections.deque(maxlen=POWER_HISTORY)
        self.client = None
        self.link_cfg = link_cfg
        self._videorx = None                        # видеосеанс SRTP через службу sheregesh-video-rx (ADR-0016)
        if link_cfg is not None or peer is not None:
            self._videorx = self._make_videorx(link_cfg)
        if link_cfg is not None:                    # сетевой лабораторный профиль: mTLS к удалённому блоку (S3.1)
            peer = (link_cfg.peer_hosts[0] if link_cfg.peer_hosts else "discover", link_cfg.port)
            self.client = PeerClient(None, link_cfg.port, heartbeat_timeout_s=data["link.yaml"]["heartbeat_timeout_s"],
                                     tls_cfg=link_cfg, status_file=link_status_file,
                                     video_session=self._videorx).start()
            from sheregesh import board, services as svc
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"],
                               peer_factory=lambda clock: RemotePeer(self.client, clock),
                               services=svc.Cached("console"),       # REQ-SW-001: службы пульта на плате
                               sensors=board.sensors())              # температура SoC платы, питание — нет данных
        elif peer is not None:
            host, port = peer
            self.client = PeerClient(host, port, heartbeat_timeout_s=data["link.yaml"]["heartbeat_timeout_s"],
                                     status_file=link_status_file, video_session=self._videorx).start()
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"],
                               peer_factory=lambda clock: RemotePeer(self.client, clock))
        else:
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"])
        self.log("info", "app", f"start: software {__version__}, protocol {PROTOCOL_VERSION}, "
                                f"peer {'%s:%d' % peer if peer else 'in-process emulator'}")
        for code, _ in report.actions:
            self.bench.events.inject("CFG-RECOVERED", detail=code)
            self.log("warning", "config", f"recovery action {code}")
        # Внешний модуль удалённого блока (ADR-0012): только показ status.module и команды module-cmd через клиент
        self._module = ModuleView(self, command=lambda: getattr(self._module_peer(), "module_command", None),
                                  log=lambda level, text: self.log(level, "module", text))
        self._module_key = None                     # (support, seq) последней записи в журналы
        self._module_fault = ""                     # код активного события MODULE-FAULT
        # Стики и кнопки аппаратуры → удалённый блок (ADR-0013): передача выключена после каждого старта пульта
        self._sticks = SticksView(self, source=stick_source if stick_source is not None else StickSource(INPUT_DIR),
                                  client=lambda: self._sticks_peer(),
                                  log=lambda level, text: self._sticks_log(level, text))
        self.video = VideoReceiver(self, command=video_command)
        # Координация видео (videoops.py): вход из конфигурации, маршрут приёмника, источник для экранов, выбор
        self.videoops = videoops.VideoOps(self.video, client=lambda: self.client, link_cfg=link_cfg,
                                          videorx=self._videorx, events=self.bench.events,
                                          status=lambda: getattr(self, "_status", None),
                                          log=lambda level, text: self.log(level, "video", text),
                                          journal=self.journal, local_video=local_video, scan=video_scan, parent=self)
        self.videoops.sourceChanged.connect(self.videoSourceChanged)
        self.videoops.sourcesChanged.connect(self.videoSourcesChanged)
        self._apply_video_source()
        self.demo = demo
        self.runner = DEMO.runner(self.bench) if demo else None
        self._scenario_label = ""
        self._status, self._events, self._active = {}, [], []
        self._events_rev = 0
        self._last_result = ""
        self._last_export = ""
        self._last_remote = ""                      # итог последней команды обслуживания блока (протокол 1.6)
        self._ever_connected = False                # связь с блоком была с запуска пульта (оверлей «ожидание блока»)
        self._remote_off = False                    # блок выключен командой с пульта и ещё не вернулся
        self.lastRemoteCode = ""                    # … её код (remote_command result) — для локального канала
        self._last_ok = False                       # итог последней операции с конфигурацией или файлами
        # Применение, резервная копия, восстановление и выгрузка журнала — вне потока интерфейса (configops.py):
        # итог приходит сигналом, состояние (ревизия, монитор, события) меняется только здесь.
        self._ops = configops.ConfigOps(self)
        self._ops.finished.connect(self._op_done)
        self._ops.busyChanged.connect(self.configBusyChanged)
        self.configChanged.connect(self.aboutChanged)       # ревизия и язык — тоже в about
        self._about_static = {"build": "…", "gstreamer": "…", "videoCodec": "…", "hardware": "…", "serial": "…"}
        self._aboutDone.connect(self._about_ready)
        self._timeDone.connect(lambda level, text: self.log(level, "time", text))
        threading.Thread(target=lambda: self._aboutDone.emit(_about_static()), name="about", daemon=True).start()
        self._timer = QTimer(self)
        self._timer.setInterval(max(20, int(TICK_S * 1000 / max(speed, 0.01))))
        self._timer.timeout.connect(self.tick)
        self._refresh()
        if autostart:
            self._timer.start()
            self._sticks.start()                    # потоки устройства и передачи — только в работающем пульте

    # ---------- время и сценарий ----------
    @Slot()
    def tick(self):
        self.last_tick = time.monotonic()           # для watchdog: таймер бэкенда действительно работает
        if self.runner and not self.runner.done:
            n = len(self.runner.log)
            self.runner.advance()
            if len(self.runner.log) > n:
                self._scenario_label = self.runner.log[-1][1]
        else:
            self.bench.step()
        self._refresh()

    def run_until(self, t):
        """Для тестов и снимков экрана: детерминированно довести сценарий до момента t без таймера."""
        while self.bench.clock() < t - 1e-9:
            self.tick()

    def _make_videorx(self, link_cfg):
        """Видеосеанс SRTP: порт видео — из link.json (video_port), канал — через self.client."""
        port = link_cfg.video_port if link_cfg is not None else videorx.link_video_port()
        vrx = videorx.VideoRx(lambda t, b: self.client is not None and self.client.send_video(t, b),
                              lambda: self.client.peer_ip() if self.client is not None else None,
                              clock_offset=lambda: self.client.clock_offset() if self.client is not None else None,
                              log=lambda level, text: self.log(level, "video", text), rx_port=port)
        return vrx.start()

    def stop(self):
        self._ops.close(5.0)                        # начатая запись конфигурации завершается, новые не начинаются
        if self._videorx is not None:
            self._videorx.stop()
        self._timer.stop()
        self._sticks.stop()                         # без прощального кадра: блок сам уйдёт в FAILSAFE (ST-06)
        self.video.shutdown()
        self.videoops.stop_local_sender()
        if self.client is not None:
            self.client.stop()

    def video_config(self):
        return videoops.video_config(self._rev.data["system.yaml"])

    def _apply_video_source(self):
        self.videoops.apply(self.video_config())

    # ---------- журнал приложения ----------
    def log(self, level, source, text):
        self._log.add(level, source, text)
        if source == "remote":                      # обслуживание блока — ещё и в системный журнал (REQ-UI-006)
            print(f"[console] remote: {level}: {text}", file=sys.stderr, flush=True)

    # ---------- представление ----------
    @property
    def lang(self):
        return self._rev.data["system.yaml"]["language"]

    def _label(self, label):
        return label.get(self.lang, label.get("en", "")) if isinstance(label, dict) else label

    def issue_text(self, issue):
        where = f"{issue.file}: {issue.path}" if issue.path else issue.file
        return f"{where} — {ISSUE_TEXT.get(issue.kind, {}).get(self.lang, issue.kind)}"

    def _result(self, key, **kw):
        return RESULTS[key][self.lang].format(**kw)

    def _render_event(self, e):
        params = dict(e.params)
        if "source" in params:
            params["source"] = SOURCE_NAMES[self.lang].get(params["source"], params["source"])
        if "sensor" in params:
            params["sensor"] = SENSOR_NAMES[self.lang].get(params["sensor"], params["sensor"])
        text, action = render(e.code, self.lang, params)
        return {"id": e.id, "t": round(e.t, 1), "code": e.code, "severity": e.severity, "text": text,
                "action": action, "active": e.active,
                "resolved": -1 if e.resolved_t is None else round(e.resolved_t, 1)}

    def _refresh(self):
        s = self.bench.snapshot
        p = s.power
        peer_age = None if s.peer.last_heartbeat is None else round(s.t - s.peer.last_heartbeat, 1)
        self._status = {
            "t": round(s.t, 1), "overall": s.overall, "indicator": s.indicator,
            "indicatorColor": self.bench.indicator.color()[0] if self.bench.indicator.color() else "",
            "powerAvailable": p is not None,
            "powerSource": SOURCE_NAMES[self.lang][p.source] if p else "",
            "powerPercent": round(p.percent, 1) if p else -1,
            "lowBattery": bool(p and p.low_battery),
            "tempAvailable": s.temperatures is not None,
            "tempSoc": (s.temperatures or {}).get("soc", -1),
            "tempBoard": (s.temperatures or {}).get("board", -1),          # на Pi 4 датчика платы нет
            "fanDuty": round(s.fan_duty), "fanRpm": -1 if s.fan_rpm is None else s.fan_rpm,
            "services": [{"side": side, "name": n.removeprefix("sheregesh-").removesuffix(".service"),
                          "state": st.get("active", ""), "restarts": st.get("restarts", 0)}
                         for side in ("console", "remote") for n, st in sorted((s.services or {}).get(side, {}).items())],
            "peerState": s.peer_state, "peerProtocol": s.peer.protocol or "", "peerDevice": s.peer.device_id or "",
            "peerSoftware": semver_or_raw(s.peer.software), "peerHeartbeatAge": -1 if peer_age is None else peer_age,
            "localProtocol": PROTOCOL_VERSION, "software": __semver__,
            "shutdownRequested": s.shutdown_requested or "",
            "scenario": DEMO.name if self.demo else "",
            "scenarioLabel": self._label(self._scenario_label),
            "scenarioDone": bool(self.runner and self.runner.done),
            "activeCount": len(s.active),
        }
        self._status.update(self._link_status(s))
        self._update_module(s)
        self._sticks.update_link(self._status["peerLink"] if self._status["peerMode"] != "emulated" else "",
                                 s.peer.session)
        self._journal_transitions()
        if p is not None:
            self._power_history.append(round(p.percent, 1))
        self._log.mirror_events(self.bench.monitor.log.events)
        events = self.bench.monitor.log.events
        sig = (events[-1].id if events else 0, tuple(e.id for e in s.active), self.lang,
               sum(1 for e in events if e.resolved_t is not None))
        if sig != getattr(self, "_events_sig", None):      # перестраивать список только при изменениях
            self._events_sig = sig
            self._active = [self._render_event(e) for e in s.active]
            self._events = [self._render_event(e) for e in reversed(events)]
            self._events_rev += 1
            self.eventsChanged.emit()
        if self._log.take_dirty():                  # журнал — в QML только при новых записях
            self.logsChanged.emit()
        self.statusChanged.emit()

    def _link_status(self, s):
        tel = s.peer.telemetry or {}
        temps = tel.get("temperatures") or {}
        rp = tel.get("power") or {}
        vid = tel.get("video") or {}
        snap = self.client.snapshot() if self.client is not None else {}
        if self.client is not None:
            cnt = snap["counters"]
            if snap["phase"] != self._last_phase:
                if snap["phase"] == "auth-failed":
                    self._auth_episodes += 1
                self.log("warning" if snap["phase"] != "connected" else "info", "link",
                         f"phase {snap['phase']} ({snap['detail']})")
                self._last_phase = snap["phase"]
                if snap["phase"] == "connected" and snap.get("secure"):
                    self._time_peer(snap.get("host"))
        else:
            cnt = {"connects": 0, "disconnects": 0, "bad_messages": 0, "ignored_messages": 0, "received": 0,
                   "seq_gaps": 0}
        link = peer_link(s.peer_state, snap)
        self.videoops.on_link(s.peer_state, snap, vid, link)
        if link == "CONNECTED":
            self._ever_connected, self._remote_off = True, False
        return {
            "peerMode": ("network" if snap.get("secure") else "process") if self.client is not None else "emulated",
            "peerLink": link, "linkView": link_view(s.peer_state, link),
            "linkEverConnected": self._ever_connected, "remoteOff": self._remote_off,
            "peerRole": peer_role(snap) if s.peer_state == "connected" else "",
            "linkAuthFailures": cnt.get("auth_failures", self._auth_episodes),
            "linkSecure": bool(snap.get("secure")), "linkTls": snap.get("tls") or "", "linkHost": snap.get("host") or "",
            "linkPeerUri": snap.get("peer_uri") or "",
            # круговая задержка — из телеметрии блока; без связи прежнее значение не показывается
            "linkRttMs": (tel.get("link") or {}).get("rtt_avg_ms", -1) if s.peer_state == "connected" else -1,
            "peerDetail": s.peer.detail, "peerSession": s.peer.session or "",
            "peerHardware": s.peer.hardware or "",
            "linkConnects": cnt["connects"], "linkBad": cnt["bad_messages"], "linkIgnored": cnt["ignored_messages"],
            "linkReceived": cnt["received"], "linkGaps": cnt["seq_gaps"],
            "remoteAvailable": bool(tel), "remoteState": tel.get("state", ""),
            "remoteTempSoc": temps.get("soc", -1), "remoteTempBoard": temps.get("board", -1),
            "remotePowerPercent": rp.get("percent", -1),
            "remotePowerSource": SOURCE_NAMES[self.lang].get(rp.get("source", ""), rp.get("source", "")),
            "remoteLowBattery": bool(rp.get("low_battery")), "remoteUptime": tel.get("uptime_s", -1),
            "remoteEvents": ", ".join(tel.get("active_events") or []),
            "remoteVideo": bool(vid.get("active")), "videoSource": self.videoops.source,
            "batteryWarning": self._rev.data["diagnostics.yaml"]["battery_warning_pct"],
            "batteryCritical": self._rev.data["diagnostics.yaml"]["battery_critical_pct"],
            "undervoltage": bool(s.power and s.power.undervoltage),
        }

    def _time_peer(self, host):
        """ADR-0017: блок сеанса может спрашивать время у пульта (chrony). systemctl — не в потоке интерфейса."""
        def run():
            ok, detail = timepeer.start(host)
            self._timeDone.emit("info" if ok else "warning",
                                f"time peer {detail}" if ok else f"time peer not set: {detail}")
        threading.Thread(target=run, name="time-peer", daemon=True).start()

    # ---------- внешний модуль удалённого блока (ADR-0012, S6) ----------
    def _module_peer(self):
        """Клиент канала с блоком, у которого вызывается module_command (для испытаний подменяется)."""
        return self.client

    def _update_module(self, s):
        tel = s.peer.telemetry or {}
        mv = self._module
        mv.update(telemetry=tel.get("module"), session=s.peer.session, link=self._status["peerLink"],
                  mode=self._status["peerMode"], received=bool(tel))
        # Диагностическое событие: ошибка модуля по данным блока (при потере связи — снимается, ADR-0012, 12.2)
        code = (mv.lastErrorCode or "MOD-UNKNOWN") if mv.support == "ok" and mv.state == "FAULT" else ""
        if code != self._module_fault:
            if self._module_fault:
                self.bench.events.clear("MODULE-FAULT")
            if code:                                # у события свой параметр «code»: inject(code, …) его не примет
                self.bench.monitor.log.raise_(self.bench.clock(), "MODULE-FAULT", code=code)
            self._module_fault = code
        key = (mv.support, mv.seq)
        if key == self._module_key:
            return
        state = mv.state if mv.support == "ok" else mv.support
        hist = mv.history[0] if mv.history and mv.history[0]["seq"] == mv.seq else {}
        detail = (f"from={hist.get('from') or '-'} reason={mv.reason or '-'} code={hist.get('code') or '-'} "
                  f"profile={mv.profileId or '-'} backend={mv.backendKind or '-'}")
        level = {"READY": "info", "FAULT": "error", "DISCONNECTED": "warning", "RECOVERING": "warning"}.get(state, "info")
        if mv.support != "ok":
            level = "warning" if mv.support == "no-link" else "info"
        first, self._module_key = self._module_key is None, key
        if mv.support == "local" or (first and mv.support != "ok"):
            return                                  # пульт без блока / запуск до первой телеметрии — не переход
        self.log(level, "module", f"{state} ({detail})")
        prio = {"READY": jr.INFO, "FAULT": jr.ERR, "DISCONNECTED": jr.WARNING, "RECOVERING": jr.WARNING,
                "no-link": jr.WARNING}.get(state, jr.NOTICE)
        self.journal.emit(prio, f"module: {state} ({detail})")

    @Property(QObject, constant=True)
    def module(self):
        """Представление внешнего модуля для ModulePage.qml (sheregesh_console.module.ModuleView)."""
        return self._module

    # ---------- стики и кнопки → блок (ADR-0013, S7) ----------
    def _sticks_peer(self):
        """Клиент канала с блоком, у которого вызывается send_rc (для испытаний подменяется)."""
        return self.client

    def _sticks_log(self, level, text):
        self.log(level, "sticks", text)
        self.journal.emit({"warning": jr.WARNING, "error": jr.ERR}.get(level, jr.INFO), f"sticks: {text}")

    @Property(QObject, constant=True)
    def sticks(self):
        """Представление стиков и передачи для SticksPage.qml (sheregesh_console.sticks.SticksView)."""
        return self._sticks

    def _journal_transitions(self):
        """Системный журнал (E2E-12): только смены обобщённого состояния канала и наличия видеопотока."""
        st = self._status
        view = st["linkView"]
        if view != self._journal_link:
            prio = {"CONNECTED": jr.INFO, "CONNECTING": jr.NOTICE, "ERROR": jr.ERR}.get(view, jr.WARNING)
            peer = st["peerDevice"] or "-"
            self.journal.emit(prio, f"link: {view} (state={st['peerLink']} detail={st['peerDetail'] or '-'} "
                                    f"peer={peer} host={st['linkHost'] or '-'} tls={st['linkTls'] or 'none'} "
                                    f"mode={st['peerMode']})")
            self._journal_link = view
        self.videoops.journal_state()

    @Property(dict, notify=statusChanged)
    def status(self):
        return self._status

    @Property(QObject, constant=True)
    def videoReceiver(self):
        return self.video

    @Property(dict, notify=videoSourceChanged)
    def videoSource(self):
        """Источник видео для экранов (sheregesh_console.videosource): вход, состояние, режим, причина, решение для
        кадра (display, live)."""
        return self.videoops.display

    @Property(list, notify=videoSourcesChanged)
    def videoSources(self):
        """Источники блока: [{id, input, name, available, active}] (пусто — блок списка не сообщает)."""
        return self.videoops.sources

    @Property(dict, notify=videoSourcesChanged)
    def videoSelection(self):
        """Ход выбора: state idle|switching|error, id/name выбираемого, error — код (unknown, unavailable, unsupported,
        no-link, busy, timeout, failed), detail — текст блока; blocker — почему выбор недоступен ("" — доступен)."""
        return self.videoops.selection

    @Slot(str, bool, result=bool)
    def selectVideoSource(self, source_id, confirmed):
        """Выбор источника на блоке — только после подтверждения на экране; итог — в videoSelection (videoops.py)."""
        return self.videoops.select(source_id, confirmed)

    @Property(list, notify=statusChanged)
    def powerHistory(self):
        return list(self._power_history)

    @Property(list, notify=logsChanged)
    def logs(self):
        return self._log.rows()

    # QML получает списки событий и журнала только через вызов слота: результат вызова — готовый
    # JS-массив. Свойство-список в QML 6 — ссылка, каждое обращение к элементу перечитывает весь
    # список из Python, и фильтрация в QML становится O(n²) (5 с паузы GUI на 60-минутном прогоне).
    @Property(int, notify=logsChanged)
    def logsRevision(self):
        return self._log.revision

    @Property(int, notify=logsChanged)
    def logCount(self):
        return len(self._log)

    @Slot(int, result=list)
    def logRows(self, level_filter):
        """Записи журнала от новых к старым; 0 — все, 1 — предупреждения и ошибки, 2 — ошибки."""
        return self._log.rows(level_filter)

    @Property(dict, notify=aboutChanged)
    def about(self):
        """Сведения для страницы «О системе». Команды (git, gst-launch, проба кодека) выполнены заранее в фоне:
        чтение свойства их не запускает (QML читает его на каждое поле)."""
        st = self._about_static
        return {"software": __semver__, "protocol": PROTOCOL_VERSION, "build": st["build"],
                "schema": self.schema.version, "revision": self._rev.id,
                "device": self._rev.data["system.yaml"]["device_name"],
                "hardware": st.get("hardware", "…"), "serial": st.get("serial", "…"),
                "platform": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
                "qt": f"Qt {qVersion()}, PySide6 {PYSIDE_VERSION}", "gstreamer": st["gstreamer"],
                "videoCodec": st["videoCodec"], "catalog": len(CATALOG)}

    def _about_ready(self, st):
        self._about_static = st
        self.aboutChanged.emit()

    @Slot(result=bool)
    def exportLogs(self):
        """Выгрузка журнала на «USB» с фильтром секретов и контрольной суммой (REQ-SW-001/005). Записи берутся сейчас,
        запись файлов — в фоне; имя файла — в lastExport по завершении. False — идёт другая операция."""
        if self._refuse_busy():
            return False
        name, data, count = self._log.export_payload()
        usb = self.usb_dir

        def run():
            logops.write_export(usb, name, data)
            return name, count
        return self._ops.submit("logs", run)

    def _remote_call(self):
        return getattr(self._module_peer(), "remote_command", None)

    @Slot(result=bool)
    def exportRemoteLogs(self):
        """Журнал удалённого блока на «USB» пульта (протокол 1.6, REQ-SW-001/005): блок фильтрует секреты, пульт сверяет
        SHA-256 частей и пишет файл с контрольной суммой рядом. False — идёт другая операция."""
        if self._refuse_busy():
            return False
        call, usb = self._remote_call(), self.usb_dir

        def run():
            if not callable(call):
                return {"ok": False, "result": "unsupported", "error": "no link to remote unit"}
            r = call("log-export", REMOTE_LOG_WAIT_S)
            if r.get("ok"):
                data = r.pop("data")
                logops.write_export(usb, r["name"], data)
                r["size"] = len(data)
            return r
        self.log("info", "remote", "log export requested")
        return self._ops.submit("remote-logs", run)

    @Slot(bool, result=bool)
    def shutdownRemote(self, confirmed):
        """Штатное выключение удалённого блока командой с пульта (REQ-UI-006): только с подтверждением оператора;
        событие — в журналах обоих устройств."""
        if not confirmed or self._refuse_busy():
            return False
        call = self._remote_call()

        def run():
            if not callable(call):
                return {"ok": False, "result": "unsupported", "error": "no link to remote unit"}
            return call("shutdown", REMOTE_OFF_WAIT_S)
        self.log("warning", "remote", "shutdown requested by operator (confirmed)")
        return self._ops.submit("remote-shutdown", run)

    @Slot(bool, result=bool)
    def armRemoteDevImage(self, confirmed):
        """Разовый режим разработчика удалённого блока (REQ-UPD-005: подтверждение на пульте). Только из диалога."""
        if not confirmed or self._refuse_busy():
            return False
        call = self._remote_call()

        def run():
            if not callable(call):
                return {"ok": False, "result": "unsupported", "error": "no link to remote unit"}
            return call("dev-arm", REMOTE_OFF_WAIT_S + 10)
        self.log("warning", "remote", "developer mode on remote unit requested by operator (confirmed)")
        return self._ops.submit("remote-dev", run)

    @Property(str, notify=configChanged)
    def lastRemoteResult(self):
        return self._last_remote

    @Property(str, notify=configChanged)
    def lastExport(self):
        return self._last_export

    @Property(list, notify=eventsChanged)
    def events(self):
        return self._events

    @Property(int, notify=eventsChanged)
    def eventsRevision(self):
        return self._events_rev

    @Property(int, notify=eventsChanged)
    def eventCount(self):
        return len(self._events)

    @Slot(int, result=list)
    def eventRows(self, filter_index):
        """Активные события сверху, затем остальные от новых к старым; 0 — все, 1 — активные,
        2 — предупреждения и ошибки, 3 — ошибки."""
        ok = {0: lambda e: True, 1: lambda e: e["active"], 2: lambda e: e["severity"] != "info",
              3: lambda e: e["severity"] == "error"}
        rows = [e for e in self._events if ok.get(filter_index, ok[0])(e)]
        return [e for e in rows if e["active"]] + [e for e in rows if not e["active"]]

    @Property(list, notify=eventsChanged)
    def activeEvents(self):
        return self._active

    @Property(str, notify=appearanceChanged)
    def language(self):
        return self.lang

    @Property(str, notify=appearanceChanged)
    def theme(self):
        return self._rev.data["system.yaml"]["theme"]

    @Property(int, notify=appearanceChanged)
    def uiScale(self):
        return self._rev.data["system.yaml"]["ui_scale"]

    @Property(dict, notify=appearanceChanged)
    def palette(self):
        return PALETTES[self.theme]

    # ---------- настройки ----------
    @Property(dict, notify=configChanged)
    def config(self):
        """Плоское представление активной ревизии для формы настроек."""
        d = self._rev.data
        return {**VIDEO_DEFAULTS, **d["system.yaml"], **d["diagnostics.yaml"], **d["link.yaml"]}

    @Property(str, notify=configChanged)
    def revision(self):
        return self._rev.id

    @Property(list, notify=configChanged)
    def history(self):
        return self.store.history()[:6]

    @Property(str, notify=configChanged)
    def lastResult(self):
        return self._last_result

    @Property(bool, notify=configChanged)
    def lastOk(self):
        return self._last_ok

    @Property(bool, notify=configBusyChanged)
    def configBusy(self):
        """Идёт применение, резервная копия, восстановление или выгрузка журнала."""
        return self._ops.busy

    def _refuse_busy(self):
        if not self._ops.busy:
            return False
        self._last_result = self._result("config-busy")
        self.configChanged.emit()
        self.message.emit("warning", self._last_result)
        return True

    def _op_done(self, op, result):
        """Итог фоновой операции — в потоке интерфейса."""
        self._last_ok = not isinstance(result, Exception) and getattr(result, "ok", True)
        if isinstance(result, Exception):
            self.log("warning", "config", f"{op} failed: {result}")
            key = "backup-bad" if op == "restore" and isinstance(result, BackupError) else "op-failed"
            self._last_result = self._result(key, reason=result)
            self.configChanged.emit()
            self.message.emit("error", self._last_result)
        elif op in ("apply", "restore"):
            self._after_change(result, "applied" if op == "apply" else "restored")
        elif op == "backup":
            self._last_result = self._result("backup", name=result.name)
            self.log("info", "config", f"backup {result.name}")
            self.configChanged.emit()
        elif op == "logs":
            name, count = result
            self._last_export = name
            self._last_result = self._result("logs", name=name, count=count)
            self.log("info", "logs", f"exported {name}")
            self.configChanged.emit()
        elif op in ("remote-logs", "remote-shutdown", "remote-dev"):
            self._remote_done(op, result)

    def _remote_done(self, op, r):
        self._last_ok = bool(r.get("ok"))
        self.lastRemoteCode = str(r.get("result") or ("ok" if self._last_ok else "error"))
        if not self._last_ok:
            code = str(r.get("result") or "error")
            err = str(r.get("error") or "")
            reason = f" ({err})" if err and err not in REMOTE_GENERIC_ERRORS else ""
            self._last_result = self._result("remote-failed", result=REMOTE_CODES.get(code, {}).get(self.lang, code),
                                             reason=reason)
            self.log("warning", "remote", f"{op} {r.get('result')} {r.get('error', '')}".strip())
            self.message.emit("error", self._last_result)
        elif op == "remote-logs":
            self._last_export = r["name"]
            self._last_result = self._result("remote-logs", name=r["name"], size=r.get("size", "?"))
            self.log("info", "remote", f"remote log exported {r['name']}")
        elif op == "remote-dev":
            self._last_result = self._result("remote-dev", reason=r.get("error") or "15 min")
            self.log("warning", "remote", f"remote unit developer mode armed: {r.get('error', '')}".strip())
        else:
            self._remote_off = True                 # до следующей связи: «блок выключен оператором», не «потеряна»
            self._last_result = self._result("remote-off")
            self.log("warning", "remote", "remote unit accepted shutdown: link loss is expected")
            self.message.emit("info", self._last_result)
        self._last_remote = self._last_result
        self.configChanged.emit()

    def _files_from_form(self, form: dict):
        fields = {name: list(spec["schema"]["properties"]) for name, spec in self.schema.files.items()}
        current = self._rev.data
        files = {}
        for name, keys in fields.items():
            data = dict(current[name])
            for k in keys:
                if k in form:
                    data[k] = form[k]
            files[name] = _yaml(data)
        return files

    @Slot(dict, result=list)
    def validateForm(self, form):
        """Проверка без применения. Возвращает список строк с файлом, полем и причиной."""
        return [self.issue_text(i) for i in self.store.validate(self._files_from_form(form))]

    def _health_check(self, rev, services):
        """Проверка затронутых служб S1: интерфейс знает язык и тему. Вызывается в потоке проверки хранилища, поэтому
        только читает ревизию: пороги монитора и прочие службы перенастраивает владелец (_after_change) в потоке
        интерфейса по фактически активной ревизии — поздно завершившаяся проверка их не трогает."""
        data = rev.data
        return data["system.yaml"]["language"] in SOURCE_NAMES and data["system.yaml"]["theme"] in PALETTES

    def _after_change(self, res, ok_key):
        self._rev = self.store.active()
        data = self._rev.data
        if self.client is not None:
            self.client.set_heartbeat_timeout(data["link.yaml"]["heartbeat_timeout_s"])
        self.log("info" if res.ok else "warning", "config",
                 f"apply {res.stage}: revision {res.revision}, active {res.active}")
        self.bench.monitor.set_thresholds(data["diagnostics.yaml"], data["link.yaml"])
        self._apply_video_source()
        if res.ok:
            self.bench.events.inject("CFG-APPLIED", revision=res.revision)
            self._last_result = self._result(ok_key, revision=res.revision)
        elif res.stage == "validation":
            self._last_result = self._result("invalid", issues="; ".join(self.issue_text(i) for i in res.issues))
        elif res.stage == "busy":                     # ревизия не создавалась: не откат, событие CFG-ROLLBACK не нужно
            self._last_result = self._result("busy", active=res.active)
        else:
            self.bench.events.inject("CFG-ROLLBACK", revision=res.revision, active=res.active)
            self._last_result = self._result("rolled-back", active=res.active)
        self.configChanged.emit()
        self.appearanceChanged.emit()
        self._refresh()
        self.message.emit("info" if res.ok else "error", self._last_result)
        return res.ok

    @Slot(dict, bool, result=bool)
    def applyForm(self, form, confirmed):
        """Изменение состояния: только с явным подтверждением в диалоге (REQ-SYS-009). True — применение начато
        (итог — lastResult и message по завершении); False — без подтверждения или идёт другая операция."""
        if not confirmed:
            self._last_result = "confirmation required"
            self.configChanged.emit()
            return False
        if self._refuse_busy():
            return False
        files, store = self._files_from_form(form), self.store
        return self._ops.submit("apply", lambda: store.apply(files, self._health_check, reason="GUI settings"))

    @Slot(result=bool)
    def backup(self):
        """Резервная копия на «USB» в фоне; имя — в lastResult по завершении."""
        if self._refuse_busy():
            return False
        store, usb = self.store, self.usb_dir

        def run():
            usb.mkdir(parents=True, exist_ok=True)
            return store.backup(usb)
        return self._ops.submit("backup", run)

    @Slot(result=list)
    def backups(self):
        if not self.usb_dir.exists():
            return []
        return sorted((p.name for p in self.usb_dir.glob("*.tar")), reverse=True)

    @Slot(str, bool, result=bool)
    def restore(self, name, confirmed):
        """Восстановление из резервной копии после подтверждения, в фоне; итог — по завершении."""
        if not confirmed or self._refuse_busy():
            return False
        store, path = self.store, self.usb_dir / name
        return self._ops.submit("restore", lambda: store.restore(path, self._health_check, confirmed=True))

    @Slot()
    def reload(self):
        """Перечитать конфигурацию с диска (новое хранилище, как при перезапуске). Не во время записи."""
        if self._refuse_busy():
            return
        self.store = ConfigStore(self.store.root, self.schema)
        self.store.recover()
        self._rev = self.store.active()
        self._apply_video_source()
        self.log("info", "config", f"reloaded from disk: revision {self._rev.id}")
        self.configChanged.emit()
        self.appearanceChanged.emit()
        self._refresh()

    @Slot(result=str)
    def clockText(self):
        return time.strftime("%H:%M:%S", time.gmtime(self.bench.clock()))
