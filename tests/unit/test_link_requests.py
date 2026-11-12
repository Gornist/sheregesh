# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Учёт запросов пульта (link/requests.py, этап 7.3): queued, sent, completed, timeout, offline, unknown."""
from sheregesh.link import discovery
from sheregesh.link.requests import Requests


def test_states_and_loss_before_and_after_send():
    r = Requests()
    a = r.new("module-cmd")
    r.open("module-cmd", a, "queued", op="reset")
    assert r.snapshot("module-cmd") == {"req": 1, "op": "reset", "state": "queued"}
    r.lost()                                            # обрыв до отправки — блок не получал
    assert r.state("module-cmd", a) == "offline"
    b = r.new("module-cmd")
    r.open("module-cmd", b, "queued", op="ping")
    r.sent("module-cmd", b)
    r.lost()                                            # после отправки — итог неизвестен
    assert r.state("module-cmd", b) == "unknown" and r.state("module-cmd", a) is None   # старый req — не текущий
    c = r.new("module-cmd")
    r.open("module-cmd", c, "queued", op="ping")
    r.sent("module-cmd", c)
    r.timed_out("module-cmd", c)
    r.completed("module-cmd", c)                        # итог пришёл позже ожидания вызывающего
    assert r.state("module-cmd", c) == "completed"
    r.lost()
    assert r.state("module-cmd", c) == "completed"      # завершённый не становится неизвестным


def test_kinds_and_numbers_are_separate():
    r = Requests()
    assert [r.new("video-select"), r.new("video-select"), r.new("module-cmd")] == [1, 2, 1]
    r.open("video-select", 2, "unsupported", id="rtsp:0")
    r.sent("video-select", 1)                           # не текущий req — без изменений
    assert r.snapshot("video-select") == {"req": 2, "id": "rtsp:0", "state": "unsupported"}
    assert r.snapshot("module-cmd") is None


def test_discovery_candidates():
    assert discovery.candidates("192.168.77.0/30") == ["192.168.77.1", "192.168.77.2"]
    assert len(discovery.candidates("10.0.0.0/16")) == discovery.MAX_HOSTS


def test_discovery_respects_deadline_and_stop():
    """Ревью 2026-11-12: поиск мог идти десятки секунд. Общий лимит времени и остановка прерывают его."""
    t = [0.0]
    probed = []

    def connect(addr, timeout):
        probed.append(addr[0])
        t[0] += 1.0                       # каждая проба «занимает» секунду
        raise OSError("closed")
    assert discovery.discover("10.9.0.0/28", 7443, lambda s, h: s, connect=connect, deadline_s=3.0,
                              clock=lambda: t[0]) is None
    assert len(probed) <= 4
    probed.clear()
    assert discovery.discover("10.9.0.0/28", 7443, lambda s, h: s, connect=connect, stop=lambda: True) is None
    assert probed == []


def test_discovery_cache_expires(tmp_path):
    import json
    import time
    from types import SimpleNamespace
    cfg = SimpleNamespace(dir=tmp_path)
    discovery.write_cache(cfg, "192.168.1.173")
    assert discovery.read_cache(cfg) == "192.168.1.173"
    (tmp_path / "peer-cache.json").write_text(json.dumps({"host": "192.168.1.5", "time": time.time() - 40 * 86400}))
    assert discovery.read_cache(cfg) is None


def test_client_connect_does_not_wait_for_discovery(monkeypatch):
    """Поиск идёт в своём потоке: _connect возвращается сразу, найденный адрес берётся следующей попыткой."""
    import threading
    import time
    from types import SimpleNamespace
    from sheregesh.link import client as C
    found = threading.Event()

    def slow(spec, port, verify, stop=None, **kw):
        time.sleep(0.3)
        found.set()
        return "10.9.0.7"
    monkeypatch.setattr(C.discovery, "discover", slow)
    monkeypatch.setattr(C.discovery, "write_cache", lambda cfg, h: None)
    c = object.__new__(C.PeerClient)
    c.hosts, c.port, c.ctx = [], 7443, object()
    c.tls_cfg = SimpleNamespace(discover="10.9.0.0/28")
    c._lock, c._stop = threading.Lock(), threading.Event()
    c._last_discover, c._discover_thread, c._discovered = 0.0, None, None
    c._set = lambda **kw: None
    t0 = time.monotonic()
    assert c._connect() is None and time.monotonic() - t0 < 0.2
    assert found.wait(2)
    c._discover_thread.join(1)
    tried = []
    monkeypatch.setattr(C.socket, "create_connection", lambda addr, timeout: tried.append(addr) or (_ for _ in ()).throw(OSError()))
    c._connect()
    assert ("10.9.0.7", 7443) in tried and c.hosts[0] == "10.9.0.7"
