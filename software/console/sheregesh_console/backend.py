# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Связь QML ↔ монитор и конфигурация (ADR-0003). GUI не пишет файлы и не обращается к оборудованию:
все данные — от эмуляторов, все изменения конфигурации — через ConfigStore с подтверждением."""
import collections
import hashlib
import os
import platform
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import yaml
from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot, qVersion

from sheregesh import PROTOCOL_VERSION, __version__
from sheregesh import video as gst
from sheregesh import videoinput
from sheregesh.config import BackupError, ConfigStore, Schema, console_defaults
from sheregesh.faults import CATALOG, render
from sheregesh.link.client import PeerClient, RemotePeer
from sheregesh.videolink import VideoClient
from sheregesh_emulators import Bench
from sheregesh_emulators.demo import DEMO
from sheregesh_emulators.scenario import TICK_S

from . import journal as jr
from . import videosource as vsrc
from .module import ModuleView
from .sticks import INPUT_DIR, StickSource, SticksView
from .video import VideoReceiver
from . import videorx

LOG_LIMIT = 2000
POWER_HISTORY = 240
# Фильтр секретов для экспорта журналов (REQ-SW-005): шаблоны из docs/security/secrets.md
REDACT = re.compile(r"SHRG-CANARY-[A-Z]+-[A-Za-z0-9]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:(password|passwd|token|secret)\s*[=:]\s*\S+)")

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
}


def _yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


RECONNECT_WINDOW_S = 30.0
SELECT_TIMEOUT_S = 15.0      # выбор источника видео: блок должен отметить новый источник активным за это время
# Ответ блока/клиента на выбор источника → код ошибки для экрана (VideoPage.selectErrorText)
_SELECT_ERRORS = (("unknown", ("unknown", "not-found", "no-such", "invalid-id", "bad-id")),
                  ("unavailable", ("unavailable", "not-available", "no-device", "offline")),
                  ("unsupported", ("unsupported", "not-supported", "not-implemented")),
                  ("no-link", ("not-connected", "no-link", "disconnected", "no-session")),
                  ("busy", ("busy", "in-progress")),
                  ("timeout", ("timeout", "timed-out")))


def select_error(result):
    """Итог вызова select_video_source → (код, подробность). Код "" — блок принял запрос. Окончательный вид ответа
    задаёт клиент канала (агент B), поэтому принимаются: None/True/"ok" — принято; False — отказ; dict {ok, error,
    detail|message}; строка-код; исключение (код из .code или текста)."""
    if isinstance(result, BaseException):
        code, detail = str(getattr(result, "code", "") or ""), str(result) or type(result).__name__
    elif result is None or result is True:
        return "", ""
    elif result is False:
        return "failed", ""
    elif isinstance(result, dict):
        if result.get("ok", not result.get("error")) is True:
            return "", ""
        code = str(result.get("error") or result.get("code") or "")
        detail = str(result.get("detail") or result.get("message") or "")
    elif isinstance(result, str):
        if result.strip().lower() in ("ok", "accepted", "switching", "selected"):
            return "", ""
        code, detail = result, ""
    else:
        return "", ""
    probe = re.sub(r"[\s_]+", "-", f"{code} {detail}".lower())
    for norm, keys in _SELECT_ERRORS:
        if any(k in probe for k in keys):
            return norm, vsrc.scrub(detail)[:200]
    return "failed", vsrc.scrub(detail or code)[:200]
# Видеовход пульта (S3.3): необязательные поля system.yaml. Без них — прежнее поведение (синтетический поток).
VIDEO_DEFAULTS = {"video_source": "synthetic", "video_device": "", "video_mode": "1280x720@30", "video_format": "auto"}


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
    _selectDone = Signal(int, object)   # из потока вызова select_video_source → в поток интерфейса
    message = Signal(str, str)          # уровень (info/warning/error), текст

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
        self._logs = collections.deque(maxlen=LOG_LIMIT)
        self._log_seq = 0
        self._last_event_id = 0
        self._last_phase = None
        self.journal = journal if journal is not None else jr.Journal()
        self._journal_link = None                   # последнее записанное в системный журнал (только переходы)
        self._journal_video = None
        self._auth_episodes = 0                     # переходы канала в auth-failed (если клиент не ведёт счётчик)
        self._last_video_state = None
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
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"],
                               peer_factory=lambda clock: RemotePeer(self.client, clock))
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
        self._vsrc = {**vsrc.empty(), **vsrc.display(vsrc.empty(), "off")}
        self._remote_video_tel = None               # video из телеметрии блока, пока сеанс управления есть
        self._peer_link = ""                        # peer_link канала управления (для «Переподключение» на «Видео»)
        self._vsources = []                         # status.video.sources блока (videosource.sources)
        self._vsel = {"state": "idle", "id": "", "name": "", "error": "", "detail": ""}
        self._sel_seq = 0
        self._handover = vsrc.Handover()            # переход источника блока: подтверждён ли он кадрами (GUI-01)
        self._selectDone.connect(self._on_select_done)
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
        self.video.stateChanged.connect(self._journal_video_state)
        self.video.stateChanged.connect(self._update_video_source)
        self.video.frameChanged.connect(self._on_video_frame)
        self._local_sender = None
        self._local_video = local_video
        self._video_scan = dict(video_scan or {})
        self._video_source = None
        self._apply_video_source()
        self.demo = demo
        self.runner = DEMO.runner(self.bench) if demo else None
        self._scenario_label = ""
        self._status, self._events, self._active = {}, [], []
        self._events_rev = 0
        self._last_result = ""
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
        """Видеосеанс SRTP: настройки видео — из link.json (video.transport, video_port), канал — через self.client."""
        pref, port = videorx.link_video_prefs(getattr(link_cfg, "source", None))
        if link_cfg is not None:
            port = link_cfg.video_port
        vrx = videorx.VideoRx(lambda t, b: self.client is not None and self.client.send_video(t, b),
                              lambda: self.client.peer_ip() if self.client is not None else None,
                              clock_offset=lambda: self.client.clock_offset() if self.client is not None else None,
                              log=lambda level, text: self.log(level, "video", text), transport_pref=pref,
                              rx_port=port)
        return vrx.start()

    def stop(self):
        if self._videorx is not None:
            self._videorx.stop()
        self._timer.stop()
        self._sticks.stop()                         # без прощального кадра: блок сам уйдёт в FAILSAFE (ST-06)
        self.video.shutdown()
        self._stop_local_sender()
        if self.client is not None:
            self.client.stop()

    def _stop_local_sender(self):
        if self._local_sender is not None:
            try:
                os.killpg(self._local_sender.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._local_sender.wait()
            self._local_sender = None

    def video_config(self):
        sysy = self._rev.data["system.yaml"]
        return {k: sysy.get(k, v) for k, v in VIDEO_DEFAULTS.items()}

    def _apply_video_source(self):
        """Видеоисточник из конфигурации (S3.3): synthetic — прежний режим (поток удалённого блока или локальный
        генератор), uvc — локальное устройство V4L2/UVC, disabled — видео выключено. Устройство UVC опрашивается и
        захватывается только при явном выборе uvc."""
        v = self.video_config()
        if v == getattr(self, "_video_applied", None):
            return
        self._video_applied = v
        self._video_source = v["video_source"]
        if self._video_source == "uvc":
            self._stop_local_sender()
            dev, mode, fmt, scan = v["video_device"], v["video_mode"], v["video_format"], self._video_scan
            self.video.configure_local((dev, mode, fmt), lambda: videoinput.open_uvc(dev, mode, fmt, **scan))
            self.log("info", "video", f"source uvc device={dev or 'auto'} mode={mode} format={fmt}")
        elif self._video_source == "disabled":
            self._stop_local_sender()
            self.video.configure(None, None)
            self.log("info", "video", "source disabled")
        else:
            self.video.configure(None, None)      # поток удалённого блока подключится в _link_status
            if self._local_video:
                self._start_local_video()

    def _start_local_video(self):
        self._stop_local_sender()
        codec = gst.choose_codec()
        if codec is None:
            self.log("error", "video", "no working synthetic video codec")
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        self.video.configure(codec, port)
        self._local_sender = gst.spawn(gst.sender_args(codec, port), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        self.log("info", "video", f"local synthetic generator {codec} on 127.0.0.1:{port}")

    # ---------- журнал приложения ----------
    def log(self, level, source, text):
        self._log_seq += 1
        self._logs.append({"id": self._log_seq, "time": time.strftime("%H:%M:%S"), "level": level,
                           "source": source, "text": text})
        self._logs_dirty = True

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
            "tempSoc": s.temperatures["soc"] if s.temperatures else -1,
            "tempBoard": s.temperatures["board"] if s.temperatures else -1,
            "fanDuty": round(s.fan_duty), "fanRpm": -1 if s.fan_rpm is None else s.fan_rpm,
            "peerState": s.peer_state, "peerProtocol": s.peer.protocol or "", "peerDevice": s.peer.device_id or "",
            "peerSoftware": s.peer.software or "", "peerHeartbeatAge": -1 if peer_age is None else peer_age,
            "localProtocol": PROTOCOL_VERSION, "software": __version__,
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
        self._mirror_to_log(s)
        events = self.bench.monitor.log.events
        sig = (events[-1].id if events else 0, tuple(e.id for e in s.active), self.lang,
               sum(1 for e in events if e.resolved_t is not None))
        if sig != getattr(self, "_events_sig", None):      # перестраивать список только при изменениях
            self._events_sig = sig
            self._active = [self._render_event(e) for e in s.active]
            self._events = [self._render_event(e) for e in reversed(events)]
            self._events_rev += 1
            self.eventsChanged.emit()
        if getattr(self, "_logs_dirty", False):     # журнал — в QML только при новых записях
            self._logs_dirty = False
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
            vrx = self._videorx
            if self._video_source != "synthetic":
                pass                                # локальный видеовход или видео выключено (S3.3)
            elif vrx is not None and vrx.active and s.peer_state == "connected":
                # Видеосеанс SRTP (ADR-0016): кадры отдаёт служба rx; откат на TLS — по решению пульта (videorx)
                self.video.configure_service(vrx.codec, vrx.frames_path, vrx.stats)
            elif s.peer_state == "connected" and vid.get("codec") and vid.get("transport") == "tls" \
                    and self.link_cfg is not None and self.link_cfg.video and snap.get("host"):
                # Защищённый видеоканал (S3.1-VIDEO): тот же блок, что у сеанса управления; порт — из своей
                # конфигурации, не из телеметрии. Незащищённого удалённого источника нет.
                host, port, cfg = snap["host"], self.link_cfg.video_port, self.link_cfg
                self.video.configure_remote(vid["codec"], (host, port), lambda: VideoClient(cfg, host, port))
            elif self.link_cfg is not None and (s.peer_state != "connected" or vid.get("transport") != "tls"):
                if self.video.state != "off":       # сеанс управления потерян — видеоканал закрывается
                    self.video.configure(None, None)
            elif vid.get("codec") and vid.get("port") and s.peer_state == "connected":
                self.video.configure(vid["codec"], vid["port"])
        else:
            cnt = {"connects": 0, "disconnects": 0, "bad_messages": 0, "ignored_messages": 0, "received": 0,
                   "seq_gaps": 0}
        self._remote_video_tel = vid if self.client is not None and s.peer_state == "connected" else None
        self._peer_link = peer_link(s.peer_state, snap) if self.client is not None else ""
        self._update_video_source()
        # Пропадание видео — диагностическое событие, видно на главной панели и в диагностике
        expected = (self.client is not None and bool(vid.get("enabled")) and s.peer_state == "connected") or \
            self._local_sender is not None
        expected = {"uvc": True, "disabled": False}.get(self._video_source, expected)
        if (self.video.state in ("no-video", "restarting", "no-device", "unsupported")
                or self._vsrc["display"] in vsrc.BLOCKING) and expected:   # на блоке нет сигнала/устройства
            self.bench.events.inject("VIDEO-LOST", state=self._vsrc["display"])
        elif self._vsrc["live"] or not expected:
            self.bench.events.clear("VIDEO-LOST")
        if self.video.state != self._last_video_state:
            self.log("info" if self.video.state in ("playing", "waiting", "off") else "warning", "video",
                     f"state {self.video.state} {self.video.error}".strip())
            self._last_video_state = self.video.state
        link = peer_link(s.peer_state, snap)
        return {
            "peerMode": ("network" if snap.get("secure") else "process") if self.client is not None else "emulated",
            "peerLink": link, "linkView": link_view(s.peer_state, link),
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
            "remoteVideo": bool(vid.get("active")), "videoSource": self._video_source,
            "batteryWarning": self._rev.data["diagnostics.yaml"]["battery_warning_pct"],
            "batteryCritical": self._rev.data["diagnostics.yaml"]["battery_critical_pct"],
            "undervoltage": bool(s.power and s.power.undervoltage),
        }

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
        self._journal_video_state()

    @Slot()
    def _journal_video_state(self):
        """Наличие видеопотока — по сигналу приёмника, а не по такту бэкенда: обрыв короче такта (0,125–0,5 с)
        при опросе по такту в журнал не попадал (CI ubuntu, E2E-12)."""
        cls = jr.video_class(self.video.state)
        if cls is None or cls == self._journal_video:
            return
        src = getattr(self, "_video_source", None)
        if cls == "playing":
            how = "restored" if self._journal_video == "no-stream" else "started"
            self.journal.emit(jr.INFO, f"video: stream {how} (codec={self.video.codec or '-'} source={src})")
        elif cls == "no-stream":
            self.journal.emit(jr.WARNING, f"video: no stream (state={self.video.state} "
                                          f"error={self.video.error or '-'} source={src})")
        elif self._journal_video is not None:
            self.journal.emit(jr.NOTICE, f"video: off (source={src})")
        self._journal_video = cls

    def _mirror_to_log(self, s):
        for e in self.bench.monitor.log.events:
            if e.id > self._last_event_id:
                self._last_event_id = e.id
                level = {"error": "error", "warning": "warning"}.get(e.severity, "info")
                params = " ".join(f"{k}={v}" for k, v in e.params.items())
                self.log(level, "monitor", f"{e.code} {params}".strip())

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
        return self._vsrc

    def _video_report(self):
        """Откуда сведения об источнике: режим и сырые данные для videosource.describe."""
        if self._video_source == "uvc":
            return {"mode": "local-input", "local_info": self.video.source}
        if self._video_source == "disabled":
            return {"mode": "off"}
        if self._local_sender is not None:
            return {"mode": "local-synthetic"}
        if self.client is not None:
            return {"mode": "remote", "telemetry_video": self._remote_video_tel,
                    "session_info": self.video.session_info(), "link": self._peer_link}
        return {"mode": "off"}

    @Slot()
    def _update_video_source(self):
        report = self._video_report()
        src = vsrc.describe(rx_state=self.video.state, rx_error=self.video.error, **report)
        self._update_video_sources(vsrc.sources(report.get("telemetry_video"))
                                   if report["mode"] == "remote" and src["state"] != vsrc.RECONNECTING else [])
        # кадры прежнего источника (буфер, NO_VIDEO_AFTER_S приёмника) не выдаются за кадры нового (GUI-01)
        fresh = self._handover.update(src, self.video.frames_since) if report["mode"] == "remote" else True
        src.update(vsrc.display(src, self.video.state, fresh))
        if self._vsel["state"] == "switching" and src["display"] in ("playing", vsrc.CONNECTING):
            # до подтверждения блоком (активный id в телеметрии) кадры — прежнего источника: не выдаются за новый
            src.update(display="switching", live=False)
        if src != self._vsrc:
            # переходы состояния источника (у приёмника — свой журнал «state …» в _link_status)
            source_level = vsrc.BLOCKING + ("starting", vsrc.CONNECTING)
            if src["display"] != self._vsrc["display"] and (src["display"] in source_level or
                                                             self._vsrc["display"] in source_level):
                self.log("warning" if src["display"] in vsrc.BLOCKING else "info", "video",
                         f"source {src['input'] or '-'} {src['display']} {src['error']}".strip())
            self._vsrc = src
            self.videoSourceChanged.emit()

    @Slot()
    def _on_video_frame(self):
        """Переход источника ждёт кадров нового (GUI-01): решение — с первым же подходящим кадром, а не с тактом
        бэкенда или очередным уведомлением приёмника (до 0,5 с). Вне перехода кадры экран не пересчитывают."""
        if self._handover.pending and not self._handover.waiting:
            self._update_video_source()

    # ---------- выбор источника видео на блоке (S4) ----------
    def _video_peer(self):
        """Клиент канала с блоком, у которого вызывается select_video_source (для испытаний подменяется)."""
        return self.client

    def _selector(self):
        return getattr(self._video_peer(), "select_video_source", None)

    def _select_blocker(self):
        """Почему выбор сейчас невозможен: "" | local | no-link | no-list | unsupported."""
        if self._video_report()["mode"] != "remote":
            return "local"
        if self._video_report().get("link") == "RECONNECTING" or (
                self.client is not None and self._status.get("peerState", "connected") != "connected"):
            return "no-link"
        if not self._vsources:
            return "no-list"
        if not callable(self._selector()):
            return "unsupported"
        return ""

    def _set_selection(self, **kw):
        new = {**self._vsel, **kw}
        if new != self._vsel:
            self._vsel = new
            self.videoSourcesChanged.emit()

    def _update_video_sources(self, sources):
        changed = sources != self._vsources
        self._vsources = sources
        blocker = self._select_blocker() if hasattr(self, "_status") else "local"
        changed = changed or blocker != getattr(self, "_sel_blocker", None)
        self._sel_blocker = blocker
        switching = self._vsel["state"] == "switching"
        if switching and any(x["id"] == self._vsel["id"] and x["active"] for x in sources):
            self.log("info", "video", f"source selected {self._vsel['id']}")
            self._sel_seq += 1                      # ответ/таймаут прежнего запроса больше не нужен
            self._handover.confirmed()
            self._set_selection(state="idle", error="", detail="")
        elif switching and blocker == "no-link":   # связь потеряна во время переключения: итог неизвестен
            self._sel_seq += 1
            self._handover.cancel()
            self._set_selection(state="error", error="no-link", detail="")
        elif changed:
            self.videoSourcesChanged.emit()

    @Property(list, notify=videoSourcesChanged)
    def videoSources(self):
        """Источники блока: [{id, input, name, available, active}] (пусто — блок списка не сообщает)."""
        return self._vsources

    @Property(dict, notify=videoSourcesChanged)
    def videoSelection(self):
        """Ход выбора: state idle|switching|error, id/name выбираемого, error — код (unknown, unavailable, unsupported,
        no-link, busy, timeout, failed), detail — текст блока; blocker — почему выбор недоступен ("" — доступен)."""
        return {**self._vsel, "blocker": self._select_blocker()}

    @Slot(str, bool, result=bool)
    def selectVideoSource(self, source_id, confirmed):
        """Выбор источника на блоке — только после подтверждения на экране (без случайных переключений). True —
        запрос отправлен (или источник уже активен); итог — в videoSelection."""
        if not confirmed or self._vsel["state"] == "switching":
            return False
        src = next((x for x in self._vsources if x["id"] == source_id), None)
        name = src["name"] if src else vsrc.scrub(source_id)[:64]
        blocker = self._select_blocker()
        err = {"no-link": "no-link", "unsupported": "unsupported", "local": "unsupported",
               "no-list": "unsupported"}.get(blocker, "")
        if not err and src is None:
            err = "unknown"
        elif not err and not src["available"]:
            err = "unavailable"
        if err:
            self.log("warning", "video", f"source select {source_id}: {err}")
            self._set_selection(state="error", id=source_id, name=name, error=err, detail="")
            return False
        if src["active"]:
            self._set_selection(state="idle", id=source_id, name=name, error="", detail="")
            return True
        self._sel_seq += 1
        seq, call = self._sel_seq, self._selector()
        self.log("info", "video", f"source select {source_id} ({src['input'] or '-'})")
        self._set_selection(state="switching", id=source_id, name=name, error="", detail="")
        self._handover.select()
        self._update_video_source()

        def run():                                  # вызов идёт по сети — не в потоке интерфейса
            try:
                result = call(source_id)
            except Exception as e:                  # noqa: BLE001 — любая ошибка клиента показывается текстом
                result = e
            self._selectDone.emit(seq, result)
        threading.Thread(target=run, name="video-select", daemon=True).start()
        QTimer.singleShot(int(SELECT_TIMEOUT_S * 1000), lambda: self._on_select_done(seq, {"ok": False, "error": "timeout"}))
        return True

    @Slot(int, object)
    def _on_select_done(self, seq, result):
        if seq != self._sel_seq or self._vsel["state"] != "switching":
            return                                  # устаревший ответ или уже подтверждено телеметрией
        code, detail = select_error(result)
        if code:
            self._sel_seq += 1
            self.log("warning", "video", f"source select {self._vsel['id']}: {code} {detail}".strip())
            self._handover.cancel()
            self._set_selection(state="error", error=code, detail=detail)
        self._update_video_source()                 # блок мог уже отметить источник активным; снять «Переключение»

    @Property(list, notify=statusChanged)
    def powerHistory(self):
        return list(self._power_history)

    @Property(list, notify=logsChanged)
    def logs(self):
        return list(reversed(self._logs))

    # QML получает списки событий и журнала только через вызов слота: результат вызова — готовый
    # JS-массив. Свойство-список в QML 6 — ссылка, каждое обращение к элементу перечитывает весь
    # список из Python, и фильтрация в QML становится O(n²) (5 с паузы GUI на 60-минутном прогоне).
    @Property(int, notify=logsChanged)
    def logsRevision(self):
        return self._log_seq

    @Property(int, notify=logsChanged)
    def logCount(self):
        return len(self._logs)

    @Slot(int, result=list)
    def logRows(self, level_filter):
        """Записи журнала от новых к старым; 0 — все, 1 — предупреждения и ошибки, 2 — ошибки."""
        ok = {0: lambda r: True, 1: lambda r: r["level"] != "info", 2: lambda r: r["level"] == "error"}
        keep = ok.get(level_filter, ok[0])
        return [r for r in reversed(self._logs) if keep(r)]

    @Property(dict, notify=configChanged)
    def about(self):
        def git_rev():
            try:
                return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                                      capture_output=True, text=True, timeout=2).stdout.strip() or "—"
            except (OSError, subprocess.SubprocessError):
                return "—"
        if not hasattr(self, "_build"):            # не меняется за время работы: вычислить один раз,
            self._build = git_rev()                # QML читает свойство на каждое поле страницы
            try:
                self._gst_version = subprocess.run(["gst-launch-1.0", "--version"], capture_output=True, text=True,
                                                   timeout=2).stdout.splitlines()[0].split()[-1]
            except (OSError, subprocess.SubprocessError, IndexError):
                self._gst_version = "—"
            self._video_codec = gst.describe(gst.choose_codec())       # с фактическими элементами (проба S2-4)
        gst_version = self._gst_version
        return {"software": __version__, "protocol": PROTOCOL_VERSION, "build": self._build,
                "schema": self.schema.version, "revision": self._rev.id,
                "device": self._rev.data["system.yaml"]["device_name"],
                "hardware": "HW-1.0 Rev A (эмулятор, x86)" if self.lang == "ru" else "HW-1.0 Rev A (emulator, x86)",
                "platform": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
                "qt": f"Qt {qVersion()}, PySide6 {PYSIDE_VERSION}", "gstreamer": gst_version,
                "videoCodec": self._video_codec, "catalog": len(CATALOG)}

    @Slot(result=str)
    def exportLogs(self):
        """Выгрузка журнала на «USB» с фильтром секретов и контрольной суммой (REQ-SW-001/005)."""
        self.usb_dir.mkdir(parents=True, exist_ok=True)
        name = time.strftime("sheregesh-log-%Y%m%d-%H%M%S.txt")
        lines = [f"{r['time']} {r['level'].upper():7} {r['source']:8} {REDACT.sub('[REDACTED]', r['text'])}"
                 for r in self._logs]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        tmp = self.usb_dir / f".tmp-{name}"
        tmp.write_bytes(data)
        os.replace(tmp, self.usb_dir / name)
        (self.usb_dir / f"{name}.sha256").write_text(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        self._last_result = self._result("logs", name=name, count=len(lines))
        self.log("info", "logs", f"exported {name}")
        self.configChanged.emit()
        return name

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
        """Проверка затронутых служб S1: монитор принимает пороги, интерфейс — язык и тему."""
        data = rev.data
        if "monitor" in services:
            self.bench.monitor.set_thresholds(data["diagnostics.yaml"], data["link.yaml"])
        if "link" in services:
            self.bench.monitor.set_thresholds(data["diagnostics.yaml"], data["link.yaml"])
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
        """Изменение состояния: только с явным подтверждением в диалоге (REQ-SYS-009)."""
        if not confirmed:
            self._last_result = "confirmation required"
            self.configChanged.emit()
            return False
        res = self.store.apply(self._files_from_form(form), self._health_check, reason="GUI settings")
        return self._after_change(res, "applied")

    @Slot(result=str)
    def backup(self):
        self.usb_dir.mkdir(parents=True, exist_ok=True)
        path = self.store.backup(self.usb_dir)
        self._last_result = self._result("backup", name=path.name)
        self.configChanged.emit()
        return path.name

    @Slot(result=list)
    def backups(self):
        if not self.usb_dir.exists():
            return []
        return sorted((p.name for p in self.usb_dir.glob("*.tar")), reverse=True)

    @Slot(str, bool, result=bool)
    def restore(self, name, confirmed):
        if not confirmed:
            return False
        try:
            res = self.store.restore(self.usb_dir / name, self._health_check, confirmed=True)
        except BackupError as e:
            self._last_result = self._result("backup-bad", reason=e)
            self.configChanged.emit()
            self.message.emit("error", self._last_result)
            return False
        return self._after_change(res, "restored")

    @Slot()
    def reload(self):
        """Перечитать конфигурацию с диска (новое хранилище, как при перезапуске)."""
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
