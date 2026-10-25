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
from .video import VideoReceiver

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
    "backup": {"ru": "Резервная копия записана: {name}", "en": "Backup written: {name}"},
    "backup-bad": {"ru": "Резервная копия отклонена: {reason}", "en": "Backup rejected: {reason}"},
    "logs": {"ru": "Журнал выгружен: {name} ({count} записей)", "en": "Log exported: {name} ({count} records)"},
}


def _yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


RECONNECT_WINDOW_S = 30.0
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
    message = Signal(str, str)          # уровень (info/warning/error), текст

    def __init__(self, data_dir, demo=False, speed=1.0, autostart=True, peer=None, local_video=False,
                 video_command=None, link_cfg=None, link_status_file=None, video_scan=None, journal=None):
        """peer=None — удалённый блок эмулируется в процессе пульта; peer=(host, port) — внешний процесс-эмулятор.
        local_video — локальный синтетический генератор видео (без удалённого блока).
        video_scan — для испытаний: корни sysfs/dev и ioctl обнаружения устройств UVC (sheregesh.videoinput.scan)."""
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
        if link_cfg is not None:                    # сетевой лабораторный профиль: mTLS к удалённому блоку (S3.1)
            peer = (link_cfg.peer_hosts[0] if link_cfg.peer_hosts else "discover", link_cfg.port)
            self.client = PeerClient(None, link_cfg.port, heartbeat_timeout_s=data["link.yaml"]["heartbeat_timeout_s"],
                                     tls_cfg=link_cfg, status_file=link_status_file).start()
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"],
                               peer_factory=lambda clock: RemotePeer(self.client, clock))
        elif peer is not None:
            host, port = peer
            self.client = PeerClient(host, port, heartbeat_timeout_s=data["link.yaml"]["heartbeat_timeout_s"],
                                     status_file=link_status_file).start()
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"],
                               peer_factory=lambda clock: RemotePeer(self.client, clock))
        else:
            self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"])
        self.log("info", "app", f"start: software {__version__}, protocol {PROTOCOL_VERSION}, "
                                f"peer {'%s:%d' % peer if peer else 'in-process emulator'}")
        for code, _ in report.actions:
            self.bench.events.inject("CFG-RECOVERED", detail=code)
            self.log("warning", "config", f"recovery action {code}")
        self.video = VideoReceiver(self, command=video_command)
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

    def stop(self):
        self._timer.stop()
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
            if self._video_source != "synthetic":
                pass                                # локальный видеовход или видео выключено (S3.3)
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
        # Пропадание видео — диагностическое событие, видно на главной панели и в диагностике
        expected = (self.client is not None and bool(vid.get("enabled")) and s.peer_state == "connected") or \
            self._local_sender is not None
        expected = {"uvc": True, "disabled": False}.get(self._video_source, expected)
        if self.video.state in ("no-video", "restarting", "no-device", "unsupported") and expected:
            self.bench.events.inject("VIDEO-LOST", state=self.video.state)
        elif self.video.state == "playing" or not expected:
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
        cls = jr.video_class(self.video.state)
        if cls is not None and cls != self._journal_video:
            if cls == "playing":
                self.journal.emit(jr.INFO, f"video: stream {'restored' if self._journal_video == 'no-stream' else 'started'} "
                                           f"(codec={self.video.codec or '-'} source={self._video_source})")
            elif cls == "no-stream":
                self.journal.emit(jr.WARNING, f"video: no stream (state={self.video.state} "
                                              f"error={self.video.error or '-'} source={self._video_source})")
            elif self._journal_video is not None:
                self.journal.emit(jr.NOTICE, f"video: off (source={self._video_source})")
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
