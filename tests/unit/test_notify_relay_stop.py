# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Дефект app-stop (S3.1): код выхода ретранслятора при останове и отказах.

Запускается настоящий sheregesh.notify_relay с фиктивным «cage» (sh), у которого в той же группе процессов есть
«приложение» (sleep). systemd считает останов успешным только при коде 0 (или смерти от SIGTERM), поэтому:
штатный SIGTERM/SIGINT — 0 при любом поведении ребёнка; SIGABRT (watchdog) и падение ребёнка — ненулевой код.
После выхода ретранслятора в группе ребёнка не должно оставаться процессов. systemd не нужен.
"""
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from sheregesh import notify_relay

ROOT = Path(__file__).resolve().parents[2]
ENV = {k: v for k, v in os.environ.items() if k not in ("NOTIFY_SOCKET", "WATCHDOG_PID", "WATCHDOG_USEC")}
ENV["PYTHONPATH"] = str(ROOT / "software")

# Фиктивный cage: $$ — лидер группы (ребёнок ретранслятора), фоновый sleep — «приложение» в той же группе.
#   term          оба завершаются по SIGTERM (обычный cage)
#   ignore        оба игнорируют SIGTERM (зависший композитор) — нужен SIGKILL по GRACE_S
#   exit1         cage по SIGTERM выходит с кодом 1, приложение умирает
#   leader-exits  cage выходит по SIGTERM с кодом 0, приложение SIGTERM игнорирует и остаётся в группе
#   crash         cage падает сам (код 70), приложение остаётся в группе
FAKE_CAGE = r"""
case $1 in
  ignore) trap '' TERM ;;
  exit1) trap 'exit 1' TERM ;;
  leader-exits) trap 'exit 0' TERM ;;
