# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, разделы 4, 6.3, 6.4, 7.3): менеджер внешнего модуля на InlineEmulatorBackend + SimClock.

Каждая строка таблицы 4.2 и каждый лабораторный сценарий 6.4; таймауты 4.3 по модельному времени; backoff;
бюджет перезапусков 3/60 с и EMU-UNAVAILABLE; reset/reinit; auto_init=false; DISABLED останавливает бэкенд;
общий сценарий SCRIPT_FULL; одинаковый прогон — одинаковая история. Системные часы и sleep не используются."""
import pytest

from sheregesh.module.backend import ModuleError
from sheregesh.module.lab import InlineEmulatorBackend
from sheregesh.module.manager import ModuleManager, RestartBudget
from sheregesh.module.settings import ModuleSettings
from sheregesh.module.states import State, Timings
from sheregesh_emulators.devices import SimClock
from sheregesh_emulators.jrmodule import IDENTITIES, SCRIPT_FULL, JrModuleEmulator

S = State
DT = 0.1


class Rig:
    def __init__(self, start=True, start_ok=True, identity=None, **settings):
        self.clock = SimClock()
        self.emu = JrModuleEmulator(self.clock, identity=identity)
        self.be = InlineEmulatorBackend(self.emu, clock=self.clock, timings=Timings(), start_ok=start_ok)
        self.logs = []
        self.made = 0

        def factory(_s):
            self.made += 1
            return self.be
        self.m = ModuleManager(ModuleSettings(**settings), factory, clock=self.clock,
                               log=lambda s: self.logs.append(f"{self.clock():.1f} {s}"), strict=True)
        self.seen = []
        if start:
            self.m.start()

    def run(self, seconds):
        """Продвинуть модельное время; вернуть состояния, наблюдавшиеся на каждом такте."""
        out = []
        for _ in range(int(round(seconds / DT))):
            self.clock.advance(DT)
            self.m.tick()
            out.append(self.m.state)
        self.seen += out
        return out

    def until(self, state, limit=30.0):
        """Такты до состояния; модельное время до него (AssertionError, если не наступило за limit)."""
        t0 = self.clock()
        while self.clock() - t0 < limit:
            self.clock.advance(DT)
            self.m.tick()
            if self.m.state == state:
                return round(self.clock() - t0, 3)
        raise AssertionError(f"{state} not reached in {limit} s; state {self.m.state}\n" + "\n".join(self.logs[-20:]))

    def lab(self, scenario):
        return self.m.command("lab", {"scenario": scenario})

    def snap(self):
        return self.m.snapshot()

    def transitions(self):
        return [(h["from"], h["state"], h["reason"], h["code"]) for h in reversed(self.m.history)]


def ready_rig(**kw):
    r = Rig(**kw)
    r.until(S.READY, 5)
    return r


# ---------- таблица 4.2 ----------
def test_start_disabled_and_enabled():
    r = Rig(enabled=False)
    assert r.m.state == S.DISABLED and r.transitions() == [("", "DISABLED", "start", "")]
    assert r.made == 0 and r.snap()["backend"]["state"] == "stopped"
    r = Rig()
    assert r.transitions() == [("", "DISCONNECTED", "start", "")]
    assert r.made == 1


def test_start_to_ready_path_and_timing():
    r = Rig()
    t = r.until(S.READY, 5)
    assert [x[1:3] for x in r.transitions()] == [("DISCONNECTED", "start"), ("DETECTED", "present"),
                                                  ("INITIALIZING", "auto-init"), ("READY", "init-ok")]
    assert t <= 2.0
    s = r.snap()
    assert s["identity"] == IDENTITIES["radiomaster-nomad"] and s["error"] is None and s["output"] == "none"
    assert 1000 <= s["diag"]["init_ms"] <= 1100
    assert s["backend"]["state"] == "running" and s["backend"]["lab"] is True


def test_detected_to_initializing_on_next_tick_only():
    r = Rig()
    while r.m.state != S.DETECTED:
        r.clock.advance(DT)
        r.m.tick()
    r.clock.advance(DT)
    r.m.tick()
    assert r.m.state == S.INITIALIZING


def test_auto_init_false_stays_detected_until_reinit():
    r = Rig(auto_init=False)
    r.run(10)
    assert r.m.state == S.DETECTED
    assert r.m.command("reinit", {}) == "ok"
    assert r.m.state == S.INITIALIZING and r.transitions()[-1][2] == "cmd-reinit"
    r.until(S.READY, 3)


def test_absent_from_detected_initializing_ready():
    for target in (S.DETECTED, S.INITIALIZING, S.READY):
        r = Rig(auto_init=target != S.DETECTED)
        r.until(target, 5)
        assert r.lab("detach") == "ok"
        r.run(0.2)
        assert r.m.state == S.DISCONNECTED and r.transitions()[-1][1:3] == ("DISCONNECTED", "absent")
        assert r.snap()["identity"] is None


def test_init_error_and_profile_mismatch():
    r = Rig(auto_recover=False)
    r.until(S.DETECTED, 2)
    r.lab("iface-error")
    r.until(S.FAULT, 3)
    assert r.transitions()[-1][2:] == ("init-error", "MOD-IFACE-ERROR")
    r = Rig(auto_recover=False, identity={"vendor": "Other", "model": "X", "firmware": "1", "serial": "2"})
    r.until(S.FAULT, 3)
    assert r.transitions()[-1][2:] == ("profile-mismatch", "MOD-PROFILE-MISMATCH")
    assert r.snap()["error"]["state"] == "INITIALIZING" and r.snap()["error"]["active"] is True


def test_init_failed_code_for_unknown_profile_in_emulator():
    r = Rig(auto_recover=False)
    r.emu.profile_id = "something-else"
    r.until(S.FAULT, 3)
    assert r.snap()["error"]["code"] == "MOD-INIT-FAILED"


def test_init_timeout_is_profile_init_timeout():
    r = Rig(auto_recover=False)
    r.until(S.DETECTED, 2)
    r.lab("slow-init")
    r.until(S.INITIALIZING, 1)
    t = r.until(S.FAULT, 10)
    assert 4.9 <= t <= 5.2
    assert r.transitions()[-1][2:] == ("init-timeout", "MOD-INIT-TIMEOUT")


def test_ready_iface_fault_and_no_response():
    r = ready_rig(auto_recover=False)
    r.lab("iface-error")
    t = r.until(S.FAULT, 3)
    assert t <= 1.2 and r.transitions()[-1][2:] == ("iface-fault", "MOD-IFACE-ERROR")
    r = ready_rig(auto_recover=False)
    r.lab("hang")
    t = r.until(S.FAULT, 6)
    assert 2.0 <= t <= 3.2 and r.transitions()[-1][2:] == ("no-response", "MOD-NO-RESPONSE")


@pytest.mark.parametrize("state", [S.DISCONNECTED, S.DETECTED, S.INITIALIZING, S.READY, S.RECOVERING])
def test_channel_down_from_any_active_state(state):
    r = Rig(auto_recover=False, auto_init=state != S.DETECTED)
    if state == S.DISCONNECTED:
        r.run(0.5)
        r.lab("detach")
    elif state == S.RECOVERING:
        r.until(S.READY, 5)
        r.m.command("reinit", {})
    r.until(state, 5) if r.m.state != state else None
    assert r.m.state == state
    r.lab("crash")
    r.run(0.2)
    assert r.m.state == S.FAULT and r.transitions()[-1][2:] == ("channel-down", "EMU-EXITED")
    assert r.snap()["backend"]["last_exit"] == 70 and r.snap()["identity"] is None


def test_channel_down_keepalive_and_start_failed():
    r = ready_rig(auto_recover=False)
    r.be.freeze()
    r.until(S.FAULT, 6)
    assert r.transitions()[-1][2:] == ("channel-down", "EMU-NO-KEEPALIVE")
    r = Rig(start_ok=False, auto_recover=False)
    r.run(0.2)
    assert r.transitions()[-1][1:] == ("FAULT", "start-failed", "EMU-START-FAILED")


def test_factory_error_is_start_failed():
    def bad(_s):
        raise ModuleError("backend 'hardware' is not available in S6")
    m = ModuleManager(ModuleSettings(auto_recover=False), bad, clock=SimClock(), strict=True)
    m.start()
    assert m.state == S.FAULT and m.snapshot()["error"]["code"] == "EMU-START-FAILED"
    assert "not available" in m.snapshot()["error"]["text"]


def test_reinit_from_ready_and_disconnected_goes_recovering():
    r = ready_rig()
    assert r.m.command("reinit", {}) == "ok"
    assert r.transitions()[-1][1:3] == ("RECOVERING", "cmd-reinit")
    r.until(S.READY, 5)
    assert [x[1] for x in r.transitions()[-4:]] == ["RECOVERING", "DETECTED", "INITIALIZING", "READY"]
    r.lab("detach")
    r.run(0.3)
    assert r.m.command("reinit", {}) == "ok"
    assert r.m.state == S.RECOVERING
    r.run(0.3)
    assert r.m.state == S.DISCONNECTED and r.transitions()[-1][1:3] == ("DISCONNECTED", "absent")
    assert r.be.starts == 1                              # канал жив — процесс не перезапускался


def test_reinit_in_initializing_and_recovering_restarts_without_transition():
    r = Rig(auto_recover=False)
    r.until(S.DETECTED, 2)
    r.lab("slow-init")
    r.until(S.INITIALIZING, 1)
    seq = r.m.seq
    assert r.m.command("reinit", {}) == "ok"
    assert r.m.seq == seq and r.m.state == S.INITIALIZING
    t = r.until(S.READY, 3)                              # новая инициализация — обычная (slow-init израсходован)
    assert t <= 1.2


def test_recover_timeout():
    r = ready_rig(auto_recover=False)
    r.lab("detach")
    r.run(0.3)
    r.be.frozen = True                                   # канал «жив» для менеджера, но молчит
    r.be._last_alive = 1e9                               # без keepalive-отказа: проверяем таймаут восстановления
    r.m.command("reinit", {})
    t = r.until(S.FAULT, 12)
    assert 9.9 <= t <= 10.2 and r.transitions()[-1][2:] == ("recover-timeout", "MOD-RECOVER-TIMEOUT")


# ---------- восстановление, backoff, бюджет ----------
def test_backoff_sequence_and_reset_on_ready():
    r = ready_rig()
    r.lab("iface-error")
    r.until(S.FAULT, 3)
    attempts = []
    t_fault = r.clock()
    for _ in range(5):
        dt = r.until(S.RECOVERING, 40)
        attempts.append(round(r.clock() - t_fault, 1))
        r.until(S.FAULT, 5)
        t_fault = r.clock()
    assert attempts == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert r.snap()["recovery"]["attempt"] == 6 and r.snap()["recovery"]["next_s"] == 30.0
    r.lab("heal")
    r.until(S.READY, 40)
    assert r.snap()["recovery"] == {"auto": True, "attempt": 0, "next_s": None, "exhausted": False}
    e = r.snap()["error"]
    assert e["code"] == "MOD-IFACE-ERROR" and e["active"] is False      # последняя ошибка хранится
    assert r.be.starts == 1                              # ошибки MOD-* процесс не перезапускают и бюджет не тратят
    assert r.snap()["backend"]["restarts"] == 0


def test_auto_recover_false_waits_for_command():
    r = ready_rig(auto_recover=False)
    r.lab("iface-error")
    r.until(S.FAULT, 3)
    r.run(60)
    assert set(r.seen[-600:]) == {S.FAULT} and r.snap()["recovery"]["next_s"] is None
    r.lab("heal")
    assert r.m.command("reset", {}) == "ok"
    assert r.transitions()[-1][1:3] == ("RECOVERING", "cmd-reset")
    r.until(S.READY, 5)


def test_crash_restarts_process_and_spends_budget():
    r = ready_rig()
    r.lab("crash")
    t = r.until(S.READY, 15)
    assert t <= 4.0
    path = [x[1:4] for x in r.transitions()[-5:]]
    assert path[0] == ("FAULT", "channel-down", "EMU-EXITED") and path[1][:2] == ("RECOVERING", "recover-attempt")
    assert r.be.starts == 2 and r.snap()["backend"]["restarts"] == 1


def test_restart_budget_three_in_60s_then_unavailable_and_reset():
    r = ready_rig()
    for i in range(3):
        r.lab("crash")
        r.until(S.READY, 15)
    assert r.snap()["backend"]["restarts"] == 3
    r.lab("crash")                                        # четвёртая авария за 60 с
    r.until(S.FAULT, 1)
    r.run(15)
    s = r.snap()
    assert s["state"] == "FAULT" and s["error"]["code"] == "EMU-UNAVAILABLE"
    assert s["backend"]["state"] == "unavailable" and s["recovery"]["exhausted"] and s["recovery"]["next_s"] is None
    assert r.transitions()[-1][1:] == ("FAULT", "restart-limit", "EMU-UNAVAILABLE")
    assert any("emulator restart limit reached: 3 in 60 s" in x for x in r.logs)
    starts = r.be.starts
    r.run(120)                                            # окно прошло — автоматических попыток всё равно нет
    assert r.be.starts == starts and r.m.state == S.FAULT
    assert r.m.command("reset", {}) == "ok"
    r.until(S.READY, 5)
    s = r.snap()
    assert s["recovery"]["exhausted"] is False and s["backend"]["restarts"] == 1 and s["backend"]["state"] == "running"


def test_budget_window_slides():
    clock = SimClock()
    b = RestartBudget(3, 60.0, clock)
    assert [b.try_spend() for _ in range(4)] == [True, True, True, False]
    clock.advance(59.9)
    assert not b.try_spend() and b.used() == 3
    clock.advance(0.2)
    assert b.used() == 0 and b.try_spend()
    b.reset()
    assert b.used() == 0


def test_emu_unavailable_then_available_and_reset():
    r = ready_rig()
    assert r.lab("emu-unavailable") == "ok"
    r.run(0.2)
    assert r.transitions()[-1][1:] == ("FAULT", "channel-down", "EMU-EXITED")
    t = r.until(S.FAULT, 1) if r.m.state != S.FAULT else 0
    deadline = r.clock() + 30
    while not (r.m.state == S.FAULT and r.snap()["error"]["code"] == "EMU-UNAVAILABLE"):
        r.run(DT)
        assert r.clock() < deadline, r.logs[-10:]
    codes = [x[3] for x in r.transitions() if x[1] == "FAULT"]
    assert codes[-4:] == ["EMU-START-FAILED"] * 3 + ["EMU-UNAVAILABLE"]
    assert r.be.starts == 4 and r.snap()["backend"]["restarts"] == 3
    r.run(30)                                             # удержание: попыток нет
    assert r.be.starts == 4
    assert r.lab("emu-available") == "ok" and r.m.state == S.FAULT
    r.run(5)
    assert r.m.state == S.FAULT                           # состояние не меняется — дальше команда reset
    r.m.command("reset", {})
    r.until(S.READY, 5)
    del t


def test_lab_not_running_and_forbidden():
    r = ready_rig()
    r.lab("emu-unavailable")
    r.run(0.2)
    assert r.lab("detach") == "not-running"
    assert r.lab("emu-available") == "ok"                 # родительские сценарии — при любом канале

    class NoLab(InlineEmulatorBackend):
        supports_lab = False
        kind = "hardware"
    clock = SimClock()
    be = NoLab(JrModuleEmulator(clock), clock=clock, timings=Timings())
    m = ModuleManager(ModuleSettings(), lambda s: be, clock=clock, strict=True)
    m.start()
    for _ in range(30):
        clock.advance(DT)
        m.tick()
    assert m.state == S.READY
    sent = len(be.sent)
    for s in ("detach", "crash", "emu-unavailable"):
        assert m.command("lab", {"scenario": s}) == "forbidden"
    m.tick()
    assert m.state == S.READY and not [x for x in be.sent[sent:] if x[0] == "lab"] and not be.refuse
    assert m.snapshot()["backend"]["lab"] is False


def test_commands_when_disabled():
    r = Rig(enabled=False)
    for op, args in (("reinit", {}), ("reset", {}), ("lab", {"scenario": "detach"})):
        assert r.m.command(op, args) == "disabled"
    assert r.m.command("status", {}) == "ok" and r.m.command("configure", {"enabled": True}) == "unknown-op"


# ---------- настройки ----------
def test_disable_stops_backend_and_enable_restarts():
    r = ready_rig()
    r.m.apply_settings(ModuleSettings(enabled=False))
    assert r.m.state == S.DISABLED and r.transitions()[-1][2] == "settings-disabled"
    assert not r.be.running and r.snap()["backend"]["state"] == "stopped"
    r.run(5)
    assert r.m.state == S.DISABLED
    r.m.apply_settings(ModuleSettings(enabled=True))
    assert r.transitions()[-1][1:3] == ("DISCONNECTED", "settings-enabled")
    r.until(S.READY, 5)


def test_disable_from_fault_and_from_unavailable():
    r = ready_rig(auto_recover=False)
    r.lab("iface-error")
    r.until(S.FAULT, 3)
    r.m.apply_settings(ModuleSettings(enabled=False, auto_recover=False))
    assert r.m.state == S.DISABLED and r.snap()["error"]["active"] is False
    r.m.apply_settings(ModuleSettings(enabled=True, auto_recover=False))
    r.until(S.READY, 5)                                  # новый процесс: лабораторные флаги сброшены


def test_auto_recover_toggle_in_fault():
    r = ready_rig(auto_recover=False)
    r.lab("iface-error")
    r.until(S.FAULT, 3)
    r.lab("heal")
    r.m.apply_settings(ModuleSettings(auto_recover=True))
    assert r.snap()["recovery"]["next_s"] == 1.0
    r.until(S.READY, 5)


# ---------- лабораторные сценарии 6.4 ----------
def test_lab_scenarios_reactions():
    r = ready_rig()
    r.lab("detach")
    assert r.until(S.DISCONNECTED, 1) <= 0.2
    r.lab("attach")
    assert r.until(S.READY, 5) <= 1.5
    r.lab("hang")
    r.until(S.FAULT, 5)
    assert r.snap()["error"]["code"] == "MOD-NO-RESPONSE"
    r.until(S.RECOVERING, 3)
    r.until(S.FAULT, 8)
    assert r.snap()["error"]["code"] == "MOD-INIT-TIMEOUT"   # повтор восстановления при hang
    r.lab("heal")
    r.until(S.READY, 10)
    r.lab("slow-init")
    r.run(3)
    assert r.m.state == S.READY                          # действует только на следующую инициализацию
    r.m.command("reinit", {})
    r.until(S.FAULT, 8)
    assert r.snap()["error"]["code"] == "MOD-INIT-TIMEOUT"
    r.until(S.READY, 5)


def test_output_is_none_in_every_tick_of_script_full():
    r = Rig()
    t_end = SCRIPT_FULL[-1][0] + 10
    events = list(SCRIPT_FULL)
    while r.clock() < t_end:
        while events and events[0][0] <= r.clock() + 1e-9:
            _, sc, _ = events.pop(0)
            if sc:
                r.lab(sc)
        r.clock.advance(DT)
        r.m.tick()
        assert r.snap()["output"] == "none"


def run_script(rig):
    script = SCRIPT_FULL + [(SCRIPT_FULL[-1][0] + 10, None, None)]
    results = []
    for (t, scenario, expected), (t_next, _, _) in zip(script, script[1:]):
        while rig.clock() < t - 1e-9:
            rig.run(DT)
        if scenario:
            assert rig.lab(scenario) == "ok"
        seen = {rig.m.state, *rig.run(t_next - t)}
        results.append((t, scenario, expected, expected in seen))
    return results


def test_script_full_gives_expected_states():
    r = Rig()
    res = run_script(r)
    assert all(ok for *_, ok in res), [x for x in res if not x[3]]
    assert r.m.state == S.READY
    codes = [x[3] for x in r.transitions() if x[1] == "FAULT"]
    assert codes == ["MOD-IFACE-ERROR", "MOD-IFACE-ERROR", "MOD-NO-RESPONSE", "MOD-INIT-TIMEOUT", "MOD-INIT-TIMEOUT",
                     "EMU-EXITED"]
    assert r.snap()["backend"]["restarts"] == 1


def test_same_run_same_history():
    def once():
        r = Rig()
        run_script(r)
        return r.transitions(), [(h["seq"], h["t"]) for h in r.m.history], r.logs
    assert once() == once()


def test_history_snapshot_newest_first_and_bounded():
    r = Rig()
    run_script(r)
    h = r.snap()["history"]
    assert len(h) == 8 and h[0]["seq"] == r.m.seq and [x["seq"] for x in h] == list(range(r.m.seq, r.m.seq - 8, -1))
    assert r.snap()["diag"]["transitions"] == r.m.seq and r.snap()["diag"]["faults"] == 6


def test_internal_error_is_contained_by_service():
    """Исключение внутри подсистемы (раздел 7.3): ModuleService его перехватывает, журнал — одна строка, FAULT
    (internal-error), канал перезапускается; status() и health() продолжают отвечать."""
    from sheregesh.module.service import ModuleService
    clock = SimClock()
    be = InlineEmulatorBackend(JrModuleEmulator(clock), clock=clock, timings=Timings())
    logs = []
    svc = ModuleService(None, clock=clock, log=logs.append, backend_factory=lambda s: be)
    svc.start()
    for _ in range(30):
        clock.advance(DT)
        svc.tick()
    assert svc.manager.state == S.READY
    real_poll, broken = be.poll, [3]

    def poll():
        if broken[0]:
            broken[0] -= 1
            raise RuntimeError("boom")
        return real_poll()
    be.poll = poll
    for _ in range(3):
        clock.advance(DT)
        svc.tick()
    assert svc.manager.state == S.FAULT and svc.status()["error"]["code"] == "EMU-EXITED"
    assert any(h["reason"] == "internal-error" for h in svc.status()["history"])
    assert sum("internal error RuntimeError: boom" in x for x in logs) == 1
    for _ in range(50):
        clock.advance(DT)
        svc.tick()
    assert svc.manager.state == S.READY and svc.health()["state"] == "READY" and svc.health()["restarts"] == 1
