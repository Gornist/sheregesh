# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сквозные испытания канала пульт ↔ удалённый блок (спринт DUAL-DEVICE E2E, сетевая часть), L3 на loopback.

Две РЕАЛЬНЫЕ стороны отдельными процессами с тестовой PKI (mTLS, как на стенде): удалённый блок —
`python -m sheregesh_remote --link-config … --status-file …` с параметрами службы sheregesh-remote-net (heartbeat 1 с,
событие каждые 5 с); пульт — процесс с PeerClient и тем же таймаутом heartbeat, что в конфигурации пульта по умолчанию
(link.yaml: 3 с), состояние канала — в файле состояния, как у службы. Между ними — TCP-посредник «кабель» (CablePull):
cut — ни байта в обе стороны, без FIN/RST (модель выдернутого кабеля), restore — пересылка возобновляется.

Пороги — REQ-SYS-002 (SRS 1.0): потеря связи видна ОБЕИМ сторонам ≤ 5 с, восстановление без ручных действий ≤ 10 с;
REQ-SW-004: блок без пульта показывает «нет пульта». Замеры пишутся в reports/e2e-link-net.json.
Число циклов E2E-11 — SHEREGESH_E2E_CYCLES (по умолчанию 20).
"""
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from procs import ENV, ROOT, Agent, free_port, wait_for
from sheregesh.link import pki

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")

DETECT_S = 5.0          # REQ-SYS-002 п. 2
RECOVER_S = 10.0        # REQ-SYS-002 п. 3
HB_TIMEOUT_S = 3.0      # link.yaml пульта по умолчанию (software/sheregesh/config/defaults.py)
REPORT = {}


def _save():
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "e2e-link-net.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- стенд в процессах

@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e-lab")
    ca = root / "ca"
    pki.ca_init(ca)

    def device(name, role, did, peer):
        d = root / name
        cert = pki.ca_sign(ca, pki.keygen(role, did, d), role, did)
        pki.install(d, cert, (ca / "ca.crt").read_text(), role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    yield {"remote": device("remote", "remote", "PI2SERIAL", "console:PI1SERIAL"),
           "console": device("console", "console", "PI1SERIAL", "remote:PI2SERIAL"), "root": root}
    _save()


def remote_unit(lab, tmp, port=None):
    """Удалённый блок с параметрами службы sheregesh-remote-net (без видео)."""
    status = tmp / "remote-link-status.json"
    a = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--no-video",
              "--status-file", str(status), "--event-interval", "5", port=port)
    a.status = status
    return a


def remote_connected(a):
    try:
        return json.loads(a.status.read_text()).get("connected")
    except (OSError, ValueError):
        return None


CONSOLE = r"""
import json, os, signal, sys, threading, time
from sheregesh.link import tls
from sheregesh.link.client import PeerClient
cfg = tls.LinkConfig.load(sys.argv[1])
c = PeerClient(None, int(sys.argv[2]), heartbeat_timeout_s=float(sys.argv[4]), tls_cfg=cfg, status_file=sys.argv[3])
c.start()
def term(*_):
    c.stop()
    os._exit(0)
signal.signal(signal.SIGTERM, term)
last, next_res = None, 0.0
while True:
    s = c.snapshot()
    if (s["phase"], s["detail"]) != last:
        last = (s["phase"], s["detail"])
        print(json.dumps({"t": time.time(), "phase": s["phase"], "detail": s["detail"],
                          "connects": s["counters"]["connects"]}), flush=True)
    if time.monotonic() >= next_res:
        next_res = time.monotonic() + 0.5
        print(json.dumps({"t": time.time(), "fds": len(os.listdir("/dev/fd")), "threads": threading.active_count()}),
              flush=True)
    time.sleep(0.01)
