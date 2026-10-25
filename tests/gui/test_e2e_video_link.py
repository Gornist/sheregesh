# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DUAL-DEVICE E2E, видео pi2 → pi1 на одной машине: настоящий GStreamer на обеих сторонах.

Блок:  VideoServer (videotestsrc 1280×720 → кодер → RTP → rtpstreampay) → TLS 1.3 mTLS (ADR-0011)
Пульт: VideoClient → stdin декодера (stream_receiver_args) → RGBx 640×360 → VideoReceiver — ровно так кадры получает GUI.

Кодек — тот, что выбирает проба (на Pi 4 — v4l2 H.264; в CI и на macOS без openh264 — MJPEG). Вариант H.264 на
машине разработчика — с x264enc вместо аппаратного кодера (подмена только кодера, декодер — проба: avdec_h264).

Пороги — только из SRS (REQ-SW-002): средняя частота ≥ 29 к/с, потери кадров ≤ 1 %, «нет видео» ≤ 3 с после обрыва,
восстановление ≤ 10 с после возобновления. Для задержки и ресурсов порогов в SRS нет — записываются замеры
(reports/e2e-video.json); утечки потоков, дескрипторов и процессов проверяются на равенство исходному уровню.
"""
import json
import os
import resource
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sheregesh import video, videolink
from sheregesh.link import pki
from sheregesh.videoinput import Mode, VideoSource
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.video import FRAME_H, FRAME_W, VideoReceiver

ROOT = Path(__file__).resolve().parents[2]
X264 = ["x264enc", "tune=zerolatency", "speed-preset=ultrafast", "key-int-max=30", "bitrate=2000"]
NO_VIDEO_MAX_S = 3.0          # REQ-SW-002 п. 3
RECOVER_MAX_S = 10.0          # REQ-SW-002 п. 3
FPS_MIN = 29.0                # REQ-SW-002 п. 2
LOSS_MAX = 0.01               # REQ-SW-002 п. 2

pytestmark = [pytest.mark.skipif(not video.available() or video.choose_codec() is None,
                                 reason="нет GStreamer с работающим кодеком"),
              pytest.mark.test_id("TEST-SW-002")]


def record(name, data):
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    path = out / "e2e-video.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc[name] = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **data}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nE2E-VIDEO {name}: {json.dumps(data, ensure_ascii=False)}")


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2evideo")
    pki.ca_init(root / "ca")

    def device(name, role, did, peer):
        d = root / name
        pki.install(d, pki.ca_sign(root / "ca", pki.keygen(role, did, d), role, did), (root / "ca/ca.crt").read_text(),
                    role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2")}


@pytest.fixture(params=["probe", "h264-x264"])
def codec(request, monkeypatch):
    if request.param == "probe":
        return video.choose_codec()
    if video.h264_elements() is not None:
        pytest.skip("H.264 проверен вариантом probe")
    if not (video.has_element("x264enc") and video.has_element("avdec_h264") and video.has_element("rtph264pay")):
        pytest.skip("нет x264enc/avdec_h264")
    monkeypatch.setattr(video, "h264_elements", lambda: (X264, "avdec_h264"))
    return "h264"


class Log(list):
    def __call__(self, m):
        self.append(m)


def server(lab, codec, port=0, command=None):
    log = Log()
    return videolink.VideoServer(lab["remote"], codec, lambda a: None, log, bind="127.0.0.1", port=port,
                                 command=command).start(), log


class States:
    """Переходы VideoReceiver.state с моментами (так их видит QML через stateChanged)."""

    def __init__(self, rx):
        self.rx, self.items = rx, []
        rx.stateChanged.connect(self._on)

    def _on(self):
        if not self.items or self.items[-1][1] != self.rx.state:
            self.items.append((time.monotonic(), self.rx.state, self.rx.error))

    def since(self, t0):
        return [(round(t - t0, 2), s, e[:60]) for t, s, e in self.items if t >= t0]


def wait_stable_playing(rx, timeout_s, hold_s=1.0):
    """Момент начала непрерывного «playing» длительностью hold_s (короткая вспышка буферизованных кадров перед
    переподключением не считается восстановлением) или None."""
    end = time.monotonic() + timeout_s
    start = None
    while time.monotonic() < end:
        now = time.monotonic()
        if rx.state == "playing":
            start = start or now
            if now - start >= hold_s:
                return start
        else:
            start = None
        pump(20)
    return None


def remote_receiver(lab, codec, port):
    rx = VideoReceiver()
    rx.configure_remote(codec, ("127.0.0.1", port), lambda: videolink.VideoClient(lab["console"], "127.0.0.1", port))
    return rx


class Proxy:
    """TCP-посредник в пути TLS: pause() — «чёрная дыра» (байты стоят, соединения живы), reset() — RST обеим сторонам."""

    def __init__(self, dst_port):
        self.dst = dst_port
        self.ls = socket.create_server(("127.0.0.1", 0))
        self.port = self.ls.getsockname()[1]
        self.paused = threading.Event()
        self.pairs = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                a, _ = self.ls.accept()
            except OSError:
                return
            try:
                b = socket.create_connection(("127.0.0.1", self.dst))
            except OSError:                                               # блок не слушает — как отказ в соединении
                a.close()
                continue
            self.pairs.append((a, b))
            threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    def _pump(self, a, b):
        try:
            while True:
                if self.paused.is_set():
                    time.sleep(0.02)
                    continue
                r, _, _ = select.select([a, b], [], [], 0.1)
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        raise OSError("eof")
                    (b if s is a else a).sendall(d)
        except (OSError, ValueError):
            pass
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass

    def reset(self):
        pairs, self.pairs = self.pairs, []
        for pair in pairs:
            for s in pair:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    s.close()
                except OSError:
                    pass

    def close(self):
        self.paused.clear()
        self.ls.close()
        self.reset()


# ---------------------------------------------------------------- ресурсы процесса
def descendants():
    """Дочерние процессы этого процесса (все уровни: обёртка pdeath/setpriv → gst-launch-1.0)."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    kids, procs = {}, {}
    for ln in out.splitlines():
        pid, ppid, cmd = ln.split(None, 2) if len(ln.split(None, 2)) == 3 else (*ln.split(None, 1), "")
        kids.setdefault(int(ppid), []).append(int(pid))
        procs[int(pid)] = cmd
    found, todo = [], [os.getpid()]
    while todo:
        for c in kids.get(todo.pop(), []):
            if procs.get(c, "").startswith("ps "):
                continue
            found.append((c, procs.get(c, "")[:80]))
            todo.append(c)
    return found


