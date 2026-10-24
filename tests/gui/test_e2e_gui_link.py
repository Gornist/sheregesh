# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DUAL-DEVICE E2E, часть GUI (offscreen, программный рендерер; L3 на эмуляторах, НЕ проверка физического дисплея).

Индикация канала с удалённым блоком (обобщённые состояния CONNECTING / CONNECTED / DISCONNECTED / ERROR поверх
состояний ADR-0007), автообновление после восстановления и E2E-08: при потере связи событийный цикл GUI не стоит.
Удалённый блок — отдельный процесс-эмулятор sheregesh_remote на 127.0.0.1 (без плат).
"""
import shutil
import signal
import sys
import threading
import time

import pytest
from PySide6.QtCore import QElapsedTimer, QObject, QTimer
from PySide6.QtQuickControls2 import QQuickStyle

from procs import Agent, free_port
from sheregesh_console import backend as backend_mod
from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend, link_view, peer_link, peer_role
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.video import VideoReceiver

QQuickStyle.setStyle("Fusion")
GAP_LIMIT_MS = 250          # предел паузы событийного цикла (таймер 20 мс; запас на медленный раннер CI)


class Latency:
    """Максимальная пауза событийного цикла GUI: таймер 20 мс, пауза между срабатываниями = задержка событий."""

    def __init__(self):
        self.timer, self.clock, self.worst, self.ticks = QTimer(), QElapsedTimer(), 0, 0
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._tick)
        self.clock.start()
        self.timer.start()

    def _tick(self):
        self.worst = max(self.worst, self.clock.restart())
        self.ticks += 1

    def stop(self):
        self.timer.stop()
        return self.worst


@pytest.fixture
def make_ui(qapp, tmp_path):
    made = []

    def make(**kw):
        compile_translations()
        u = Ui(Backend(tmp_path / f"data{len(made)}", autostart=True, **kw))
        made.append(u)
        return u
    yield make
    for u in made:
        u.close()


def text(u, name):
    o = u.window.findChild(QObject, name)
    assert o is not None, name
    return o.property("text")


def visible(u, name):
    return u.window.findChild(QObject, name).property("visible")


# ---------------- отображение состояний ----------------
def test_link_view_maps_adr0007_states_only():
    """Обобщённое состояние — только отображение peer_link (ADR-0007), без новых состояний протокола."""
    cases = [
        ("connected", {}, "CONNECTED"),
        ("connecting", {}, "CONNECTING"),                 # первое подключение после старта
        ("lost", {"lost_s": 3.0}, "CONNECTING"),          # RECONNECTING — идут попытки
        ("lost", {"lost_s": 45.0}, "DISCONNECTED"),
        ("lost", {}, "DISCONNECTED"),                     # связи не было, окно старта истекло
        ("auth-failed", {"lost_s": 2}, "ERROR"),
        ("incompatible", {}, "ERROR"),
    ]
    for state, snap, want in cases:
        assert link_view(state, peer_link(state, snap)) == want, (state, snap)


def test_peer_role_from_hello_or_verified_uri():
    assert peer_role({"peer": {"role": "remote"}}) == "remote"
    assert peer_role({"peer": {}, "secure": True, "peer_uri": "urn:sheregesh:remote:PI2SERIAL"}) == "remote"
    assert peer_role({"peer": {}, "secure": False, "peer_uri": None}) == ""


@pytest.mark.test_id("TEST-SW-004")
def test_link_states_autoupdate_and_gui_responsive_during_loss(make_ui, monkeypatch):
    """CONNECTING → DISCONNECTED (блока нет) → CONNECTED (блок запущен) → обрыв без закрытия TCP (SIGSTOP):
    CONNECTING → DISCONNECTED → восстановление CONNECTED без действий оператора. E2E-08: пауза событийного цикла
    < GAP_LIMIT_MS без блока и от обрыва до восстановления (запуск процесса-эмулятора не в счёт: баннер читает поток
    теста)."""
    monkeypatch.setattr(backend_mod, "RECONNECT_WINDOW_S", 3.0)     # окно RECONNECTING укорочено для теста
    port = free_port()
    u = make_ui(peer=("127.0.0.1", port))
    st = lambda: u.backend.status                                    # noqa: E731
    lat = Latency()
    assert st()["linkView"] == "CONNECTING" and text(u, "linkBadgeText") == "Удалённый блок: подключение…"
    assert wait_until(lambda: st()["linkView"] == "DISCONNECTED", 8)            # таймаут heartbeat 3 с
    assert text(u, "linkBadgeText") == "Удалённый блок: нет связи"
    gap_nopeer = lat.stop()
    agent = Agent("--heartbeat-interval", "0.2", port=port)
    try:
        t0 = time.monotonic()
        assert wait_until(lambda: st()["linkView"] == "CONNECTED", 12)        # автообновление, без перезапуска GUI
        t_connect = time.monotonic() - t0
        assert text(u, "linkBadgeText") == "Удалённый блок: связь есть"
        assert wait_until(lambda: visible(u, "remoteTelemetry"), 5)
        ident = text(u, "peerIdentity")
        assert "remote-emu-01" in ident and "ПО " in ident, ident                # роль в loopback не известна
        assert visible(u, "linkPlain") and not visible(u, "linkSecurity")       # loopback — без TLS, так и сказано
        lat = Latency()                                                          # от обрыва до восстановления
        agent.signal(signal.SIGSTOP)                                             # блок завис: TCP открыт, тишина
        t0 = time.monotonic()
        assert wait_until(lambda: st()["linkView"] == "CONNECTING", 8)
        t_detect = time.monotonic() - t0
        assert st()["peerLink"] == "RECONNECTING" and text(u, "linkBadgeText") == "Удалённый блок: подключение…"
        assert wait_until(lambda: st()["linkView"] == "DISCONNECTED", 10)
        agent.signal(signal.SIGCONT)
        t0 = time.monotonic()
        assert wait_until(lambda: st()["linkView"] == "CONNECTED", 15)
        t_recover = time.monotonic() - t0
        assert wait_until(lambda: visible(u, "remoteTelemetry"), 5)
        assert st()["linkConnects"] >= 2                                         # новое соединение, тот же GUI
        worst = lat.stop()
    finally:
        agent.stop()
    print(f"\nE2E-GUI link: connect {t_connect:.2f} s, loss detected {t_detect:.2f} s, recovered {t_recover:.2f} s; "
          f"event loop worst gap: no peer {gap_nopeer} ms, loss→recovery {worst} ms ({lat.ticks} ticks)")
    assert gap_nopeer < GAP_LIMIT_MS and worst < GAP_LIMIT_MS, f"событийный цикл GUI стоял {gap_nopeer}/{worst} мс"


@pytest.mark.test_id("TEST-SW-004")
def test_gui_responsive_when_remote_process_killed(make_ui):
    """E2E-08: блок пропал (процесс убит, соединение отвергается) — интерфейс отвечает, переключение экранов идёт."""
    agent = Agent("--heartbeat-interval", "0.2")
    try:
        u = make_ui(peer=("127.0.0.1", agent.port))
        assert wait_until(lambda: u.backend.status["linkView"] == "CONNECTED", 10)
        lat = Latency()
        agent.signal(signal.SIGKILL)
        agent.proc.wait(5)
        t_end = time.monotonic() + 6
        i = 0
        while time.monotonic() < t_end:                    # оператор листает экраны во время потери связи
            u.window.setProperty("currentPage", i % len(PAGE_NAMES))
            i += 1
            pump(100)
        worst = lat.stop()
        assert u.backend.status["linkView"] == "CONNECTING"
        assert "не запущен" in text(u, "peerDetail")
        print(f"\nE2E-GUI kill: event loop worst gap {worst} ms over 6 s, {i} page switches")
        assert worst < GAP_LIMIT_MS, f"событийный цикл GUI стоял {worst} мс"
    finally:
        agent.stop()


# ---------------- видео: остановка конвейера не блокирует поток GUI ----------------
class _StuckClient:
    """Подставной VideoClient: run() не реагирует на close() (подключение к пропавшему блоку ещё идёт) или
    непрерывно пишет в stdin декодера, который его не читает."""

    def __init__(self, mode):
        self.mode, self.closed = mode, threading.Event()

    def run(self, fd):
        import os
        if self.mode == "connect":
            time.sleep(5)                                   # create_connection/TLS к адресу, которого больше нет
            return "timeout"
        try:
            while True:
                os.write(fd, b"\0" * 65536)                 # декодер не читает — канал переполняется, запись стоит
        except OSError as e:
            return str(e)

    def close(self):
        self.closed.set()

    def frame_decoded(self, t):
        pass

    def stats(self):
        return {}


@pytest.mark.test_id("TEST-SW-002")
@pytest.mark.parametrize("mode", ["connect", "pipe-full"])
def test_video_stop_on_link_loss_does_not_block_gui_thread(qapp, mode):
    """Потеря сеанса управления → configure(None, None) в таймере бэкенда. Прежде stop() ждал поток видеоканала
    до 2 с (join) и декодер (wait) прямо в потоке GUI."""
    rx = VideoReceiver(command=[sys.executable, "-c", "import time; time.sleep(30)"])    # декодер, не читающий stdin
    try:
        rx.configure_remote("h264", ("127.0.0.1", 1), lambda: _StuckClient(mode))
        pump(300)
        assert rx.state == "waiting"
        t0 = time.monotonic()
        rx.configure(None, None)
        dt = (time.monotonic() - t0) * 1000
        assert rx.state == "off"
        print(f"\nvideo stop ({mode}): {dt:.1f} ms in GUI thread")
        assert dt < 100, f"остановка видео заняла {dt:.0f} мс в потоке GUI"
    finally:
        rx.shutdown()


# ---------------- ERROR: отказ аутентификации (mTLS, чужой сертификат) ----------------
@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    from sheregesh.link import pki
    root = tmp_path_factory.mktemp("e2e-gui-lab")
    pki.ca_init(root / "ca")

    def device(name, role, did, peer):
        d = root / name
        pki.install(d, pki.ca_sign(root / "ca", pki.keygen(role, did, d), role, did),
                    (root / "ca/ca.crt").read_text(), role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2SERIAL", "console:PI1SERIAL"),
            "console": device("console", "console", "PI1SERIAL", "remote:PI2SERIAL"),
            "other": device("other", "console", "INTRUDER", "remote:PI2SERIAL")}


@pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")
@pytest.mark.test_id("TEST-SW-004")
def test_auth_failure_shown_as_error_then_tls_session_shown(make_ui, lab):
    from dataclasses import replace
    agent = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--heartbeat-interval", "0.2",
                  "--no-video")
    try:
        u = make_ui(link_cfg=replace(lab["other"], port=agent.port, video=False))
        assert wait_until(lambda: u.backend.status["linkView"] == "ERROR", 10), u.backend.status["peerDetail"]
        assert u.backend.status["peerLink"] == "AUTH_FAILED"
        assert text(u, "linkBadgeText") == "Удалённый блок: ошибка связи"
        assert text(u, "peerValue") == "Ошибка аутентификации"
        assert visible(u, "linkAuthFailures") and text(u, "linkAuthFailures").startswith("Отказы аутентификации: ")
        u2 = make_ui(link_cfg=replace(lab["console"], port=agent.port, video=False))
        assert wait_until(lambda: u2.backend.status["linkView"] == "CONNECTED", 10), u2.backend.status
        assert wait_until(lambda: visible(u2, "linkSecurity"), 5)
        assert "TLSv1.3" in text(u2, "linkSecurity") and not visible(u2, "linkAuthFailures")
        assert "роль remote" in text(u2, "peerIdentity")                     # из проверенного URI сертификата
    finally:
        agent.stop()
