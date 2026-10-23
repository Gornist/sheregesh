# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Синтетический видеопоток (REQ-SW-002, ADR-0003/0007): конвейеры gst-launch-1.0.

Источник — только встроенный генератор videotestsrc. Передача — RTP/UDP на 127.0.0.1.
Кодек: H.264 (openh264enc → avdec_h264/openh264dec), при отсутствии — MJPEG (jpegenc → jpegdec).
На Pi 4 кодирование и декодирование будут аппаратными (v4l2h264enc/v4l2h264dec) — проверяется на S2.
"""
import shutil
import subprocess
from functools import lru_cache

GST_LAUNCH = "gst-launch-1.0"
H264_CAPS = "application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000"
JPEG_CAPS = "application/x-rtp,media=video,encoding-name=JPEG,payload=26,clock-rate=90000"


@lru_cache(maxsize=None)
def has_element(name: str) -> bool:
    if not shutil.which("gst-inspect-1.0"):
        return False
    return subprocess.run(["gst-inspect-1.0", "--exists", name], capture_output=True).returncode == 0


def available() -> bool:
    return shutil.which(GST_LAUNCH) is not None and has_element("videotestsrc") and has_element("udpsrc")


def choose_codec(prefer="h264"):
    h264 = has_element("openh264enc") and has_element("rtph264pay") and \
        (has_element("avdec_h264") or has_element("openh264dec")) and has_element("rtph264depay")
    jpeg = has_element("jpegenc") and has_element("rtpjpegpay") and has_element("jpegdec") and has_element("rtpjpegdepay")
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
           "videoconvert", "!"]
    if codec == "h264":
        enc = ["openh264enc", "complexity=0", "gop-size=30", f"bitrate={2_000_000}", "!",
               "rtph264pay", "config-interval=1", "pt=96", "!"]
    else:
        enc = ["jpegenc", "quality=70", "!", "rtpjpegpay", "!"]
    return src + enc + ["udpsink", f"host={host}", f"port={port}", "sync=false"]


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