def open_fds():
    return len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))


def rss_kb():
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    return int(out.strip() or 0)


def gst_children():
    return [p for p in descendants() if "gst-launch" in p[1]]


# ---------------------------------------------------------------- E2E-05: поток идёт
def test_e2e05_stream_end_to_end(qapp, lab, codec):
    srv, log = server(lab, codec)
    rx = remote_receiver(lab, codec, srv.port)
    try:
        t0 = time.monotonic()
        assert wait_until(lambda: rx.state == "playing", 20), rx.metrics()
        first = time.monotonic() - t0
        pump(3000)                                                        # прогрев: кодер, джиттер-буфер
        m0, s0, t1 = rx.metrics(), srv.stats()["frames"], time.monotonic()
        pump(15000)
        m1, s1, t2 = rx.metrics(), srv.stats()["frames"], time.monotonic()
        dec = m1["frames_decoded"] - m0["frames_decoded"]
        sent = s1 - s0
        fps = dec / (t2 - t1)
        loss = max(0.0, (sent - dec) / sent) if sent else 1.0
        link = m1["link"]
        with rx._lock:
            frame = rx._latest
        assert frame is not None and len(frame) == FRAME_W * FRAME_H * 4 and len(set(frame[::997])) > 4  # не пустой
        data = {"codec": codec, "encoder": video.encoder_args(codec)[0], "first_frame_s": round(first, 2),
                "window_s": round(t2 - t1, 1), "sent_frames": sent, "decoded_frames": dec,
                "decoded_fps": round(fps, 2), "frame_loss": round(loss, 4), "lost_rtp_packets": link["lost_packets"],
                "seq_gaps": link["seq_gaps"], "transport_latency_ms": link["transport_latency_ms"],
                "decode_latency_ms": link["decode_latency_ms"], "restarts": m1["restarts"]}
        record(f"E2E-05 stream ({codec})", data)
        assert m1["mode"] == "remote-tls" and link["tls"] == "TLSv1.3" and m1["restarts"] == 0
        assert link["lost_packets"] == 0 and link["seq_gaps"] == 0 and link["bad_rtp"] == 0
        assert fps >= FPS_MIN, data
        assert loss <= LOSS_MAX, data
        if codec == "h264":
            assert link["decode_latency_ms"]["samples"] > 100 and link["decode_latency_ms"]["desync_resets"] == 0
    finally:
        rx.shutdown()
        srv.close()


