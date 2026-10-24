# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приёмник пульта в удалённом режиме (S3.1-VIDEO): видео только по защищённому каналу, декодер читает RTP из stdin;
разрыв канала → перезапуск декодера с новым подключением. Декодер и генератор — подставные (без GStreamer)."""
import shutil
import sys
from pathlib import Path

import pytest

from sheregesh import videolink
from sheregesh.link import pki
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.video import FRAME_H, FRAME_W, VideoReceiver

SYS = Path(__file__).resolve().parents[1] / "system"
FAKE = [sys.executable, str(SYS / "fake_rtp_sender.py"), "30"]
# Подставной декодер: RFC 4571 из stdin, на каждый пакет с маркером конца кадра — кадр RGBx в stdout.
DECODER = [sys.executable, "-c", (
    "import sys\nr, w = sys.stdin.buffer, sys.stdout.buffer\nframe = bytes(%d)\n"
    "while True:\n    h = r.read(2)\n    if len(h) < 2: break\n    p = r.read(int.from_bytes(h, 'big'))\n"
    "    if len(p) > 1 and p[1] & 0x80:\n        w.write(frame); w.flush()\n") % (FRAME_W * FRAME_H * 4)]

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("vrecv")
    pki.ca_init(root / "ca")

    def device(name, role, did, peer):
        d = root / name
        pki.install(d, pki.ca_sign(root / "ca", pki.keygen(role, did, d), role, did), (root / "ca/ca.crt").read_text(),
                    role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2")}


def test_remote_video_decoded_and_reconnects_after_drop(qapp, lab):
    srv = videolink.VideoServer(lab["remote"], "h264", lambda a: None, lambda m: None, bind="127.0.0.1", port=0,
                                command=FAKE).start()
    rx = VideoReceiver(command=DECODER)
    try:
        rx.configure_remote("h264", ("127.0.0.1", srv.port),
                            lambda: videolink.VideoClient(lab["console"], "127.0.0.1", srv.port))
        assert wait_until(lambda: rx.state == "playing", 10), rx.metrics()
        pump(2500)
        m = rx.metrics()
        assert m["mode"] == "remote-tls" and m["frames_decoded"] > 30 and m["decoded_fps_5s"] > 5
        assert m["link"]["tls"] == "TLSv1.3" and m["link"]["stream_sps"]["width"] == 1280
        dl = m["link"]["decode_latency_ms"]                                 # запись кадра в декодер → кадр RGBx
        assert dl["samples"] > 10 and 0 <= dl["p50"] < 500 and dl["desync_resets"] == 0
        srv.drop("сеанс управления завершён")                               # блок закрыл видеосеанс
        assert wait_until(lambda: rx.metrics()["sessions"] >= 2 and rx.state == "playing", 20), rx.metrics()
        rx.configure(None, None)                                            # управление потеряно — видео выключено
        assert rx.state == "off" and rx.metrics()["mode"] == "off"
        assert wait_until(lambda: not srv.active, 5)
    finally:
        rx.shutdown()
        srv.close()
