# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сетевой лабораторный профиль SHRG-LAB (S3.1): TLS 1.3 + взаимная аутентификация, два процесса через 127.0.0.1.

CA стенда, ключи и сертификаты создаются во временном каталоге (openssl); в репозиторий ничего не попадает.
"""
import json
import shutil
import signal
import socket
import ssl
import time
from pathlib import Path

import pytest

from procs import Agent, free_port, wait_for
from sheregesh.link import pki, tls
from sheregesh.link.client import PeerClient

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab")
    ca, rogue = root / "ca", root / "rogue-ca"
    pki.ca_init(ca)
    pki.ca_init(rogue, name="Rogue CA")

    def device(name, role, did, peer, signer=ca, peer_hosts=("127.0.0.1",)):
        d = root / name
        cert = pki.ca_sign(signer, pki.keygen(role, did, d), role, did)
        pki.install(d, cert, (signer / "ca.crt").read_text(), role, did)
        if signer is not ca:                     # доверяет CA стенда, но свой сертификат — от чужого CA
            shutil.copyfile(ca / "ca.crt", d / "ca.crt")
        return pki.configure(root / f"{name}.json", role, did, peer, list(peer_hosts), None, d)
    return {
        "remote": device("remote", "remote", "PI2SERIAL", "console:PI1SERIAL"),
        "console": device("console", "console", "PI1SERIAL", "remote:PI2SERIAL"),
        "other": device("other", "console", "INTRUDER", "remote:PI2SERIAL"),          # сертификат CA стенда, чужой URI
        "rogue": device("rogue", "console", "PI1SERIAL", "remote:PI2SERIAL", signer=rogue),  # верный URI, чужой CA
        "root": root,
    }


def net_agent(lab, *extra):
    return Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--event-interval", "0.5",
                 "--heartbeat-interval", "0.2", "--no-video", *extra)


def client(cfg, port, timeout=1.0):
    return PeerClient(None, port, heartbeat_timeout_s=timeout, tls_cfg=cfg).start()


def test_mutual_tls_session_telemetry_events_and_acks(lab):
    a = net_agent(lab)
    c = client(lab["console"], a.port)
    try:
        assert "tls=1.3 mutual peer=urn:sheregesh:console:PI1SERIAL" in a.banner[-1]
        assert wait_for(lambda: c.snapshot()["phase"] == "connected", 10), c.snapshot()
        snap = c.snapshot()
        assert snap["tls"] == "TLSv1.3" and snap["session"] == "1.2" and snap["secure"]
        assert snap["peer"]["device_id"] == "remote-emu-01"
        assert wait_for(lambda: (c.snapshot()["telemetry"] or {}).get("link", {}).get("events_acked", 0) >= 3, 10)
        link = c.snapshot()["telemetry"]["link"]
        assert link["acks"] > link["events_acked"] > 0 and 0 < link["rtt_avg_ms"] < 500
        events = [code for code, _ in c.drain_link_events()]
        assert "REMOTE-EVENT" in events                                  # событие доставлено в журнал пульта
    finally:
        c.stop()
        a.stop()


@pytest.mark.parametrize("who, reason", [("other", "identity"), ("rogue", "")])
def test_wrong_certificate_is_rejected_on_both_sides(lab, who, reason):
    a = net_agent(lab)
    c = client(lab[who], a.port)
    try:
        assert wait_for(lambda: c.snapshot()["phase"] == "auth-failed", 10), c.snapshot()
        assert c.snapshot()["detail"].startswith("auth")
        time.sleep(0.5)
    finally:
        c.stop()
        a.stop()
    log = a.stderr()
    assert "auth:" in log and "session from" not in log, log
    if reason:
        assert reason in log


def test_client_rejects_server_with_wrong_identity(lab):
    """Пульт с ожидаемым peer_id другого блока не принимает подлинный, но не тот блок."""
    a = net_agent(lab)
    d = lab["console"]
    wrong = tls.LinkConfig(d.role, d.device_id, "remote:SOMEONE-ELSE", d.dir, port=a.port, peer_hosts=("127.0.0.1",))
    c = client(wrong, a.port)
    try:
        assert wait_for(lambda: c.snapshot()["phase"] == "auth-failed", 10)
        assert "identity" in c.snapshot()["detail"]
    finally:
        c.stop()
        a.stop()


def test_plain_tcp_and_tls_without_client_certificate_are_rejected(lab):
    a = net_agent(lab)
    try:
        with socket.create_connection(("127.0.0.1", a.port), timeout=3) as s:     # без TLS: данные SHRG-LAB не идут
            s.sendall(b'{"proto":"1.1","type":"hello","seq":1,"ts":0,"body":{}}\n')
            s.settimeout(6)
            assert b'"type":"hello"' not in s.recv(4096)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)                            # TLS, но без сертификата клиента
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", a.port), timeout=3) as raw:
            with ctx.wrap_socket(raw) as s:
                s.settimeout(6)
                with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
                    data = s.recv(4096)
                    assert data == b""
                    raise ConnectionError("closed without data")
    finally:
        a.stop()
    assert a.stderr().count("auth:") >= 2


def test_incompatible_protocol_over_tls(lab):
    a = net_agent(lab, "--protocol", "2.0")
    c = client(lab["console"], a.port)
    try:
        assert wait_for(lambda: c.snapshot()["phase"] == "incompatible", 10), c.snapshot()
        assert c.snapshot()["detail"] == "major" and c.snapshot()["tls"] in ("TLSv1.3", None)
    finally:
        c.stop()
        a.stop()


def test_loss_detection_and_recovery_after_server_restart(lab):
    a = net_agent(lab)
    port = a.port
    c = client(lab["console"], port, timeout=1.0)
    try:
        assert wait_for(lambda: c.snapshot()["phase"] == "connected", 10)
        a.signal(signal.SIGSTOP)                                                 # блок «завис»: heartbeat нет
        assert wait_for(lambda: c.snapshot()["phase"] != "connected", 6)
        assert wait_for(lambda: c.snapshot()["lost_s"] is not None, 3)
        a.signal(signal.SIGCONT)
        a.stop()
        a = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--no-video", port=port)
        assert wait_for(lambda: c.snapshot()["phase"] == "connected", 15), c.snapshot()
        assert c.counters.connects >= 2 and c.snapshot()["lost_s"] is None
    finally:
        c.stop()
        a.stop()


def test_server_refuses_to_start_without_keys(lab, tmp_path):
    cfg = json.loads(Path(lab["remote"].source).read_text())
    cfg["dir"] = str(tmp_path / "empty")
    (tmp_path / "link.json").write_text(json.dumps(cfg))
    import subprocess
    import sys
    from procs import ENV
    r = subprocess.run([sys.executable, "-m", "sheregesh_remote", "--link-config", str(tmp_path / "link.json")],
                       env=ENV, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "не запускается" in r.stderr


def test_key_with_open_permissions_is_refused(lab, tmp_path):
    d = tmp_path / "d"
    shutil.copytree(lab["remote"].dir, d)
    (d / "device.key").chmod(0o644)
    with pytest.raises(tls.LinkConfigError, match="0600"):
        pki.configure(tmp_path / "x.json", "remote", "PI2SERIAL", "console:PI1SERIAL", [], None, d)


def test_internet_addresses_are_not_lab_addresses():
    assert tls.is_lab_address("192.168.1.169") and tls.is_lab_address("127.0.0.1") and tls.is_lab_address("fe80::1")
    assert not tls.is_lab_address("8.8.8.8") and not tls.is_lab_address("2001:4860::8888")
    assert not tls.peer_allowed("192.168.7.5", nets=[__import__("ipaddress").ip_interface("192.168.1.89/24")])
    assert tls.peer_allowed("192.168.1.169", nets=[__import__("ipaddress").ip_interface("192.168.1.89/24")])
    with pytest.raises(ValueError):
        PeerClient("192.168.1.169", 7700)                                       # без TLS — только loopback


def test_rotated_certificate_replaces_old_without_downtime_of_config(lab, tmp_path):
    """Замена сертификата: новый ключ и сертификат устанавливаются атомарно; старый ключ больше не действует."""
    d = tmp_path / "rot"
    shutil.copytree(lab["console"].dir, d)
    old = (d / "device.crt").read_text()
    csr = pki.keygen("console", "PI1SERIAL", d)
    assert (d / "device.key.new").exists() and (d / "device.crt").read_text() == old     # до установки — старый
    cert = pki.ca_sign(lab["root"] / "ca", csr, "console", "PI1SERIAL")
    pki.install(d, cert, (lab["root"] / "ca" / "ca.crt").read_text(), "console", "PI1SERIAL")
    assert not (d / "device.key.new").exists() and (d / "device.crt").read_text() != old
    cfg = pki.configure(tmp_path / "rot.json", "console", "PI1SERIAL", "remote:PI2SERIAL", ["127.0.0.1"], None, d)
    a = net_agent(lab)
    c = client(cfg, a.port)
    try:
        assert wait_for(lambda: c.snapshot()["phase"] == "connected", 10)
    finally:
        c.stop()
        a.stop()


def test_agent_notifies_ready_and_watchdog(lab, monkeypatch):
    """Type=notify: READY=1 после того, как сервер слушает; WATCHDOG=1 из основного цикла."""
    import os
    import tempfile

    import procs
    d = tempfile.mkdtemp(prefix="sd", dir="/tmp")
    path = os.path.join(d, "n")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.settimeout(10)
    monkeypatch.setitem(procs.ENV, "NOTIFY_SOCKET", path)
    monkeypatch.setitem(procs.ENV, "WATCHDOG_USEC", "2000000")
    a = net_agent(lab)
    try:
        got = [s.recv(256).decode() for _ in range(3)]
        assert got[0].startswith("READY=1") and "mTLS" in got[0]
        assert "WATCHDOG=1" in got[1:]
    finally:
        a.stop()
        s.close()
        shutil.rmtree(d)