# ---------------------------------------------------------------- E2E-06/10: источник исчез и вернулся
def test_e2e06_e2e10_source_loss_detected_and_recovered(qapp, lab):
    codec = video.choose_codec()
    box = {"srv": server(lab, codec)[0]}
    port = box["srv"].port
    px = Proxy(port)
    rx = remote_receiver(lab, codec, px.port)
    st = States(rx)
    rows = []

    def kill_sender():
        box["killed"] = box["srv"].ingest.pid
        os.killpg(box["killed"], signal.SIGKILL)            # конвейер видеоприёма (videoingest)

    def sender_relaunched():
        pid = box["srv"].ingest.pid
        return pid is not None and pid != box.get("killed")

    def service_down():
        box["srv"].close()

    def service_up():
        box["srv"] = server(lab, codec, port=port)[0]

    # (имя, обрыв, возобновление или None — возобновление сразу, пауза до возобновления, с)
    cases = [("генератор (gst) блока убит SIGKILL", kill_sender, None, 0),
             ("видеосервис блока остановлен на 5 с и запущен", service_down, service_up, 5),
             ("блок закрыл TLS (сеанс управления кончился)", lambda: box["srv"].drop("сеанс управления завершён"),
              None, 0),
             ("RST в пути (обрыв TCP)", px.reset, None, 0),
             ("сеть «чёрная дыра» 3 с (TCP жив)", px.paused.set, px.paused.clear, 3),
             ("сеть «чёрная дыра» 12 с (блок рвёт по таймауту записи)", px.paused.set, px.paused.clear, 12)]
    try:
        assert wait_stable_playing(rx, 20), rx.metrics()
        threads_before = threading.active_count()
        for name, cut, restore, pause_s in cases:
            assert wait_stable_playing(rx, 15, hold_s=2.0), (name, rx.metrics())
            sessions0 = rx.metrics()["sessions"]
            t0 = time.monotonic()
            cut()
            if cut is kill_sender and not wait_until(lambda: rx.state != "playing", NO_VIDEO_MAX_S):
                # Видеоприём блока (videoingest) перезапустил генератор внутри того же видеосеанса быстрее, чем пульт
                # объявляет «нет видео» (NO_VIDEO_AFTER_S): обрыв для оператора незаметен — это и есть восстановление.
                assert sender_relaunched() and rx.state == "playing", (name, st.since(t0), rx.metrics())
                rows.append({"case": name, "no_video_after_s": None, "recovered_after_resume_s": 0.0,
                             "new_sessions": rx.metrics()["sessions"] - sessions0, "states": st.since(t0),
                             "note": "generator relaunched in-session by video ingest, no visible gap"})
                continue
            assert wait_until(lambda: rx.state != "playing", 10), (name, st.since(t0))
            detect = time.monotonic() - t0
            if restore is not None:
                pump(pause_s * 1000)
                tr = time.monotonic()
                restore()
            else:
                tr = t0
            back = wait_stable_playing(rx, 30)
            assert back is not None, (name, st.since(t0), rx.metrics())
            row = {"case": name, "no_video_after_s": round(detect, 2), "recovered_after_resume_s": round(back - tr, 2),
                   "new_sessions": rx.metrics()["sessions"] - sessions0, "states": st.since(t0)}
            rows.append(row)
            assert detect <= NO_VIDEO_MAX_S, row
            assert back - tr <= RECOVER_MAX_S, row
        m = rx.metrics()
        record(f"E2E-06/10 loss and recovery ({codec})", {"codec": codec, "cases": rows, "restarts": m["restarts"],
                                                           "sessions": m["sessions"]})
        assert m["state"] == "playing" and m["link"]["connected"]
        t = time.monotonic()
        rx.configure(None, None)                                          # приёмник не завис: остановка мгновенная
        assert time.monotonic() - t < 3 and rx.state == "off"
        assert wait_until(lambda: not gst_children(), 5), gst_children()
        assert threading.active_count() <= threads_before + 1             # посредник: один поток на текущую пару
    finally:
        rx.shutdown()
        px.close()
        box["srv"].close()


