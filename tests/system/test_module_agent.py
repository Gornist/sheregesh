# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012): внешний модуль в настоящем процессе агента удалённого блока (loopback, --module) и PeerClient.

status.module → READY; module_command всех op; лабораторные сценарии attach → READY → detach → iface-error → heal;
crash и SIGKILL дочернего процесса эмулятора → восстановление с новым PID; бюджет перезапусков 3/60 с →
EMU-UNAVAILABLE → reset; SIGTERM и SIGKILL агента → процесса эмулятора нет; перезапуск агента с тем же каталогом —
настройки сохранены; link-status.json (ModuleService.health()); modulectl --expect READY."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from procs import ENV, Agent, wait_for
from sheregesh import PROTOCOL_VERSION
from sheregesh.link.client import PeerClient
from sheregesh.module import wire

READY_S = 15


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def descendants(pid):
    """Потомки процесса (ps): дочерний процесс эмулятора и, вне Linux, его сторож pdeath.py."""
    rows = [r.split() for r in subprocess.run(["ps", "-eo", "pid=,ppid=,command="], capture_output=True,
                                              text=True).stdout.splitlines() if r.strip()]
    kids, out = {}, []
    for r in rows:
        kids.setdefault(int(r[1]), []).append((int(r[0]), " ".join(r[2:])))
    stack = [pid]
    while stack:
        for c, cmd in kids.get(stack.pop(), []):
            out.append((c, cmd))
            stack.append(c)
    return out


def emulators(pid):
    return [c for c, cmd in descendants(pid) if "sheregesh_emulators.jrmodule" in cmd and "pdeath.py" not in cmd]


class Unit:
    def __init__(self, tmp, *extra, config=True):
        self.tmp = tmp
        self.status_file = tmp / "link-status.json"
        self.agent = Agent("--module", "--heartbeat-interval", "0.5", "--status-file", str(self.status_file),
                           *(["--remote-config-dir", str(tmp / "remote-config")] if config else []), *extra)
        self.pc = PeerClient("127.0.0.1", self.agent.port, heartbeat_timeout_s=3.0).start()

    def mod(self):
        return (self.pc.snapshot()["telemetry"] or {}).get("module") or {}

    def state(self):
        return self.mod().get("state")

    def wait_state(self, state, timeout=READY_S, after_seq=None):
        ok = wait_for(lambda: self.state() == state and (after_seq is None or self.mod().get("seq", 0) > after_seq),
                      timeout, 0.05)
        assert ok, (state, self.mod(), self.agent.proc.poll())
        return self.mod()

    def cmd(self, op, args=None, wait_s=10.0):
        return self.pc.module_command(op, args, wait_s=wait_s)

    def lab(self, scenario):
        r = self.cmd("lab", {"scenario": scenario})
        assert r["ok"], r
        return r

    def health(self):
        try:
            return json.loads(self.status_file.read_text()).get("module") or {}
        except (OSError, ValueError):
            return {}

    def close(self):
        self.pc.stop()
        self.agent.stop()


@pytest.fixture
def unit(tmp_path):
    units = []

    def make(*extra, **kw):
        u = Unit(tmp_path, *extra, **kw)
        units.append(u)
        return u
    yield make
    for u in units:
        u.close()


def test_ready_status_and_health(unit):
    u = unit()
    t0 = time.monotonic()
    m = u.wait_state("READY")
    assert time.monotonic() - t0 <= READY_S
    assert u.pc.snapshot()["session"] == PROTOCOL_VERSION
    assert m["profile"] == {"id": "radiomaster-nomad", "name": "RadioMaster Nomad", "family": "elrs",
                            "interface": "uart-crsf"}
    assert m["output"] == "none" and m["backend"]["kind"] == "lab-emulator" and m["backend"]["lab"] is True
    assert m["backend"]["state"] == "running" and m["identity"]["model"] == "Nomad"
    assert m["settings"]["source"] == "factory" and m["settings"]["revision"] == "r000001"
    assert [h["state"] for h in reversed(m["history"])] == ["DISCONNECTED", "DETECTED", "INITIALIZING", "READY"]
    emus = emulators(u.agent.proc.pid)
    assert emus == [m["backend"]["pid"]]
    assert wait_for(lambda: u.health().get("state") == "READY", 5)
    h = u.health()
    assert {k: h[k] for k in ("profile", "backend_state", "pid", "restarts", "settings_revision", "settings_source")} == \
        {"profile": "radiomaster-nomad", "backend_state": "running", "pid": m["backend"]["pid"], "restarts": 0,
         "settings_revision": "r000001", "settings_source": "factory"}


