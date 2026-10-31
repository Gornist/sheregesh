# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструмент испытаний канала pi1↔pi2 (tools/probe/check_link.py): сбой стенда не превращается в PASS,
отказ сервера засчитывается только с доказательствами, изменённое состояние восстанавливается."""
import json
import socket
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import check_link as cl  # noqa: E402


@pytest.mark.parametrize("exc, data, kind", [
    (ssl.SSLError(1, "alert"), None, "tls-rejected"),
    (socket.timeout("timed out"), None, "timeout"),
    (TimeoutError(), None, "timeout"),
    (ConnectionRefusedError(), None, "conn-error"),
    (ConnectionResetError(), None, "closed"),
    (None, b"", "closed"),
    (None, b'{"type":"error","body":{"code":"LINK-AUTH"}}\n', "auth-error"),
    (None, b'{"proto":"1.1","type":"hello"}\n', "data"),
    (None, b"garbage", "unknown"),
])
def test_classify_client(exc, data, kind):
    assert cl.classify_client(exc=exc, data=data)[0] == kind


@pytest.mark.parametrize("client, auth, sessions, result", [
    (("tls-rejected", ""), ["auth: TLS handshake from 192.168.1.5 failed"], [], "REJECTED"),
    (("closed", ""), ["auth: reject 192.168.1.5: identity"], [], "REJECTED"),
    (("auth-error", ""), ["auth: reject 192.168.1.5: identity"], [], "REJECTED"),
    (("tls-rejected", ""), [], [], "INCONCLUSIVE"),          # нет записи сервера — отказ не доказан
    (("timeout", ""), ["auth: ..."], [], "INCONCLUSIVE"),    # таймаут — не успешный отказ
    (("conn-error", ""), [], [], "INCONCLUSIVE"),
    (("unknown", ""), ["auth: ..."], [], "INCONCLUSIVE"),
    (("data", ""), [], [], "ACCEPTED"),
    (("closed", ""), ["auth: ..."], ["session from 192.168.1.5"], "ACCEPTED"),
])
def test_judge_attempt_needs_all_evidence(client, auth, sessions, result):
    assert cl.judge_attempt(client, auth, sessions) == result


def test_pause_always_resumes_process(monkeypatch):
    """Прерывание посреди сценария (Ctrl-C, сбой SSH) не оставляет сервер блока замороженным."""
    calls = []

    def fake_ssh(host, cmd, timeout=120, check=False):
        calls.append(cmd)
        if cmd.startswith("systemctl show -p MainPID"):
            return "4242"
        if "NRestarts" in cmd:
            return "0"
        return ""
    state = {"n": 0}

    def fake_wait(pred, timeout, step=0.5):
        state["n"] += 1
        if state["n"] == 1:
            return 0.1                                   # соединение до сценария есть
        raise KeyboardInterrupt
    monkeypatch.setattr(cl, "ssh", fake_ssh)
    monkeypatch.setattr(cl, "wait", fake_wait)
    with pytest.raises(KeyboardInterrupt):
        cl.sc_pause("c", "r", 5, False)
    assert "kill -STOP 4242" in calls
    assert any(c.startswith("kill -CONT 4242") for c in calls[calls.index("kill -STOP 4242"):])


def test_pause_without_pid_is_error_not_pass(monkeypatch):
    monkeypatch.setattr(cl, "ssh", lambda h, c, timeout=120, check=False: "0" if "MainPID" in c else "")
    monkeypatch.setattr(cl, "wait", lambda *a, **k: 0.1)
    assert cl.sc_pause("c", "r", 5, False)["verdict"] == "ERROR"


def test_ports_judgement():
    listening = ["tcp 127.0.0.1:7700", "tcp6 ::1:631", "tcp 0.0.0.0:22", "tcp6 :::22", "udp 0.0.0.0:68",
                 "udp 127.0.0.53:53", "tcp 0.0.0.0:7443"]
    assert cl.judge_ports("remote", listening, [22, 7443]) == []
    assert cl.judge_ports("console", listening, [22]) == ["tcp 0.0.0.0:7443"]        # пульт не слушает 7443
    assert cl.judge_ports("remote", [*listening, "udp6 :::7444"], [22, 7443]) == ["udp6 :::7444"]   # UDP видео нет
    assert cl.judge_ports("remote", [*listening, "tcp 0.0.0.0:7444"], [22, 7443, 7444]) == \
        ["tcp 0.0.0.0:7444", "lan tcp 7444"]                                           # TLS-видео удалено
    assert cl.judge_ports("console", ["tcp 0.0.0.0:7444"], [7444]) == ["tcp 0.0.0.0:7444", "lan tcp 7444"]


def test_decode_proc_net_addresses():
    assert cl._decode("tcp", "0100007F:1E14") == "tcp 127.0.0.1:7700"
    assert cl._decode("tcp6", "00000000000000000000000000000000:0016") == "tcp6 :::22"
    assert cl._decode("tcp6", "00000000000000000000000001000000:0277") == "tcp6 ::1:631"


def test_health_skip_in_network_mode_is_fail(monkeypatch):
    checks = [{"id": i, "status": "PASS"} for i in cl.NET_CHECKS]
    skipped = [{"id": i, "status": "SKIP" if i == "certs.device" else "PASS"} for i in cl.NET_CHECKS]
    replies = {"c": {"verdict": "PASS", "checks": checks}, "r": {"verdict": "PASS", "checks": skipped}}
    monkeypatch.setattr(cl, "ssh", lambda h, c, timeout=120, check=False: json.dumps(replies[h]))
    r = cl.sc_health("c", "r")
    assert r["verdict"] == "FAIL" and r["network_checks_not_pass"] == ["remote:certs.device=SKIP"]
    monkeypatch.setattr(cl, "ssh", lambda h, c, timeout=120, check=False: "not json")
    assert cl.sc_health("c", "r")["verdict"] == "ERROR"


def test_main_tool_failures_are_error(monkeypatch, tmp_path):
    monkeypatch.setattr(cl, "ssh", lambda h, c, timeout=120, check=False: "x")

    def boom(c, r):
        raise OSError("ssh: connect timed out")
    monkeypatch.setattr(cl, "SCENARIOS", {"a": boom, "b": lambda c, r: {"verdict": "OK?"},
                                          "c": lambda c, r: {"verdict": "PASS"}})
    rc = cl.main(["--console", "c", "--remote", "r", "--out", str(tmp_path), "--only", "a,b,c"])
    s = json.loads((tmp_path / "summary.json").read_text())
    assert rc == 1 and s["results"] == {"a": "ERROR", "b": "ERROR", "c": "PASS"}
    assert s["counts"] == {"PASS": 1, "FAIL": 0, "SKIP": 0, "ERROR": 2}
    assert json.loads((tmp_path / "a.json").read_text())["duration_s"] >= 0


def test_unconfirmed_resume_aborts_remaining_scenarios(monkeypatch, tmp_path):
    """SIGCONT не подтверждён (SSH пропал) — ERROR, остальные сценарии не выполняются (SKIP)."""
    def fake_ssh(host, cmd, timeout=120, check=False):
        if "MainPID" in cmd:
            return "4242"
        if "NRestarts" in cmd:
            return "0"
        return ""                                   # в том числе ответ на kill -CONT: подтверждения нет
    monkeypatch.setattr(cl, "ssh", fake_ssh)
    monkeypatch.setattr(cl, "wait", lambda *a, **k: 0.1)
    monkeypatch.setattr(cl, "jread", lambda h, p: {"peerLink": "RECONNECTING"})
    r = cl.sc_pause("c", "r", 0, False)
    assert r["verdict"] == "ERROR" and r["abort"]
    ran = []
    monkeypatch.setattr(cl, "SCENARIOS", {"hb-loss": lambda c, r: cl.sc_pause(c, r, 0, False),
                                          "restart": lambda c, r: ran.append(1) or {"verdict": "PASS"}})
    assert cl.main(["--console", "c", "--remote", "r", "--out", str(tmp_path), "--only", "hb-loss,restart"]) == 1
    s = json.loads((tmp_path / "summary.json").read_text())
    assert s["results"] == {"hb-loss": "ERROR", "restart": "SKIP"} and not ran


def test_ports_srtp_chrony_and_video_service_sockets():
    """ADR-0016/0017: UDP видео (пульт 7444/7445, блок 7445) и chrony 123 — разрешены; временный UDP — только сокет
    службы видео (udpsink GStreamer), чужой временный — нет. Стенд 2026-10-29: ports FAIL на исправных платах."""
    console = ["udp 0.0.0.0:123", "udp6 :::123", "udp 0.0.0.0:7444", "udp 0.0.0.0:7445", "udp 0.0.0.0:60073",
               "udp6 :::36755", "udp 0.0.0.0:68"]
    owned = {"udp 0.0.0.0:60073", "udp6 :::36755"}
    assert cl.judge_ports("console", console, [22], video_owned=owned) == []
    assert cl.judge_ports("console", console, [22]) == ["udp 0.0.0.0:60073", "udp6 :::36755"]   # чей — неизвестно
    remote = ["udp 0.0.0.0:7445", "udp 0.0.0.0:59726", "udp 0.0.0.0:123", "tcp 0.0.0.0:7443"]
    assert cl.judge_ports("remote", remote, [22, 7443], video_owned={"udp 0.0.0.0:59726"}) == []
    assert cl.judge_ports("remote", ["udp 0.0.0.0:7444"], [22]) == ["udp 0.0.0.0:7444"]      # блок 7444/udp не слушает
    assert cl.judge_ports("console", ["udp 0.0.0.0:5353"], [22], video_owned={"udp 0.0.0.0:5353"}) == \
        ["udp 0.0.0.0:5353"]                                                                   # не временный — нет
