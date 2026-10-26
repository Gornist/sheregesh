# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S8 (ADR-0014, 3.2): цепочка выхода CRSF в настоящих процессах (loopback): пульт PeerClient.send_rc → агент
удалённого блока (ControlReceiver → ModuleService → CrsfOutput) → псевдотерминал → эмулятор ELRS TX
(sheregesh_emulators.elrs_tx, дочерний процесс агента через video.spawn).

Бэкенд выбирается как оператором — module-cmd configure backend=crsf-pty-lab; по умолчанию lab-emulator, выхода нет.
Эмулятор запускается через «отвод» (TAP): тот же python -m sheregesh_emulators.elrs_tx, строки состояния (каждые
20 мс, время — monotonic эмулятора, общее с тестом) дублируются в файл — так видно, что принял модуль, и когда.
Проверки: READY ≤ 5 с с device «RM Nomad X-Band (emu)» и синхронизацией 20 000 мкс; передача включена → live, каналы
эмулятора = каналы пульта, взведение; кадры прекращены → FAILSAFE → нейтраль ≤ timeout + 100 мс после последнего
кадра; модуль молчит (silent) → FAULT MOD-NO-RESPONSE и output none, heal → READY; агент остановлен (SIGSTOP — кадры
прекращаются, эмулятор жив; SIGKILL агента эмулятор не переживает: pdeathsig) → эмулятор uart-disconnected ≤ 1,5 с;
агент завершён → процесса эмулятора нет; configure backend=lab-emulator → поведение S6, output none."""
import json
import os
import signal
import sys
import threading
import time

import pytest

from procs import Agent, wait_for
from sheregesh.link.client import PeerClient

CH = [0, -12, 1000, 3, -1000, 640, -355, 999]
DEV = {"name": "OpenTX Radiomaster Boxer Joystick", "ok": True}
NAME = "RM Nomad X-Band (emu)"
TAP = '''import subprocess, sys
log = open(sys.argv[1], "ab", buffering=0)
p = subprocess.Popen([sys.executable, "-s", "-m", "sheregesh_emulators.elrs_tx", "--state-ms", "20", *sys.argv[2:]],
                     stdout=subprocess.PIPE)
for line in iter(p.stdout.readline, b""):
    log.write(line)
    try:
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    except BrokenPipeError:
        break
sys.exit(p.wait())
'''


class Sender(threading.Thread):
    """Поток пульта: send_rc 50 Гц, пока не остановлен."""

    def __init__(self, pc, ch=CH):
        super().__init__(daemon=True)
        self.pc, self.body = pc, {"en": True, "ch": list(ch), "btn": 5, "dev": dict(DEV)}
        self.halt = threading.Event()

    def run(self):
        nxt = time.monotonic()
        while not self.halt.is_set():
            self.pc.send_rc(self.body)
            nxt += 0.02
            self.halt.wait(max(0.0, nxt - time.monotonic()))

    def stop(self):
        self.halt.set()
        self.join(2)


class Chain:
    def __init__(self, tmp, tap=True):
        self.tap_log = tmp / "elrs-tap.jsonl"
        extra = []
        if tap:
            script = tmp / "elrs_tap.py"
            script.write_text(TAP)
            extra = ["--crsf-emulator-command", sys.executable, str(script), str(self.tap_log)]
        self.agent = Agent("--module", "--heartbeat-interval", "0.3", "--status-file", str(tmp / "st.json"),
                           "--remote-config-dir", str(tmp / "remote-config"), *extra)
        self.pc = PeerClient("127.0.0.1", self.agent.port, heartbeat_timeout_s=3.0).start()

    def mod(self):
        return (self.pc.snapshot()["telemetry"] or {}).get("module") or {}

    def emu_lines(self):
        """Строки state эмулятора из отвода: [(t monotonic, тело)]."""
        out = []
        try:
            data = self.tap_log.read_text()
        except OSError:
            return out
        for line in data.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if isinstance(m, dict) and m.get("ev") == "state":
                out.append((m["t"], m))
        return out

    def emu(self):
        lines = self.emu_lines()
        return lines[-1][1] if lines else {}

    def configure(self, backend):
        res = self.pc.module_command("configure", {"backend": backend}, wait_s=10)
        assert res["result"] == "ok", res

    def close(self):
        self.pc.stop()
        self.agent.stop()


@pytest.fixture
def chain(tmp_path):
    made = []

    def make(**kw):
        c = Chain(tmp_path, **kw)
        made.append(c)
        assert wait_for(lambda: c.mod().get("state") == "READY", 15), c.mod()
        return c
    yield make
    for c in made:
        c.close()


def test_crsf_chain_end_to_end(chain):
    c = chain()
    m = c.mod()
    assert m["backend"]["kind"] == "lab-emulator" and m["output"] == "none" and "crsf" not in m   # по умолчанию — S6
    t0 = time.monotonic()
    c.configure("crsf-pty-lab")
    assert wait_for(lambda: c.mod().get("state") == "READY" and c.mod()["backend"]["kind"] == "crsf-pty-lab", 8), \
        c.mod()
    assert time.monotonic() - t0 <= 5.0 + 0.5                                          # CR-01 (+ сохранение)
    m = c.mod()
    assert m["crsf"]["device"]["name"] == NAME and m["identity"]["model"] == "Nomad"
    assert m["settings"]["backend"] == "crsf-pty-lab" and m["backend"]["lab"] is True
    assert wait_for(lambda: c.mod()["output"] == "neutral" and c.mod()["crsf"]["synced"], 3), c.mod()
    assert abs(c.mod()["crsf"]["period_us"] - 20000) <= 200                              # CR-02
    assert wait_for(lambda: c.emu().get("armed") is False and c.emu().get("axes") == [0] * 8, 2), c.emu()  # CR-03

    snd = Sender(c.pc)
    snd.start()
    try:
        assert wait_for(lambda: c.mod()["output"] == "live", 3), c.mod()
        assert wait_for(lambda: c.emu().get("armed") is True, 2), c.emu()
        time.sleep(1.2)
        e = c.emu()
        assert all(abs(a - b) <= 2 for a, b in zip(e["axes"], CH)), e["axes"]               # CR-04
        assert e["host"] == "connected" and abs(e["rate_hz"] - 50.0) <= 2.0, e               # CR-05 (частота)
        assert wait_for(lambda: (c.mod()["crsf"]["emu"] or {}).get("armed") is True, 2)
        assert c.mod()["crsf"]["crc_errors"] == 0 and c.mod()["crsf"]["emu"]["crc_errors"] == 0
    finally:
        snd.stop()
    last = c.pc.snapshot()["rc_sent"]["at"]
    assert wait_for(lambda: any(t > last and b["armed"] is False for t, b in c.emu_lines()), 2)
    t_neutral = min(t for t, b in c.emu_lines() if t > last and b["armed"] is False)
    assert t_neutral - last <= 0.5 + 0.1 + 0.05, t_neutral - last                        # CR-06: timeout 500 мс
    assert [b for t, b in c.emu_lines() if t >= t_neutral][0]["axes"] == [0] * 8
    assert wait_for(lambda: c.mod()["output"] == "neutral", 2)

    res = c.pc.module_command("lab", {"scenario": "silent"})                             # CR-07
    assert res["result"] == "ok", res
    t1 = time.monotonic()
    assert wait_for(lambda: c.mod().get("state") == "FAULT", 4), c.mod()
    assert time.monotonic() - t1 <= 2.0 + 1.0 + 0.4
    m = c.mod()
    assert m["error"]["code"] == "MOD-NO-RESPONSE" and m["output"] == "none"
    assert c.pc.module_command("lab", {"scenario": "heal"})["result"] == "ok"
    assert wait_for(lambda: c.mod().get("state") == "READY" and c.mod()["output"] == "neutral", 20), c.mod()

    pid = c.mod()["backend"]["pid"]
    assert pid > 0
    os.kill(c.agent.proc.pid, signal.SIGSTOP)                # агент «умер» для линии: кадров нет, эмулятор жив
    t_stop = time.monotonic()
    try:
        assert wait_for(lambda: any(t > t_stop and b["host"] == "uart-disconnected" for t, b in c.emu_lines()), 3)
        t_dis = min(t for t, b in c.emu_lines() if t > t_stop and b["host"] == "uart-disconnected")
        assert t_dis - t_stop <= 1.5, t_dis - t_stop                                      # CR-08 (линия)
    finally:
        os.kill(c.agent.proc.pid, signal.SIGCONT)
    assert wait_for(lambda: c.emu().get("host") == "connected", 3), c.emu()
    assert wait_for(lambda: c.mod().get("state") == "READY" and c.mod()["output"] == "neutral", 25), c.mod()

    c.configure("lab-emulator")                                                          # CR-09
    assert wait_for(lambda: c.mod().get("state") == "READY" and c.mod()["backend"]["kind"] == "lab-emulator", 10)
    m = c.mod()
    assert m["output"] == "none" and "crsf" not in m
    assert wait_for(lambda: not _alive(pid), 5), pid                                      # эмулятор ELRS TX остановлен
    c.agent.stop()
    log = c.agent.proc.stderr.read()
    for t in ("crsf: none -> neutral", "crsf: neutral -> live", "crsf: live -> neutral", "crsf: neutral -> none"):
        assert t in log, t


def test_default_emulator_command_and_agent_exit(chain):
    """Эмулятор по умолчанию (python -s -m sheregesh_emulators.elrs_tx --tty …): READY, нейтраль; SIGTERM агента —
    процесса эмулятора нет."""
    c = chain(tap=False)
    c.configure("crsf-pty-lab")
    assert wait_for(lambda: c.mod().get("state") == "READY" and c.mod()["output"] == "neutral", 8), c.mod()
    m = c.mod()
    assert m["crsf"]["port"] == "pty" and m["crsf"]["device"]["name"] == NAME
    assert wait_for(lambda: (c.mod()["crsf"]["emu"] or {}).get("state") == "connected", 3), c.mod()["crsf"]
    pid = m["backend"]["pid"]
    assert pid > 0 and _alive(pid)
    c.agent.stop()
    assert wait_for(lambda: not _alive(pid), 5), pid


def _alive(pid):
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
