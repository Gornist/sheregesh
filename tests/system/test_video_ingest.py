# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеоприём блока (sheregesh.videoingest): состояния no-device / no-signal / starting / streaming / error, горячее
отключение и возврат устройства, потеря и возврат сигнала, недоступный и вернувшийся сервер RTSP, смена источника без
утечек (20 циклов: процессы, дескрипторы, потоки), непрерывность RTP при перезапуске конвейера.

Без GStreamer и устройств: конвейер — tests/system/fake_ingest_sender.py (RTP в stdout, поведение — управляющим
файлом), источник — подделка с управляемыми present()/signal(). Сквозная проверка через настоящий TLS-видеоканал —
test_video_server_survives_unplug_and_signal_loss."""
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from procs import wait_for
from sheregesh import videoinput as vi
from sheregesh import videolink
from sheregesh.videoingest import Ingest, RtpContinuity, jpeg_size

SENDER = Path(__file__).with_name("fake_ingest_sender.py")


class FakeSource(vi.VideoSource):
    """Устройство V4L2 «на столе»: bench.plugged — USB вставлен, bench.sig — что сообщает драйвер о сигнале."""
    kind, input_type, name, available = "v4l2", "HDMI", "Fake HDMI capture", True

    def __init__(self, bench):
        self.bench = bench
        self.id, self.device = f"v4l2:{bench.ident}", f"/dev/v4l/by-id/{bench.ident}"
        self.mode = vi.Mode(1280, 720, 30, "MJPG")

    def present(self):
        return (True, "") if self.bench.plugged else (False, f"device {self.device} disappeared")

    def signal(self):
        return "no-device" if not self.bench.plugged else self.bench.sig


class Bench:
    def __init__(self, tmp, ident="usb-MACROSILICON_USB_Video-video-index0", codec="h264"):
        self.ctl, self.pids = tmp / f"{ident}.ctl", tmp / f"{ident}.pids"
        self.ctl.write_text("run")
        self.plugged, self.sig, self.ident, self.codec = True, None, ident, codec
        self.opened = 0

    def opener(self):
        self.opened += 1
        if not self.plugged:
            return vi.Unavailable("v4l2", f"V4L2 device {self.ident} not found", missing=True)
        return FakeSource(self)

    def command(self, _src):
        return [sys.executable, str(SENDER), str(self.ctl), str(self.pids), "30", self.codec]

    def set(self, mode):
        self.ctl.write_text(mode)

    def unplug(self):                       # USB вынут: узел исчез, v4l2src завершается с ошибкой
        self.plugged = False
        self.set("exit")

    def replug(self):
        self.set("run")
        self.plugged = True

    def all_pids(self):
        return [int(x) for x in self.pids.read_text().split()] if self.pids.exists() else []


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                    # зомби (не дождались) — тоже «жив» для этой проверки
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        return True


def open_fds():
    return len(os.listdir("/dev/fd"))


class Log(list):
    def __call__(self, m):
        self.append(m)


def make(bench, **kw):
    kw = {"no_signal_s": 0.5, "start_timeout_s": 3.0, "stall_restart_s": 2.0, "presence_s": 0.2, "signal_s": 0.1,
          "max_backoff_s": 0.5, **kw}
    log = Log()
    return Ingest(bench.opener, bench.command, log, **kw).start(), log


class Consumer:
    """Видеосеанс без сети: забирает пакеты, как _ServerSession."""

    def __init__(self, ing):
        self.ing, self.pkts, self.stop = ing, [], threading.Event()
        self.token = ing.attach()
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        import select
        while not self.stop.is_set():
            select.select([self.ing.wake_fd], [], [], 0.1)
            self.pkts += [p for _t, p in self.ing.take()]

    def close(self):
        self.stop.set()
        self.t.join(2)
        self.ing.detach(self.token)

    def seqs(self):
        return [int.from_bytes(p[2:4], "big") for p in self.pkts]


def state_is(ing, *states):
    return lambda: ing.state in states


def test_stream_states_metrics_and_release(tmp_path):
    b = Bench(tmp_path)
    ing, log = make(b)
    try:
        assert wait_for(state_is(ing, "idle"), 3) and b.all_pids() == []          # без сеанса конвейера нет
        assert ing.status()["state"] == "starting"                               # пульту: сеанс вот-вот начнётся
        c = Consumer(ing)
        assert wait_for(state_is(ing, "streaming"), 5), (ing.state, ing.error)
        time.sleep(1.2)
        st = ing.status()
        assert st["state"] == "streaming" and st["input"] == "hdmi" and st["type"] == "v4l2" and st["error"] == ""
        assert st["format"] == "mjpeg" and (st["width"], st["height"]) == (1280, 720) and st["fps"] == 30.0
        assert (st["stream_width"], st["stream_height"]) == (1280, 720)          # из SPS потока
        assert 20 <= st["fps_actual"] <= 40, st
        assert len(c.pkts) > 60 and all(videolink.rtp_info(p) for p in c.pkts)
        c.close()
        assert wait_for(state_is(ing, "idle"), 3)
        assert wait_for(lambda: not any(alive(p) for p in b.all_pids()), 3)       # конвейер остановлен вместе с сеансом
        assert [s for _t, s, _e in ing.transitions][:3] == ["starting", "streaming", "idle"] or \
            "streaming" in [s for _t, s, _e in ing.transitions]
    finally:
        ing.close()


def test_signal_lost_and_restored_without_restart(tmp_path):
    """Нет кадров → no-signal (последний кадр не повторяется — пакеты просто не идут); кадры вернулись → streaming."""
    b = Bench(tmp_path)
    ing, _ = make(b, stall_restart_s=30)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        b.set("pause")
        assert wait_for(state_is(ing, "no-signal"), 3) and ing.error.startswith("no frames for")
        n = len(c.pkts)
        time.sleep(0.6)
        assert len(c.pkts) == n                                                  # ничего «прошлого» не отправлено
        assert ing.status()["fps_actual"] == 0.0 and ing.status()["state"] == "no-signal"
        b.set("run")
        assert wait_for(state_is(ing, "streaming"), 3) and ing.restarts == 0 and len(b.all_pids()) == 1
    finally:
        c.close()
        ing.close()


def test_stall_restarts_pipeline_and_rtp_stays_continuous(tmp_path):
    b = Bench(tmp_path)
    ing, _ = make(b, stall_restart_s=1.0)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        b.set("pause")
        assert wait_for(lambda: ing.restarts >= 1, 5)                            # завис — перезапуск конвейера
        assert ing.state == "no-signal"
        b.set("run")
        assert wait_for(state_is(ing, "streaming"), 5) and len(b.all_pids()) >= 2
        time.sleep(0.5)
        seqs = c.seqs()
        assert all((y - x) & 0xFFFF == 1 for x, y in zip(seqs, seqs[1:])), "разрыв номеров RTP после перезапуска"
        assert len({p[8:12] for p in c.pkts}) == 1                               # один SSRC
        assert sum(alive(p) for p in b.all_pids()) == 1
    finally:
        c.close()
        ing.close()


def test_driver_reported_no_signal_waits_without_restart(tmp_path):
    b = Bench(tmp_path)
    ing, _ = make(b, stall_restart_s=0.8)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        b.sig = "no-signal"                                                       # VIDIOC_ENUMINPUT: V4L2_IN_ST_NO_SIGNAL
        b.set("pause")
        assert wait_for(lambda: ing.state == "no-signal" and "driver reports" in ing.error, 3), ing.error
        time.sleep(1.5)
        assert ing.restarts == 0 and len(b.all_pids()) == 1                      # ждём сигнала, не дёргаем устройство
        b.sig = None
        b.set("run")
        assert wait_for(state_is(ing, "streaming"), 3)
    finally:
        c.close()
        ing.close()


def test_hot_unplug_is_no_device_and_replug_recovers(tmp_path):
    b = Bench(tmp_path)
    ing, log = make(b)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        b.unplug()
        assert wait_for(state_is(ing, "no-device"), 3), (ing.state, ing.error)
        assert "disappeared" in ing.error or "not found" in ing.error
        assert wait_for(lambda: not any(alive(p) for p in b.all_pids()), 3)
        opened = b.opened
        time.sleep(0.7)
        assert b.opened > opened and len(b.all_pids()) == 1                       # ищем заново, но не запускаем
        b.replug()
        assert wait_for(state_is(ing, "streaming"), 5), (ing.state, ing.error)
        assert sum(alive(p) for p in b.all_pids()) == 1
        assert any("no-device" in m for m in log) and any("-> streaming" in m for m in log)
    finally:
        c.close()
        ing.close()


def test_unplug_detected_by_signal_probe_while_pipeline_hangs(tmp_path):
    """v4l2src иногда не завершается при отключении (зависает без кадров): узел исчез → no-device, процесс убит."""
    b = Bench(tmp_path)
    ing, _ = make(b, stall_restart_s=30)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        b.plugged = False
        b.set("pause")
        assert wait_for(state_is(ing, "no-device"), 3)
        assert wait_for(lambda: not any(alive(p) for p in b.all_pids()), 3)
        b.replug()
        assert wait_for(state_is(ing, "streaming"), 5)
    finally:
        c.close()
        ing.close()


def test_pipeline_error_with_device_present_is_error_and_retried(tmp_path):
    b = Bench(tmp_path)
    ing, _ = make(b)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        b.set("exit")
        assert wait_for(state_is(ing, "error"), 3)
        assert "pipeline exited with code 1" in ing.error and "Could not read from resource" in ing.error
        b.set("run")
        assert wait_for(state_is(ing, "streaming"), 5) and len(b.all_pids()) >= 2
    finally:
        c.close()
        ing.close()


def test_idle_presence_without_session(tmp_path):
    b = Bench(tmp_path)
    b.plugged = False
    ing, _ = make(b)
    try:
        assert wait_for(state_is(ing, "no-device"), 3) and "not found" in ing.error
        b.plugged = True
        assert wait_for(state_is(ing, "idle"), 3)
        assert b.all_pids() == []                                                # без сеанса устройство не захватывается
    finally:
        ing.close()


def test_unsupported_mode_is_error_not_no_device(tmp_path):
    ing = Ingest(lambda: vi.Unavailable("v4l2", "mode 1920×1080@60 not supported by X; offered: …"),
                 lambda s: ["false"], presence_s=0.1).start()
    try:
        assert wait_for(state_is(ing, "error"), 3) and "not supported" in ing.error
        tok = ing.attach()
        time.sleep(0.4)
        assert ing.state == "error"
        ing.detach(tok)
    finally:
        ing.close()


def test_switch_sources_20_cycles_releases_everything(tmp_path):
    a, b = Bench(tmp_path, "cam-a"), Bench(tmp_path, "cam-b")
    ing, _ = make(a)
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 5)
        time.sleep(0.3)
        fds, threads = open_fds(), threading.active_count()
        for i in range(20):
            bench = b if i % 2 == 0 else a
            ing.switch(bench.opener)
            assert wait_for(lambda: ing.state == "streaming" and ing.src is not None and
                            ing.src.id == f"v4l2:{bench.ident}", 5), (i, ing.state, ing.error)
            running = [p for p in a.all_pids() + b.all_pids() if alive(p)]
            assert len(running) == 1, (i, running)                                # старый конвейер остановлен
        time.sleep(0.3)
        assert len(a.all_pids()) + len(b.all_pids()) == 21
        assert abs(open_fds() - fds) <= 2, (fds, open_fds())                      # дескрипторы не копятся
        assert abs(threading.active_count() - threads) <= 2, (threads, threading.active_count())
    finally:
        c.close()
        ing.close()
    assert not any(alive(p) for p in a.all_pids() + b.all_pids())


def test_close_releases_process_and_pipe(tmp_path):
    b = Bench(tmp_path)
    fds = open_fds()
    ing, _ = make(b)
    c = Consumer(ing)
    assert wait_for(state_is(ing, "streaming"), 5)
    c.close()
    ing.close()
    assert not any(alive(p) for p in b.all_pids())
    assert wait_for(lambda: open_fds() <= fds, 3), (fds, open_fds())


# ---------------- RTSP: доступность сервера ----------------
def test_rtsp_unreachable_then_back(tmp_path):
    port = socket.socket()
    port.bind(("127.0.0.1", 0))
    num = port.getsockname()[1]
    port.close()
    b = Bench(tmp_path)
    src = vi.RTSPSource(f"rtsp://user:pw@127.0.0.1:{num}/live")
    ing = Ingest(lambda: src, b.command, presence_s=0.2, no_signal_s=0.5, max_backoff_s=0.5).start()
    c = Consumer(ing)
    srv = None
    try:
        assert wait_for(state_is(ing, "connect-error"), 3) and "unreachable" in ing.error
        assert b.all_pids() == [] and "pw" not in str(ing.status())
        srv = socket.create_server(("127.0.0.1", num))                            # сервер вернулся
        assert wait_for(state_is(ing, "streaming"), 5), (ing.state, ing.error)
        time.sleep(1.0)
        st = ing.status()
        assert st["input"] == "rtsp" and st["type"] == "rtsp" and st["width"] == 1280    # размер — из потока
        assert 20 <= st["fps"] <= 40                                             # номинала нет — фактическая
        srv.close()
        srv = None
        b.set("exit")                                                             # обрыв: rtspsrc завершился
        assert wait_for(state_is(ing, "connect-error"), 3) and "unreachable" in ing.error
    finally:
        if srv:
            srv.close()
        c.close()
        ing.close()


# ---------------- непрерывность RTP ----------------
def _pkt(seq, ts, ssrc=b"\x00\x00\x53\x48", marker=False):
    return bytes([0x80, (0x80 if marker else 0) | 96]) + seq.to_bytes(2, "big") + ts.to_bytes(4, "big") + ssrc + b"x"


def test_rtp_continuity_rewrite():
    r = RtpContinuity()
    first = [_pkt(100 + i, 9000 * i) for i in range(3)]
    assert [r.rewrite(p, 10.0 + i / 30) for i, p in enumerate(first)] == first   # первое поколение — байт в байт
    r.new_generation()
    out = r.rewrite(_pkt(7, 555, ssrc=b"\xaa\xbb\xcc\xdd"), 12.0 + 2 / 30)       # новый процесс: свои seq/ts/ssrc
    assert int.from_bytes(out[2:4], "big") == 103 and out[8:12] == b"\x00\x00\x53\x48"
    assert int.from_bytes(out[4:8], "big") == 18000 + 2 * 90000                   # +2 с реального времени
    assert int.from_bytes(r.rewrite(_pkt(8, 555 + 3000), 12.1)[2:4], "big") == 104
    r.reset()
    assert r.rewrite(_pkt(65535, 1), 0.0) == _pkt(65535, 1)


def test_jpeg_size_from_rfc2435_header():
    assert jpeg_size(bytes([0, 0, 0, 0, 1, 50, 160, 90]) + b"..") == (1280, 720)
    assert jpeg_size(bytes([0, 0, 1, 0, 1, 50, 160, 90])) is None                # не первый фрагмент


# ---------------- сквозная проверка через защищённый видеоканал ----------------
@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    from sheregesh.link import pki
    if not shutil.which("openssl"):
        pytest.skip("нет openssl")
    root = tmp_path_factory.mktemp("ilab")
    ca = root / "ca"
    pki.ca_init(ca)

    def device(name, role, did, peer):
        d = root / name
        cert = pki.ca_sign(ca, pki.keygen(role, did, d), role, did)
        pki.install(d, cert, (ca / "ca.crt").read_text(), role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2")}


def test_video_server_survives_unplug_and_signal_loss(lab, tmp_path):
    """Видеосеанс не рвётся при отключении устройства и потере сигнала; пульт видит паузу в кадрах, затем поток
    продолжается в том же сеансе без разрывов номеров RTP (буфер джиттера пульта не сбрасывается)."""
    b = Bench(tmp_path)
    log = Log()
    ing = Ingest(b.opener, b.command, log, no_signal_s=0.5, presence_s=0.2, signal_s=0.1, max_backoff_s=0.5)
    srv = videolink.VideoServer(lab["remote"], "h264", lambda a: None, log, bind="127.0.0.1", port=0,
                                ingest=ing).start()
    cl = videolink.VideoClient(lab["console"], "127.0.0.1", srv.port)
    r, w = os.pipe()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("why", cl.run(w)), daemon=True)
    d = threading.Thread(target=lambda: [None for _ in iter(lambda: os.read(r, 65536), b"")], daemon=True)
    t.start()
    d.start()
    try:
        assert wait_for(lambda: cl.stats()["frames"] > 30, 8), cl.stats()
        assert cl.stats()["source"] == {"codec": "h264", "width": 1280, "height": 720, "fps": 30, "source": {
            "input": "hdmi", "type": "v4l2", "device": f"/dev/v4l/by-id/{b.ident}", "width": 1280, "height": 720,
            "fps": 30.0}}                                                       # INFO: режим источника для пульта
        b.unplug()
        assert wait_for(lambda: srv.stats()["source"]["state"] == "no-device", 3)
        f0 = cl.stats()["frames"]
        time.sleep(1.0)
        assert cl.stats()["frames"] - f0 <= 2 and cl.stats()["connected"]         # сеанс жив, кадров нет
        b.replug()
        assert wait_for(lambda: srv.stats()["source"]["state"] == "streaming", 5)
        b.set("pause")
        assert wait_for(lambda: srv.stats()["source"]["state"] == "no-signal", 3)
        b.set("run")
        assert wait_for(lambda: cl.stats()["frames"] > f0 + 30, 8)
        st = cl.stats()
        assert st["connected"] and st["seq_gaps"] == 0 and st["lost_packets"] == 0, st
        assert srv.stats()["sessions"] == 1 and srv.stats()["sender_restarts"] >= 1   # перезапуск — в том же сеансе
    finally:
        cl.close()
        t.join(5)
        srv.close()
        os.close(w)
        d.join(5)
        os.close(r)
    assert not any(alive(p) for p in b.all_pids())


# ---------------- агент блока: конфигурация видеоприёма и телеметрия ----------------
def test_agent_video_config_reload_and_telemetry_contract(lab, tmp_path):
    """video-input.json рядом с link.json (каталог пользователя, без правки юнита в /usr), SIGHUP — смена источника;
    пульт получает status.video.source через свою проверку телеметрии (_sanitize_status)."""
    import json
    import signal as sig
    from procs import Agent, free_port
    from sheregesh.link.client import PeerClient
    link = tmp_path / "link.json"
    shutil.copyfile(lab["remote"].source, link)
    b = Bench(tmp_path)
    a = Agent("--link-config", str(link), "--bind", "127.0.0.1", "--heartbeat-interval", "0.2",
              "--net-video-port", str(free_port()), "--video-command", *b.command(None))
    pc = PeerClient(None, a.port, heartbeat_timeout_s=2.0, tls_cfg=lab["console"]).start()
    try:
        def inp():
            return ((pc.snapshot()["telemetry"] or {}).get("video") or {}).get("source") or {}
        assert wait_for(lambda: inp().get("state") == "starting", 10), pc.snapshot()["telemetry"]
        dead = free_port()
        (tmp_path / "video-input.json").write_text(json.dumps(
            {"source": "rtsp", "url": f"rtsp://op:secret@127.0.0.1:{dead}/cam", "codec": "h264", "junk": 1}))
        a.signal(sig.SIGHUP)
        assert wait_for(lambda: inp().get("type") == "rtsp" and inp().get("state") == "connect-error", 10), inp()
        got = inp()
        assert got["input"] == "rtsp" and "unreachable" in got["error"] and "secret" not in json.dumps(got)
        assert set(got) == {"state", "input", "type", "name", "id", "device", "error", "format", "standard", "width",
                            "height", "stream_width", "stream_height", "fps", "fps_actual", "restarts", "since_s"}
        assert a.proc.poll() is None
    finally:
        pc.stop()
        a.stop()
    log = a.stderr()
    assert "video source:" in log and "secret" not in log, log


# ---------------- RTSP сквозной: настоящие rtspsrc и GStreamer, локальный сервер RTSP ----------------
def _gst_rtsp_ok():
    from sheregesh import video
    return shutil.which("gst-launch-1.0") and all(video.has_element(e) for e in
                                                  ("rtspsrc", "rtpjpegdepay", "rtpjpegpay", "jpegenc", "rtpstreampay"))


@pytest.mark.skipif(not _gst_rtsp_ok(), reason="нет GStreamer с rtspsrc/jpegenc")
def test_rtsp_end_to_end_with_real_rtspsrc():
    """rtspsrc (protocols=tcp) → rtpjpegdepay → сквозной rtpjpegpay (без перекодирования) → Ingest: поток идёт,
    размер — из заголовка JPEG; сервер упал → no-device (TCP не принимает); сервер вернулся → поток восстановлен."""
    from mini_rtsp_server import MiniRTSP
    from sheregesh import video
    srv = MiniRTSP()
    port = srv.port
    src_url = srv.url
    ing = Ingest(lambda: vi.RTSPSource(src_url, "mjpeg", latency_ms=50, timeout_s=3),
                 lambda s: video.stream_sender_args("jpeg", src=s), Log(), no_signal_s=1.0, start_timeout_s=10,
                 stall_restart_s=6, presence_s=0.3, max_backoff_s=1.0).start()
    c = Consumer(ing)
    try:
        assert wait_for(state_is(ing, "streaming"), 20), (ing.state, ing.error)
        time.sleep(1.5)
        st = ing.status()
        assert (st["stream_width"], st["stream_height"]) == (320, 240) and st["format"] == "mjpeg"
        assert 8 <= st["fps_actual"] <= 20, st
        assert all(p[1] & 0x7F == 26 for p in c.pkts[:20])                       # RTP/JPEG как есть
        srv.close()
        assert wait_for(state_is(ing, "connect-error", "no-signal"), 10), ing.state
        assert wait_for(state_is(ing, "connect-error"), 15) and "unreachable" in ing.error, (ing.state, ing.error)
        srv = MiniRTSP(port)
        assert wait_for(state_is(ing, "streaming"), 25), (ing.state, ing.error)
    finally:
        c.close()
        ing.close()
        srv.close()
