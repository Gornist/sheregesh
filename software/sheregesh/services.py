# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обязательные службы и их состояние (REQ-SW-001, docs/software/services.md).

Снимок — состояние служб роли у systemd по D-Bus (ListUnitsByNames и два свойства службы, ~7 мс CPU на Pi 4), при
недоступной шине — одна команда `systemctl show` (порядка 30 мс CPU на запуск процесса): ActiveState, SubState,
NRestarts, Result. Блок передаёт свой
снимок пульту необязательным полем `services` сообщения status (старый пульт поле отбрасывает); пульт сравнивает снимки
обеих сторон (SystemMonitor) и поднимает события каталога: SVC-RESTART — служба перезапущена, SVC-DOWN — служба не
работает. Без systemd (CI, рабочая станция) снимок пуст.
"""
import logging
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

MANDATORY = {"console": ("sheregesh-console.service", "sheregesh-video-rx.service"),
             "remote": ("sheregesh-remote-net.service", "sheregesh-video-tx.service")}
PROPS = ("Id", "LoadState", "ActiveState", "SubState", "NRestarts", "Result")
MAX_NAME = 64


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=2).stdout


def snapshot(names, run=_run):
    """{служба: {active, sub, restarts, result}}; пусто, если systemctl нет или команда не удалась."""
    if not names or (run is _run and shutil.which("systemctl") is None):
        return {}
    try:
        out = run(["systemctl", "show", "-p", ",".join(PROPS), *names])
    except (OSError, subprocess.SubprocessError):
        return {}
    res = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = kv.get("Id", "")
        if name in names and kv.get("LoadState") == "loaded":   # юнита нет (CI, другая роль) — не «не работает»
            res[name] = {"active": kv.get("ActiveState", ""), "sub": kv.get("SubState", ""),
                         "restarts": int(kv["NRestarts"]) if kv.get("NRestarts", "").isdigit() else 0,
                         "result": kv.get("Result", "")}
    return res


class SystemdBus:
    """Состояние служб у systemd по системной шине D-Bus (Gio): без запуска процесса systemctl на каждый снимок.
    Ошибка шины — исключение; вызывающий переходит на systemctl."""
    DEST = "org.freedesktop.systemd1"

    def __init__(self, timeout_ms=1000):
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        self._glib, self.timeout_ms = GLib, timeout_ms
        self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)

    def _call(self, path, iface, method, args, reply):
        G = self._glib
        return self._bus.call_sync(self.DEST, path, iface, method, G.Variant(*args), G.VariantType(reply), 0,
                                   self.timeout_ms, None).unpack()

    def snapshot(self, names):
        rows = self._call("/org/freedesktop/systemd1", self.DEST + ".Manager", "ListUnitsByNames",
                          ("(as)", (list(names),)), "(a(ssssssouso))")[0]
        res = {}
        for name, _desc, load, active, sub, _follow, path, *_ in rows:
            if name not in names or load != "loaded":
                continue
            get = lambda key: self._call(path, "org.freedesktop.DBus.Properties", "Get",       # noqa: E731
                                         ("(ss)", (self.DEST + ".Service", key)), "(v)")[0]
            r = get("NRestarts")
            res[name] = {"active": active, "sub": sub, "restarts": r if isinstance(r, int) else 0,
                         "result": str(get("Result"))}
        return res


def sanitize(value):
    """Снимок второй стороны (недоверенные данные): только ожидаемые поля и типы, не больше 8 служб."""
    if not isinstance(value, dict):
        return None
    out = {}
    for name, st in list(value.items())[:8]:
        if not (isinstance(name, str) and name.startswith("sheregesh-") and len(name) <= MAX_NAME) \
                or not isinstance(st, dict):
            continue
        r = st.get("restarts")
        out[name] = {k: str(st.get(k, ""))[:16] for k in ("active", "sub", "result")}
        out[name]["restarts"] = r if isinstance(r, int) and not isinstance(r, bool) and 0 <= r < 10 ** 6 else 0
    return out


def short(name):
    """sheregesh-video-rx.service → video-rx (для текста событий и строк диагностики)."""
    return name.removeprefix("sheregesh-").removesuffix(".service")


class Cached:
    """Снимок служб роли не чаще раза в period_s. Источник — D-Bus (bus: фабрика SystemdBus, None — только systemctl);
    первая ошибка шины переводит на systemctl до конца работы процесса (backend — «dbus» | «systemctl»)."""

    def __init__(self, role, period_s=2.0, run=_run, clock=time.monotonic, bus=SystemdBus):
        self.names, self.period_s, self.run, self.clock = MANDATORY.get(role, ()), period_s, run, clock
        self._at, self._value = None, {}
        self._bus_factory, self._bus = bus, None
        self.backend = "dbus" if bus is not None and self.names else "systemctl"

    def _take(self):
        if self.backend == "dbus":
            try:
                if self._bus is None:
                    self._bus = self._bus_factory()
                return self._bus.snapshot(self.names)
            except Exception as e:   # нет gi, шины или systemd (CI, контейнер) — дальше systemctl
                log.warning("службы: D-Bus недоступен (%s), дальше systemctl", type(e).__name__)
                self.backend, self._bus = "systemctl", None
        return snapshot(self.names, self.run)

    def __call__(self):
        now = self.clock()
        if self._at is None or now - self._at >= self.period_s:
            self._at, self._value = now, self._take()
        return self._value


class Watch:
    """Сравнение снимков одной стороны: перезапуски с прошлого снимка и службы не в состоянии active.
    Первый снимок задаёт отсчёт; перезапуск до него сообщается, если служба была перезапущена (restarts > 0) —
    так пульт, перезапущенный после сбоя, сообщает о сбое своей же службы GUI."""

    def __init__(self):
        self._restarts = {}

    def update(self, snap):
        restarted, down = [], []
        for name, st in (snap or {}).items():
            prev = self._restarts.get(name)
            r = st.get("restarts", 0)
            if (prev is None and r > 0) or (prev is not None and r > prev):
                restarted.append((name, r))
            self._restarts[name] = r
            if st.get("active") not in ("active", "reloading"):
                down.append((name, st.get("active") or "?"))
        return restarted, down
