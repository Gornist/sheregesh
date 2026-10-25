# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Локальный канал управления пультом (S5 GUI-02, offscreen, L3): sheregesh.link.videoselect передаёт выбор
работающему пульту, пульт выполняет Backend.selectVideoSource(id, True) в своём сеансе — второго сеанса управления
с блоком нет.

Блок подменён (Backend._video_report + клиент с select_video_source, как в test_gui_video_select); канал —
настоящий unix-сокет во временном каталоге 0700. Это не проверка на стенде: там — команды из отчёта S5.
"""
import io
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout

import pytest

from sheregesh.link import videoselect
from sheregesh_console import control
from sheregesh_console.control import ControlServer
from sheregesh_console.qtwait import pump, wait_until
from test_gui_tile_geometry import wide_font  # noqa: F401 — фикстура шрифта (общая для экранов)
from test_gui_video_select import SOURCES, ui  # noqa: F401 — фикстура пульта с подменным блоком


@pytest.fixture
def server(ui):  # noqa: F811
    d = tempfile.mkdtemp(prefix="shc-")              # каталог 0700; путь короче предела sun_path (104 байта, macOS)
    srv = ControlServer(ui.backend, os.path.join(d, videoselect.CONSOLE_SOCKET)).start()
    yield srv
    srv.stop()
    shutil.rmtree(d, ignore_errors=True)


def in_thread(fn, timeout=15):
    """Команда — в своём потоке, поток Qt тем временем обрабатывает события (вызовы бэкенда идут через него)."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:               # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert wait_until(lambda: not t.is_alive(), timeout)
    if "error" in box:
        raise box["error"]
    return box["value"]


def cli(*argv):
    out, err = io.StringIO(), io.StringIO()

    def run():
        with redirect_stdout(out), redirect_stderr(err):
            return videoselect.main(list(argv))
    rc = in_thread(run)
    return rc, json.loads(out.getvalue()), err.getvalue()


def raw(path, data):
    def run():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(path))
            try:
                s.sendall(data)
            except OSError:                       # слишком длинный запрос: пульт ответил и закрыл, не дочитав
                pass
            return json.loads(s.makefile().readline())
    return in_thread(run)


def test_socket_is_private(server):
    assert stat.S_IMODE(os.stat(server.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(server.path)).st_mode) == 0o700


def test_refuses_shared_directory(ui, tmp_path):  # noqa: F811
    d = tmp_path / "shared"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(PermissionError):
        ControlServer(ui.backend, d / "s.sock").start()
    assert not (d / "s.sock").exists()


def test_list_via_console(server):
    rc, out, err = cli("--via", "console", "--socket", server.path, "--list")
    assert rc == 0 and out["result"] == "ok" and out["via"] == "console", (out, err)
    assert [s["id"] for s in out["sources"]] == [s["id"] for s in SOURCES] and out["active"] == ["synthetic"]


def test_select_runs_in_console_session(ui, server):  # noqa: F811
    """Выбор — вызовом select_video_source клиента пульта (того же сеанса), итог — по sources[].active блока."""
    b = ui.backend
    rc, out, err = cli("--socket", server.path, "rtsp:1", "--wait", "5")     # --via auto: пульт найден
    assert rc == 0 and out["result"] == "ok" and out["via"] == "console" and out["active"] == ["rtsp:1"], (out, err)
    assert b.peer.calls == ["rtsp:1"] and b.videoSelection["state"] == "idle" and "пульт не запущен" not in err
    rc, out, _ = cli("--socket", server.path, "rtsp:1")                         # уже активен — без нового запроса
    assert rc == 0 and out["result"] == "ok" and b.peer.calls == ["rtsp:1"]


@pytest.mark.parametrize("argv,code,rc", [
    (["v4l2:dead:beef"], "unknown", 1),                                          # нет в списке блока
    ([SOURCES[2]["id"]], "unavailable", 1),                                      # недоступен
])
def test_select_refusals_without_sending(ui, server, argv, code, rc):  # noqa: F811
    got_rc, out, _ = cli("--socket", server.path, *argv)
    assert (got_rc, out["result"]) == (rc, code) and ui.backend.peer.calls == [] and out["active"] == ["synthetic"]


def test_select_refused_by_unit(ui, server):  # noqa: F811
    ui.backend.peer.reply = {"ok": False, "error": "unavailable", "detail": "/dev/video0 busy"}
    rc, out, _ = cli("--socket", server.path, SOURCES[1]["id"])
    assert rc == 1 and out["result"] == "unavailable" and out["detail"] == "/dev/video0 busy"


def test_busy_while_switching(ui, server):  # noqa: F811
    b = ui.backend
    b.peer.gate.clear()                                                          # блок отвечает не сразу
    assert b.selectVideoSource(SOURCES[1]["id"], True)                          # выбор с экрана идёт
    rc, out, _ = cli("--socket", server.path, "rtsp:1")
    assert rc == 1 and out["result"] == "busy" and b.peer.calls == [SOURCES[1]["id"]]
    b.peer.gate.set()
    assert wait_until(lambda: b.videoSelection["state"] == "idle", 3)


@pytest.mark.parametrize("data,code", [
    (b"not json\n", "bad-request"), (b'{"cmd": "reboot"}\n', "bad-request"),
    (b'{"cmd": "select", "id": "rtsp:1 x"}\n', "bad-request"), (b'{"cmd": "select", "id": 5}\n', "bad-request"),
    (b'{"cmd": "select", "id": "rtsp:1", "wait": "x"}\n', "bad-request"), (b"[" * 5000 + b"\n", "bad-request"),
    (b'{"cmd": "select", "id": "../../etc"}\n', "unknown"),        # допустимые символы, но не из списка блока
])
def test_bad_requests(ui, server, data, code):  # noqa: F811
    assert raw(server.path, data)["result"] == code
    assert ui.backend.peer.calls == []


def test_auto_falls_back_to_direct_session_with_warning(tmp_path):
    """Пульт не запущен: --via auto — прямой сеанс (как до S5) с предупреждением; --via console — отказ (код 2)."""
    missing = os.path.join(tempfile.mkdtemp(prefix="shc-"), "none.sock")     # короче предела sun_path
    rc, out, err = cli("--socket", missing, "--link-config", str(tmp_path / "no-link.json"), "--list")
    assert rc == 2 and out["via"] == "direct" and out["result"] == "not-connected" and "link config" in out["detail"]
    assert "пульт не запущен" in err
    rc, out, err = cli("--via", "console", "--socket", missing, "--list")
    assert rc == 2 and out["via"] == "console" and "console not running" in out["detail"]


def test_bad_id_rejected_by_cli():
    with pytest.raises(SystemExit) as e, redirect_stderr(io.StringIO()):
        videoselect.main(["--via", "console", "rtsp:1; rm -rf /"])
    assert e.value.code == 2


def test_default_paths():
    assert control.server_path({}) == ""
    assert control.server_path({"XDG_RUNTIME_DIR": "/run/sheregesh-ui"}) == "/run/sheregesh-ui/sheregesh-console.sock"
    assert videoselect.socket_path({}) == "/run/sheregesh-ui/sheregesh-console.sock"
    assert videoselect.socket_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == "/run/user/1000/sheregesh-console.sock"
    assert videoselect.socket_path({"SHEREGESH_CONSOLE_CONTROL": "/x/s"}) == control.server_path(
        {"SHEREGESH_CONSOLE_CONTROL": "/x/s"}) == "/x/s"
