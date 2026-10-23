# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Синтетический видеопоток (REQ-SW-002, ADR-0003/0007): конвейеры gst-launch-1.0.

Источник — только встроенный генератор videotestsrc. Передача — RTP/UDP на 127.0.0.1.
Кодек: H.264 (openh264enc → avdec_h264/openh264dec), при отсутствии — MJPEG (jpegenc → jpegdec).
На Pi 4 кодирование и декодирование будут аппаратными (v4l2h264enc/v4l2h264dec) — проверяется на S2.
"""
import ctypes
import os
import shutil
import signal
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

GST_LAUNCH = "gst-launch-1.0"
_PR_SET_PDEATHSIG = 1
_PDEATH = Path(__file__).with_name("pdeath.py")


def die_with_parent():
    """preexec_fn: новая группа процессов и SIGKILL при смерти родителя (Linux).

    Без этого аварийное завершение эмулятора или пульта оставляет живой gst-launch-1.0, который
    продолжает слать или принимать поток (дефект найден интеграционным тестом S1.5).
    """
    os.setsid()
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except OSError:
        pass


def spawn(args, **kw):
    """Дочерний процесс GStreamer, который завершается вместе с родителем.

    Предпочтительно через setpriv --pdeathsig (util-linux): preexec_fn небезопасен в многопоточном процессе.
    Вне Linux pdeathsig нет — через сторож pdeath.py. Во всех случаях процесс — лидер своей группы (killpg).
    """
    if shutil.which("setpriv"):
        return subprocess.Popen(["setpriv", "--pdeathsig", "KILL", "--", *args], stdin=subprocess.DEVNULL,
                                process_group=0, **kw)
    if sys.platform.startswith("linux"):
        return subprocess.Popen(args, stdin=subprocess.DEVNULL, preexec_fn=die_with_parent, **kw)
    return subprocess.Popen([sys.executable, "-I", str(_PDEATH), "--parent", str(os.getpid()), "--", *args],
                            stdin=subprocess.DEVNULL, process_group=0, **kw)
H264_CAPS = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
JPEG_CAPS = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"


@lru_cache(maxsize=None)
def has_element(name: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    return subprocess.run(["gst-inspect-1.0", "--exists", name], capture_output=True).returncode == 0


def available() -> bool:
    return shutil.which(GST_LAUNCH) is not None and has_element("videotestsrc") and has_element("udpsrc")


ENCODERS = {"h264": ["openh264enc", "complexity=0", "gop-size=30", "bitrate=2000000", "!",
                      "rtph264pay", "config-interval=1", "pt=96"],
            "jpeg": ["jpegenc", "quality=70", "!", "rtpjpegpay"]}


@lru_cache(maxsize=None)
def codec_works(codec) -> bool:
    """Пробное кодирование 3 кадров. Наличия элемента мало: в Ubuntu libopenh264 бывает заглушкой без кодирования."""
    if not shutil.which(GST_LAUNCH):
        return False
    r = subprocess.run([GST_LAUNCH, "videotestsrc", "num-buffers=3", "!", "video/x-raw,width=64,height=64", "!",
                        "videoconvert", "!", "video/x-raw,format=I420", "!", *ENCODERS[codec], "!", "fakesink"],
                       capture_output=True, text=True, timeout=20)
    out = r.stdout + r.stderr
    return r.returncode == 0 and "Error" not in out and "WARNING" not in out


def choose_codec(prefer="h264"):
    h264 = has_element("openh264enc") and has_element("rtph264pay") and \
        (has_element("avdec_h264") or has_element("openh264dec")) and has_element("rtph264depay") and codec_works("h264")
    jpeg = has_element("jpegenc") and has_element("rtpjpegpay") and has_element("jpegdec") and \
        has_element("rtpjpegdepay") and codec_works("jpeg")
    if prefer == "h264" and h264:
        return "h264"
    if jpeg:
        return "jpeg"
    return "h264" if h264 else None


def sender_args(codec, port, width=1280, height=720, fps=30, host="127.0.0.1", pattern="smpte"):
    src = [GST_LAUNCH, "-q", "videotestsrc", "is-live=true", f"pattern={pattern}", "!",
           f"video/x-raw,width={width},height={height},framerate={fps}/1", "!",
           "timeoverlay", "halignment=right", "valignment=bottom", "font-desc=Sans 28", "!",
           "textoverlay", "text=SHEREGESH SYNTHETIC TEST PATTERN", "valignment=top", "font-desc=Sans 24", "!",
           "videoconvert", "!", "video/x-raw,format=I420", "!"]   # rtpjpegpay принимает только 4:2:0/4:2:2
    return src + ENCODERS[codec] + ["!", "udpsink", f"host={host}", f"port={port}", "sync=false"]


def receiver_args(codec, port, width, height):
    """Вывод — сырые кадры RGBx фиксированного размера в stdout (fd=1)."""
    if codec == "h264":
        dec = "avdec_h264" if has_element("avdec_h264") else "openh264dec"
        head = [f"caps={H264_CAPS}", "!", "rtpjitterbuffer", "latency=50", "!", "rtph264depay", "!", dec, "!"]
    else:
        head = [f"caps={JPEG_CAPS}", "!", "rtpjitterbuffer", "latency=50", "!", "rtpjpegdepay", "!", "jpegdec", "!"]
    return [GST_LAUNCH, "-q", "udpsrc", f"port={port}", "address=127.0.0.1"] + head + \
        ["videoconvert", "!", "videoscale", "!", f"video/x-raw,format=RGBx,width={width},height={height}", "!",
         "fdsink", "fd=1", "sync=false"]
