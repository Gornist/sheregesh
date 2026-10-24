#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Измерение локального видеовхода S3.3: частота, интервалы кадров, время до первого кадра и загрузка ЦП.

Измеряется ровно тот конвейер, который запускает пульт (sheregesh.video.capture_args → RGBx 640×360 в stdout):
  python3 tools/dev/video_input_bench.py --emulate mjpeg --mode 1280x720@30 --seconds 20   # без устройства
  python3 tools/dev/video_input_bench.py --device 534d:2109 --mode 1920x1080@30           # устройство UVC

--emulate — источник без устройства: videotestsrc, закодированный в формат UVC (YUYV/MJPEG/H.264). Стоимость
эмуляции оценивается отдельным базовым прогоном (тот же источник → fakesink) и вычитается.
Задержка обработки: конвейер — одна цепочка без очередей и буфера джиттера (sync=false), поэтому добавленная им
задержка кадра не больше времени ЦП обработки на кадр (processing_ms_per_frame). Время в устройстве, на шине USB и
до фотонов дисплея сюда не входит — его измеряют по процедуре «стекло-стекло» (REPORT.md S3.3).
Захват устройства запускается только этой командой, по явному выбору оператора.
"""
import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "software"))
from sheregesh import video, videoinput  # noqa: E402

W, H = 640, 360


class EmulatedSource(videoinput.VideoSource):
    """Только для измерений: тестовая таблица в формате устройства UVC."""
    kind = "emulated"
    available = True

    def __init__(self, pixfmt, width, height, fps):
        self.mode = videoinput.Mode(width, height, fps, pixfmt)
        self.id = self.name = f"emulated-{pixfmt.lower()}"

    def capture(self):
        m = self.mode
        raw = f"video/x-raw,width={m.width},height={m.height},framerate={m.fps}/1"
        size = f"width={m.width},height={m.height},framerate={m.fps}/1"
        enc = {"YUYV": ["videoconvert", "!", f"video/x-raw,format=YUY2,{size}", "!"],
               "MJPG": ["videoconvert", "!", "video/x-raw,format=I420", "!", "jpegenc", "quality=85", "!", f"image/jpeg,{size}", "!"],
               "H264": [*(video.h264_elements() or (video.OPENH264_ENC,))[0], "!",
                        f"video/x-h264,stream-format=byte-stream,{size}", "!"]}[m.pixfmt]
        return ["videotestsrc", "is-live=true", "pattern=ball", "!", raw, "!", *enc]


def run(args, seconds):
    """Процесс конвейера на seconds: (моменты полных кадров RGBx, ЦП-секунды, стена, код)."""
    t0 = time.monotonic()
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, buf, stamps = W * H * 4, 0, []
    fd = p.stdout.fileno()
    while time.monotonic() - t0 < seconds:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            done = os.wait4(p.pid, os.WNOHANG)            # не p.poll(): процесс нужен неснятым для rusage
            if done[0]:
                return stamps, done[2].ru_utime + done[2].ru_stime, time.monotonic() - t0, done[1]
            continue
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        buf += len(chunk)
        while buf >= size:
            buf -= size
            stamps.append(time.monotonic() - t0)
    os.kill(p.pid, 15)
    _, status, ru = os.wait4(p.pid, 0)
    return stamps, ru.ru_utime + ru.ru_stime, time.monotonic() - t0, status


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(q / 100 * len(s)))], 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emulate", choices=["yuyv", "mjpeg", "h264"])
    ap.add_argument("--device", default="", help="vvvv:pppp[:serial] или videoN; без --emulate")
    ap.add_argument("--format", default="auto", choices=["auto", "yuyv", "mjpeg", "h264"])
    ap.add_argument("--mode", default="1280x720@30")
    ap.add_argument("--seconds", type=float, default=15.0)
    a = ap.parse_args(argv)
    if a.emulate:
        w, h, fps = videoinput.parse_mode(a.mode)
        src = EmulatedSource({"yuyv": "YUYV", "mjpeg": "MJPG", "h264": "H264"}[a.emulate], w, h, fps)
    else:
        src = videoinput.open_uvc(a.device, a.mode, a.format)
        if not src.available:
            print(json.dumps({"error": src.error}, ensure_ascii=False))
            return 2
    full = video.capture_args(src, W, H)
    base = video._raw(src) + ["fakesink", "sync=false"]       # тот же источник без нашего пути обработки
    run(video._raw(src) + ["fakesink", "num-buffers=1"], 10)  # прогрев: реестр модулей GStreamer
    stamps, cpu, wall, _ = run(full, a.seconds)
    _, cpu_base, wall_base, _ = run(base, a.seconds)
    iv = [1000 * (b - x) for x, b in zip(stamps, stamps[1:])]
    steady = [t for t in stamps if t > 2.0]                          # без разгона первых двух секунд
    proc_s = max(cpu - cpu_base * wall / wall_base, 0.0)
    out = {"source": {k: v for k, v in src.info().items() if k != "formats"}, "pipeline": " ".join(full[2:]),
           "seconds": a.seconds, "frames": len(stamps),
           "first_frame_ms": round(1000 * stamps[0]) if stamps else None,
           "fps_steady": round((len(steady) - 1) / (steady[-1] - steady[0]), 1) if len(steady) > 2 else 0.0,
           "interval_ms": {"p50": pct(iv, 50), "p95": pct(iv, 95), "max": round(max(iv), 1) if iv else None},
           "cpu_pct_one_core": {"total": round(100 * cpu / wall, 1), "source_only": round(100 * cpu_base / wall_base, 1),
                                "processing": round(100 * proc_s / wall, 1)},
           "processing_ms_per_frame": round(1000 * proc_s / len(stamps), 2) if stamps else None,
           "host": f"{os.uname().sysname} {os.uname().machine} {os.cpu_count()} CPU"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if stamps else 1


if __name__ == "__main__":
    raise SystemExit(main())