"""


class Console:
    """Пульт отдельным процессом: PeerClient + файл состояния канала; переходы фаз — строками JSON в stdout."""

    def __init__(self, lab, tmp, port):
        self.status = tmp / "console-link-status.json"
        self.proc = subprocess.Popen([sys.executable, "-c", CONSOLE, str(lab["console"].source), str(port),
                                      str(self.status), str(HB_TIMEOUT_S)], env=ENV, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        self.t_start = time.time()
        self.phases, self.res = [], []
        self._lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                (self.phases if "phase" in rec else self.res).append(rec)

    def phase(self):
        with self._lock:
            return self.phases[-1]["phase"] if self.phases else None

    def first(self, pred, after):
        """Время первого перехода после момента after, удовлетворяющего pred."""
        with self._lock:
            return next((p["t"] for p in self.phases if p["t"] >= after and pred(p)), None)

    def last_res(self):
        with self._lock:
            return dict(self.res[-1]) if self.res else None

    def kill(self, sig):
        os.kill(self.proc.pid, sig)

    def stop(self, sig=signal.SIGTERM):
        if self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGCONT)
                os.kill(self.proc.pid, sig)
                self.proc.wait(timeout=8)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()


class CablePull:
    """TCP-посредник между пультом и блоком — модель кабеля.

    cut: ни байта ни в одну сторону, без FIN/RST; ядро обеих сторон соединение держит (как при выдернутом кабеле, пока
    TCP не исчерпал повторы). Новые соединения на время обрыва удерживаются и до блока не доходят (SYN «в пустоту»).
    restore: пересылка возобновляется, накопленное доставляется, закрытия (FIN) распространяются — как повторная
    передача после подключения кабеля. reset: RST обеим сторонам (закрытие сокета с SO_LINGER 0).
    """

    def __init__(self, upstream_port):
        self.upstream = upstream_port
        self.up = threading.Event()
        self.up.set()
        self.closed = False
        self.pairs = set()
        self.lock = threading.Lock()
        self.ls = socket.socket()
        self.ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.ls.bind(("127.0.0.1", 0))
        self.ls.listen(16)
        self.port = self.ls.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def cut(self):
        self.up.clear()

    def restore(self):
        self.up.set()

    def live(self):
        with self.lock:
            return len(self.pairs)

    def reset(self):
        with self.lock:
            pairs = list(self.pairs)
        for pair in pairs:
            for s in pair:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                    s.close()
                except OSError:
                    pass

    def close(self):
        self.closed = True
        self.up.set()
        self.ls.close()
        self.reset()

    def _accept(self):
        while not self.closed:
            try:
                c, _ = self.ls.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        while not self.up.wait(0.05):
            if self.closed:
                c.close()
                return
        c.setblocking(False)               # клиент мог уйти, пока «кабеля не было»: такой SYN до блока не дошёл бы
        buf, eof = b"", False
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    eof = True
                    break
                buf += d
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            eof = True
        c.setblocking(True)
        if eof:
            c.close()
            return
        try:
            u = socket.create_connection(("127.0.0.1", self.upstream), timeout=2)
            u.settimeout(None)
            if buf:
                u.sendall(buf)
        except OSError:                    # блок не слушает: как отказ ядра (RST), а не закрытие после accept
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            c.close()
            return
        pair = (c, u)
        with self.lock:
            self.pairs.add(pair)
        state = {"done": 0}
        for src, dst in ((c, u), (u, c)):
            threading.Thread(target=self._pump, args=(src, dst, pair, state), daemon=True).start()

    def _pump(self, src, dst, pair, state):
        try:
            while not self.closed:
                if not self.up.is_set():
                    time.sleep(0.02)
                    continue
                r, _, _ = select.select([src], [], [], 0.05)
                if not r or not self.up.is_set():
                    continue
                d = src.recv(65536)
                if not d:
                    dst.shutdown(socket.SHUT_WR)
                    break
                dst.sendall(d)
        except (OSError, ValueError):
            for s in pair:
                try:
                    s.close()
                except OSError:
                    pass
        with self.lock:
            state["done"] += 1
            if state["done"] == 2 or src.fileno() < 0:
                self.pairs.discard(pair)
                for s in pair:
                    try:
                        s.close()
                    except OSError:
                        pass


def proc_resources(pid):
    """Открытые дескрипторы и потоки процесса: /proc в Linux, lsof/ps в macOS."""
    if Path(f"/proc/{pid}/fd").is_dir():
        return {"fds": len(os.listdir(f"/proc/{pid}/fd")), "threads": len(os.listdir(f"/proc/{pid}/task"))}
    fds = subprocess.run(["lsof", "-n", "-P", "-a", "-p", str(pid), "-d", "0-65535"], capture_output=True,
                         text=True).stdout.count("\n") - 1
    threads = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True).stdout.count("\n") - 1
    return {"fds": fds, "threads": threads}


@pytest.fixture
def stand(lab, tmp_path):
    """Блок + «кабель» + пульт; ждёт установления сессии с обеих сторон."""
    r = remote_unit(lab, tmp_path)
    cable = CablePull(r.port)
    c = Console(lab, tmp_path, cable.port)
    s = {"remote": r, "cable": cable, "console": c, "tmp": tmp_path, "lab": lab}
    yield s
    s["console"].stop()
    s["cable"].close()
    s["remote"].stop()


def both_connected(s, timeout):
    return wait_for(lambda: s["console"].phase() == "connected" and remote_connected(s["remote"]), timeout, step=0.02)


def timed(pred, timeout, step=0.02):
    """Секунды до выполнения условия или None."""
    t0 = time.monotonic()
    return round(time.monotonic() - t0, 2) if wait_for(pred, timeout, step) else None


# ---------------------------------------------------------------- E2E-03

@pytest.mark.test_id("TEST-SYS-002")
def test_e2e03_connect_both_sides(lab, tmp_path):
    """E2E-03: старт обеих сторон → сессия mTLS видна в состоянии пульта и в файле состояния блока."""
    r = remote_unit(lab, tmp_path)
    t0 = time.time()
    c = Console(lab, tmp_path, r.port)
    try:
        assert wait_for(lambda: c.phase() == "connected", 15), c.phases
        t_console = c.first(lambda p: p["phase"] == "connected", t0) - t0
        assert wait_for(lambda: remote_connected(r), 5)
        t_remote = time.time() - t0
        st = json.loads(r.status.read_text())
        assert st["peer"]["uri"] == "urn:sheregesh:console:PI1SERIAL" and st["peer"]["tls"] == "TLSv1.3"
        cs = json.loads(c.status.read_text())
        assert cs["phase"] == "connected" and cs["tls"] == "TLSv1.3"
        assert cs["peer_uri"] == "urn:sheregesh:remote:PI2SERIAL" and cs["session"] == "1.2"
        REPORT["E2E-03"] = {"console_connected_s": round(t_console, 2), "remote_status_connected_s": round(t_remote, 2),
                            "note": "от запуска процесса пульта (интерпретатор + TLS)"}
    finally:
        c.stop()
        r.stop()


# ---------------------------------------------------------------- E2E-07 / E2E-09

def detect(s, t0, timeout=15, remote_side=True):
    """Секунды от t0 до «нет связи» у пульта (переход фазы) и у блока (файл состояния); опрос обеих сторон сразу."""
    console_s = remote_s = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (console_s is None or (remote_side and remote_s is None)):
        if console_s is None:
            t = s["console"].first(lambda p: p["phase"] != "connected", t0)
            console_s = None if t is None else round(t - t0, 2)
        if remote_side and remote_s is None and remote_connected(s["remote"]) is False:
            remote_s = round(time.time() - t0, 2)
        time.sleep(0.02)
    return console_s, remote_s


def _loss(s, name, cut, restore, *, remote_side=True):
    """Обрыв cut() → время обнаружения обеими сторонами → restore() → время восстановления обеими сторонами."""
    assert both_connected(s, 15), (s["console"].phases, remote_connected(s["remote"]))
    time.sleep(1.5)                                             # установившийся обмен, status блока записан
    t0 = time.time()
    cut()
    console_s, remote_s = detect(s, t0, remote_side=remote_side)
    rec = {"console_detect_s": console_s, "remote_detect_s": remote_s}
    t1 = time.time()
    restore()
    ok = both_connected(s, 30)
    rec["recover_both_s"] = round(time.time() - t1, 2) if ok else None
    t = s["console"].first(lambda p: p["phase"] == "connected", t1) if ok else None
    if t is not None:                                           # у замороженного пульта фаза могла не смениться
        rec["console_recover_s"] = round(t - t1, 2)
    REPORT.setdefault("E2E-07/09", {})[name] = rec
    return rec


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_cable_cut_silent_detected_by_both_sides_and_recovers(stand):
    """«Выдернутый кабель» (тишина без FIN/RST): обе стороны видят «нет связи» ≤ 5 с, восстановление ≤ 10 с."""
    rec = _loss(stand, "cable-cut", stand["cable"].cut, stand["cable"].restore)
    assert rec["console_detect_s"] is not None and rec["console_detect_s"] <= DETECT_S, rec
    assert rec["remote_detect_s"] is not None and rec["remote_detect_s"] <= DETECT_S, rec
    assert rec["recover_both_s"] is not None and rec["recover_both_s"] <= RECOVER_S, rec


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_long_cable_cut_does_not_stall_remote(stand):
    """Обрыв дольше заполнения буферов: основной цикл блока не зависает (файл состояния обновляется), восстановление."""
    s = stand
    assert both_connected(s, 15)
    s["cable"].cut()
    time.sleep(12)
    m0 = s["remote"].status.stat().st_mtime
    assert wait_for(lambda: s["remote"].status.stat().st_mtime > m0, 5), "файл состояния блока не обновляется"
    assert remote_connected(s["remote"]) is False
    t1 = time.time()
    s["cable"].restore()
    assert both_connected(s, 30)
    REPORT.setdefault("E2E-07/09", {})["cable-cut-12s"] = {"recover_both_s": round(time.time() - t1, 2)}


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_cable_reset_rst(stand):
    """Закрытие сокета (RST обеим сторонам) — обнаружение сразу, восстановление ≤ 10 с."""
    rec = _loss(stand, "socket-rst", stand["cable"].reset, lambda: None)
    assert rec["console_detect_s"] <= DETECT_S and rec["remote_detect_s"] <= DETECT_S, rec
    assert rec["recover_both_s"] <= RECOVER_S, rec


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_remote_frozen_sigstop(stand):
    """Блок «завис» (SIGSTOP): ядро блока ещё подтверждает TCP — пульт обнаруживает по heartbeat ≤ 5 с."""
    r = stand["remote"]
    rec = _loss(stand, "remote-sigstop", lambda: r.signal(signal.SIGSTOP), lambda: r.signal(signal.SIGCONT),
                remote_side=False)
    assert rec["console_detect_s"] <= DETECT_S, rec
    assert rec["recover_both_s"] <= RECOVER_S, rec


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_console_frozen_sigstop(stand):
    """Пульт «завис» (SIGSTOP): ядро пульта подтверждает TCP, ack нет — блок показывает «нет пульта» ≤ 5 с."""
    c = stand["console"]
    rec = _loss(stand, "console-sigstop", lambda: c.kill(signal.SIGSTOP), lambda: c.kill(signal.SIGCONT))
    assert rec["remote_detect_s"] is not None and rec["remote_detect_s"] <= DETECT_S, rec
    assert rec["recover_both_s"] is not None and rec["recover_both_s"] <= RECOVER_S, rec


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_remote_killed_and_restarted(stand):
    """SIGKILL блока: ядро закрывает сокет — пульт видит сразу; блок перезапущен (как Restart=on-failure)."""
    s = stand
    port = s["remote"].port

    def restart():
        time.sleep(3)                                            # RestartSec=3 службы
        s["remote"] = remote_unit(s["lab"], s["tmp"], port=port)
    rec = _loss(s, "remote-sigkill", lambda: s["remote"].signal(signal.SIGKILL), restart, remote_side=False)
    assert rec["console_detect_s"] <= 1.0, rec
    assert rec["recover_both_s"] <= RECOVER_S + 3 + 1, rec          # + RestartSec + запуск интерпретатора


@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_console_killed_and_restarted(stand):
    """SIGKILL пульта: блок видит закрытие и показывает «нет пульта»; новый процесс пульта соединяется."""
    s = stand

    def restart():
        s["console"] = Console(s["lab"], s["tmp"], s["cable"].port)

    t0 = time.time()
    assert both_connected(s, 15)
    time.sleep(1.5)
    t0 = time.time()
    s["console"].kill(signal.SIGKILL)
    remote_s = timed(lambda: remote_connected(s["remote"]) is False, 10)
    t1 = time.time()
    restart()
    ok = both_connected(s, 20)
    REPORT.setdefault("E2E-07/09", {})["console-sigkill"] = {
        "remote_detect_s": round(t1 - t0, 2) if remote_s is not None else None,
        "recover_both_s (новый процесс)": round(time.time() - t1, 2) if ok else None}
    assert remote_s is not None and t1 - t0 <= DETECT_S
    assert ok


# ---------------------------------------------------------------- E2E-14

@pytest.mark.test_id("TEST-SYS-002")
def test_e2e14_restart_each_side(stand):
    """Штатный перезапуск приложения каждой стороны (SIGTERM, как systemctl restart) → сессия восстанавливается."""
    s = stand
    out = {}
    assert both_connected(s, 15)
    # пульт: SIGTERM → bye → новый процесс
    t0 = time.time()
    s["console"].stop()
    gone = timed(lambda: remote_connected(s["remote"]) is False, 10)
    s["console"] = Console(s["lab"], s["tmp"], s["cable"].port)
    ok = both_connected(s, 20)
    out["console-restart"] = {"remote_sees_loss_s": gone, "session_again_s": round(time.time() - t0, 2) if ok else None}
    assert gone is not None and gone <= DETECT_S and ok
    # блок: SIGTERM → новый процесс на том же порту
    port = s["remote"].port
    t0 = time.time()
    s["remote"].stop()
    det = s["console"].first(lambda p: p["phase"] != "connected", t0) if timed(
        lambda: s["console"].first(lambda p: p["phase"] != "connected", t0), 10) is not None else None
    det = None if det is None else round(det - t0, 2)
    s["remote"] = remote_unit(s["lab"], s["tmp"], port=port)
    t1 = time.time()
    ok = both_connected(s, 20)
    out["remote-restart"] = {"console_sees_loss_s": det, "session_after_remote_up_s": round(time.time() - t1, 2)
                             if ok else None}
    REPORT["E2E-14"] = out
    assert det is not None and det <= DETECT_S and ok
    assert time.time() - t1 <= RECOVER_S


# ---------------------------------------------------------------- E2E-11

@pytest.mark.test_id("TEST-SYS-002")
def test_e2e11_cycles_no_resource_growth(stand):
    """N циклов «кабель выдернут/вставлен»: каждый успешен; дескрипторы, потоки и соединения не растут."""
    s = stand
    n = int(os.environ.get("SHEREGESH_E2E_CYCLES", "20"))
    assert both_connected(s, 15)
    time.sleep(1.5)
    base_r, base_c = proc_resources(s["remote"].proc.pid), s["console"].last_res()
    cycles = []
    for i in range(n):
        t0 = time.time()
        s["cable"].cut()
        det_c, det_r = detect(s, t0, timeout=10)
        t1 = time.time()
        s["cable"].restore()
        ok = both_connected(s, 20)
        cycles.append({"i": i, "console_detect_s": det_c, "remote_detect_s": det_r,
                       "recover_s": round(time.time() - t1, 2) if ok else None})
        assert ok, cycles[-1]
    time.sleep(2.5)                                     # фоновые закрытия завершены, ресурсы пульта переизмерены
    end_r, end_c = proc_resources(s["remote"].proc.pid), s["console"].last_res()
    succ = [c for c in cycles if c["recover_s"] is not None and c["console_detect_s"] is not None
            and c["remote_detect_s"] is not None]
    REPORT["E2E-11"] = {
        "cycles": n, "success": len(succ),
        "console_detect_max_s": max((c["console_detect_s"] or 99) for c in cycles),
        "remote_detect_max_s": max((c["remote_detect_s"] or 99) for c in cycles),
        "recover_max_s": max((c["recover_s"] or 99) for c in cycles),
        "remote_res": {"before": base_r, "after": end_r}, "console_res": {"before": base_c, "after": end_c},
        "live_tcp_pairs_after": s["cable"].live(), "detail": cycles}
    assert len(succ) == n, cycles
    assert end_r["fds"] <= base_r["fds"] + 2 and end_r["threads"] <= base_r["threads"] + 1, (base_r, end_r)
    assert end_c["fds"] <= base_c["fds"] + 2 and end_c["threads"] <= base_c["threads"] + 1, (base_c, end_c)
    assert s["cable"].live() == 1                       # одна живая пара: текущая сессия, без «висящих»
    assert all(c["console_detect_s"] <= DETECT_S and c["remote_detect_s"] <= DETECT_S
               and c["recover_s"] <= RECOVER_S for c in cycles), cycles


# ---------------------------------------------------------------- основной цикл блока при непрочитанном буфере

@pytest.mark.test_id("TEST-SYS-002")
def test_e2e07_remote_ticker_not_blocked_by_unread_send_buffer(lab, tmp_path):
    """Пульт держит соединение, но не читает (долгий обрыв в ускоренном виде: буфер отправки блока заполняется).
    Проверка живости по ack отключена (--peer-timeout 0, как у сессии 1.0 без ack), поток ускорен — основной цикл
    блока (WATCHDOG, файл состояния, heartbeat) не должен вставать на drain(): зависшее соединение разрывается."""
    from sheregesh import PROTOCOL_VERSION
    from sheregesh.link import tls
    from sheregesh.link.codec import encode
    status = tmp_path / "remote-link-status.json"
    a = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--no-video", "--status-file",
              str(status), "--heartbeat-interval", "0.002", "--peer-timeout", "0")
    a.status = status
    raw = socket.socket()
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)        # маленькое окно: буфер блока заполнится быстро
    try:
        raw.connect(("127.0.0.1", a.port))
        s = tls.make_context(lab["console"], server=False).wrap_socket(raw)
        s.sendall(encode(PROTOCOL_VERSION, "hello", 1, time.time(),
                         {"role": "console", "proto": PROTOCOL_VERSION, "software": "0", "hardware": "HW-1.0 Rev A"}))
        assert wait_for(lambda: remote_connected(a), 5)
        worst, t_end = 0.0, time.monotonic() + 25
        while time.monotonic() < t_end:                              # пульт не читает ничего
            worst = max(worst, time.time() - status.stat().st_mtime)
            time.sleep(0.1)
        REPORT.setdefault("E2E-07/09", {})["unread-send-buffer"] = {"status_file_max_age_s": round(worst, 2),
                                                                    "remote_connected_end": remote_connected(a)}
        assert worst < 5.0, f"основной цикл блока стоял {worst:.1f} с (WatchdogSec=15 у службы)"
    finally:
        raw.close()
        a.stop()
