#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Задержка «стекло–стекло» видео стенда по штрихкоду времени в кадре (HDMI-источник devbox → pi2 → пульт pi1).

Источник — tools/lab-video/hdmi_testpattern.py на HDMI devbox: в каждом кадре 32 бита миллисекунд от полуночи UTC
по часам devbox (клетки 8×4 в правом нижнем углу, рамка-ориентир). На пульте каждый декодированный кадр берётся с
того же сокета, откуда его берёт GUI (/run/sheregesh-video/frames-main.sock), штрихкод читается, задержка =
время получения кадра (часы pi1) + смещение (devbox − pi1) − время из кадра.

Смещение часов меряется напрямую, не по NTP: на время замера на devbox — UDP-сервер времени (этот же файл,
--role timeserver), pi1 опрашивает его PROBES раз, берётся ответ с наименьшим RTT (погрешность ≤ RTT/2, в LAN —
доли мс). Измеряется: отрисовка кадра GTK на devbox → композитор и вывод HDMI → MS2130 → захват UVC и кодер pi2 →
SRTP → декодер pi1. Не входит: показ на дисплее пульта (у pi1 сейчас нет дисплея); время из кадра — момент
отрисовки, до вывода на HDMI (композитор/vsync devbox — в пределах 1–2 кадров 60 Гц) — входит в замер.

