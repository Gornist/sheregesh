# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Связь QML ↔ монитор и конфигурация (ADR-0003). GUI не пишет файлы и не обращается к оборудованию:
все данные — от эмуляторов, все изменения конфигурации — через ConfigStore с подтверждением."""
import time
from pathlib import Path

import yaml
from PySide6.QtCore import Property, QObject, QTimer, Signal, Slot

from sheregesh import PROTOCOL_VERSION, __version__
from sheregesh.config import BackupError, ConfigStore, Schema, console_defaults
from sheregesh.faults import render
from sheregesh_emulators import Bench
from sheregesh_emulators.demo import DEMO
from sheregesh_emulators.scenario import TICK_S

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
}


def _yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


class Backend(QObject):
    statusChanged = Signal()
    eventsChanged = Signal()
    configChanged = Signal()
    appearanceChanged = Signal()
    message = Signal(str, str)          # уровень (info/warning/error), текст

    def __init__(self, data_dir, demo=False, speed=1.0, autostart=True):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.usb_dir = self.data_dir / "usb"
        self.schema = Schema.load()
        self.store = ConfigStore(self.data_dir / "config", self.schema)
        if not self.store.is_initialized():
            self.store.initialize(console_defaults())
        report = self.store.recover()
        data = self.store.active().data
        self.bench = Bench(data["diagnostics.yaml"], data["link.yaml"])
        for code, _ in report.actions:
            self.bench.events.inject("CFG-RECOVERED", detail=code)
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

    # ---------- представление ----------
    @property
    def lang(self):
        return self.store.active().data["system.yaml"]["language"]

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
        self._active = [self._render_event(e) for e in s.active]
        self._events = [self._render_event(e) for e in reversed(self.bench.monitor.log.events)]
        self.statusChanged.emit()
        self.eventsChanged.emit()

    @Property(dict, notify=statusChanged)
    def status(self):
        return self._status

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
        return self.store.active().data["system.yaml"]["theme"]

    @Property(int, notify=appearanceChanged)
    def uiScale(self):
        return self.store.active().data["system.yaml"]["ui_scale"]

    @Property(dict, notify=appearanceChanged)
    def palette(self):
        return PALETTES[self.theme]

    # ---------- настройки ----------
    @Property(dict, notify=configChanged)
    def config(self):
        """Плоское представление активной ревизии для формы настроек."""
        d = self.store.active().data
        return {**d["system.yaml"], **d["diagnostics.yaml"], **d["link.yaml"]}

    @Property(str, notify=configChanged)
    def revision(self):
        return self.store.active().id

    @Property(list, notify=configChanged)
    def history(self):
        return self.store.history()[:6]

    @Property(str, notify=configChanged)
    def lastResult(self):
        return self._last_result

    def _files_from_form(self, form: dict):
        fields = {name: list(spec["schema"]["properties"]) for name, spec in self.schema.files.items()}
        current = self.store.active().data
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
        data = self.store.active().data
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
        self.configChanged.emit()
        self.appearanceChanged.emit()
        self._refresh()

    @Slot(result=str)
    def clockText(self):
        return time.strftime("%H:%M:%S", time.gmtime(self.bench.clock()))
