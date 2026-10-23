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
from sheregesh.config import BackupError, ConfigStore, Schema, console_defaults
from sheregesh.faults import CATALOG, render
from sheregesh.link.client import PeerClient, RemotePeer
from sheregesh_emulators import Bench
from sheregesh_emulators.demo import DEMO
from sheregesh_emulators.scenario import TICK_S

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


class Backend(QObject):
    statusChanged = Signal()
    eventsChanged = Signal()
    configChanged = Signal()
    appearanceChanged = Signal()
    message = Signal(str, str)          # уровень (info/warning/error), текст

    def __init__(self, data_dir, demo=False, speed=1.0, autostart=True, peer=None, local_video=False,
                 video_command=None):
        """peer=None — удалённый блок эмулируется в процессе пульта; peer=(host, port) — внешний процесс-эмулятор.
        local_video — локальный синтетический генератор видео (без удалённого блока)."""
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
        self._last_video_state = None
        self._power_history = collections.deque(maxlen=POWER_HISTORY)
        self.client = None
        if peer is not None:
            host, port = peer
            self.client = PeerClient(host, port, heartbeat_timeout_s=data["link.yaml"]["heartbeat_timeout_s"]).start()
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
        if local_video:
            self._start_local_video()
        self.demo = demo
        self.runner = DEMO.runner(self.bench) if demo else None
        self._scenario_label = ""
        self._status, self._events, self._active = {}, [], []
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
        if self._local_sender is not None:
            try:
                os.killpg(self._local_sender.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._local_sender.wait()
            self._local_sender = None
        if self.client is not None:
            self.client.stop()

    def _start_local_video(self):
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
            self.eventsChanged.emit()
        self.statusChanged.emit()

    def _link_status(self, s):
        tel = s.peer.telemetry or {}
        temps = tel.get("temperatures") or {}
        rp = tel.get("power") or {}
        vid = tel.get("video") or {}
        if self.client is not None:
            snap = self.client.snapshot()
            cnt = snap["counters"]
            if snap["phase"] != self._last_phase:
                self.log("warning" if snap["phase"] != "connected" else "info", "link",
                         f"phase {snap['phase']} ({snap['detail']})")
                self._last_phase = snap["phase"]
            if vid.get("codec") and vid.get("port") and s.peer_state == "connected":
                self.video.configure(vid["codec"], vid["port"])
        else:
            cnt = {"connects": 0, "disconnects": 0, "bad_messages": 0, "ignored_messages": 0, "received": 0,
                   "seq_gaps": 0}
        if self.video.state != self._last_video_state:
            self.log("info" if self.video.state in ("playing", "waiting", "off") else "warning", "video",
                     f"state {self.video.state} {self.video.error}".strip())
            self._last_video_state = self.video.state
        return {
            "peerMode": "process" if self.client is not None else "emulated",
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
            "remoteVideo": bool(vid.get("active")),
            "batteryWarning": self._rev.data["diagnostics.yaml"]["battery_warning_pct"],
            "batteryCritical": self._rev.data["diagnostics.yaml"]["battery_critical_pct"],
            "undervoltage": bool(s.power and s.power.undervoltage),
        }

    def _mirror_to_log(self, s):
        for e in self.bench.monitor.log.events:
            if e.id > self._last_event_id:
                self._last_event_id = e.id
                level = {"error": "error", "warning": "warning"}.get(e.severity, "info")
                self.log(level, "monitor", f"{e.code} {e.params}" if e.params else e.code)

    @Property(dict, notify=statusChanged)
    def status(self):
        return self._status

    @Property(QObject, constant=True)
    def videoReceiver(self):
        return self.video

    @Property(list, notify=statusChanged)
    def powerHistory(self):
        return list(self._power_history)

    @Property(list, notify=statusChanged)
    def logs(self):
        return list(reversed(self._logs))

    @Property(dict, notify=configChanged)
    def about(self):
        def git_rev():
            try:
                return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                                      capture_output=True, text=True, timeout=2).stdout.strip() or "—"
            except (OSError, subprocess.SubprocessError):
                return "—"
        if not hasattr(self, "_build"):
            self._build = git_rev()
        try:
            gst_version = subprocess.run(["gst-launch-1.0", "--version"], capture_output=True, text=True,
                                         timeout=2).stdout.splitlines()[0].split()[-1]
        except (OSError, subprocess.SubprocessError, IndexError):
            gst_version = "—"
        return {"software": __version__, "protocol": PROTOCOL_VERSION, "build": self._build,
                "schema": self.schema.version, "revision": self._rev.id,
                "device": self._rev.data["system.yaml"]["device_name"],
                "hardware": "HW-1.0 Rev A (эмулятор, x86)" if self.lang == "ru" else "HW-1.0 Rev A (emulator, x86)",
                "platform": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
                "qt": f"Qt {qVersion()}, PySide6 {PYSIDE_VERSION}", "gstreamer": gst_version,
                "videoCodec": gst.choose_codec() or "—", "catalog": len(CATALOG)}

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
        return {**d["system.yaml"], **d["diagnostics.yaml"], **d["link.yaml"]}

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
        res = self.store.apply(self._files_from_form(form), self._health_check, reason="GUI: настройки")
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
        self.log("info", "config", f"reloaded from disk: revision {self._rev.id}")
        self.configChanged.emit()
        self.appearanceChanged.emit()
        self._refresh()

    @Slot(result=str)
    def clockText(self):
        return time.strftime("%H:%M:%S", time.gmtime(self.bench.clock()))