С Mac:  python3 tools/probe/g2g_latency.py --console 192.168.1.94 --source devbox [--seconds 30] [--out DIR]
Роли на хостах (файл передаётся по SSH текстом, на платах ничего не ставится): --role timeserver | --role pi1."""
import argparse
import base64
import json
import select
import socket
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

BITS_W, BITS_H = 8, 4
PORT = 45123
PROBES = 50
DAY_MS = 86_400_000
MAX_PLAUSIBLE_MS = 5_000          # дальше — неверно прочитанный штрихкод (или часы рассинхронизированы)
FRAMES_SOCK = "/run/sheregesh-video/frames-main.sock"


# ---------------- геометрия и чтение штрихкода (как в hdmi_testpattern.Canvas) ----------------
def geometry(w, h):
    """(x0, y0, cell) клеток штрихкода для кадра w×h (картинка на весь экран, масштаб сохраняется)."""
    cell = min(w, h) * 0.035
    return w - cell * (BITS_W + 2), h - cell * (BITS_H + 2), cell


def decode(pix, w, h, stride=None, bpp=1, chan=0):
    """Кадр → миллисекунды от полуночи UTC или None (рамка-ориентир не найдена — кадр не наш). pix — GRAY8 (bpp 1)
    или RGB (bpp 3, chan 1 — зелёный: v4l2convert на пульте GRAY8 не отдаёт, для чёрно-белого штрихкода хватает)."""
    stride = stride or w * bpp
    x0, y0, cell = geometry(w, h)

    def px(x, y):
        x, y = int(round(x)), int(round(y))
        return pix[y * stride + x * bpp + chan] if 0 <= x < w and 0 <= y < h else 0

    white_rim = px(x0 - cell * 0.375, y0 + cell * 2) > 160 and px(x0 + cell * 4, y0 - cell * 0.375) > 160
    black_rim = px(x0 - cell * 0.125, y0 + cell * 2) < 96 and px(x0 + cell * 4, y0 - cell * 0.125) < 96
    if not (white_rim and black_rim):
        return None
    value = 0
    for i in range(32):
        bit = px(x0 + (i % BITS_W + 0.5) * cell, y0 + (i // BITS_W + 0.5) * cell) > 128
        value = value << 1 | int(bit)
    return value if value < DAY_MS else None


def ms_of_day(t):
    """Секунды Unix → миллисекунды от полуночи UTC."""
    return int(round(t * 1000)) % DAY_MS


def latency_ms(recv_t, offset_s, code_ms):
    """Задержка кадра: время получения (часы pi1) + смещение (devbox − pi1) − время из кадра; с переходом суток."""
    d = (ms_of_day(recv_t + offset_s) - code_ms) % DAY_MS
    return d if d < MAX_PLAUSIBLE_MS else None


def best_offset(samples):
    """[(t0, t_server, t1)] → (смещение сервер − клиент, RTT) по пробе с наименьшим RTT (алгоритм Кристиана)."""
    t0, ts, t1 = min(samples, key=lambda s: s[2] - s[0])
    return ts - (t0 + t1) / 2, t1 - t0


def summarize(lat, unread, offset, rtt, seconds):
    q = sorted(lat)

    def pct(p):
        return round(q[min(len(q) - 1, int(p * (len(q) - 1) + 0.5))], 1) if q else None
    return {"frames_read": len(lat), "frames_unread": unread, "seconds": seconds,
            "fps_read": round(len(lat) / seconds, 2) if seconds else None,
            "latency_ms": {"min": pct(0), "p50": pct(0.5), "p95": pct(0.95), "max": pct(1),
                           "mean": round(statistics.fmean(q), 1) if q else None},
            "clock": {"offset_ms": round(offset * 1000, 3), "rtt_ms": round(rtt * 1000, 3),
                      "error_bound_ms": round(rtt * 500, 3)}}


# ---------------- роли на хостах ----------------
def role_timeserver(max_s):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", PORT))
    s.settimeout(1.0)
    print("READY", flush=True)
    t_end = time.monotonic() + max_s
    while time.monotonic() < t_end:
        r, _, _ = select.select([s, sys.stdin], [], [], 1.0)
        if sys.stdin in r and not sys.stdin.readline():         # SSH закрыт — замер окончен, порт освободить сразу
            break
        if s in r:
            data, addr = s.recvfrom(64)
            s.sendto(data + struct.pack("!d", time.time()), addr)
    return 0


CAPTURE = ("v4l2src device={device} do-timestamp=true ! "
           "video/x-raw,format=YUY2,width=1280,height=720,framerate={fps}/1 ! appsink name=s max-buffers=4 drop=true "
           "sync=false")
DECODED = (f"unixfdsrc socket-path={FRAMES_SOCK} ! v4l2convert ! video/x-raw,format=RGB ! "
           "appsink name=s max-buffers=4 drop=true sync=false")


def role_pi1(server, seconds, pipeline=DECODED, bpp=3, chan=1):
    """Кадры с конвейера pipeline: по умолчанию — декодированные на пульте (RGB, зелёный канал); на блоке —
    сам захват (YUY2: яркость — чётные байты, bpp 2, chan 0) — задержка стороны источника без кодера и сети."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.0)
    samples = []
    for i in range(PROBES):
        t0 = time.time()
        s.sendto(struct.pack("!I", i), (server, PORT))
        try:
            data, _ = s.recvfrom(64)
        except socket.timeout:
            continue
        t1 = time.time()
        if struct.unpack("!I", data[:4])[0] == i:
            samples.append((t0, struct.unpack("!d", data[4:12])[0], t1))
        time.sleep(0.02)
    if not samples:
        print(json.dumps({"error": f"сервер времени {server}:{PORT} не ответил"}))
        return 2
    offset, rtt = best_offset(samples)
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstApp", "1.0")                         # без неё у appsink нет try_pull_sample
    from gi.repository import Gst, GstApp  # noqa: F401
    Gst.init(None)
    pipe = Gst.parse_launch(pipeline)
    sink = pipe.get_by_name("s")
    pipe.set_state(Gst.State.PLAYING)
    lat, unread, t_end = [], 0, time.monotonic() + seconds
    try:
        while time.monotonic() < t_end:
            sample = sink.try_pull_sample(Gst.SECOND)
            recv = time.time()
            if sample is None:
                continue
            st = sample.get_caps().get_structure(0)
            w, h = st.get_value("width"), st.get_value("height")
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            code = decode(data, w, h, len(data) // h, bpp=bpp, chan=chan)
            v = latency_ms(recv, offset, code) if code is not None else None
            if v is None:
                unread += 1
            else:
                lat.append(v)
    finally:
        pipe.set_state(Gst.State.NULL)
    print(json.dumps(summarize(lat, unread, offset, rtt, seconds), ensure_ascii=False))
    return 0 if lat else 1


# ---------------- оркестрация с Mac ----------------
def remote_cmd(host, args):
    b64 = base64.b64encode(Path(__file__).read_bytes()).decode()
    prog = f"import base64;exec(compile(base64.b64decode('{b64}'),'g2g_latency','exec'))"
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, f"python3 -c \"{prog}\" {args}"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--role", choices=("timeserver", "pi1", "capture"))
    ap.add_argument("--device", default="/dev/v4l/by-id/usb-MACROSILICON_USB3.0_UHD_20210623-video-index0")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--at", default="console", choices=("console", "capture"),
                    help="console — декодированные кадры пульта (полный путь); capture — захват на блоке (--remote), "
                         "передача видео блока на время замера останавливается")
    ap.add_argument("--remote", default="192.168.1.173")
    ap.add_argument("--server")
    ap.add_argument("--console", default="192.168.1.94")
    ap.add_argument("--source", default="devbox", help="хост SSH с HDMI-источником")
    ap.add_argument("--source-ip", default="192.168.1.141", help="его адрес для UDP с пульта")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    if a.role == "timeserver":
        return role_timeserver(a.seconds)
    if a.role == "pi1":
        return role_pi1(a.server, a.seconds)
    if a.role == "capture":
        return role_pi1(a.server, a.seconds, CAPTURE.format(device=a.device, fps=a.fps), bpp=2, chan=0)
    srv = subprocess.Popen(remote_cmd(a.source, f"--role timeserver --seconds {a.seconds + 90:.0f}"),
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not srv.stdout.readline().startswith("READY"):
            raise SystemExit("сервер времени на источнике не стартовал")
        if a.at == "console":
            r = subprocess.run(remote_cmd(f"root@{a.console}", f"--role pi1 --server {a.source_ip} --seconds {a.seconds}"),
                               capture_output=True, text=True, timeout=a.seconds + 120)
        else:
            host = f"root@{a.remote}"
            subprocess.run(["ssh", host, "systemctl stop sheregesh-video-tx"], check=True, timeout=60)
            try:
                r = subprocess.run(remote_cmd(host, f"--role capture --server {a.source_ip} --seconds {a.seconds} "
                                                    f"--device {a.device} --fps {a.fps}"),
                                   capture_output=True, text=True, timeout=a.seconds + 120)
            finally:
                subprocess.run(["ssh", host, "systemctl start sheregesh-video-tx"], timeout=60)
    finally:
        srv.stdin.close()                                       # сервер на источнике выходит по закрытию stdin
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
    line = (r.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        res = json.loads(line)
    except ValueError:
        res = {}
    if not res.get("frames_read"):
        res["error"] = res.get("error") or f"пульт: код {r.returncode}: {(r.stderr or r.stdout).strip()[-600:]}"
    res.update({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "console": a.console, "source": a.source,
                "path": "devbox GTK → HDMI → MS2130 → pi2 UVC/H.264 → SRTP → декодер pi1" if a.at == "console"
                else f"devbox GTK → HDMI → MS2130 → захват pi2 (YUYV 720p{a.fps}, без кодера и сети)"})
    print(json.dumps(res, ensure_ascii=False, indent=1))
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "g2g.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("frames_read") else 1


if __name__ == "__main__":
    sys.exit(main())
