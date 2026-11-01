# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Локальный канал пульта, команда «remote» (протокол 1.6; offscreen): log-export и shutdown теми же слотами Backend,
что кнопки «Журналы» и «Настройки»; выключение — только с флагом confirmed. Клиент канала подменный (FakeRemote)."""
import os
import shutil
import tempfile

import pytest

from sheregesh.link import remotectl, videoselect
from sheregesh_console.control import ControlServer
from test_changing_actions import FakeRemote, ui  # noqa: F401 — фикстура пульта
from test_console_control import in_thread
from test_console_sticks_control import ask


@pytest.fixture
def server(ui):  # noqa: F811
    d = tempfile.mkdtemp(prefix="shr-")
    srv = ControlServer(ui.backend, os.path.join(d, videoselect.CONSOLE_SOCKET)).start()
    yield srv
    srv.stop()
    shutil.rmtree(d, ignore_errors=True)


def test_log_export_via_control(ui, server):  # noqa: F811
    fake = FakeRemote()
    ui.backend._module_peer = lambda: fake
    out = ask(server.path, {"cmd": "remote", "op": "log-export", "wait": 10})
    assert out["result"] == "ok" and out["file"] == "sheregesh-remote-log-20261101-120000.txt", out
    assert (ui.backend.usb_dir / out["file"]).exists() and fake.calls == ["log-export"]


def test_shutdown_requires_confirmed(ui, server):  # noqa: F811
    fake = FakeRemote()
    ui.backend._module_peer = lambda: fake
    for req in ({"cmd": "remote", "op": "shutdown"}, {"cmd": "remote", "op": "shutdown", "confirmed": False}):
        assert ask(server.path, req)["result"] == "not-confirmed"
    assert fake.calls == []
    out = ask(server.path, {"cmd": "remote", "op": "shutdown", "confirmed": True, "wait": 10})
    assert out["result"] == "ok" and fake.calls == ["shutdown"]


def test_failure_code_is_reported(ui, server):  # noqa: F811
    class Old:
        def remote_command(self, op, wait_s):
            return {"ok": False, "result": "unsupported", "error": "protocol 1.5", "req": 1}
    ui.backend._module_peer = lambda: Old()
    out = ask(server.path, {"cmd": "remote", "op": "log-export", "wait": 10})
    assert out["result"] == "unsupported" and "1.5" in out["detail"]
    for bad in ({"cmd": "remote", "op": "reboot"}, {"cmd": "remote", "op": "log-export", "extra": 1}):
        assert ask(server.path, bad)["result"] == "bad-request"


def test_remotectl_cli(ui, server, capsys):  # noqa: F811
    ui.backend._module_peer = lambda: FakeRemote()
    assert remotectl.main(["shutdown"]) == 2                                   # без --confirm — ничего не уходит
    assert in_thread(lambda: remotectl.main(["log-export", "--wait", "10", "--socket", server.path]), 25) == 0
    assert '"result": "ok"' in capsys.readouterr().out
