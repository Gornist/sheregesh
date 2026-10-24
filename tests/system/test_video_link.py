# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Защищённый видеоканал (S3.1-VIDEO, ADR-0011): RTP поверх отдельного TLS 1.3 с взаимной аутентификацией.

GStreamer не нужен: генератор — подставной скрипт (RTP H.264, RFC 4571, SPS 1280×720). CA и сертификаты — во
временном каталоге. Проверяется: сеанс и метрики, отказы аутентификации, привязка к сеансу управления, закрытие при
потере управления, порча байт в пути, отсутствие процессов-генераторов после сеанса.
"""
import os
import shutil
import signal
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest

from procs import Agent, free_port, wait_for
from sheregesh import videolink
from sheregesh.link import pki, tls
from sheregesh.link.client import PeerClient

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")
FAKE = [sys.executable, str(Path(__file__).with_name("fake_rtp_sender.py")), "30"]


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("vlab")
    ca, rogue = root / "ca", root / "rogue-ca"
    pki.ca_init(ca)
    pki.ca_init(rogue, name="Rogue CA")

    def device(name, role, did, peer, signer=ca):
        d = root / name
        cert = pki.ca_sign(signer, pki.keygen(role, did, d), role, did)
        pki.install(d, cert, (signer / "ca.crt").read_text(), role, did)
        if signer is not ca:
            shutil.copyfile(ca / "ca.crt", d / "ca.crt")
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2"),
            "other": device("other", "console", "INTRUDER", "remote:PI2"),
            "rogue": device("rogue", "console", "PI1", "remote:PI2", signer=rogue)}


class Log(list):
    def __call__(self, m):
        self.append(m)


def server(lab, authorize=lambda addr: None, command=FAKE):
    log = Log()
    s = videolink.VideoServer(lab["remote"], "h264", authorize, log, bind="127.0.0.1", port=0, command=command).start()
    return s, log


def stream(client, seconds):
    """client.run в фоне; вывод (RFC 4571) читается из канала постоянно, как декодером. Через seconds возвращает
    (байты на этот момент, причина конца, поток клиента, поток чтения)."""
    r, w = os.pipe()
    got, out = bytearray(), {}

    def run():
        out["why"] = client.run(w)
        os.close(w)

    def drain():
        while True:
            d = os.read(r, 65536)
            if not d:
                break
            got.extend(d)
        os.close(r)
    t = threading.Thread(target=run, daemon=True)
    d = threading.Thread(target=drain, daemon=True)
    t.start()
    d.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and t.is_alive():
        time.sleep(0.05)
    return bytes(got), out, t, d


def test_parse_sps_resolution_and_crop():
    sys.path.insert(0, str(Path(__file__).parent))
    import fake_rtp_sender as f
    assert {k: v for k, v in videolink.parse_sps(f.sps(1280, 720)).items() if k != "profile_idc"} == \
        {"width": 1280, "height": 720}
    assert videolink.parse_sps(f.sps(1920, 1080))["height"] == 1080          # 1088 строк с обрезкой
    stap = b"\x18" + len(f.sps(640, 360)).to_bytes(2, "big") + f.sps(640, 360)
    assert videolink.parse_sps(videolink.sps_from_rtp(stap))["width"] == 640


def test_authenticated_session_streams_with_metrics(lab):
    s, log = server(lab)
    c = videolink.VideoClient(lab["console"], "127.0.0.1", s.port)
    try:
        got, out, t, r = stream(c, 4.0)
        st = c.stats()
        assert st["connected"] and st["tls"] == "TLSv1.3" and st["peer_uri"] == "urn:sheregesh:remote:PI2"
        assert st["frames"] >= 60 and st["seq_gaps"] == 0 and st["bad_rtp"] == 0
        assert st["stream_sps"]["width"] == 1280 and st["stream_sps"]["height"] == 720
        assert st["source"] == {"codec": "h264", "width": 1280, "height": 720, "fps": 30}
        assert 20 <= st["received_fps_5s"] * 5 / 4 <= 40 or st["received_fps_5s"] > 10
        lat = st["transport_latency_ms"]
        assert lat["samples"] > 0 and lat["p50"] is not None and -5 < lat["p50"] < 200
        assert st["ping_rtt_ms"] is not None
        parser = videolink.Rfc4571()
        pkts = parser.feed(bytes(got))
        assert len(pkts) >= 180 and all(videolink.rtp_info(p) for p in pkts)    # в декодер — только RTP
        assert s.stats()["sessions"] == 1 and s.stats()["active"] and s.stats()["frames"] >= 60
        assert any("video: session from 127.0.0.1" in m for m in log)
    finally:
        c.close()
        s.close()


@pytest.mark.parametrize("who, reason", [("other", "identity"), ("rogue", "")])
def test_wrong_certificate_rejected(lab, who, reason):
    s, log = server(lab)
    c = videolink.VideoClient(lab[who], "127.0.0.1", s.port)
    try:
        got, out, t, r = stream(c, 3.0)
        t.join(5)
        assert not got and not c.stats()["frames"]
        assert wait_for(lambda: s.stats()["auth_failures"] >= 1, 5) and s.stats()["sessions"] == 0
        assert any("video auth: reject" in m and reason in m for m in log), log
    finally:
        s.close()


def test_plain_tcp_and_tls_without_certificate_rejected(lab):
    s, log = server(lab)
    try:
        with socket.create_connection(("127.0.0.1", s.port), timeout=3) as raw:
            raw.sendall(b"\x02\x00\x00\x00\x10" + b"\x80" * 16)                # «RTP» без TLS
            raw.settimeout(6)
            try:
                assert raw.recv(4096) == b""
            except (ConnectionError, OSError):
                pass
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", s.port), timeout=3) as raw:
            with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
                with ctx.wrap_socket(raw) as tsock:
                    tsock.settimeout(6)
                    if tsock.recv(4096) == b"":
                        raise ConnectionError("closed")
        assert wait_for(lambda: s.stats()["auth_failures"] >= 2, 5) and s.stats()["sessions"] == 0
    finally:
        s.close()


def test_no_control_session_rejected_and_no_sender_started(lab, tmp_path):
    marker = tmp_path / "started"
    cmd = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"]
    s, log = server(lab, authorize=lambda addr: "no active control session", command=cmd)
    c = videolink.VideoClient(lab["console"], "127.0.0.1", s.port)
    try:
        got, out, t, r = stream(c, 3.0)
        t.join(5)
        assert not got and "no active control session" in (s.stats()["last_reject"] or "")
        assert not marker.exists()                                            # генератор не запускался
    finally:
        s.close()


def _children_with(pattern):
    import subprocess
    out = subprocess.run(["ps", "-ww", "-eo", "pid,args"], capture_output=True, text=True).stdout   # без обрезки по ширине
    return [ln for ln in out.splitlines() if pattern in ln and "ps -eo" not in ln]


def test_drop_closes_session_and_kills_sender(lab):
    s, log = server(lab)
    c = videolink.VideoClient(lab["console"], "127.0.0.1", s.port)
    try:
        got, out, t, r = stream(c, 2.0)
        assert c.stats()["frames"] > 10 and _children_with("fake_rtp_sender.py")
        s.drop("сеанс управления завершён")
        t.join(8)
        assert not t.is_alive() and out.get("why")
        assert wait_for(lambda: not s.active, 5) and s.stats()["dropped_by_control"] == 1
        assert wait_for(lambda: not _children_with("fake_rtp_sender.py"), 5)     # генератор не остался
    finally:
        s.close()


def test_tampered_byte_breaks_tls_without_delivering_data(lab):
    """Посторонний узел в пути меняет бит потока: TLS (AEAD) рвёт соединение, подложные данные не доходят."""
    s, log = server(lab)
    lsock = socket.create_server(("127.0.0.1", 0))
    pport = lsock.getsockname()[1]
    state = {"n": 0}

    def proxy():
        a, _ = lsock.accept()
        b = socket.create_connection(("127.0.0.1", s.port))

        def pump(src, dst, tamper):
            try:
                while True:
                    d = src.recv(65536)
                    if not d:
                        break
                    if tamper:
                        state["n"] += len(d)
                        if state["n"] > 60000:                          # после рукопожатия и первых кадров
                            d = bytearray(d)
                            d[len(d) // 2] ^= 0x01
                            d = bytes(d)
                    dst.sendall(d)
            except OSError:
                pass
            for x in (src, dst):
                try:
                    x.close()
                except OSError:
                    pass
        threading.Thread(target=pump, args=(a, b, False), daemon=True).start()
        threading.Thread(target=pump, args=(b, a, True), daemon=True).start()
    threading.Thread(target=proxy, daemon=True).start()
    c = videolink.VideoClient(lab["console"], "127.0.0.1", pport)
    try:
        got, out, t, r = stream(c, 6.0)
        t.join(8)
        assert not t.is_alive()
        assert "bad record mac" in (c.stats()["error"] or "").lower() or "decryption" in (c.stats()["error"] or "").lower() \
            or c.stats()["error"]
        pkts = videolink.Rfc4571().feed(bytes(got))
        assert all(videolink.rtp_info(p) for p in pkts)                      # всё доставленное — целые RTP
    finally:
        c.close()
        s.close()
        lsock.close()


def test_agent_binds_video_to_control_session(lab):
    """Агент блока: видео — только при живом сеансе управления того же пульта; конец сеанса закрывает видео."""
    vport = free_port()
    a = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--heartbeat-interval", "0.2",
              "--net-video-port", str(vport), "--video-command", *FAKE)
    try:
        lone = videolink.VideoClient(lab["console"], "127.0.0.1", vport)     # без сеанса управления — отказ
        got, out, t, r = stream(lone, 2.0)
        t.join(5)
        assert not got
        pc = PeerClient(None, a.port, heartbeat_timeout_s=1.0, tls_cfg=lab["console"]).start()
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected", 10)
        tel = lambda: (pc.snapshot()["telemetry"] or {}).get("video") or {}   # noqa: E731
        assert wait_for(lambda: tel().get("transport") == "tls", 10), pc.snapshot()["telemetry"]
        c = videolink.VideoClient(lab["console"], "127.0.0.1", vport)
        got, out, t, r = stream(c, 2.5)
        assert c.stats()["frames"] > 20 and c.stats()["stream_sps"]["width"] == 1280
        pc.stop()                                                           # сеанс управления закончился
        t.join(10)
        assert not t.is_alive()                                             # видеосеанс закрыт блоком
    finally:
        a.stop()
    log = a.stderr()
    assert "video auth: reject 127.0.0.1: no active control session" in log, log
    assert "video: session" in log and "closed: сеанс управления" in log, log
