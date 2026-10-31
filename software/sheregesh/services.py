# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обязательные службы и их состояние (REQ-SW-001, docs/software/services.md).

Снимок — одна команда `systemctl show` на все службы роли: ActiveState, SubState, NRestarts, Result. Блок передаёт свой
снимок пульту необязательным полем `services` сообщения status (старый пульт поле отбрасывает); пульт сравнивает снимки
обеих сторон (SystemMonitor) и поднимает события каталога: SVC-RESTART — служба перезапущена, SVC-DOWN — служба не
работает. Без systemd (CI, рабочая станция) снимок пуст.
"""
import shutil
import subprocess
import time

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
    """Снимок служб роли не чаще раза в period_s (команда — порядка десятков миллисекунд)."""

    def __init__(self, role, period_s=2.0, run=_run, clock=time.monotonic):
        self.names, self.period_s, self.run, self.clock = MANDATORY.get(role, ()), period_s, run, clock
        self._at, self._value = None, {}

    def __call__(self):
        now = self.clock()
        if self._at is None or now - self._at >= self.period_s:
            self._at, self._value = now, snapshot(self.names, self.run)
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
