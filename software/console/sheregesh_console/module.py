# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Внешний модуль удалённого блока на пульте (ADR-0012, раздел 10): только представление.

ModuleView показывает status.module из телеметрии блока (уже проверенное клиентом канала, wire.sanitize_module) и
передаёт команды module-cmd через клиент канала (PeerClient.module_command). О менеджере, профиле, бэкенде и
эмуляторе модуля пульт ничего не знает, кроме полей телеметрии. Время блока приходит только относительными секундами
(since_s, ago_s): абсолютное время для экрана считается здесь по часам пульта в момент приёма.

Тексты для экрана — в ModulePage.qml (qsTr, перевод в i18n/*.ts); snapshot()["display"] берёт те же исходные строки
через переводчик приложения (контекст ModulePage), поэтому локальный канал и ui-state показывают то же, что экран.
"""
import threading
import time

from PySide6.QtCore import Property, QCoreApplication, QObject, QTimer, Signal, Slot

try:                                            # формат и перечни — общие с блоком (агент B, sheregesh.module.wire)
    from sheregesh.module import wire as _wire
except ImportError:                             # до слияния с агентом B: перечни ADR-0012, раздел 6
    _wire = None

STATES = tuple(getattr(_wire, "STATES", ("DISCONNECTED", "DETECTED", "INITIALIZING", "READY", "FAULT", "RECOVERING",
                                         "DISABLED")))
LAB_SCENARIOS = tuple(getattr(_wire, "LAB_SCENARIOS", ("attach", "detach", "iface-error", "hang", "heal", "slow-init",
                                                       "crash", "emu-unavailable", "emu-available")))
MIN_SESSION = (1, 3)                            # status.module и module-cmd — с протокола 1.3
CONFIGURE_KEYS = {"enabled": bool, "auto_init": bool, "auto_recover": bool, "profile": str}
WAIT_S = 5.0                                    # итог reinit/reset/lab от блока
CONFIGURE_WAIT_S = 30.0                         # configure: сохранение ревизии на microSD блока
GUARD_S = 5.0                                   # вызов клиента не вернулся за wait + GUARD_S — «timeout»
NEW_TIME_S = 2.0                                # расхождение пересчёта времени ошибки больше этого — новая ошибка
CONTEXT = "ModulePage"                          # контекст перевода (имя файла QML)

# Исходные строки (английские) — те же, что qsTr в ModulePage.qml; перевод — i18n/sheregesh_ru.ts
STATE_SOURCE = {"DISCONNECTED": "Module absent", "DETECTED": "Module detected", "INITIALIZING": "Initializing",
                "READY": "Ready", "FAULT": "Fault", "RECOVERING": "Recovering", "DISABLED": "Disabled"}
SUPPORT_SOURCE = {"no-link": "No link to the remote unit", "old-remote": "Not supported by the remote unit",
                  "absent": "No module data", "local": "No remote unit"}
ERROR_SOURCE = {"MOD-IFACE-ERROR": "Interface error", "MOD-NO-RESPONSE": "Module does not respond",
                "MOD-INIT-TIMEOUT": "Initialization did not finish in time", "MOD-INIT-FAILED": "Initialization error",
                "MOD-PROFILE-MISMATCH": "Module does not match the profile",
                "MOD-RECOVER-TIMEOUT": "Recovery did not finish", "EMU-START-FAILED": "Emulator did not start",
                "EMU-EXITED": "Emulator exited abnormally", "EMU-NO-KEEPALIVE": "Emulator does not respond",
                "EMU-UNAVAILABLE": "Emulator unavailable"}


def tr(source):
    return QCoreApplication.translate(CONTEXT, source)


def parse_session(session):
    """"1.3" → (1, 3); иначе None."""
    try:
        major, minor = str(session).split(".", 1)
        return int(major), int(minor)
    except (TypeError, ValueError):
        return None


def support_of(module, session, link, mode, received=True):
    """Что можно показать: ok | no-link | old-remote | absent | local (ADR-0012, 10.2).
    received — телеметрия блока в этом сеансе уже пришла (до первого status — no-link, а не absent)."""
    if mode == "emulated":
        return "local"
    if link != "CONNECTED" or not received:
        return "no-link"
    ver = parse_session(session)
    if ver is None:
        return "no-link"
    if ver < MIN_SESSION:
        return "old-remote"
    return "ok" if isinstance(module, dict) and module.get("state") in STATES else "absent"


def clock_text(wall):
    return time.strftime("%H:%M:%S", time.localtime(wall)) if wall else ""


def scrub(text, limit=200):
    """Строка блока для экрана: без управляющих символов, не длиннее limit."""
    return "".join(c if c.isprintable() else " " for c in str(text or ""))[:limit]


def _obj(v):
    return v if isinstance(v, dict) else {}


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _str(v):
    return v if isinstance(v, str) else ""


class ModuleView(QObject):
    """Логика страницы «Модуль»: свойства для QML, слоты команд с подтверждением, snapshot() для локального канала и
    ui-state. Все свойства уведомляются одним сигналом changed (испускается только при изменении видимого)."""

    changed = Signal()
    _done = Signal(int, object)                 # из потока вызова module_command → в поток интерфейса

    def __init__(self, parent=None, command=None, log=None, wall=time.time):
        """command — функция без аргументов, возвращающая PeerClient.module_command (или None: клиента нет / клиент
        без поддержки); log(level, text) — журнал приложения; wall — часы пульта (секунды эпохи)."""
        super().__init__(parent)
        self._command_fn = command or (lambda: None)
        self._log = log or (lambda level, text: None)
        self._wall = wall
        self._tel = {}                          # последнее status.module (ok или устаревшее при no-link)
        self._session = ""
        self._support = "no-link"
        self._stale_at = 0.0                    # время приёма последних данных (для «данные на …»)
        self._changed_wall = 0.0                # время последнего перехода по часам пульта
        self._seq = None
        self._err_wall = 0.0
        self._err_key = None
        self._hist_wall = {}                    # seq перехода → время по часам пульта
        self._cmd = {"state": "idle", "op": "", "result": "", "detail": "", "id": 0}
        self._cmd_seq = 0
        self._view = {}
        self._done.connect(self._on_done)
        self._rebuild()

    # ---------- телеметрия ----------
    def update(self, telemetry=None, session=None, link="", mode="", received=True):
        """Новое состояние канала и status.module (из Backend._refresh, поток интерфейса)."""
        now = self._wall()
        support = support_of(telemetry, session, link, mode, received)
        self._session = str(session or "")
        if support == "ok":
            tel = telemetry
            seq = int(_num(tel.get("seq")))
            since = float(_num(tel.get("since_s")))
            if self._seq is not None and seq < self._seq:   # блок перезапущен: seq с начала, прежние времена — не его
                self._hist_wall, self._err_key = {}, None
            if seq != self._seq or not self._changed_wall:
                self._changed_wall = now - since
                self._seq = seq
            err = _obj(tel.get("error"))
            if err:
                at = now - float(_num(err.get("since_s")))
                key = (err.get("code"), err.get("state"))
                if key != self._err_key or abs(at - self._err_wall) > NEW_TIME_S:
                    self._err_key, self._err_wall = key, at
            else:
                self._err_key, self._err_wall = None, 0.0
            hist = {}
            for h in tel.get("history") or []:
                if isinstance(h, dict) and isinstance(h.get("seq"), int):
                    hist[h["seq"]] = self._hist_wall.get(h["seq"]) or now - float(_num(h.get("ago_s")))
            self._hist_wall = hist
            self._tel = tel
            self._stale_at = now
        elif support == "no-link" and self._support in ("ok", "no-link") and self._tel:
            pass                                # последние данные остаются, приглушённо (stale)
        else:
            self._tel, self._seq, self._changed_wall = {}, None, 0.0
            self._err_key, self._err_wall, self._hist_wall = None, 0.0, {}
        if support != self._support:            # смены support пишет в журналы Backend (вместе со сменой seq)
            if self._cmd["state"] == "sending" and support != "ok":
                # связь потеряна во время команды: итог неизвестен (видно по state/settings после возврата)
                self._cmd_seq += 1
                self._cmd = {**self._cmd, "state": "error", "result": "no-link" if support == "no-link"
                             else "unsupported", "detail": ""}
            self._support = support
        self._rebuild(now)

    def _rebuild(self, now=None):
        now = self._wall() if now is None else now
        t = self._tel
        prof, back, err = _obj(t.get("profile")), _obj(t.get("backend")), _obj(t.get("error"))
        lab = bool(back.get("lab"))
        history = []
        for h in t.get("history") or []:
            if isinstance(h, dict):
                history.append({"seq": int(_num(h.get("seq"))), "from": _str(h.get("from")),
                                "state": _str(h.get("state")), "reason": _str(h.get("reason")),
                                "code": _str(h.get("code")), "at": clock_text(self._hist_wall.get(h.get("seq")))})
        view = {
            "support": self._support,
            "stale": self._support != "ok" and bool(t),
            "staleAt": clock_text(self._stale_at) if self._support != "ok" and t else "",
            "state": _str(t.get("state")) if self._support == "ok" else "",
            "lastState": _str(t.get("state")),
            "reason": _str(t.get("reason")),
            "seq": int(_num(t.get("seq"), -1)) if t else -1,
            "profileId": _str(prof.get("id")), "profileName": _str(prof.get("name")),
            "profileFamily": _str(prof.get("family")), "profileInterface": _str(prof.get("interface")),
            "profiles": [dict(p) for p in t.get("profiles") or [] if isinstance(p, dict)],
            "backendKind": _str(back.get("kind")), "isLab": lab,
            "emulatorState": _str(back.get("state")),
            "emulatorPid": int(_num(back.get("pid"))), "emulatorRestarts": int(_num(back.get("restarts"))),
            "emulatorRestartLimit": int(_num(back.get("restart_limit"))),
            "emulatorLastExit": back.get("last_exit") if isinstance(back.get("last_exit"), int) else None,
            "lastErrorCode": _str(err.get("code")), "lastErrorText": scrub(err.get("text")),
            "lastErrorAt": clock_text(self._err_wall) if err else "", "errorActive": bool(err.get("active")),
            "lastErrorState": _str(err.get("state")),
            "changedAt": clock_text(self._changed_wall) if t else "",
            "changedAgoS": max(0, int(now - self._changed_wall)) if t and self._changed_wall else 0,
            "sinceS": float(_num(t.get("since_s"))),
            "identity": dict(_obj(t.get("identity"))), "settings": dict(_obj(t.get("settings"))),
            "diagnostics": dict(_obj(t.get("diag"))), "recovery": dict(_obj(t.get("recovery"))),
            "history": history,
            "output": _str(t.get("output")) if t else "",
            "protocolNote": self._session,
            "command": dict(self._cmd),
            "labScenarios": list(LAB_SCENARIOS) if lab else [],
        }
        if view != self._view:
            self._view = view
            self.changed.emit()

    # ---------- свойства ----------
    def _p(name, kind, notify=changed):         # noqa: N805 — фабрика свойств класса (notify — из тела класса)
        return Property(kind, lambda self: self._view[name], notify=notify)

    support = _p("support", str)
    stale = _p("stale", bool)
    staleAt = _p("staleAt", str)
    state = _p("state", str)
    lastState = _p("lastState", str)
    reason = _p("reason", str)
    seq = _p("seq", int)
    profileId = _p("profileId", str)
    profileName = _p("profileName", str)
    profileFamily = _p("profileFamily", str)
    profileInterface = _p("profileInterface", str)
    profiles = _p("profiles", list)
    backendKind = _p("backendKind", str)
    isLab = _p("isLab", bool)
    emulatorState = _p("emulatorState", str)
    emulatorPid = _p("emulatorPid", int)
    emulatorRestarts = _p("emulatorRestarts", int)
    emulatorRestartLimit = _p("emulatorRestartLimit", int)
    lastErrorCode = _p("lastErrorCode", str)
    lastErrorText = _p("lastErrorText", str)
    lastErrorAt = _p("lastErrorAt", str)
    lastErrorState = _p("lastErrorState", str)
    errorActive = _p("errorActive", bool)
    changedAt = _p("changedAt", str)
    changedAgoS = _p("changedAgoS", int)
    sinceS = _p("sinceS", float)
    identity = _p("identity", dict)
    settings = _p("settings", dict)
    diagnostics = _p("diagnostics", dict)
    recovery = _p("recovery", dict)
    history = _p("history", list)
    output = _p("output", str)
    protocolNote = _p("protocolNote", str)
    command = _p("command", dict)
    labScenarios = _p("labScenarios", list)
    del _p

    # ---------- команды ----------
    def _set_cmd(self, **kw):
        self._cmd = {**self._cmd, **kw}
        self._rebuild()

    def _reject(self, op, code):
        """Команда не отправлена: итог сразу в command (state error)."""
        self._cmd_seq += 1
        self._log("warning", f"command {op}: {code}")
        self._set_cmd(state="error", op=op, result=code, detail="", id=self._cmd_seq)
        return False

    def _start(self, op, args, wait_s, confirmed):
        """Одна команда за раз, только с подтверждением; вызов клиента — в фоновом потоке, итог — сигналом."""
        if not confirmed or self._cmd["state"] == "sending":
            return False
        if self._support != "ok":
            return self._reject(op, "no-link" if self._support == "no-link" else "unsupported")
        call = self._command_fn()
        if not callable(call):                  # клиент канала без module_command (до слияния с агентом B)
            return self._reject(op, "unsupported")
        self._cmd_seq += 1
        seq = self._cmd_seq
        self._log("info", f"command {op} {args}" if args else f"command {op}")
        self._set_cmd(state="sending", op=op, result="", detail="", id=seq)

        def run():                              # вызов идёт по сети — не в потоке интерфейса
            try:
                result = call(op, dict(args), wait_s)
            except Exception as e:              # noqa: BLE001 — любая ошибка клиента показывается текстом
                result = {"ok": False, "result": "error", "error": f"{type(e).__name__}: {e}"}
            self._done.emit(seq, result)
        threading.Thread(target=run, name="module-cmd", daemon=True).start()
        QTimer.singleShot(int((wait_s + GUARD_S) * 1000),
                          lambda: self._on_done(seq, {"ok": False, "result": "timeout", "error": ""}))
        return True

    @Slot(int, object)
    def _on_done(self, seq, result):
        if seq != self._cmd_seq or self._cmd["state"] != "sending":
            return                              # устаревший ответ (потеря связи, таймаут-страховка)
        if result is None or result is True:
            ok, code, detail = True, "ok", ""
        elif isinstance(result, dict):
            ok = result.get("ok") is True
            code = _str(result.get("result")) or ("ok" if ok else "failed")
            detail = scrub(result.get("error") or result.get("detail"))
        else:
            ok, code, detail = False, "failed", scrub(result)
        self._log("info" if ok else "warning", f"command {self._cmd['op']}: {code} {detail}".strip())
        self._set_cmd(state="done" if ok else "error", result=code, detail=detail)

    @Slot(bool, result=bool)
    def requestReinit(self, confirmed):
        return self._start("reinit", {}, WAIT_S, confirmed)

    @Slot(bool, result=bool)
    def requestReset(self, confirmed):
        return self._start("reset", {}, WAIT_S, confirmed)

    @Slot(bool, bool, result=bool)
    def setEnabled(self, enabled, confirmed):
        return self.configure({"enabled": bool(enabled)}, confirmed)

    @Slot(bool, bool, result=bool)
    def setAutoInit(self, value, confirmed):
        return self.configure({"auto_init": bool(value)}, confirmed)

    @Slot(dict, bool, result=bool)
    def configure(self, args, confirmed):
        """configure: подмножество enabled, auto_init, auto_recover, profile (типы — как в схеме remote-1)."""
        args = dict(args or {})
        if not confirmed or self._cmd["state"] == "sending":
            return False
        if not args or any(k not in CONFIGURE_KEYS or type(v) is not CONFIGURE_KEYS[k] for k, v in args.items()):
            return self._reject("configure", "bad-request")
        return self._start("configure", args, CONFIGURE_WAIT_S, confirmed)

    @Slot(str, bool, result=bool)
    def runLabScenario(self, name, confirmed):
        """Лабораторный сценарий эмулятора — только при лабораторном бэкенде и имени из перечня."""
        if not confirmed or self._cmd["state"] == "sending":
            return False
        if self._support == "ok" and not self._view["isLab"]:
            return self._reject("lab", "forbidden")
        if self._support == "ok" and name not in self._view["labScenarios"]:
            return self._reject("lab", "bad-request")
        return self._start("lab", {"scenario": name}, WAIT_S, confirmed)

    # ---------- для локального канала и ui-state ----------
    def state_text(self):
        """Текст плашки состояния (как moduleStateText на экране)."""
        v = self._view
        if v["support"] != "ok":
            return tr(SUPPORT_SOURCE[v["support"]])
        return tr(STATE_SOURCE.get(v["state"], v["state"]))

    def error_text(self):
        """Описание последней ошибки ("" — ошибок не было), как в moduleErrorText без подписи и времени."""
        code = self._view["lastErrorCode"]
        return tr(ERROR_SOURCE[code]) if code in ERROR_SOURCE else code

    def snapshot(self):
        """Формат M (ADR-0012, 9.3): support, stale, session, telemetry, display (+ command)."""
        v = self._view
        return {"support": v["support"], "stale": v["stale"], "session": self._session or None,
                "telemetry": dict(self._tel) if self._tel else None,
                "display": {"stateText": self.state_text(), "errorText": self.error_text(),
                            "changedAt": v["changedAt"]},
                "command": dict(v["command"])}