def test_commands_and_lab_scenarios(unit):
    u = unit()
    u.wait_state("READY")
    assert u.cmd("status") == {"ok": True, "result": "ok", "error": "", "req": 1}
    seq = u.mod()["seq"]
    assert u.cmd("reinit")["result"] == "ok"
    m = u.wait_state("READY", after_seq=seq)
    assert any(h["from"] == "INITIALIZING" and h["state"] == "READY" and h["reason"] == "init-ok" for h in m["history"])
    assert any(h["state"] == "RECOVERING" and h["reason"] == "cmd-reinit" for h in m["history"])
    assert m["diag"]["init_ms"] <= 5000
    u.lab("detach")
    u.wait_state("DISCONNECTED", 5)
    u.lab("attach")
    u.wait_state("READY", 10)
    u.lab("iface-error")
    m = u.wait_state("FAULT", 6)
    assert m["error"]["code"] == "MOD-IFACE-ERROR" and m["error"]["active"]
    u.lab("heal")
    m = u.wait_state("READY", 20)
    assert m["error"]["code"] == "MOD-IFACE-ERROR" and m["error"]["active"] is False
    assert u.cmd("reset")["result"] == "ok"
    assert u.mod()["cmd"] == {"req": u.pc.snapshot()["module_cmd"]["req"], "op": "reset", "result": "ok", "error": ""}
    assert u.pc.snapshot()["module_cmd"]["state"] == "sent"
    assert u.cmd("lab", {"scenario": "arm"})["result"] == "bad-request"           # отвергнуто до отправки
    assert u.cmd("fly")["result"] == "bad-request"
    assert u.cmd("configure", {"profile": "no-such-module"})["result"] == "invalid"
    assert u.state() == "READY"


def test_crash_and_external_sigkill_recover_with_new_pid(unit):
    u = unit()
    m = u.wait_state("READY")
    for how in ("crash", "kill"):
        pid0, seq0, r0 = m["backend"]["pid"], m["seq"], m["backend"]["restarts"]
        if how == "crash":
            u.lab("crash")
        else:
            os.kill(pid0, signal.SIGKILL)
        assert wait_for(lambda: any(h["state"] == "FAULT" and h["seq"] > seq0 for h in u.mod().get("history", [])), 5)
        fault = next(h for h in u.mod()["history"] if h["state"] == "FAULT" and h["seq"] > seq0)
        assert fault["code"] == "EMU-EXITED"
        m = u.wait_state("READY", 15, after_seq=seq0)
        assert m["backend"]["pid"] != pid0 and not alive(pid0) and m["backend"]["restarts"] == r0 + 1
        assert m["backend"]["last_exit"] == (70 if how == "crash" else -9)
        assert emulators(u.agent.proc.pid) == [m["backend"]["pid"]]


def test_restart_budget_then_unavailable_and_reset(unit):
    u = unit()
    m = u.wait_state("READY")
    for i in range(3):
        seq = m["seq"]
        u.lab("crash")
        m = u.wait_state("READY", 15, after_seq=seq)
    assert m["backend"]["restarts"] == 3
    u.lab("crash")
    assert wait_for(lambda: (u.mod().get("error") or {}).get("code") == "EMU-UNAVAILABLE", 10), u.mod()
    m = u.mod()
    assert m["state"] == "FAULT" and m["backend"]["state"] == "unavailable" and m["recovery"]["exhausted"]
    time.sleep(2.5)                                                     # удержание: процессов и попыток нет
    assert emulators(u.agent.proc.pid) == [] and u.mod()["backend"]["restarts"] == 3 and u.state() == "FAULT"
    assert u.cmd("reset")["result"] == "ok"
    m = u.wait_state("READY", 10)
    assert m["backend"]["restarts"] == 1 and not m["recovery"]["exhausted"]


def test_emu_unavailable_and_available(unit):
    u = unit()
    u.wait_state("READY")
    t0 = time.monotonic()
    u.lab("emu-unavailable")
    assert wait_for(lambda: (u.mod().get("error") or {}).get("code") == "EMU-UNAVAILABLE", 30), u.mod()
    assert time.monotonic() - t0 <= 30
    codes = [h["code"] for h in reversed(u.mod()["history"]) if h["state"] == "FAULT"]
    assert codes[-2:] == ["EMU-START-FAILED", "EMU-UNAVAILABLE"]
    assert emulators(u.agent.proc.pid) == []
    assert u.cmd("lab", {"scenario": "detach"})["result"] == "not-running"
    u.lab("emu-available")
    time.sleep(1.5)
    assert u.state() == "FAULT"                                         # состояние не меняется до reset
    assert u.cmd("reset")["result"] == "ok"
    u.wait_state("READY", 10)


