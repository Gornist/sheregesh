# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S6 (ADR-0012, раздел 6.1; CHG-012): совместимость протокола 1.3 с пиром 1.2.

Блок 1.2 + пульт 1.3: module_command → unsupported, status.module нет. Сырой сеанс 1.2 с module-cmd: не исполняется,
только учитывается. Пульт 1.2 с блоком 1.3: прежнее поведение (video-select исполняется, status.module нет).
Блок 1.3 без подсистемы (--no-module): status.module нет, module_command → unsupported."""
import json
import socket
import time

from procs import Agent, wait_for
from sheregesh import PROTOCOL_VERSION
from sheregesh.link.client import PeerClient
from sheregesh.link.codec import Decoder, encode


def _raw(port, proto, messages, read_s=2.0):
    """Пульт «по проводу»: hello с версией proto, затем messages [(тип, тело)]; → все принятые status."""
    out = []
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(encode(proto, "hello", 1, 0.0, {"role": "console", "proto": proto, "software": "t", "device_id": "t"}))
        for i, (mtype, body) in enumerate(messages, 2):
            s.sendall(encode(proto, mtype, i, 0.0, body))
        dec, end = Decoder(), time.monotonic() + read_s
        s.settimeout(0.2)
        while time.monotonic() < end:
            try:
                data = s.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            out += [m["body"] for m in dec.feed(data) if isinstance(m, dict) and m["type"] == "status"]
    return out


def test_old_remote_12_new_console_unsupported():
    a = Agent("--protocol", "1.2", "--module", "--heartbeat-interval", "0.3")
    pc = PeerClient("127.0.0.1", a.port).start()
    try:
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected" and pc.snapshot()["telemetry"], 5)
        assert pc.snapshot()["session"] == "1.2"
        r = pc.module_command("status")
        assert r["result"] == "unsupported" and "1.2" in r["error"]
        assert pc.snapshot()["module_cmd"]["state"] == "unsupported"
        time.sleep(1.0)
        assert "module" not in pc.snapshot()["telemetry"]
    finally:
        pc.stop()
        a.stop()
    assert "module-cmd" not in a.stderr()


def test_raw_12_session_module_cmd_not_executed():
    a = Agent("--module", "--heartbeat-interval", "0.3")
    try:
        sts = _raw(a.port, "1.2", [("module-cmd", {"req": 9, "op": "configure", "args": {"enabled": False}}),
                                   ("module-cmd", {"req": 10, "op": "lab", "args": {"scenario": "detach"}})])
        assert sts and all("module" not in s for s in sts)
        assert sts[-1]["video"] is not None
        # контроль: сессия 1.3 видит модуль, команда 1.2 ничего не изменила (модуль включён, не DISABLED)
        sts = _raw(a.port, "1.3", [], read_s=4.0)
        m = sts[-1]["module"]
        assert m["settings"]["enabled"] is True and m["state"] != "DISABLED" and "cmd" not in m
        assert not any(h["reason"] in ("settings-disabled", "absent") for h in m["history"])
    finally:
        a.stop()
    assert "configure" not in a.stderr() and "lab detach" not in a.stderr()


def test_raw_13_bad_module_cmd_is_bad_request():
    a = Agent("--module", "--heartbeat-interval", "0.3")
    try:
        sts = _raw(a.port, "1.3", [("module-cmd", {"req": 4, "op": "lab", "args": {"scenario": "detach", "ch": 1}}),
                                   ("module-cmd", {"req": 5, "op": "arm"})], read_s=1.5)
        cmds = [s["module"].get("cmd") for s in sts if "module" in s]
        assert {"req": 4, "op": "lab", "result": "bad-request", "error": cmds[0]["error"]} in cmds
        assert cmds[-1]["req"] == 5 and cmds[-1]["result"] == "unknown-op" and cmds[-1]["op"] == ""
        assert not any(h["reason"] == "absent" for s in sts for h in s["module"]["history"])
    finally:
        a.stop()


def test_old_console_12_with_new_remote_unchanged(tmp_path):
    cfg = tmp_path / "video-input.json"
    cfg.write_text(json.dumps({"source": "synthetic", "rtsp_urls": ["rtsp://127.0.0.1:9/cam"]}))
    a = Agent("--module", "--video", "--video-config", str(cfg), "--heartbeat-interval", "0.3")
    try:
        sts = _raw(a.port, "1.2", [("video-select", {"id": "rtsp:0", "req": 5})], read_s=3.0)
        assert sts and all("module" not in s for s in sts)
        assert any((s["video"].get("select") or {}).get("result") == "ok" for s in sts)
    finally:
        a.stop()


def test_remote_13_without_module_subsystem():
    a = Agent("--no-module", "--heartbeat-interval", "0.3")
    pc = PeerClient("127.0.0.1", a.port).start()
    try:
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected" and pc.snapshot()["telemetry"], 5)
        assert pc.snapshot()["session"] == PROTOCOL_VERSION and "module" not in pc.snapshot()["telemetry"]
        r = pc.module_command("status")
        assert r["result"] == "unsupported" and "disabled" in r["error"]
        sts = []
        pc.stop()
        sts = _raw(a.port, "1.3", [("module-cmd", {"req": 1, "op": "status"})], read_s=1.0)
        assert sts and all("module" not in s for s in sts)
    finally:
        pc.stop()
        a.stop()
    assert "module-cmd ignored: module subsystem disabled" in a.stderr()
