# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S8 (ADR-0014, 3.2): бэкенд модуля crsf в ModuleService на SimClock — CrsfOutput (sheregesh.crsf) через
LoopbackPort к эмулятору ELRS TX в процессе (sheregesh_emulators.elrs_tx), без потоков и pty.

Жизненный цикл по ответам CRSF (ping → device info → READY), режим выхода none | neutral | live по сторожу команд
(IDLE/FAILSAFE → failsafe_ch, ACTIVE → каналы пульта со взведением), модуль молчит → FAULT MOD-NO-RESPONSE и none,
heal → снова READY, ошибки CRC подряд → MOD-IFACE-ERROR, чужое имя модуля → MOD-PROFILE-MISMATCH, crsf-uart без UART3
→ FAULT «UART не найден» без исключений, status.module проходит sanitize_module без изменений и укладывается в 4 КиБ."""
import json

from sheregesh.crsf import frames as F
from sheregesh.crsf.output import CrsfOutput
from sheregesh.module import crsf, wire
from sheregesh.module.service import ModuleService
from sheregesh.module.settings import ModuleSettings
from sheregesh.module.states import State, Timings
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.elrs_tx import NAME, ElrsTxEmulator, LoopbackPort

S = State
STICKS = [0, -12, 1000, 3, -1000, 640, -355, 999]


class InlineCrsfLab(crsf.CrsfBackend):
    """crsf-pty-lab без процесса: лабораторные сценарии — прямо в ядро эмулятора."""
    kind = "crsf-pty-lab"
    supports_lab = True
    lab_scenarios = tuple(crsf.LAB_MAP)

    def __init__(self, emu, **kw):
        super().__init__(**kw)
        self.emu = emu

    def lab(self, scenario):
        return self.emu.lab(crsf.LAB_MAP[scenario])


class Rig:
    def __init__(self, backend="crsf-pty-lab", name=NAME, failsafe=(0,) * 8, find_uart=None):
        self.clock = SimClock(100.0)
        self.emu = ElrsTxEmulator(self.clock, name=name)
        self.logs = []
        self.ctl = ("IDLE", (0,) * 8, 0)
        self.settings = ModuleSettings(backend=backend, failsafe_ch=failsafe)
        self.find_uart = find_uart
        self.svc = ModuleService(None, clock=self.clock, log=self.logs.append, backend_factory=self.factory)
        self.svc.store.load = lambda: (self.settings, {"source": "memory", "revision": "", "notice": "CFG-MEMORY"})
        self.svc.start()
        self._due = self.clock()
        self._tick = self._ctl = self.clock()
        self.stalled = False                       # агент завис: ни сторожа, ни такта ModuleService; CRSF работает

    def factory(self, s):
        kw = dict(profile=self.svc.manager.profile if self.svc.manager else _nomad(), settings=s, clock=self.clock,
                  log=self.logs.append, timings=Timings(), source=self.svc.output_source,
                  output_factory=self.output, port_factory=lambda: (LoopbackPort(self.emu), "loopback"))
        if s.backend == "crsf-uart":
            kw.pop("port_factory")
            return crsf.UartCrsfBackend(find_uart=self.find_uart, **kw)
        return InlineCrsfLab(self.emu, **kw)

    def output(self, port):
        out = CrsfOutput(port, self.svc.output_source, clock=self.clock, log=self.logs.append)
        out.start = lambda: None                   # без потока: такты — run()
        return out

    @property
    def out(self):
        b = self.svc.manager.backend
        return getattr(b, "out", None)

    def run(self, seconds, until=None):
        """Модельное время шагами 0,5 мс: такт CrsfOutput по его сроку, сторож 25 мс, ModuleService 100 мс."""
        end = self.clock() + seconds
        while self.clock() < end - 1e-9:
            self.clock.advance(0.0005)
            now = self.clock()
            out = self.out
            if out is not None and now >= self._due - 1e-9:
                self._due = out.step()
            elif out is None:
                self._due = now
            self.emu.step()
            if self.stalled:
                continue
            if now - self._ctl >= 0.025 - 1e-9:
                self._ctl = now
                st, ch, btn = self.ctl            # как агент: свежий RC каждый такт, срок = RC + 500 мс
                self.svc.set_control(st, ch, btn, deadline=now + 0.5 if st == "ACTIVE" else None)
            if now - self._tick >= 0.1 - 1e-9:
                self._tick = now
                self.svc.tick()
                if until is not None and until():
                    return True
        return until() if until is not None else None

    def mod(self):
        return json.loads(json.dumps(self.svc.status()))

    def state(self):
        return self.svc.manager.state


def _nomad():
    from sheregesh.module.profiles import get_profile
    return get_profile("radiomaster-nomad")


def test_ready_neutral_live_failsafe():
    r = Rig(failsafe=(0, 0, -1000, 0, 0, 0, 0, 0))
    assert r.svc.status()["output"] == "none"
    t0 = r.clock()
    assert r.run(5.0, lambda: r.state() == S.READY), r.logs
    m = r.mod()
    assert m["identity"] == {"vendor": "RadioMaster", "model": "Nomad", "firmware": "4.1.0", "serial": "ELRS"}
    assert m["backend"]["kind"] == "crsf-pty-lab" and m["backend"]["lab"] is True
    r.run(1.0)
    m = r.mod()
    assert m["output"] == "neutral" and m["crsf"]["mode"] == "neutral" and m["crsf"]["synced"] is True
    assert m["crsf"]["device"] == {"name": NAME, "sw": "4.1.0", "serial": "ELRS"}
    assert m["crsf"]["period_us"] == 20000 and m["crsf"]["port"] == "loopback"
    st = r.emu.state()
    assert st["host"] == "connected" and st["armed"] is False
    assert st["axes"] == [0, 0, -1000, 0, 0, 0, 0, 0]          # нейтраль — failsafe_ch из настроек
    assert abs(st["rate_hz"] - 50.0) <= 1.0
    r.ctl = ("ACTIVE", tuple(STICKS), 5)
    r.run(0.5)
    st = r.emu.state()
    assert r.mod()["output"] == "live" and st["armed"] is True
    assert all(abs(a - b) <= 2 for a, b in zip(st["axes"], STICKS)), st["axes"]
    assert st["ch"][8:] == [F.CH_CENTER] * 8                      # каналы 9–16 — центр, кнопки в каналы не идут
    r.ctl = ("FAILSAFE", (0,) * 8, 0)
    r.run(0.1)
    st = r.emu.state()
    assert r.mod()["output"] == "neutral" and st["armed"] is False and st["axes"] == [0, 0, -1000, 0, 0, 0, 0, 0]
    trans = [s for s in r.logs if s.startswith("crsf: ") and " -> " in s]
    assert [t.split(" (")[0] for t in trans] == ["crsf: none -> neutral", "crsf: neutral -> live",
                                                 "crsf: live -> neutral"], trans
    assert r.clock() - t0 < 8


def test_stalled_agent_live_snapshot_expires():
    """Агент перестал тактовать (сторож и set_control молчат), поток CRSF и модуль работают: live, полученный от
    последнего свежего RC, действует не дольше таймаута сторожа (500 мс) плюс один период отправителя, затем на
    модуль идут failsafe_ch без взведения. Новых команд нет — live не возвращается."""
    fs = (0, 0, -1000, 0, 0, 0, 0, 0)
    r = Rig(failsafe=fs)
    assert r.run(5.0, lambda: r.state() == S.READY), r.logs
    r.ctl = ("ACTIVE", tuple(STICKS), 5)
    r.run(0.5)
    assert r.mod()["output"] == "live" and r.emu.state()["armed"] is True
    t_rc = r.clock()                               # последний такт сторожа ≈ последний свежий RC
    r.stalled = True
    r.run(0.45)
    assert r.emu.state()["armed"] is True          # срок ещё не истёк
    sent = r.out.frames_sent
    r.run(1.0)                                     # ≈ 1 с без такта агента (наблюдение 1004 мс из плана)
    st = r.emu.state()
    assert r.out.frames_sent - sent >= 40          # отправитель не остановился: ≈ 50 кадров/с
    assert st["armed"] is False and st["axes"] == list(fs), st
    snap = r.out.snapshot()
    assert snap["mode"] == "neutral" and snap["armed"] is False and snap["ch"] == list(fs)
    expired = [s for s in r.logs if "expired" in s]
    assert expired, r.logs
    assert r.clock() - t_rc > 0.5


def test_live_needs_deadline_and_new_session_drops_it():
    """ModuleService: ACTIVE без срока live не даёт; повторный set_control с тем же сроком его не продлевает (поток
    выхода переходит на нейтраль); новый сеанс сразу снимает live до первого свежего RC нового сеанса."""
    fs = (0, 0, -1000, 0, 0, 0, 0, 0)
    r = Rig(failsafe=fs)
    assert r.run(5.0, lambda: r.state() == S.READY), r.logs
    r.stalled = True                               # set_control — вручную
    r.svc.set_control("ACTIVE", STICKS, 5)
    cmd = r.svc.output_source()
    assert cmd.mode == "neutral" and cmd.ch == fs and cmd.deadline is None
    deadline = r.clock() + 0.3
    r.svc.set_control("ACTIVE", STICKS, 5, deadline=deadline)
    cmd = r.svc.output_source()
    assert cmd.mode == "live" and cmd.deadline == deadline and cmd.neutral == fs
    r.run(0.25)
    assert r.emu.state()["armed"] is True
    r.svc.set_control("ACTIVE", STICKS, 5, deadline=deadline)   # такт агента без нового RC
    r.run(0.1)
    st = r.emu.state()
    assert st["armed"] is False and st["axes"] == list(fs)
    r.svc.set_control("ACTIVE", STICKS, 5, deadline=r.clock() + 0.5)
    r.run(0.1)
    assert r.emu.state()["armed"] is True
    session = r.svc.output_source().session
    r.svc.new_session()
    cmd = r.svc.output_source()
    assert cmd.mode == "neutral" and cmd.session == session + 1
    r.run(0.1)
    assert r.emu.state()["armed"] is False


def test_silent_fault_none_then_heal_ready():
    r = Rig()
    assert r.run(5.0, lambda: r.svc.output == "neutral")
    r.svc.command({"req": 1, "op": "lab", "args": {"scenario": "silent"}})
    assert r.svc.cmd["result"] == "ok"
    t0 = r.clock()
    assert r.run(5.0, lambda: r.state() == S.FAULT)
    assert r.clock() - t0 <= _nomad().no_response_s + 1.0
    m = r.mod()
    assert m["error"]["code"] == "MOD-NO-RESPONSE" and m["output"] == "none"
    assert m["crsf"]["mode"] in ("none", "neutral")             # поток выхода видит none на следующем такте
    r.run(0.2)
    assert r.mod()["crsf"]["mode"] == "none" and r.svc.output_source()[0] == "none"
    frames = r.emu.rc_frames
    r.run(1.0)
    assert r.emu.rc_frames == frames                            # кадров каналов нет — только ping
    r.svc.command({"req": 2, "op": "lab", "args": {"scenario": "heal"}})
    assert r.run(40.0, lambda: r.state() == S.READY)
    assert r.run(1.0, lambda: r.svc.output == "neutral")


def test_hang_maps_to_silent_and_slow_init_is_not_supported():
    r = Rig()
    assert r.run(5.0, lambda: r.state() == S.READY)
    r.svc.command({"req": 1, "op": "lab", "args": {"scenario": "slow-init"}})
    assert r.svc.cmd["result"] == "bad-request"
    r.svc.command({"req": 2, "op": "lab", "args": {"scenario": "hang"}})
    assert r.svc.cmd["result"] == "ok" and r.emu.scenario == "silent"
    assert set(wire.LAB_SCENARIOS_ELRS) <= set(crsf.PtyCrsfLabBackend.lab_scenarios) <= set(wire.LAB_SCENARIOS)
    assert crsf.UartCrsfBackend.supports_lab is False and crsf.PtyCrsfLabBackend.supports_lab is True


def test_crc_errors_fault_iface():
    r = Rig()
    assert r.run(5.0, lambda: r.state() == S.READY)
    r.emu.lab("crc")
    assert r.run(3.0, lambda: r.state() == S.FAULT)
    assert r.mod()["error"]["code"] == "MOD-IFACE-ERROR" and "CRC" in r.mod()["error"]["text"]
    assert r.svc.output == "none"


def test_wrong_module_name_profile_mismatch():
    r = Rig(name="BETAFPV Micro TX")
    assert r.run(5.0, lambda: r.state() == S.FAULT)
    m = r.mod()
    assert m["error"]["code"] == "MOD-PROFILE-MISMATCH" and m["output"] == "none"
    assert r.emu.rc_frames == 0


def test_uart_not_found_is_fault_without_exception():
    r = Rig(backend="crsf-uart", find_uart=lambda: None)
    r.run(1.0)
    m = r.mod()
    assert m["state"] == "FAULT" and m["error"]["code"] == "MOD-IFACE-ERROR"
    assert "UART не найден" in m["error"]["text"] and "uart-not-found" in m["error"]["text"]
    assert m["output"] == "none" and m["backend"]["kind"] == "crsf-uart" and m["backend"]["lab"] is False
    r.run(30.0)                                                 # восстановления по backoff, бюджет — EMU-UNAVAILABLE
    m = r.mod()
    assert m["state"] == "FAULT" and m["recovery"]["exhausted"] is True
    assert m["error"]["code"] == "EMU-UNAVAILABLE" and "UART не найден" in m["error"]["text"]
    assert not any(s.startswith("internal error") for s in r.logs)
    assert r.svc.command({"req": 3, "op": "lab", "args": {"scenario": "silent"}}) is None
    assert r.svc.cmd["result"] == "forbidden"


def test_status_sanitize_roundtrip_and_size():
    r = Rig()
    assert r.run(5.0, lambda: r.svc.output == "neutral")
    r.ctl = ("ACTIVE", tuple(STICKS), 1)
    r.run(0.5)
    body = r.mod()
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= wire.MAX_STATUS_BYTES
    assert wire.sanitize_module(body) == body
    assert body["output"] == "live" and body["settings"]["backend"] == "crsf-pty-lab"
    assert body["settings"]["failsafe_ch"] == [0] * 8 and body["settings"]["uart"]["baud"] == 400000
    h = r.svc.health()
    assert h["output"] == "live" and h["backend"] == "crsf-pty-lab" and h["crsf"]["synced"] is True


def test_sanitize_old_console_view_and_bad_crsf():
    r = Rig()
    assert r.run(5.0, lambda: r.svc.output == "neutral")
    body = r.mod()
    bad = {**body, "output": "armed", "crsf": {"mode": "fly", "period_us": -5, "device": "x", "link": [1],
                                               "emu": {"ch": ["a", 5000, -7], "armed": "yes"}}}
    m = wire.sanitize_module(bad)
    assert m["output"] == "unknown" and m["crsf"]["mode"] == "unknown" and m["crsf"]["period_us"] == 0
    assert m["crsf"]["device"] is None and m["crsf"]["link"] is None
    assert m["crsf"]["emu"]["ch"] == [0, 1000, -7] and m["crsf"]["emu"]["armed"] is None
    assert "crsf" not in wire.sanitize_module({**body, "crsf": "x"})


def test_backend_switch_via_configure(tmp_path):
    """configure backend=… сохраняется и перезапускает канал через DISABLED (settings-backend)."""
    clock = SimClock()
    made = []

    def factory(s):
        made.append(s.backend)
        if s.backend == "lab-emulator":
            from sheregesh.module.lab import InlineEmulatorBackend
            from sheregesh_emulators.jrmodule import JrModuleEmulator
            return InlineEmulatorBackend(JrModuleEmulator(clock), clock=clock, timings=Timings())
        return crsf.UartCrsfBackend(_nomad(), s, clock=clock, log=lambda _s: None, timings=Timings(),
                                    source=svc.output_source, find_uart=lambda: None)
    svc = ModuleService(tmp_path / "rc", clock=clock, log=lambda _s: None, backend_factory=factory)
    svc.start()
    for _ in range(30):
        clock.advance(0.1)
        svc.tick()
    assert svc.manager.state == S.READY and svc.settings.backend == "lab-emulator"
    svc.command({"req": 1, "op": "configure", "args": {"backend": "crsf-uart"}})
    assert svc.cmd["result"] == "pending"
    svc._pending[0].result(timeout=10)                         # сохранение — в исполнителе
    clock.advance(0.1)
    svc.tick()
    assert svc.cmd["result"] == "ok" and svc.settings.backend == "crsf-uart"
    for _ in range(5):
        clock.advance(0.1)
        svc.tick()
    assert made == ["lab-emulator", "crsf-uart"]
    hist = [h["reason"] for h in svc.status()["history"]]
    assert "settings-backend" in hist and svc.manager.state == S.FAULT
    svc.command({"req": 2, "op": "configure", "args": {"backend": "lab-emulator"}})
    svc._pending[0].result(timeout=10)
    for _ in range(50):
        clock.advance(0.1)
        svc.tick()
    assert made == ["lab-emulator", "crsf-uart", "lab-emulator"] and svc.manager.state == S.READY
    assert svc.status()["output"] == "none" and "crsf" not in svc.status()
