# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Протокол 1.6 (sheregesh.link.remotecmd, sheregesh_remote.maintenance): провод remote-cmd, сборка частей журнала,
обработчик блока — журнал (journalctl, фильтр секретов, ограничение), выключение (юнит, polkit), busy, bad-request."""
import asyncio
import base64
import hashlib
import subprocess

import pytest

from sheregesh.link import remotecmd
from sheregesh.link.codec import TYPES
from sheregesh_remote import maintenance as mt

CANARY = "SHRG-CANARY-LOG-abc123"
KEY_HEADER = "-----BEGIN " + "PRIVATE KEY-----"          # из частей: сканер истории (REQ-REL-011) не видит шаблон


def test_types_and_check_command():
    assert {"remote-cmd", "remote-log", "remote-cmd-result"} <= TYPES["1.6"]
    assert not {"remote-cmd", "remote-log", "remote-cmd-result"} & TYPES["1.5"]
    assert remotecmd.check_command({"req": 3, "op": "log-export"}) == {"req": 3, "op": "log-export"}
    assert remotecmd.check_command({"req": 4, "op": "shutdown", "confirm": True})["op"] == "shutdown"
    for bad in ({"req": 1, "op": "shutdown"}, {"req": 1, "op": "shutdown", "confirm": 1}, {"req": 0, "op": "log-export"},
                {"req": True, "op": "log-export"}, {"req": 1, "op": "reboot"}, {"req": 1, "op": "sh -c id"}, []):
        with pytest.raises(remotecmd.BadCommand):
            remotecmd.check_command(bad)


def roundtrip(data):
    parts = remotecmd.split(data)
    asm = remotecmd.Assembly(1)
    for i, p in enumerate(parts):
        asm.add({"req": 1, "part": i, "parts": len(parts), "data": p})
    return asm.finish({"parts": len(parts), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}), parts


@pytest.mark.parametrize("size", [0, 1, remotecmd.CHUNK, remotecmd.CHUNK + 1, remotecmd.MAX_LOG])
def test_split_and_assemble(size):
    data = bytes(i % 251 for i in range(size))
    got, parts = roundtrip(data)
    assert got == data and len(parts) <= remotecmd.MAX_PARTS
    assert all(len(p) < 48 * 1024 for p in parts)          # строка сообщения < MAX_LINE 64 КБ


def test_assembly_rejects_disorder_and_mismatch():
    parts = remotecmd.split(b"x" * (remotecmd.CHUNK * 2))
    asm = remotecmd.Assembly(1)
    asm.add({"part": 1, "parts": 2, "data": parts[1]})
    with pytest.raises(ValueError, match="bad part"):
        asm.finish({})
    asm = remotecmd.Assembly(1)
    asm.add({"part": 0, "parts": 2, "data": parts[0]})
    with pytest.raises(ValueError, match="incomplete"):
        asm.finish({"parts": 2, "size": remotecmd.CHUNK * 2, "sha256": ""})
    asm.add({"part": 1, "parts": 2, "data": parts[1]})
    with pytest.raises(ValueError, match="checksum"):
        asm.finish({"parts": 2, "size": remotecmd.CHUNK * 2, "sha256": "0" * 64})
    asm = remotecmd.Assembly(1)
    asm.add({"part": 0, "parts": 1, "data": "!!"})
    assert asm.error == "bad base64"


def test_limit_keeps_last_whole_lines():
    data = b"".join(b"line %06d\n" % i for i in range(1000))
    cut = remotecmd.limit(data, 100)
    assert len(cut) <= 100 and cut.endswith(b"line 000999\n") and cut.startswith(b"line ")


class Run:
    def __init__(self, rc=0, out=b"", err=b"", exc=None):
        self.rc, self.out, self.err, self.exc, self.argv = rc, out, err, exc, []

    def __call__(self, argv, **kw):
        self.argv.append(argv)
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.err)


def handle(m, body):
    sent = []

    async def send(mtype, b):
        sent.append((mtype, b))

    async def go():
        await m.handle(body, send)
    asyncio.run(go())
    return sent


def test_log_export_redacts_and_reports_checksum():
    run = Run(out=f"svc started\npassword=hunter2 token: x\n{CANARY}\n{KEY_HEADER}\n".encode())
    sent = handle(mt.Maintenance(True, log=lambda s: None, run=run), {"req": 7, "op": "log-export"})
    assert run.argv[0][:2] == ["journalctl", "-b"] and "sheregesh-*" in run.argv[0]
    logs, (rtype, res) = sent[:-1], sent[-1]
    assert rtype == "remote-cmd-result" and res["result"] == "ok" and res["parts"] == len(logs)
    data = b"".join(base64.b64decode(b["data"]) for _, b in logs)
    assert res["sha256"] == hashlib.sha256(data).hexdigest() and res["size"] == len(data)
    assert b"svc started" in data and b"hunter2" not in data and CANARY.encode() not in data
    assert KEY_HEADER.encode() not in data and data.count(b"[REDACTED]") == 4
    assert res["name"].startswith("sheregesh-remote-log-")


@pytest.mark.parametrize("run", [Run(rc=1, err=b"No journal files were opened"),
                                 Run(exc=subprocess.TimeoutExpired("journalctl", 20)), Run(exc=OSError("no exec"))])
def test_log_export_failure_is_an_error_result(run):
    sent = handle(mt.Maintenance(True, log=lambda s: None, run=run), {"req": 2, "op": "log-export"})
    assert [t for t, _ in sent] == ["remote-cmd-result"] and sent[0][1]["result"] == "error" and sent[0][1]["error"]


def test_emulator_log_is_agent_ring():
    mt.remember("agent line one")
    sent = handle(mt.Maintenance(False, log=lambda s: None, run=Run(exc=AssertionError)), {"req": 1, "op": "log-export"})
    data = b"".join(base64.b64decode(b["data"]) for t, b in sent if t == "remote-log")
    assert b"agent line one" in data and sent[-1][1]["result"] == "ok"


def test_shutdown_starts_only_the_poweroff_unit():
    run, logs = Run(), []
    sent = handle(mt.Maintenance(True, log=logs.append, run=run), {"req": 5, "op": "shutdown", "confirm": True})
    assert run.argv == [["systemctl", "start", "--no-block", "sheregesh-remote-poweroff.service"]]
    assert sent == [("remote-cmd-result", {"req": 5, "op": "shutdown", "result": "ok", "error": ""})]
    assert any("shutdown requested by console" in s for s in logs)


def test_shutdown_refused_by_polkit_is_reported():
    run = Run(rc=4, err=b"Access denied")
    sent = handle(mt.Maintenance(True, log=lambda s: None, run=run), {"req": 5, "op": "shutdown", "confirm": True})
    assert sent[0][1]["result"] == "error" and "Access denied" in sent[0][1]["error"]


def test_shutdown_without_confirm_and_unknown_op_are_bad_requests():
    run = Run()
    m = mt.Maintenance(True, log=lambda s: None, run=run)
    for body in ({"req": 1, "op": "shutdown"}, {"req": 2, "op": "exec", "args": ["id"]}):
        assert handle(m, body)[0][1]["result"] == "bad-request"
    assert run.argv == []


def test_second_command_while_busy():
    m = mt.Maintenance(True, log=lambda s: None, run=Run())
    m.busy = True
    sent = handle(m, {"req": 9, "op": "log-export"})
    assert sent == [("remote-cmd-result", {"req": 9, "op": "log-export", "result": "busy", "error": "another command runs"})]
