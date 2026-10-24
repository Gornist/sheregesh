# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DUAL-DEVICE E2E: процесс агента удалённого блока (сетевой профиль, настоящий GStreamer) → видеоканал TLS 7444 →
приёмник пульта (VideoReceiver). Сеанс управления держит PeerClient, как пульт.

Неисправность блока video-off:DUR («источник исчез» по расписанию, в том числе в демонстрационном сценарии demo-s1)
должна убирать и видео защищённого канала: «нет видео» ≤ 3 с, восстановление ≤ 10 с после окончания (REQ-SW-002).
"""
import signal
import time

import pytest

from procs import Agent, free_port, wait_for
from sheregesh import video, videolink
from sheregesh.link import pki
from sheregesh.link.client import PeerClient
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.video import VideoReceiver

pytestmark = [pytest.mark.skipif(not video.available() or video.choose_codec() is None,
                                 reason="нет GStreamer с работающим кодеком"),
              pytest.mark.test_id("TEST-SW-002")]

FAULT_AT, FAULT_DUR = 6.0, 5.0


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2eagent")
    pki.ca_init(root / "ca")

    def device(name, role, did, peer):
        d = root / name
        pki.install(d, pki.ca_sign(root / "ca", pki.keygen(role, did, d), role, did), (root / "ca/ca.crt").read_text(),
                    role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2")}


def test_agent_video_off_fault_stops_and_restores_tls_video(qapp, lab):
    vport = free_port()
    t_start = time.monotonic()
    a = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--heartbeat-interval", "0.2",
              "--net-video-port", str(vport), "--fault", f"{FAULT_AT}:video-off:{FAULT_DUR}")
    pc = rx = None
    try:
        pc = PeerClient(None, a.port, heartbeat_timeout_s=1.0, tls_cfg=lab["console"]).start()
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected", 10)
        tel = lambda: (pc.snapshot()["telemetry"] or {}).get("video") or {}   # noqa: E731
        assert wait_for(lambda: tel().get("transport") == "tls", 10), pc.snapshot()["telemetry"]
        codec = tel()["codec"]
        rx = VideoReceiver()
        rx.configure_remote(codec, ("127.0.0.1", vport),
                            lambda: videolink.VideoClient(lab["console"], "127.0.0.1", vport))
        assert wait_until(lambda: rx.state == "playing", 10), rx.metrics()
        assert time.monotonic() - t_start < FAULT_AT - 0.5, "агент стартовал слишком долго для расписания"
        assert wait_until(lambda: rx.state != "playing", FAULT_AT + 4 - (time.monotonic() - t_start)), \
            ("video-off не убрал видео защищённого канала", rx.metrics())
        t_lost = time.monotonic() - t_start
        assert t_lost - FAULT_AT <= 3.0, t_lost                         # «нет видео» ≤ 3 с (с запасом на часы агента)
        assert rx.state in ("no-video", "restarting", "waiting")
        pump(max(0.0, FAULT_AT + FAULT_DUR - 1.0 - (time.monotonic() - t_start)) * 1000)
        assert rx.state != "playing", ("видео вернулось раньше конца неисправности", rx.metrics())
        assert wait_until(lambda: rx.state == "playing", 15), rx.metrics()
        t_back = time.monotonic() - t_start
        assert t_back - (FAULT_AT + FAULT_DUR) <= 10.0, t_back           # восстановление ≤ 10 с
        print(f"\nE2E-VIDEO agent video-off ({codec}): lost {t_lost - FAULT_AT:+.2f} s after fault, "
              f"back {t_back - FAULT_AT - FAULT_DUR:+.2f} s after fault end, sessions {rx.metrics()['sessions']}")
        assert pc.snapshot()["phase"] == "connected"                   # сеанс управления неисправность не трогает
    finally:
        if rx is not None:
            rx.shutdown()
        if pc is not None:
            pc.stop()
        a.stop()
    log = a.stderr()
    assert "fault video-off" in log, log


def test_ctl_loss_no_video_shown_before_control_timeout(qapp, lab):
    """Сценарий ctl-loss стенда (s3.1-video): процесс агента блока остановлен SIGSTOP на 8 с.

    Из чего складывается время: видеоканал (videolink) собственного тайм-аута не имеет — TLS остаётся открытым, кадры
    просто перестают приходить. «Нет видео» в GUI — порог возраста последнего кадра VideoReceiver (NO_VIDEO_AFTER_S
    1,5 с, опрос 33 мс). Отдельно, позже: тайм-аут heartbeat канала управления (link.yaml heartbeat_timeout_s = 3 с) →
    бэкенд выключает видео (mode «off»). На стенде 3,31 с — это второе событие (≥ 3 с по построению) плюс дискретность
    замера (ui-state.json раз в 1 с, опрос пробы 0,5 с через ssh), а не время показа «нет видео».
    """
    vport = free_port()
    a = Agent("--link-config", str(lab["remote"].source), "--bind", "127.0.0.1", "--heartbeat-interval", "0.2",
              "--net-video-port", str(vport))
    pc = rx = None
    try:
        pc = PeerClient(None, a.port, heartbeat_timeout_s=3.0, tls_cfg=lab["console"]).start()   # как link.yaml
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected", 10)
        tel = lambda: (pc.snapshot()["telemetry"] or {}).get("video") or {}   # noqa: E731
        assert wait_for(lambda: tel().get("transport") == "tls", 10), pc.snapshot()["telemetry"]
        codec = tel()["codec"]
        rx = VideoReceiver()
        rx.configure_remote(codec, ("127.0.0.1", vport),
                            lambda: videolink.VideoClient(lab["console"], "127.0.0.1", vport))
        assert wait_until(lambda: rx.state == "playing", 10), rx.metrics()
        pump(2000)
        t0 = time.monotonic()
        a.signal(signal.SIGSTOP)
        try:
            assert wait_until(lambda: rx.state != "playing", 10), rx.metrics()
            no_video = time.monotonic() - t0
            assert wait_until(lambda: pc.snapshot()["phase"] != "connected", 10), pc.snapshot()
            ctl_lost = time.monotonic() - t0
            assert rx.state == "no-video"                           # кадров нет, TLS открыт — приёмник не завис
            pump(max(0.0, 8.0 - (time.monotonic() - t0)) * 1000)
        finally:
            a.signal(signal.SIGCONT)
        tr = time.monotonic()
        assert wait_until(lambda: rx.state == "playing", 20), rx.metrics()
        back = time.monotonic() - tr
        # после возобновления пульт заново устанавливает сеанс управления, блок закрывает старый видеосеанс
        # («сеанс управления заменён»), приёмник переподключается: ждём устойчивого видео нового сеанса
        assert wait_for(lambda: pc.snapshot()["phase"] == "connected", 15), pc.snapshot()
        assert wait_until(lambda: rx.metrics()["sessions"] >= 2 and rx.state == "playing", 20), rx.metrics()
        stable = time.monotonic() - tr
        pump(2000)
        assert rx.state == "playing", rx.metrics()
        print(f"\nE2E-VIDEO ctl-loss ({codec}): no-video {no_video:.2f} s, control lost {ctl_lost:.2f} s, "
              f"video back {back:.2f} s after resume, new video session playing {stable:.2f} s after resume")
        assert no_video <= 2.0, no_video                # REQ-SW-002 ≤ 3 с; запас 1 с на уведомление QML и опрос
        assert back <= 10.0 and stable <= 10.0, (back, stable)
    finally:
        if rx is not None:
            rx.shutdown()
        if pc is not None:
            pc.stop()
        a.stop()
