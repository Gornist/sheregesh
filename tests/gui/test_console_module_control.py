# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Локальный канал пульта, команда «module» (S6, ADR-0012, 9.3; offscreen, L3): запрос выполняется теми же слотами
ModuleView с confirmed=True, что кнопки экрана «Модуль», в сеансе пульта; ответ — итог команды и снимок M.

Блок подменён (телеметрия status.module и клиент с module_command, как в test_gui_module); канал — настоящий
unix-сокет во временном каталоге 0700. Команда modulectl (агент B) здесь не используется — запросы пишутся в сокет.
"""
import json
import os
import shutil
import socket
import tempfile

import pytest

from sheregesh.link import videoselect
from sheregesh_console import module as module_mod
from sheregesh_console.control import ControlServer
from test_console_control import in_thread
from test_gui_module import LOST, READY, feed, ui  # noqa: F401 — фикстура пульта с подменным блоком
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта (общая для экранов)


@pytest.fixture
def server(ui):  # noqa: F811
    d = tempfile.mkdtemp(prefix="shm-")              # каталог 0700; путь короче предела sun_path (104 байта, macOS)
    srv = ControlServer(ui.backend, os.path.join(d, videoselect.CONSOLE_SOCKET)).start()
    yield srv
    srv.stop()
    shutil.rmtree(d, ignore_errors=True)


def ask(path, req):
    def run():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(40)
            s.connect(str(path))
            s.sendall((json.dumps(req) + "\n").encode())
            return json.loads(s.makefile().readline())
    return in_thread(run, timeout=45)


def test_status_returns_snapshot(ui, server):  # noqa: F811
    feed(ui, READY)
    out = ask(server.path, {"cmd": "module", "op": "status"})
    assert out["result"] == "ok" and out["detail"] == ""
    m = out["module"]
    assert m["support"] == "ok" and m["stale"] is False and m["session"] == "1.3"
    assert m["telemetry"]["state"] == "READY" and m["telemetry"]["profile"]["id"] == "radiomaster-nomad"
    assert m["display"]["stateText"] == "Готов" and m["display"]["changedAt"].count(":") == 2
    assert ui.backend.peer.calls == []                  # status — снимок пульта, без команды блоку


@pytest.mark.parametrize("req,call", [
    ({"op": "reinit"}, ("reinit", {}, module_mod.WAIT_S)),
    ({"op": "reset", "wait": 5}, ("reset", {}, module_mod.WAIT_S)),
    ({"op": "lab", "args": {"scenario": "detach"}}, ("lab", {"scenario": "detach"}, module_mod.WAIT_S)),
    ({"op": "configure", "args": {"enabled": False}, "wait": 30},
     ("configure", {"enabled": False}, module_mod.CONFIGURE_WAIT_S)),
    ({"op": "configure", "args": {"auto_init": False, "auto_recover": True}},
     ("configure", {"auto_init": False, "auto_recover": True}, module_mod.CONFIGURE_WAIT_S)),
])
def test_commands_run_in_console_session(ui, server, req, call):  # noqa: F811
    feed(ui, READY)
    out = ask(server.path, {"cmd": "module", **req})
    assert out["result"] == "ok", out
    assert ui.backend.peer.calls == [call]
    assert out["module"]["command"]["state"] == "done" and out["module"]["command"]["op"] == call[0]


@pytest.mark.parametrize("reply,code", [({"ok": False, "result": "not-running", "error": "", "req": 3}, "not-running"),
                                        ({"ok": False, "result": "disabled", "error": "", "req": 3}, "disabled"),
                                        ({"ok": False, "result": "not-saved", "error": "read-only", "req": 3},
                                         "not-saved")])
def test_block_result_codes_are_returned(ui, server, reply, code):  # noqa: F811
    feed(ui, READY)
    ui.backend.peer.reply = reply
    out = ask(server.path, {"cmd": "module", "op": "lab", "args": {"scenario": "crash"}})
    assert out["result"] == code and out["detail"] == reply["error"]


@pytest.mark.parametrize("req,detail", [
    ({"op": "arm"}, "op:"),
    ({"op": "reinit", "args": {"x": 1}}, "reinit: no args"),
    ({"op": "lab", "args": {}}, "lab:"),
    ({"op": "lab", "args": {"scenario": "detach", "channel": 1}}, "lab:"),
    ({"op": "configure", "args": {"backend": "hardware"}}, "configure:"),
    ({"op": "configure", "args": {"enabled": "yes"}}, "configure:"),
    ({"op": "configure", "args": {}}, "configure:"),
    ({"op": "reinit", "wait": "long"}, "wait"),
    ({"op": "status", "args": []}, "args"),
])
def test_bad_requests_change_nothing(ui, server, req, detail):  # noqa: F811
    feed(ui, READY)
    out = ask(server.path, {"cmd": "module", **req})
    assert out["result"] == "bad-request" and detail in out["detail"], out
    assert ui.backend.peer.calls == [] and out["module"]["support"] == "ok"


def test_unknown_lab_scenario_is_bad_request(ui, server):  # noqa: F811
    feed(ui, READY)
    out = ask(server.path, {"cmd": "module", "op": "lab", "args": {"scenario": "arm-motors"}})
    assert out["result"] == "bad-request" and ui.backend.peer.calls == []


def test_no_link_and_old_remote(ui, server):  # noqa: F811
    feed(ui, READY)
    feed(ui, READY, status=LOST)
    out = ask(server.path, {"cmd": "module", "op": "reinit"})
    assert out["result"] == "no-link" and out["module"]["support"] == "no-link" and out["module"]["stale"] is True
    assert out["module"]["display"]["stateText"] == "Нет связи с удалённым блоком"
    feed(ui, None, session="1.2")
    out = ask(server.path, {"cmd": "module", "op": "lab", "args": {"scenario": "detach"}})
    assert out["result"] == "unsupported" and out["module"]["support"] == "old-remote"
    assert ui.backend.peer.calls == []


def test_timeout_and_busy(ui, server):  # noqa: F811
    feed(ui, READY)
    peer = ui.backend.peer
    peer.gate.clear()                                     # блок не отвечает
    out = ask(server.path, {"cmd": "module", "op": "reinit", "wait": 0.5})
    assert out["result"] == "timeout" and out["module"]["command"]["state"] == "sending"
    out = ask(server.path, {"cmd": "module", "op": "reset", "wait": 0.5})    # прежняя команда ещё идёт
    assert out["result"] == "busy" and "reinit" in out["detail"]
    peer.gate.set()


def test_list_still_works_and_cmd_list_mentions_module(server):
    out = ask(server.path, {"cmd": "nope"})
    assert out["result"] == "bad-request" and "module" in out["detail"]