# ---------------------------------------------------------------- дробная частота 30000/1001 через весь путь
class NtscSource(VideoSource):
    """Эмуляция устройства UVC с режимом YUYV 1280×720 @ 30000/1001 (как его описывает videoinput.Mode): caps
    захвата — ровно дробная частота, без устройства (videotestsrc вместо v4l2src)."""
    kind, id, name, available = "uvc", "uvc:emulated-ntsc", "emulated NTSC camera", True

    def __init__(self):
        self.mode = Mode(1280, 720, 30, "YUYV", "30000/1001")
        self.formats = (self.mode,)

    def capture(self):
        m = self.mode
        return ["videotestsrc", "is-live=true", "pattern=ball", "!",
                f"video/x-raw,format=YUY2,width={m.width},height={m.height},framerate={m.framerate}", "!"]


def test_e2e_ntsc_29_97_through_whole_path(qapp, lab, codec):
    src = NtscSource()
    srv, log = server(lab, codec, command=video.stream_sender_args(codec, 1280, 720, 30, src=src))
    rx = remote_receiver(lab, codec, srv.port)
    try:
        assert wait_until(lambda: rx.state == "playing", 20), rx.metrics()
        pump(3000)
        samples = []
        for _ in range(5):                                                # 5 окон по 5 с: частота и отставание
            m0, s0, t0 = rx.metrics()["frames_decoded"], srv.stats()["frames"], time.monotonic()
            client = rx._client
            lat0 = len(client.latency)
            pump(5000)
            m1, s1, t1 = rx.metrics()["frames_decoded"], srv.stats()["frames"], time.monotonic()
            lat = sorted(list(client.latency)[lat0 - len(client.latency):] if len(client.latency) > lat0 else
                         list(client.latency)[-100:])
            samples.append({"sender_fps": round((s1 - s0) / (t1 - t0), 3), "decoded_fps": round((m1 - m0) / (t1 - t0), 3),
                            "backlog_frames": s1 - m1, "transport_p50_ms": round(lat[len(lat) // 2], 2) if lat else None})
        total_dec = sum(s["decoded_fps"] for s in samples) / len(samples)
        total_snd = sum(s["sender_fps"] for s in samples) / len(samples)
        backlog = [s["backlog_frames"] for s in samples]
        data = {"codec": codec, "source_caps": src.capture()[4], "windows_5s": samples,
                "mean_sender_fps": round(total_snd, 3), "mean_decoded_fps": round(total_dec, 3),
                "backlog_first_last": [backlog[0], backlog[-1]], "restarts": rx.metrics()["restarts"]}
        record(f"29.97 fps ({codec})", data)
        assert 29.5 <= total_snd <= 30.3, data                           # генератор держит 30000/1001
        assert total_dec >= FPS_MIN and abs(total_dec - total_snd) <= 0.3, data
        # без накопления задержки: отставание декодированных кадров от отправленных не растёт
        assert backlog[-1] - backlog[0] <= 3, data
        assert rx.metrics()["restarts"] == 0
    finally:
        rx.shutdown()
        srv.close()


# ---------------------------------------------------------------- освобождение ресурсов
def test_e2e_resources_20_start_stop_and_20_drop_recover(qapp, lab):
    codec = video.choose_codec()
    srv, log = server(lab, codec)
    rx = VideoReceiver()
    key = ("127.0.0.1", srv.port)
    factory = lambda: videolink.VideoClient(lab["console"], "127.0.0.1", srv.port)   # noqa: E731
    try:
        rx.configure_remote(codec, key, factory)                          # прогрев: импорт, первый конвейер
        assert wait_until(lambda: rx.state == "playing", 20), rx.metrics()
        rx.configure(None, None)
        assert wait_until(lambda: not srv.active and not gst_children(), 10), gst_children()
        pump(500)
        base = {"threads": threading.active_count(), "fds": open_fds(), "rss_kb": rss_kb(), "children": len(descendants())}
        start_stop = []
        for i in range(20):
            rx.configure_remote(codec, key, factory)
            t = time.monotonic()
            assert wait_until(lambda: rx.state == "playing", 15), (i, rx.metrics())
            start_stop.append(time.monotonic() - t)
            pump(300)
            rx.configure(None, None)
            assert rx.state == "off"
        assert wait_until(lambda: not srv.active and not gst_children(), 10), gst_children()
        pump(500)
        after_ss = {"threads": threading.active_count(), "fds": open_fds(), "rss_kb": rss_kb(),
                    "children": len(descendants())}
        rx.configure_remote(codec, key, factory)
        assert wait_until(lambda: rx.state == "playing", 15)
        recover = []
        for i in range(20):
            assert wait_stable_playing(rx, 15, hold_s=0.5), (i, rx.metrics())
            t = time.monotonic()
            srv.drop("сеанс управления завершён")
            back = wait_stable_playing(rx, 20, hold_s=0.5)
            assert back is not None, (i, rx.metrics())
            recover.append(back - t)
        assert len(gst_children()) == 2                                   # ровно один декодер пульта + один генератор
        rx.configure(None, None)
        assert wait_until(lambda: not srv.active and not gst_children(), 10), gst_children()
        pump(500)
        after_dr = {"threads": threading.active_count(), "fds": open_fds(), "rss_kb": rss_kb(),
                    "children": len(descendants())}
        data = {"codec": codec, "baseline": base, "after_20_start_stop": after_ss, "after_20_drop_recover": after_dr,
                "start_to_playing_s": {"max": round(max(start_stop), 2), "mean": round(sum(start_stop) / 20, 2)},
                "drop_to_playing_s": {"max": round(max(recover), 2), "mean": round(sum(recover) / 20, 2)},
                "receiver_restarts": rx.metrics()["restarts"], "receiver_sessions": rx.metrics()["sessions"],
                "server_sessions": srv.stats()["sessions"],
                "peak_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // (1024 if sys.platform == "darwin"
                                                                                      else 1)}
        record(f"resources 20+20 cycles ({codec})", data)
        for k in ("threads", "fds", "children"):
            assert after_ss[k] == base[k] and after_dr[k] == base[k], (k, data)
        assert after_dr["rss_kb"] - base["rss_kb"] < 30 * 1024, data     # без порога SRS: грубая граница утечки
    finally:
        rx.shutdown()
        srv.close()
