# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Локальный канал пульта, команда «sticks» (S7, ADR-0013, 4.2; offscreen, L3): status / enable / disable тем же слотом
SticksView.setEnabled, что переключатель экрана «Управление»; включение — только с флагом confirmed.

Источник стиков и клиент канала подменные (как в test_gui_sticks); канал — настоящий unix-сокет во временном
каталоге 0700. Пробу приёмки (агент Q) здесь не используют — запросы пишутся в сокет.
"""
import json
import os
import shutil
import socket
import tempfile

import pytest

from sheregesh.link import videoselect
from sheregesh_console.control import ControlServer
from test_console_control import in_thread
from test_gui_sticks import BOXER_NAME, LOST, feed, rc_state, ui  # noqa: F401 — фикстура пульта с подменами
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта (общая для экранов)


@pytest.fixture
def server(ui):  # noqa: F811
    d = tempfile.mkdtemp(prefix="shs-")              # каталог 0700; путь короче предела sun_path (104 байта, macOS)
    srv = ControlServer(ui.backend, os.path.join(d, videoselect.CONSOLE_SOCKET)).start()
    yield srv
    srv.stop()
    shutil.rmtree(d, ignore_errors=True)


def ask(path, req):
    def run():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(20)
            s.connect(str(path))
            s.sendall((json.dumps(req) + "\n").encode())
            return json.loads(s.makefile().readline())
    return in_thread(run, timeout=25)


def test_status_returns_snapshot_and_is_off_after_start(ui, server):  # noqa: F811
    ui.backend.src.plug()
    feed(ui, rc=rc_state("IDLE", "start", 1.0, n=0))
    out = ask(server.path, {"cmd": "sticks", "op": "status"})
    assert out["result"] == "ok" and out["detail"] == ""
    s = out["sticks"]
    assert s["support"] == "ok" and s["present"] is True and s["enabled"] is False          # ST-01, ST-02
    assert s["device"] == {"name": BOXER_NAME, "path": "", "axes": 8, "buttons": 24}
    assert len(s["axes"]) == 8 and s["buttons"] == 5 and s["session"] == "1.4"
    assert s["remote"]["state"] == "IDLE" and s["remote"]["output"] == "none"
    assert s["display"]["stateText"] == "Нет команд"
    assert set(s["sender"]) == {"n", "sent", "last", "off_reason"} and s["sender"]["sent"] == 0
    assert {"samples", "median_ms", "p99_ms", "last_ms", "recent_ms", "shown_ms", "source"} <= set(s["latency"])
    assert ui.backend.peer.frames == []


def test_enable_requires_confirmed_flag(ui, server):  # noqa: F811
    ui.backend.src.plug()
    feed(ui)
    for req in ({"cmd": "sticks", "op": "enable"}, {"cmd": "sticks", "op": "enable", "confirmed": False}):
        out = ask(server.path, req)
        assert out["result"] == "not-confirmed" and out["sticks"]["enabled"] is False
    out = ask(server.path, {"cmd": "sticks", "op": "enable", "confirmed": True})
    assert out["result"] == "ok" and out["sticks"]["enabled"] is True
    assert ui.backend.sticks.enabled
    feed(ui, steps=3)
    assert len(ui.backend.peer.frames) == 3
    out = ask(server.path, {"cmd": "sticks", "op": "disable"})                 # без подтверждения
    assert out["result"] == "ok" and out["sticks"]["enabled"] is False
    feed(ui, steps=3)
    assert len(ui.backend.peer.frames) == 4 and ui.backend.peer.frames[-1]["en"] is False   # ST-07


@pytest.mark.parametrize("case,code", [("no-device", "no-device"), ("no-link", "no-link"), ("old", "old-remote")])
def test_enable_refused_with_reason(ui, server, case, code):  # noqa: F811
    if case != "no-device":
        ui.backend.src.plug()
    feed(ui, status=LOST if case == "no-link" else None, session="1.3" if case == "old" else "1.4")
    out = ask(server.path, {"cmd": "sticks", "op": "enable", "confirmed": True})
    assert out["result"] == code and out["detail"] != "" and out["sticks"]["enabled"] is False
    assert out["sticks"]["result"] == code
    feed(ui, steps=3)
    assert ui.backend.peer.frames == []


@pytest.mark.parametrize("req", [{"cmd": "sticks"}, {"cmd": "sticks", "op": "toggle"},
                                 {"cmd": "sticks", "op": "enable", "confirmed": "yes"},
                                 {"cmd": "sticks", "op": "status", "extra": 1}])
def test_bad_requests(ui, server, req):  # noqa: F811
    out = ask(server.path, req)
    assert out["result"] == "bad-request" and "sticks" in out and out["sticks"]["enabled"] is False


def test_unknown_cmd_lists_sticks(ui, server):  # noqa: F811
    out = ask(server.path, {"cmd": "nope"})
    assert out["result"] == "bad-request" and "sticks" in out["detail"]
