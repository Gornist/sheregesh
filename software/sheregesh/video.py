# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Синтетический видеопоток (REQ-SW-002, ADR-0003/0007): конвейеры gst-launch-1.0.

Источник — только встроенный генератор videotestsrc. Передача — RTP/UDP на 127.0.0.1.
Кодек: H.264 — аппаратный v4l2h264enc/v4l2h264dec (Pi 4), иначе openh264enc → avdec_h264/openh264dec;
при отсутствии работающей связки — MJPEG (jpegenc → jpegdec). Аппаратный путь подтверждает проба S2-4.
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


# Pi 4: аппаратный кодер требует уровень в caps и повтор SPS/PPS (иначе приёмник, подключившийся позже, не декодирует).
V4L2_ENC = ["v4l2h264enc", "extra-controls=controls,repeat_sequence_header=1,video_bitrate=2000000,h264_i_frame_period=30",
            "!", "video/x-h264,level=(string)4", "!", "h264parse"]
OPENH264_ENC = ["openh264enc", "complexity=0", "gop-size=30", "bitrate=2000000"]
RTP_H264 = ["!", "rtph264pay", "config-interval=1", "pt=96"]
ENCODERS = {"jpeg": ["jpegenc", "quality=70", "!", "rtpjpegpay"]}
_PROBE_SRC = ["videotestsrc", "num-buffers=10", "!", "video/x-raw,width=320,height=240,framerate=30/1", "!",
              "videoconvert", "!", "video/x-raw,format=I420", "!"]


def _pipeline_ok(elements) -> bool:
    """Пробный прогон: наличия элемента мало (заглушка libopenh264 в Ubuntu, недоступный /dev/video* на Pi)."""
    if not shutil.which(GST_LAUNCH):
        return False
    try:
        r = subprocess.run([GST_LAUNCH, *_PROBE_SRC, *elements, "!", "fakesink"], capture_output=True, text=True,
                           timeout=20)
    except subprocess.TimeoutExpired:
        return False
    out = r.stdout + r.stderr
    return r.returncode == 0 and "Error" not in out and "WARNING" not in out


@lru_cache(maxsize=None)
def h264_elements():
    """(кодер, декодер) H.264: аппаратные v4l2 (Pi 4), иначе openh264/libav; None — если ни одна связка не работает.

    Каждая пара проверяется кодированием и декодированием 10 кадров.
    """
    encoders = [e for e in (V4L2_ENC, OPENH264_ENC) if has_element(e[0])]
    decoders = [d for d in ("v4l2h264dec", "avdec_h264", "openh264dec") if has_element(d)]
    if not (has_element("rtph264pay") and has_element("rtph264depay") and has_element("h264parse")):
        return None
    for enc in encoders:
        for dec in decoders:
            if _pipeline_ok([*enc, "!", "h264parse", "!", dec]):
                return enc, dec
    return None


def encoder_args(codec):
    return [*h264_elements()[0], *RTP_H264] if codec == "h264" else ENCODERS[codec]


def describe(codec) -> str:
    """Для страницы «О системе» и журнала: кодек и фактические элементы GStreamer."""
    if codec == "h264" and h264_elements():
        enc, dec = h264_elements()
        return f"H.264 ({enc[0]} → {dec})"
    return {"jpeg": "MJPEG (jpegenc → jpegdec)"}.get(codec, codec or "—")


@lru_cache(maxsize=None)
def codec_works(codec) -> bool:
    if codec == "h264":
        return h264_elements() is not None
    return _pipeline_ok([*ENCODERS[codec], "!", "rtpjpegdepay", "!", "jpegdec"])


def choose_codec(prefer="h264"):
    h264 = codec_works("h264")
    jpeg = has_element("jpegenc") and has_element("rtpjpegpay") and has_element("jpegdec") and \
        has_element("rtpjpegdepay") and codec_works("jpeg")
    if prefer == "h264" and h264:
        return "h264"
    if jpeg:
        return "jpeg"
    return "h264" if h264 else None


def sender_args(codec, port, width=1280, height=720, fps=30, host="127.0.0.1", pattern="smpte"):
    src = [GST_LAUNCH, "-q", "videotestsrc", "is-live=true", f"pattern={pattern}", "!",
           f"video/x-raw,width={width},height={height},framerate={fps}/1", "!"]
    # Надписи — элементы pango (в Debian — пакет gstreamer1.0-x). Без них поток идёт без надписей, а не падает.
    if has_element("timeoverlay"):
        src += ["timeoverlay", "halignment=right", "valignment=bottom", "font-desc=Sans 28", "!"]
    if has_element("textoverlay"):
        src += ["textoverlay", "text=SHEREGESH SYNTHETIC TEST PATTERN", "valignment=top", "font-desc=Sans 24", "!"]
    src += ["videoconvert", "!", "video/x-raw,format=I420", "!"]   # rtpjpegpay принимает только 4:2:0/4:2:2
    return src + encoder_args(codec) + ["!", "udpsink", f"host={host}", f"port={port}", "sync=false"]


def receiver_args(codec, port, width, height):
    """Вывод — сырые кадры RGBx фиксированного размера в stdout (fd=1)."""
    if codec == "h264":
        dec = h264_elements()[1]
        head = [f"caps={H264_CAPS}", "!", "rtpjitterbuffer", "latency=50", "!", "rtph264depay", "!", "h264parse", "!",
                dec, "!"]
    else:
        head = [f"caps={JPEG_CAPS}", "!", "rtpjitterbuffer", "latency=50", "!", "rtpjpegdepay", "!", "jpegdec", "!"]
    return [GST_LAUNCH, "-q", "udpsrc", f"port={port}", "address=127.0.0.1"] + head + \
        ["videoconvert", "!", "videoscale", "!", f"video/x-raw,format=RGBx,width={width},height={height}", "!",
         "fdsink", "fd=1", "sync=false"]
