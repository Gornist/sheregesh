# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 JR на loopback (L3, ADR-0012 §13): «пульт ↔ блок» двумя настоящими процессами, как на стенде, без плат.

Блок — процесс `python -m sheregesh_remote --module --remote-config-dir … --status-file …` (подсистема модуля
с лабораторным эмулятором дочерним процессом); пульт — процесс `python -m sheregesh_console --peer 127.0.0.1:PORT`
offscreen с SHEREGESH_UI_STATE (то, что показывает QML страницы «Модуль») и локальным каналом управления; команды —
`python -m sheregesh.link.modulectl … --via console`, как в tools/probe/s6_jr_accept.py на pi1. Суждения —
функциями того же сценария приёмки (judge_*), чтобы loopback и стенд судили одинаково.

Проверяются: JR-01…JR-09 (эмулятор — потомок агента, профиль Nomad, reinit → READY, «Готов» на экране, detach/attach,
iface-error/hang → heal, crash и SIGKILL эмулятора, emu-unavailable → reset, перезапуск блока — пульт сам
переподключается и снова видит READY, настройки переживают перезапуск блока), отказы §12.4 (перезапуск пульта —
состояние только из телеметрии; настройки отсутствуют / повреждены / только для чтения; блок старого протокола 1.2 —
«не поддерживает»), инвариант output = "none" во всех снимках status.module.

Номера JR — критерии спринта, не TEST-ID реестра: маркер test_id не ставится. Это не проверка стенда: там —
s6_jr_accept.py на pi1+pi2 (docs/acceptance/s6-jr/PLAN.md).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from procs import ENV, free_port

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/probe"))
sys.path.insert(0, str(ROOT / "tools/deploy/ota-check"))
import s6_jr_accept as J  # noqa: E402

CENV = {**ENV, "PYTHONPATH": os.pathsep.join(str(ROOT / p) for p in ("software", "software/emulators",
                                                                     "software/console", "software/remote")),
        "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software", "PYTHONUNBUFFERED": "1"}
SLOW = 1.5                    # запас на медленный раннер CI поверх пределов ADR §11 (замер всё равно пишется)


class Poll:
    """Выборка JSON-файла (ui-state пульта или link-status блока) раз в 0,1 с: строки (mono, wall, doc) —
    как у e2e_run.Sampler, чтобы работали суждения s6_jr_accept."""

    def __init__(self, path):
        self.path, self.rows, self.lock, self.stop_ev = Path(path), [], threading.Lock(), threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop_ev.is_set():
            try:
                doc = json.loads(self.path.read_text())
            except (OSError, ValueError):
                doc = None
            if isinstance(doc, dict):
                with self.lock:
                    self.rows.append((time.monotonic(), time.time(), doc))
            self.stop_ev.wait(0.1)

    def since(self, t0):
        with self.lock:
            return [r for r in self.rows if r[0] >= t0]

    def last(self):
        with self.lock:
            return self.rows[-1] if self.rows else None

    def stop(self):
        self.stop_ev.set()


