# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Частые пути канала (ревью производительности 2026-11-12): TCP_NODELAY, лёгкий срез состояния для стиков 50 Гц."""
import socket

from sheregesh.link import client as C
from sheregesh_console import sticks


def test_no_delay_is_set_on_tcp_socket():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    s = C._no_delay(socket.create_connection(srv.getsockname()))
    try:
        assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 0
    finally:
        s.close()
        srv.close()


class LightClient:
    """Клиент с link_state(): snapshot() нужен только при новом rc-state."""

    def __init__(self):
        self.full = 0
        self.rc_at = None

    def link_state(self):
        return {"phase": "connected", "session": "1.5", "counters": {"connects": 1}, "rc_state_at": self.rc_at}

    def snapshot(self):
        self.full += 1
        return {"phase": "connected", "session": "1.5", "counters": {"connects": 1},
                "rc_state": {"n": 1, "state": "IDLE", "rx_at": self.rc_at}}


def test_link_state_used_and_snapshot_only_on_new_rc_state():
    c = LightClient()
    assert sticks._client_connected(sticks._link_state(c)) == (True, True)
    assert c.full == 0
    assert sticks._link_state(None) == {}