def test_configure_persists_across_agent_restart(unit, tmp_path):
    u = unit()
    u.wait_state("READY")
    pid = u.mod()["backend"]["pid"]
    r = u.cmd("configure", {"enabled": False}, wait_s=15)
    assert r["ok"], r
    m = u.wait_state("DISABLED", 5)
    rev = m["settings"]["revision"]
    assert rev == "r000002" and m["settings"]["source"] == "store" and m["settings"]["enabled"] is False
    assert wait_for(lambda: not alive(pid), 3) and wait_for(lambda: emulators(u.agent.proc.pid) == [], 3)
    assert u.cmd("lab", {"scenario": "detach"})["result"] == "disabled"
    u.close()
    u2 = unit()                                                          # тот же каталог настроек
    m = u2.wait_state("DISABLED", 10)
    assert m["settings"]["revision"] == rev and m["settings"]["source"] == "store"
    time.sleep(1.0)
    assert emulators(u2.agent.proc.pid) == []
    assert u2.cmd("configure", {"enabled": True}, wait_s=15)["ok"]
    u2.wait_state("READY", 15)


def test_sigterm_and_sigkill_of_agent_leave_no_emulator(unit):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        u = unit()
        pid = u.wait_state("READY")["backend"]["pid"]
        assert alive(pid)
        os.kill(u.agent.proc.pid, sig)
        u.agent.proc.wait(5)
        assert wait_for(lambda: not alive(pid), 3), f"{sig.name}: эмулятор {pid} пережил агента"
        u.pc.stop()


def test_without_config_dir_settings_are_in_memory(unit):
    u = unit(config=False)
    m = u.wait_state("READY")
    assert m["settings"]["source"] == "memory"
    assert u.cmd("configure", {"auto_init": False})["result"] == "not-saved"
    assert u.mod()["settings"]["auto_init"] is True


def test_modulectl_direct_expect_ready(unit):
    u = unit()
    u.wait_state("READY")
    u.pc.stop()                                         # блок держит один сеанс: дальше — modulectl

    def ctl(*args):
        p = subprocess.run([sys.executable, "-m", "sheregesh.link.modulectl", *args, "--via", "direct", "--port",
                            str(u.agent.port)], env=ENV, capture_output=True, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else {"stderr": p.stderr}
    rc, out = ctl("status")
    assert rc == 0 and out["via"] == "direct" and out["module"]["support"] == "ok"
    assert out["module"]["telemetry"]["state"] == "READY" and out["module"]["session"] == PROTOCOL_VERSION
    rc, out = ctl("lab", "detach", "--expect", "DISCONNECTED", "--expect-timeout", "5")
    assert rc == 0 and out["module"]["telemetry"]["state"] == "DISCONNECTED", out
    rc, out = ctl("lab", "attach", "--expect", "READY", "--expect-timeout", "15")
    assert rc == 0 and out["result"] == "ok" and out["module"]["telemetry"]["state"] == "READY", out
    rc, out = ctl("reinit", "--expect", "READY")
    assert rc == 0, out
    rc, out = ctl("configure", "auto_init=maybe")                       # неверные аргументы: до соединения
    assert rc == 2 and "expected true or false" in out["stderr"]
    rc, out = ctl("lab", "heal", "--expect", "DISABLED", "--expect-timeout", "1")
    assert rc == 1 and out["result"] == "timeout" and "expected DISABLED" in out["detail"], out
    p = subprocess.run([sys.executable, "-m", "sheregesh.link.modulectl", "status", "--via", "direct", "--port", "9",
                        "--connect-timeout", "1"], env=ENV, capture_output=True, text=True, timeout=30)
    assert p.returncode == 2 and json.loads(p.stdout)["result"] == "not-connected"


def test_status_module_is_sanitized_unchanged_and_small(unit):
    u = unit()
    u.wait_state("READY")
    u.lab("detach")
    u.lab("attach")
    u.wait_state("READY", 10)
    m = u.mod()                                         # тело блока прошло санитайзер клиента без потерь
    assert wire.sanitize_module(m) == m and "cmd" in m
    assert len(json.dumps(m, ensure_ascii=False).encode()) <= wire.MAX_STATUS_BYTES