esac
if [ "$1" = leader-exits ]; then (trap '' TERM; exec sleep 300) >/dev/null 2>&1 &
else sleep 300 >/dev/null 2>&1 & fi
echo "pids $$ $!"
[ "$1" = crash ] && exit 70
while :; do wait; done
"""


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_group_gone(pgid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not group_alive(pgid)


def proc_state(pid):
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return r.stdout.strip() or None


class Relay:
    def __init__(self, tmp, mode, grace=1.0, notify=False):
        self.tmp = Path(tmp)
        env = {**ENV, "SHEREGESH_RELAY_GRACE_S": str(grace)}
        self.sock = None
        if notify:                                     # путь с select() по сокету уведомлений
            self.sockdir = tempfile.mkdtemp(prefix="sd", dir="/tmp")   # короткий путь AF_UNIX (macOS: 104 байта)
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.sock.bind(os.path.join(self.sockdir, "n"))
            env.update(NOTIFY_SOCKET=os.path.join(self.sockdir, "n"), RUNTIME_DIRECTORY=self.sockdir)
        self.err = open(self.tmp / "relay.err", "w+")
        self.p = subprocess.Popen([sys.executable, "-m", "sheregesh.notify_relay", "--", "/bin/sh", "-c", FAKE_CAGE,
                                   "fake-cage", mode], env=env, stdout=subprocess.PIPE, stderr=self.err)
        line = self._readline(10)
        assert line.startswith("pids "), f"нет строки pids от фиктивного cage: {line!r}; stderr: {self.stderr()}"
        self.cage, self.app = map(int, line.split()[1:3])
        self.pgid = self.cage                          # ребёнок запущен с process_group=0 — лидер своей группы
        assert os.getpgid(self.app) == self.pgid and self.pgid != os.getpgid(self.p.pid)

    def _readline(self, timeout):
        fd, buf, deadline = self.p.stdout.fileno(), b"", time.monotonic() + timeout
        while not buf.endswith(b"\n") and time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if r:
                chunk = os.read(fd, 256)
                if not chunk:
                    break
                buf += chunk
        return buf.decode(errors="replace")

    def stderr(self):
        self.err.flush()
        self.err.seek(0)
        return self.err.read()

    def stop(self, sig, timeout=15):
        t0 = time.monotonic()
        self.p.send_signal(sig)
        code = self.p.wait(timeout)
        return code, time.monotonic() - t0

    def close(self):
        for pid in (self.p.pid, getattr(self, "app", None)):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if getattr(self, "pgid", None):
            notify_relay._killpg(self.pgid, signal.SIGKILL)
        self.p.wait(5)
        self.p.stdout.close()
        self.err.close()
        if self.sock is not None:
            self.sock.close()
            shutil.rmtree(self.sockdir, ignore_errors=True)


@pytest.fixture
def relay(tmp_path):
    made = []

    def make(*a, **kw):
        r = Relay(tmp_path, *a, **kw)
        made.append(r)
        return r
    yield make
    for r in made:
        r.close()


# ------------------------------------------------------------------ штатный останов: код 0, группа пуста
@pytest.mark.parametrize("notify", [False, True], ids=["no-notify", "notify"])
def test_sigterm_child_exits_on_term(relay, notify):
    r = relay("term", grace=3, notify=notify)
    code, dt = r.stop(signal.SIGTERM)
    assert code == 0, r.stderr()
    assert dt < 2.5                                    # без ожидания GRACE_S
    assert wait_group_gone(r.pgid)
    assert "signal SIGTERM" in r.stderr() and "SIGKILL" not in r.stderr()


@pytest.mark.parametrize("notify", [False, True], ids=["no-notify", "notify"])
def test_sigterm_child_ignores_term_killed_after_grace(relay, notify):
    r = relay("ignore", grace=1, notify=notify)
    code, dt = r.stop(signal.SIGTERM)
    assert code == 0, r.stderr()                       # SIGKILL по таймауту — всё ещё штатный останов
    assert 0.9 <= dt < 5
    assert wait_group_gone(r.pgid)
    assert "still alive after 1 s — SIGKILL" in r.stderr()


def test_sigterm_frozen_group_killed_after_grace(relay):
    r = relay("ignore", grace=1)                       # XNU будит остановленный процесс сигналом по умолчанию
    for pid in (r.cage, r.app):
        os.kill(pid, signal.SIGSTOP)                   # композитор и приложение заморожены
    code, dt = r.stop(signal.SIGTERM)
    assert code == 0 and 0.9 <= dt < 5, r.stderr()
    assert wait_group_gone(r.pgid)


def test_sigterm_child_nonzero_exit_on_term_is_still_clean(relay):
    r = relay("exit1", grace=3)
    code, dt = r.stop(signal.SIGTERM)
    assert code == 0 and dt < 2.5, r.stderr()
    assert wait_group_gone(r.pgid)


def test_sigterm_leader_exits_survivor_in_group_killed(relay):
    """cage вышел сам по SIGTERM, приложение игнорирует SIGTERM: добивается после выхода ребёнка, без GRACE_S."""
    r = relay("leader-exits", grace=3)
    code, dt = r.stop(signal.SIGTERM)
    assert code == 0 and dt < 2.5, r.stderr()
    assert wait_group_gone(r.pgid)


def test_sigterm_when_child_already_dead(relay):
    """Ребёнок умер (зомби, ещё не собран) к моменту SIGTERM от systemd: код 0, приложение из группы убито."""
    r = relay("term", grace=3)
    os.kill(r.p.pid, signal.SIGSTOP)
    try:
        os.kill(r.cage, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while not (proc_state(r.cage) or "Z").startswith("Z") and time.monotonic() < deadline:
            time.sleep(0.05)
        r.p.send_signal(signal.SIGTERM)
    finally:
        os.kill(r.p.pid, signal.SIGCONT)
    assert r.p.wait(15) == 0, r.stderr()
    assert wait_group_gone(r.pgid)


def test_sigint_is_clean(relay):
    r = relay("term", grace=3)
    code, _ = r.stop(signal.SIGINT)
    assert code == 0, r.stderr()
    assert wait_group_gone(r.pgid)


def test_repeated_sigterm_is_clean(relay):
    r = relay("ignore", grace=1)
    r.p.send_signal(signal.SIGTERM)
    time.sleep(0.2)
    code, _ = r.stop(signal.SIGTERM)
    assert code == 0, r.stderr()
    assert r.stderr().count("signal SIGTERM") == 1
    assert wait_group_gone(r.pgid)


def test_sigterm_with_notify_dir_removed_is_still_clean(relay):
    """Очистка своего сокета уведомлений не должна менять код выхода (каталог выполнения общий с сеансом:
    XDG_RUNTIME_DIR=/run/sheregesh-ui)."""
    r = relay("term", grace=3, notify=True)
    for x in os.listdir(r.sockdir):
        if x.startswith("notify-"):
            shutil.rmtree(os.path.join(r.sockdir, x))
    code, _ = r.stop(signal.SIGTERM)
    assert code == 0, r.stderr()
    assert wait_group_gone(r.pgid)


# ------------------------------------------------------------------ отказы: ненулевой код, группа пуста
@pytest.mark.parametrize("notify", [False, True], ids=["no-notify", "notify"])
def test_watchdog_sigabrt_is_failure(relay, notify):
    r = relay("term", grace=3, notify=notify)
    code, _ = r.stop(signal.SIGABRT)
    assert code == 128 + signal.SIGABRT, r.stderr()
    assert wait_group_gone(r.pgid)


def test_watchdog_sigabrt_ignored_term_is_failure(relay):
    r = relay("ignore", grace=1)
    code, dt = r.stop(signal.SIGABRT)
    assert code == 128 + signal.SIGABRT and dt >= 0.9, r.stderr()
    assert wait_group_gone(r.pgid)


def test_sigabrt_then_sigterm_stays_failure(relay):
    """Первый сигнал останова определяет итог: SIGTERM после watchdog не превращает отказ в штатный останов."""
    r = relay("ignore", grace=1)
    r.p.send_signal(signal.SIGABRT)
    time.sleep(0.2)
    code, _ = r.stop(signal.SIGTERM)
    assert code == 128 + signal.SIGABRT, r.stderr()
    assert wait_group_gone(r.pgid)


def test_child_crash_is_failure_and_group_cleaned(relay):
    r = relay("crash")
    assert r.p.wait(15) == 70, r.stderr()
    assert wait_group_gone(r.pgid)                     # «приложение» не пережило упавший cage


def test_child_killed_by_signal_is_failure(relay):
    r = relay("term")
    os.kill(r.cage, signal.SIGKILL)
    assert r.p.wait(15) == 128 + signal.SIGKILL, r.stderr()
    assert wait_group_gone(r.pgid)


# ------------------------------------------------------------------ согласованность с юнитом
def test_unit_stop_timeout_covers_relay_grace():
    """TimeoutStopSec (и TimeoutAbortSec по умолчанию) должен превышать GRACE_S ретранслятора с запасом на опрос
    0,5 с, иначе systemd убьёт ретранслятор раньше, чем тот добьёт группу, и юнит уйдёт в failed по таймауту."""
    unit = (ROOT / "tools/image/units/sheregesh-console.service").read_text()
    cfg = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line and not line.startswith("#"))
    assert cfg["ExecStart"].startswith("/usr/bin/sheregesh-notify-relay -- /usr/bin/cage")
    assert "SHEREGESH_RELAY_GRACE_S" not in unit       # в юните действует значение по умолчанию
    grace = 5.0 if "SHEREGESH_RELAY_GRACE_S" in os.environ else notify_relay.GRACE_S
    stop = float(cfg.get("TimeoutAbortSec", cfg["TimeoutStopSec"]).rstrip("s"))
    assert stop >= grace + 2, f"TimeoutStopSec={stop} при GRACE_S={grace}"