class Loop:
    """Блок и пульт отдельными процессами на 127.0.0.1, команды через локальный канал пульта."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.sockdir = tempfile.mkdtemp(prefix="s6l-")          # 0700; путь короче предела sun_path (macOS)
        self.sock = os.path.join(self.sockdir, "console.sock")
        self.port = free_port()
        self.rc = tmp / "remote-config"
        self.ui_path, self.ls_path = tmp / "ui-state.json", tmp / "link-status.json"
        self.agent = self.console = None
        self.ui, self.rs = Poll(self.ui_path), Poll(self.ls_path)
        self.lock = threading.Lock()
        self.outputs = []              # (state, output) каждого снимка status.module
        self.stop_ev = threading.Event()
        self.files = []                # журналы процессов (tmp/agent.log, tmp/console.log)
        threading.Thread(target=self._watch_output, daemon=True).start()

    # ---------- процессы ----------
    def start_agent(self, *extra, config=True):
        args = [sys.executable, "-m", "sheregesh_remote", "--port", str(self.port), "--module",
                "--status-file", str(self.ls_path), *(["--remote-config-dir", str(self.rc)] if config else []), *extra]
        log = open(self.tmp / "agent.log", "a")
        self.files.append(log)
        p = subprocess.Popen(args, env=ENV, stdout=subprocess.PIPE, stderr=log, text=True, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            line = p.stdout.readline()
            if not line or line.startswith("LISTENING"):
                break
        assert p.poll() is None, (self.tmp / "agent.log").read_text()[-2000:]
        threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()   # не дать трубе заполниться
        self.agent, self.agent_started = p, time.time()
        return p

    def stop_agent(self, sig=signal.SIGTERM):
        p, self.agent = self.agent, None
        if p is None or p.poll() is not None:
            return
        os.kill(p.pid, sig)                       # только агенту: эмулятор обязан уйти сам (pdeathsig / EOF)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()

    def start_console(self):
        # тексты страниц «Модуль»/«Управление» в ui-state — у интерфейса вкладок (Minimal их не создаёт)
        env = {**CENV, "SHEREGESH_UI_STATE": str(self.ui_path), "SHEREGESH_CONSOLE_CONTROL": self.sock,
               "SHEREGESH_UI": "classic"}
        log = open(self.tmp / "console.log", "a")
        self.files.append(log)
        self.console = subprocess.Popen(
            [sys.executable, "-m", "sheregesh_console", "--peer", f"127.0.0.1:{self.port}", "--control-socket",
             self.sock, "--data-dir", str(self.tmp / "console-data")],
            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.console_started = time.time()
        ok = wait_for(lambda: os.path.exists(self.sock) and self.ui_path.exists(), 30)
        assert ok and self.console.poll() is None, (self.tmp / "console.log").read_text()[-3000:]
        return self.console

    def stop_console(self):
        p, self.console = self.console, None
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def close(self):
        self.stop_ev.set()
        self.stop_console()
        self.stop_agent()
        self.ui.stop()
        self.rs.stop()
        for f in self.files:
            f.close()
        shutil.rmtree(self.sockdir, ignore_errors=True)

    # ---------- команды ----------
    def ctl(self, *args, timeout=90):
        """modulectl --via console (тот же вызов, что сценарий приёмки на pi1) → (код, ответ)."""
        env = {**ENV, "SHEREGESH_CONSOLE_CONTROL": self.sock}
        with self.lock:
            r = subprocess.run([sys.executable, "-m", "sheregesh.link.modulectl", *args, "--via", "console"],
                               env=env, capture_output=True, text=True, timeout=timeout)
        d = J.parse_cli(r.stdout + r.stderr)
        self._note(d)
        return r.returncode, d

    def lab(self, scenario):
        rc, d = self.ctl("lab", scenario, "--wait", "15")
        return d.get("result")

    def status(self):
        return J.telemetry(self.ctl("status")[1])

    def _note(self, d):
        t = J.telemetry(d)
        if t:
            self.outputs.append((t.get("state"), t.get("output")))

    def _watch_output(self):
        """Снимок status.module через сокет пульта раз в 0,3 с (между командами): output во всех состояниях,
        включая короткие (RECOVERING, INITIALIZING)."""
        while not self.stop_ev.wait(0.3):
            if not os.path.exists(self.sock) or not self.lock.acquire(blocking=False):
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(3)
                    s.connect(self.sock)
                    s.sendall(b'{"cmd": "module", "op": "status"}\n')
                    self._note(json.loads(s.makefile().readline() or "{}"))
            except (OSError, ValueError):
                pass
            finally:
                self.lock.release()

    # ---------- наблюдение ----------
    def wait(self, poll, pred, timeout, t0=None):
        t0 = time.monotonic() if t0 is None else t0
        end = time.monotonic() + timeout
        while True:
            dt, doc = J.first_row(poll.since(t0), t0, pred)
            if dt is not None or time.monotonic() >= end:
                return dt, doc
            time.sleep(0.05)

    def relink(self, pred, timeout, t0):
        """После перезапуска блока: «нет связи» на пульте, затем pred уже в новом сеансе (выборки прежнего сеанса
        не в счёт — так же судит s6_jr_accept.wait_relink). → секунды от t0 или None."""
        nl, _ = self.wait(self.ui, lambda x: J.ui_mod(x).get("support") == "no-link", timeout, t0)
        if nl is None:
            return None
        dt, _ = self.wait(self.ui, pred, timeout, t0 + nl)
        return None if dt is None else round(nl + dt, 2)

    def unit(self):
        r = self.rs.last()
        return J.rs_mod(r[2]) if r else {}

    def uimod(self):
        r = self.ui.last()
        return J.ui_mod(r[2]) if r else {}

    def procs(self, scoped=True):
        """Снимок процессов в формате s6_jr_accept.parse_procs (MainPID — процесс агента). scoped — только агент
        и его потомки: на машине разработчика параллельно идут чужие тесты со своими эмуляторами."""
        # -ww: без терминала procps обрезает строку до 80 символов (CI: длинный путь к python отрезал имя модуля)
        ps = subprocess.run(["ps", "-ww", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
        pid = self.agent.pid if self.agent else 0
        snap = J.parse_procs(f"MainPID={pid} NRestarts=0 ActiveState={'active' if pid else 'inactive'}\n{ps}")
        if scoped:
            mine = {p["pid"] for p in snap["procs"] if p["pid"] == pid or pid in J.ancestors(p["pid"], snap)}
            snap["procs"] = [p for p in snap["procs"] if p["pid"] in mine]
        return snap

    def emulators(self, agent_pid=None):
        """Эмуляторы этого стенда: потомки агента agent_pid (без agent_pid — все эмуляторы в системе)."""
        snap = self.procs(scoped=False)
        pids = J.emulator_pids(snap)
        if agent_pid is None:
            return pids
        return [e for e in pids if agent_pid in J.ancestors(e, snap)]

    def check_output(self, states=()):
        self.ctl("status")                                   # снимок конечного состояния — тоже в счёт
        bad = [x for x in self.outputs if x[1] != "none"]
        assert self.outputs and not bad, bad[:5]
        assert set(states) <= {s for s, _o in self.outputs}, (states, sorted({s for s, _o in self.outputs}))


def wait_for(pred, timeout, step=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return bool(pred())


@pytest.fixture
def loop(tmp_path):
    lp = Loop(tmp_path)
    yield lp
    lp.close()


def ready_ui(lp, timeout=J.LIMITS["ready_s"] * SLOW, t0=None, after=None):
    """«Готов» на экране пульта (support ok); after — только документы, записанные позже (новый процесс пульта)."""
    return lp.wait(lp.ui, lambda d: J.ui_state_is("READY", J.STATE_TEXT["READY"])(d) and
                   (after is None or (d.get("time") or 0) > after), timeout, t0)


def up(lp, *extra, config=True):
    lp.start_agent(*extra, config=config)
    lp.start_console()
    dt, _ = ready_ui(lp, 30)
    assert dt is not None, (lp.uimod(), (lp.tmp / "agent.log").read_text()[-2000:])


# ---------------------------------------------------------------- JR-01…JR-07


def test_jr01_to_jr07_lab_scenarios_through_console(loop):
    """attach → READY → detach → attach → iface-error → heal → hang → heal: экран пульта и блок, суждения сценария."""
    up(loop)
    # JR-01: эмулятор — потомок агента, один, backend running
    v, d = J.judge_emulator(loop.procs(), loop.unit())
    assert v == "PASS", d
    # JR-02: профиль Nomad в телеметрии через пульт и на экране, плашка «лабораторный эмулятор»
    rc, st = loop.ctl("status")
    r = loop.ui.last()
    v, d = J.judge_profile(st, r[2])
    assert rc == 0 and v == "PASS", (d, J.ui_mod(r[2]))
    assert J.LAB_BANNER in str(st["module"].get("display") or {}) or J.ui_mod(r[2])["labBanner"] is True
    # JR-03/04: reinit --expect READY; «Готов» на экране не позже 2 с после READY блока
    seq0 = loop.status().get("seq") or 0
    t0 = time.monotonic()
    rc, d = loop.ctl("reinit", "--wait", "15", "--expect", "READY", "--expect-timeout", "15")
    m = {"rc": rc, "result": d.get("result"), "ready_s": loop.wait(loop.rs, J.rs_state_is("READY", seq_gt=seq0),
                                                                    15, t0)[0]}
    # status пульта отстаёт от файла состояния блока (rs) на период телеметрии: ждём перехода в истории, а не
    # берём первый снимок (CI 40320233228: снимок без INITIALIZING → READY)
    t = {}

    def ready_in_history():
        t.update(loop.status())
        return J.has_transition(J.history_after(t.get("history"), seq0), "INITIALIZING", "READY", "init-ok")
    wait_for(ready_in_history, 5, step=0.5)
    m.update(history=J.history_after(t.get("history"), seq0), init_ms=(t.get("diag") or {}).get("init_ms"))
    v, dd = J.judge_init(m)
    assert v == "PASS", (dd, m)
    g = {"rs_ready_s": m["ready_s"], "timeout_s": 17,
         "ui_ready_s": loop.wait(loop.ui, lambda doc: J.ui_state_is("READY", "Готов")(doc) and
                                 (J.ui_mod(doc).get("seq") or 0) > seq0, 17, t0)[0]}
    v, dd = J.judge_gui_ready(g)
    assert v == "PASS", (dd, g)
    # JR-05: detach → «Модуль отсутствует», attach → «Готов»
    t0 = time.monotonic()
    m = {"detach_result": loop.lab("detach")}
    m["absent_s"] = loop.wait(loop.ui, J.ui_state_is("DISCONNECTED", J.STATE_TEXT["DISCONNECTED"]), 8, t0)[0]
    t1 = time.monotonic()
    m["attach_result"] = loop.lab("attach")
    m["ready_s"] = ready_ui(loop, 20, t1)[0]
    v, dd = J.judge_detach(m)
    assert v == "PASS", (dd, m)
    # JR-06/07: iface-error → «Ошибка интерфейса» + MODULE-FAULT, heal → READY через RECOVERING
    seq0 = loop.status().get("seq") or 0
    t0 = time.monotonic()
    m6 = {"scenario": "iface-error", "result": loop.lab("iface-error")}
    dt, doc = loop.wait(loop.ui, lambda x: J.ui_mod(x).get("state") == "FAULT" and
                        J.CODE_TEXT["MOD-IFACE-ERROR"] in str(J.ui_mod(x).get("errorText") or ""), 8, t0)
    m6.update(fault_s=dt, state_text=J.ui_mod(doc).get("stateText") if doc else None,
              fault_event_s=loop.wait(loop.ui, lambda x: J.ui_mod(x).get("faultEvent") is True, 8, t0)[0])
    v, dd = J.judge_fault_shown(m6, "iface")
    assert v == "PASS", (dd, m6)
    t1 = time.monotonic()
    m7 = {"result": loop.lab("heal")}
    m7["ready_s"] = loop.wait(loop.rs, J.rs_state_is("READY", seq_gt=seq0), J.LIMITS["recover_s"] * SLOW, t1)[0]
    m7["ui_ready_s"] = loop.wait(loop.ui, lambda x: J.ui_state_is("READY", "Готов")(x) and
                                 J.ui_mod(x).get("faultEvent") is False, 25, t1)[0]
    m7["error_text"] = loop.uimod().get("errorText")
    t = loop.status()
    m7.update(history=J.history_after(t.get("history"), seq0), error=t.get("error"))
    v, dd = J.judge_recovered(m7, {"MOD-IFACE-ERROR"})
    assert v == "PASS", (dd, m7)
    # JR-06/07 hang: первый FAULT — MOD-NO-RESPONSE; heal → READY
    seq0 = loop.status().get("seq") or 0
    t0 = time.monotonic()
    m6 = {"scenario": "hang", "result": loop.lab("hang")}
    dt, doc = loop.wait(loop.rs, lambda x: J.rs_mod(x).get("state") == "FAULT" and
                        (J.rs_mod(x).get("seq") or 0) > seq0, 10, t0)
    m6.update(fault_s=dt, ui_fault_s=loop.wait(loop.ui, lambda x: J.ui_mod(x).get("state") == "FAULT", 10, t0)[0])
    m6["first_code"] = J.first_fault_code(J.history_after(loop.status().get("history"), seq0)) or \
        (J.rs_mod(doc).get("error_code") if doc else None)
    v, dd = J.judge_fault_shown(m6, "hang")
    assert v == "PASS", (dd, m6)
    t1 = time.monotonic()
    m7 = {"result": loop.lab("heal")}
    m7["ready_s"] = loop.wait(loop.rs, J.rs_state_is("READY", seq_gt=seq0), J.LIMITS["recover_s"] * SLOW, t1)[0]
    m7["ui_ready_s"] = ready_ui(loop, 30, t1)[0]
    m7["error_text"] = loop.uimod().get("errorText")
    t = loop.status()
    m7.update(history=J.history_after(t.get("history"), seq0), error=t.get("error"))
    v, dd = J.judge_recovered(m7, {"MOD-NO-RESPONSE", "MOD-INIT-TIMEOUT"})
    assert v == "PASS", (dd, m7)
    loop.check_output(states=("READY", "DISCONNECTED", "FAULT"))


# ---------------------------------------------------------------- JR-08


def crash(loop, how):
    u0 = loop.unit()
    m = {"pid0": u0.get("pid"), "restarts0": u0.get("restarts"), "seq0": u0.get("seq") or 0}
    t0 = time.monotonic()
    if how == "crash":
        assert loop.lab("crash") == "ok"
    else:
        emus = loop.emulators(loop.agent.pid)
        assert len(emus) == 1, emus
        os.kill(emus[0], signal.SIGKILL)                    # «служба эмулятора аварийно завершилась» извне
    dt, doc = loop.wait(loop.rs, lambda x: J.rs_mod(x).get("state") == "FAULT" and
                        (J.rs_mod(x).get("seq") or 0) > m["seq0"], 8, t0)
    m["fault_code"] = J.rs_mod(doc).get("error_code") if doc else \
        J.first_fault_code(J.history_after(loop.status().get("history"), m["seq0"]))
    dt, doc = loop.wait(loop.rs, J.rs_state_is("READY", seq_gt=m["seq0"]), J.LIMITS["crash_recover_s"] * SLOW, t0)
    m["ready_s"] = dt
    u1 = J.rs_mod(doc) if doc else loop.unit()
    m.update(pid1=u1.get("pid"), restarts1=u1.get("restarts"), emulators=len(loop.emulators(loop.agent.pid)))
    return J.judge_crash(m), m


def test_jr08_emulator_crash_sigkill_and_unavailable(loop):
    """(a) lab crash, (b) SIGKILL процесса эмулятора → EMU-EXITED → READY, новый PID, restarts +1;
    (c) emu-unavailable → EMU-UNAVAILABLE, без процессов и попыток, emu-available + reset → READY."""
    up(loop)
    for how in ("crash", "kill"):
        (v, d), m = crash(loop, how)
        assert v == "PASS", (how, d, m)
    t0 = time.monotonic()
    m = {"hold_s": 3.0, "journal_ok": False, "journal_started": None}
    assert loop.lab("emu-unavailable") == "ok"
    dt, doc = loop.wait(loop.rs, J.rs_state_is("FAULT", code="EMU-UNAVAILABLE"), J.LIMITS["unavailable_s"], t0)
    m["unavail_s"] = dt
    assert dt is not None, loop.unit()
    r0, th = J.rs_mod(doc).get("restarts"), time.monotonic()
    during = []
    while time.monotonic() - th < m["hold_s"]:
        during += loop.emulators(loop.agent.pid)
        time.sleep(0.3)
    rows = [J.rs_mod(x) for _m, _w, x in loop.rs.since(th)]
    m.update(procs_during=sorted(set(during)), restarts_hold=sorted({x.get("restarts") for x in rows}),
             running_seen=any(x.get("backend_state") == "running" for x in rows))
    m["restarts_grew"] = any((x or 0) > (r0 or 0) for x in m["restarts_hold"])
    ui = loop.uimod()
    assert ui.get("state") == "FAULT" and J.CODE_TEXT["EMU-UNAVAILABLE"] in str(ui.get("errorText")), ui
    t1 = time.monotonic()
    m["available_result"] = loop.lab("emu-available")
    m["reset_result"] = loop.ctl("reset", "--wait", "15")[1].get("result")
    m["ready_s"] = loop.wait(loop.rs, J.rs_state_is("READY"), J.LIMITS["ready_s"] * SLOW, t1)[0]
    v, d = J.judge_unavailable(m)
    assert v == "PASS", (d, m)
    loop.check_output(states=("READY", "FAULT"))


def test_jr08_unit_restart_console_reconnects_without_action(loop):
    """Блок убит SIGKILL: эмулятор уходит вместе с ним (сирот нет), пульт — «нет связи», а не «Готов» по старым
    данным; новый процесс блока с тем же каталогом настроек — пульт сам переподключается и снова видит READY."""
    up(loop)
    old_agent, old_emus = loop.agent.pid, loop.emulators(loop.agent.pid)
    assert len(old_emus) == 1
    t0 = time.monotonic()
    loop.stop_agent(signal.SIGKILL)
    assert wait_for(lambda: not set(old_emus) & set(J.emulator_pids(loop.procs(scoped=False))), 10), \
        f"эмулятор {old_emus} пережил агент"
    dt, _ = loop.wait(loop.ui, lambda x: J.ui_mod(x).get("support") == "no-link", 10, t0)
    assert dt is not None, loop.uimod()
    nolink_at = time.monotonic()
    time.sleep(2)
    stale_ready = [x for _m, _w, x in loop.ui.since(nolink_at) if J.ui_state_is("READY")(x)]
    assert not stale_ready, "при потере блока пульт показывал «Готов» по старым данным"
    t1 = time.monotonic()
    loop.start_agent()
    dt, _ = ready_ui(loop, J.LIMITS["service_restart_s"], t1)
    assert dt is not None, loop.uimod()
    emus = loop.emulators(loop.agent.pid)
    assert len(emus) == 1 and not set(emus) & set(old_emus) and loop.agent.pid != old_agent
    loop.stop_agent(signal.SIGTERM)                          # штатная остановка — тоже без сирот
    assert wait_for(lambda: not set(emus) & set(J.emulator_pids(loop.procs(scoped=False))), 10)
    loop.check_output(states=("READY",))


# ---------------------------------------------------------------- JR-09


def test_jr09_settings_survive_unit_restart(loop):
    """configure enabled=false → перезапуск блока → DISABLED, процессов нет, ревизия та же, source store →
    enabled=true → READY."""
    up(loop)
    rc, d = loop.ctl("configure", "enabled=false", "--wait", "30", "--expect", "DISABLED", "--expect-timeout", "15")
    assert rc == 0 and d.get("result") == "ok", d
    m = {"off_result": "ok", "revision": (loop.status().get("settings") or {}).get("revision"), "events": []}
    assert wait_for(lambda: not loop.emulators(loop.agent.pid), 5), "DISABLED: бэкенд не остановлен"
    t0 = time.monotonic()
    loop.stop_agent()
    loop.start_agent()
    ev = {"event": "перезапуск блока", "timeout_s": 30}
    ev["disabled_s"] = ev["ui_disabled_s"] = loop.relink(J.ui_state_is("DISABLED", J.STATE_TEXT["DISABLED"]), 30, t0)
    time.sleep(1)
    t = loop.status()
    st = t.get("settings") or {}
    ev.update(state=t.get("state"), revision=st.get("revision"), source=st.get("source"),
              emulators=loop.emulators(loop.agent.pid))
    m["events"].append(ev)
    t1 = time.monotonic()
    rc, d = loop.ctl("configure", "enabled=true", "--wait", "30")
    m["on_result"] = d.get("result")
    m["ready_s"] = loop.wait(loop.rs, J.rs_state_is("READY"), J.LIMITS["ready_s"] * SLOW, t1)[0]
    v, dd = J.judge_settings(m)
    assert v == "PASS", (dd, m)
    assert ready_ui(loop, 20, t1)[0] is not None, loop.uimod()
    loop.check_output(states=("READY", "DISABLED"))


# ---------------------------------------------------------------- отказы §12.4


def test_console_restart_state_only_from_telemetry(loop):
    """Перезапуск GUI: у пульта своего состояния модуля нет — до первого status не «Готов», затем READY и история
    из телеметрии; итог команды прежнего процесса не показывается."""
    up(loop)
    assert loop.lab("detach") == "ok" and loop.lab("attach") == "ok"
    assert ready_ui(loop, 20)[0] is not None
    loop.stop_console()
    t0, wall = time.monotonic(), time.time()
    loop.start_console()
    dt, _ = ready_ui(loop, 30, t0, after=wall)
    assert dt is not None, loop.uimod()
    new = [J.ui_mod(x) for _m, _w, x in loop.ui.since(t0) if (x.get("time") or 0) > wall]
    assert not [x for x in new if x.get("state") == "READY" and x.get("support") != "ok"]
    assert all((x.get("command") or {}).get("state") in (None, "idle") for x in new), new[-1]
    hist = loop.status().get("history") or []
    assert any(h.get("from") == "DISCONNECTED" and h.get("state") == "DETECTED" for h in hist), hist
    loop.check_output(states=("READY",))


def settings_case(loop, prep, want, timeout=30):
    t0 = time.monotonic()
    loop.stop_agent()
    prep()
    loop.start_agent()
    c = {"want": want, "ready_s": loop.relink(J.ui_state_is("READY", J.STATE_TEXT["READY"]), timeout, t0)}
    st = loop.status().get("settings") or {}
    c.update(source=st.get("source"), notice=st.get("notice"), revision=st.get("revision"),
             corrupt_dirs=sorted(p.name for p in loop.tmp.glob("remote-config.corrupt-*")))
    return c


def test_settings_missing_corrupt_and_unwritable(loop):
    """Настройки (ADR §8): нет каталога → factory; state.json повреждён → recovered (CFG-*); все ревизии
    повреждены → reset и каталог remote-config.corrupt-*; хранилище недоступно → memory, configure → not-saved.
    Модуль во всех случаях доходит до READY."""
    up(loop)
    st = loop.status().get("settings") or {}
    v, d = J.judge_settings_case({"want": "factory", "ready_s": 0.0, "source": st.get("source"),
                                  "notice": st.get("notice")})
    assert v == "PASS", (d, st)
    rc, d = loop.ctl("configure", "auto_recover=true", "--wait", "30")     # вторая ревизия: есть state.prev.json
    assert d.get("result") == "ok", d
    c = settings_case(loop, lambda: (loop.rc / "state.json").write_text("{broken"), "recovered")
    v, d = J.judge_settings_case(c)
    assert v == "PASS", (d, c)

    def corrupt_all():
        files = list((loop.rc / "revisions").glob("*/*.yaml"))
        assert files
        for f in files:
            f.write_text(f.read_text() + "\n#s6-corrupt\n")
    c = settings_case(loop, corrupt_all, "reset")
    v, d = J.judge_settings_case(c)
    assert v == "PASS", (d, c)
    if os.geteuid() == 0:
        pytest.skip("root пишет в каталог только для чтения — случай memory не проверить")
    ro = loop.tmp / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        t0 = time.monotonic()
        loop.stop_agent()
        loop.rc = ro / "remote-config"                       # создать нельзя: родитель только для чтения
        loop.start_agent()
        assert loop.relink(J.ui_state_is("READY"), 30, t0) is not None, loop.uimod()
        assert (loop.status().get("settings") or {}).get("source") == "memory"
        rc, d = loop.ctl("configure", "enabled=false", "--wait", "30")
        assert d.get("result") == "not-saved", d
        assert loop.status().get("state") == "READY"         # значение не применено
    finally:
        ro.chmod(0o700)
    loop.check_output(states=("READY",))


def test_old_unit_protocol_12_console_shows_unsupported(loop):
    """Блок старого протокола 1.2 (новый пульт): «не поддерживает внешний модуль (протокол 1.2)», команды —
    unsupported; подсистема модуля на самом блоке при этом работает независимо."""
    loop.start_agent("--protocol", "1.2")
    loop.start_console()
    dt, doc = loop.wait(loop.ui, lambda x: J.ui_mod(x).get("support") == "old-remote", 30)
    assert dt is not None, loop.uimod()
    m = J.ui_mod(doc)
    assert m.get("state") in ("", None) and "1.2" in str(m.get("supportText")) and \
        "не поддерживает" in str(m.get("supportText")), m
    rc, d = loop.ctl("status")
    assert d["module"]["support"] == "old-remote" and not d["module"].get("telemetry"), d
    rc, d = loop.ctl("lab", "detach", "--wait", "5")
    assert rc != 0 and d.get("result") == "unsupported", d
    assert loop.wait(loop.rs, J.rs_state_is("READY"), 20)[0] is not None, loop.unit()
