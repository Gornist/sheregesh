# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""ADR-0017: пульт при подключении сеанса mTLS запускает sheregesh-time-peer@<адрес блока> (не в потоке интерфейса),
один раз на переход в connected; без TLS (процесс-эмулятор) — нет. Итог — в журнал приложения (источник time)."""
import threading

from sheregesh_console import backend as backend_mod
from sheregesh_console.backend import Backend
from sheregesh_console.qtwait import wait_until

COUNTERS = {"connects": 1, "disconnects": 0, "bad_messages": 0, "ignored_messages": 0, "received": 5, "seq_gaps": 0}


class FakeClient:
    def __init__(self):
        self.snap = {"phase": "connecting", "detail": "", "counters": COUNTERS, "secure": True,
                     "host": "192.168.1.171", "tls": "TLSv1.3", "peer_uri": "urn:sheregesh:remote:x"}

    def snapshot(self):
        return dict(self.snap)

    def stop(self):
        pass


def test_connected_secure_session_sets_time_peer(qapp, tmp_path, monkeypatch):
    calls, ui_thread = [], threading.get_ident()

    def start(addr):
        calls.append((addr, threading.get_ident() != ui_thread))
        return True, addr
    monkeypatch.setattr(backend_mod.timepeer, "start", start)
    b = Backend(tmp_path / "data", autostart=False)
    try:
        b.videoops.on_link = lambda *a: None             # видео здесь не проверяется
        b.client = FakeClient()
        b.tick()
        assert calls == []                               # ещё не connected
        b.client.snap["phase"] = "connected"
        b.tick()
        b.tick()                                         # тот же переход — второй раз не вызывается
        assert wait_until(lambda: calls, 2) and calls == [("192.168.1.171", True)]
        assert wait_until(lambda: any(r["source"] == "time" and "192.168.1.171" in r["text"]
                                      for r in b.logRows(0)), 2)
        b.client.snap.update(phase="connecting")
        b.tick()
        b.client.snap.update(phase="connected", secure=False, host="127.0.0.1")   # процесс-эмулятор
        b.tick()
        assert len(calls) == 1
    finally:
        b.client = None
        b.stop()
