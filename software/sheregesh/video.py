# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Синтетический видеопоток (REQ-SW-002, ADR-0003/0007): конвейеры gst-launch-1.0.

Источник — встроенный генератор videotestsrc или стандартное устройство V4L2/UVC (S3.3, sheregesh.videoinput: один
интерфейс VideoSource; конвейеры собираются только здесь). Передача — RTP/UDP на 127.0.0.1 (автономный режим) или
RTP поверх канала процесса (RFC 4571, rtpstreampay/rtpstreamdepay) для защищённого видеоканала между платами
(sheregesh.videolink, ADR-0011): тогда RTP не попадает ни в один сокет.
Кодек: H.264 — аппаратный v4l2h264enc/v4l2h264dec (Pi 4), иначе openh264enc → avdec_h264/openh264dec;
при отсутствии работающей связки — MJPEG (jpegenc → jpegdec). Аппаратный путь подтверждает проба S2-4.
"""
import ctypes
import os
import shutil
import signal
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .videoinput import SyntheticSource

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


# SIGHUP — сигнал перечитывания конфигурации блока (sheregesh_remote: video-input.json). `systemctl kill -s HUP`
# по умолчанию шлёт его всей cgroup службы, а gst-launch-1.0 по SIGHUP завершается — видео рвалось бы при каждом
# перечитывании. Игнорирование (SIG_IGN) наследуется через exec, поэтому конвейер запускается через
# `sh -c 'trap "" HUP; exec …'`: PID тот же (exec), остановка — по-прежнему killpg SIGTERM/SIGKILL.
NO_HUP = ("sh", "-c", 'trap "" HUP; exec "$@"', "sh")


def spawn(args, stdin=subprocess.DEVNULL, **kw):
    """Дочерний процесс GStreamer, который завершается вместе с родителем и не реагирует на SIGHUP (NO_HUP).

    Предпочтительно через setpriv --pdeathsig (util-linux): preexec_fn небезопасен в многопоточном процессе.
    Вне Linux pdeathsig нет — через сторож pdeath.py. Во всех случаях процесс — лидер своей группы (killpg).
    """
    args = [*NO_HUP, *args]
    if shutil.which("setpriv"):
        return subprocess.Popen(["setpriv", "--pdeathsig", "KILL", "--", *args], stdin=stdin,
                                process_group=0, **kw)
    if sys.platform.startswith("linux"):
        return subprocess.Popen(args, stdin=stdin, preexec_fn=die_with_parent, **kw)
    return subprocess.Popen([sys.executable, "-I", str(_PDEATH), "--parent", str(os.getpid()), "--", *args],
                            stdin=stdin, process_group=0, **kw)
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
# 6 Мбит/с, переменный битрейт: на pi2 (720p30, сложная сцена) задержка кодера та же, что при 2 Мбит/с (p50 10,0 мс
# против 9,7), качество выше; при минимальном QP 20 кодер выше ≈ 5,7 Мбит/с и не поднимается. Постоянный битрейт
# (video_bitrate_mode=1) не включать: кодер копит кадры, задержка ≈ 450 мс.
V4L2_ENC = ["v4l2h264enc", "extra-controls=controls,repeat_sequence_header=1,video_bitrate=6000000,h264_i_frame_period=30",
            "!", "video/x-h264,level=(string)4", "!", "h264parse"]
OPENH264_ENC = ["openh264enc", "complexity=0", "gop-size=30", "bitrate=2000000"]
RTP_H264 = ["!", "rtph264pay", "config-interval=1", "pt=96"]
ENCODERS = {"jpeg": ["jpegenc", "quality=70", "!", "rtpjpegpay"]}
_PROBE_SRC = ["videotestsrc", "num-buffers=10", "!", "video/x-raw,width=320,height=240,framerate=30/1", "!",
              "videoconvert", "!", "video/x-raw,format=I420", "!"]


PROBE_FAILURES = []   # причины неудачных проб (для журнала: «VIDEO unavailable» без причины не диагностируется)


def _pipeline_ok(elements) -> bool:
    """Пробный прогон: наличия элемента мало (заглушка libopenh264 в Ubuntu, недоступный /dev/video* на Pi)."""
    if not shutil.which(GST_LAUNCH):
        PROBE_FAILURES.append(f"{GST_LAUNCH} not found")
        return False
    try:
        r = subprocess.run([GST_LAUNCH, *_PROBE_SRC, *elements, "!", "fakesink"], capture_output=True, text=True,
                           timeout=20)
    except subprocess.TimeoutExpired:
        PROBE_FAILURES.append(f"{elements[0]}: timeout 20 s")
        return False
    bad = probe_errors(r.stdout + r.stderr)
    ok = r.returncode == 0 and not bad
    if not ok:
        PROBE_FAILURES.append(f"{elements[0]}: rc={r.returncode} {' | '.join(bad)[-300:] or (r.stdout + r.stderr).strip()[-200:]}")
    return ok


def probe_errors(out):
    """Строки, означающие отказ пробы: ERROR/WARNING: от gst-launch и «Error» кодеков (заглушка openh264).
    Предупреждения GLib и Fontconfig окружения (нет домашнего каталога, uid DynamicUser без NSS) — не отказ."""
    return [x.strip() for x in out.splitlines()
            if re.search(r"^(ERROR|WARNING:)|\bError\b", x.strip()) and "GLib-WARNING" not in x and "Fontconfig" not in x]


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


def synthetic(width=1280, height=720, fps=30, pattern="smpte"):
    """Синтетический источник (sheregesh.videoinput.SyntheticSource) с надписями, если есть элементы pango."""
    # Надписи — элементы pango (в Debian — пакет gstreamer1.0-x). Без них поток идёт без надписей, а не падает.
    overlays = []
    if has_element("timeoverlay"):
        overlays += ["timeoverlay", "halignment=right", "valignment=bottom", "font-desc=Sans 28", "!"]
    if has_element("textoverlay"):
        overlays += ["textoverlay", "text=SHEREGESH SYNTHETIC TEST PATTERN", "valignment=top", "font-desc=Sans 24", "!"]
    return SyntheticSource(width, height, fps, pattern, overlays)


# Форматы источника, которые аппаратный кодер Pi 4 (bcm2835-codec) принимает сам: videoconvert на CPU перед ним
# лишний. Замер на pi2: 720p30 — 9,5 мс до выхода кодера вместо 14,9 и вдвое меньше CPU; 1080p30 — 21,6 мс и 30 к/с
# вместо 30,5 мс и ≈ 24 к/с. I420 — синтетический генератор (формат согласуется с кодером напрямую). MJPEG сюда не
# входит: jpegdec выдаёт и 4:2:2 (Y42B), которого кодер не принимает.
V4L2_ENC_DIRECT = frozenset({"YUYV", "UYVY", "I420"})


def _direct_to_encoder(codec, src):
    """Сырые кадры источника идут в аппаратный кодер H.264 без videoconvert."""
    if codec != "h264" or (h264_elements() or ([""],))[0][0] != "v4l2h264enc":
        return False
    mode = getattr(src, "mode", None)
    return mode is not None and mode.pixfmt in V4L2_ENC_DIRECT


def _source(width, height, fps, pattern, src=None, direct=False):
    """Сырые кадры от источника (по умолчанию — синтетического) для кодера: I420 через videoconvert, либо формат
    источника как есть, если его принимает кодер (direct)."""
    raw = _raw(src or synthetic(width, height, fps, pattern))
    return raw if direct else raw + ["videoconvert", "!", "video/x-raw,format=I420", "!"]   # rtpjpegpay — только 4:2:0/4:2:2


def _raw(src):
    """gst-launch-1.0 + захват + декодирование формата источника до сырых кадров. Декодер H.264 — тот же, что выбрала
    проба кодеков (v4l2h264dec на Pi 4); проба запускается, только если источник действительно отдаёт H.264."""
    mode = getattr(src, "mode", None)
    dec = None
    if mode is not None and mode.pixfmt == "H264":
        h = h264_elements()
        dec = h[1] if h else "avdec_h264"
    return [GST_LAUNCH, "-q", *src.capture(), *src.decode(dec)]


# Сквозная передача без перекодирования: формат устройства совпадает с кодеком канала.
_PASSTHROUGH = {("MJPG", "jpeg"): ["rtpjpegpay"],
                ("H264", "h264"): ["h264parse", "config-interval=-1", "!", "video/x-h264,alignment=au", *RTP_H264]}


def _encoded(codec, width, height, fps, pattern, src):
    """Кодированный RTP от источника: сквозной, если устройство уже отдаёт кодек канала, иначе — кодер."""
    mode = getattr(src, "mode", None)
    through = _PASSTHROUGH.get((mode.pixfmt, codec)) if mode is not None else None
    if through:
        return [GST_LAUNCH, "-q", *src.capture(), *through]
    src = src or synthetic(width, height, fps, pattern)
    return _source(width, height, fps, pattern, src, _direct_to_encoder(codec, src)) + encoder_args(codec)


def capture_args(src, width, height):
    """Локальный видеовход (S3.3): источник → декодирование формата устройства → RGBx в stdout. Без кодирования
    и без сети: тот же хвост _to_rgbx, что у приёмника потока."""
    return _raw(src) + _to_rgbx(width, height)


def sender_args(codec, port, width=1280, height=720, fps=30, host="127.0.0.1", pattern="smpte", src=None):
    # bind-address: передающий сокет тоже только на loopback (иначе udpsink привязывается к 0.0.0.0:<случайный>, S2-BOOT).
    bind = ["bind-address=127.0.0.1"] if host.startswith("127.") else []
    return _encoded(codec, width, height, fps, pattern, src) + \
        ["!", "udpsink", f"host={host}", f"port={port}", *bind, "sync=false"]


def stream_sender_args(codec, width=1280, height=720, fps=30, pattern="smpte", src=None):
    """RTP в stdout с кадрированием RFC 4571 (2 байта длины + пакет) — для защищённого канала, без сокетов."""
    return _encoded(codec, width, height, fps, pattern, src) + ["!", "rtpstreampay", "!", "fdsink", "fd=1",
                                                                 "sync=false"]


def stream_receiver_args(codec, width, height):
    """RTP из stdin (RFC 4571) → сырые кадры RGBx фиксированного размера в stdout."""
    caps = H264_CAPS if codec == "h264" else JPEG_CAPS
    stream_caps = caps.replace("application/x-rtp,", "application/x-rtp-stream,")
    return [GST_LAUNCH, "-q", "fdsrc", "fd=0", "!", stream_caps, "!", "rtpstreamdepay", "!", caps, "!"] + \
        _depay_decode(codec) + _to_rgbx(width, height)


def _depay_decode(codec):
    if codec == "h264":
        return ["rtpjitterbuffer", "latency=50", "!", "rtph264depay", "!", "h264parse", "!", h264_elements()[1], "!"]
    return ["rtpjitterbuffer", "latency=50", "!", "rtpjpegdepay", "!", "jpegdec", "!"]


def _to_rgbx(width, height):
    """Сначала уменьшение (в формате декодера), потом преобразование цвета: на Pi 4 после v4l2h264dec 1280×720 это
    89 кадров/с против 14 при обратном порядке — videoconvert полного кадра не успевал (S3.1-VIDEO, pi1)."""
    return ["videoscale", "!", f"video/x-raw,width={width},height={height}", "!", "videoconvert", "!",
            f"video/x-raw,format=RGBx,width={width},height={height}", "!", "fdsink", "fd=1", "sync=false"]


def _to_unixfd(socket_path):
    """Кадры декодера без преобразования — дескрипторами dmabuf в unix-сокет (ADR-0015): приёмник в процессе пульта
    (unixfdsrc → glupload → qml6glsink) берёт их без копий; пиксели не проходят через Python."""
    return ["unixfdsink", f"socket-path={socket_path}", "sync=false"]


def gl_capable(codec) -> bool:
    """Путь мимо Python — только H.264 с аппаратным декодером (v4l2h264dec отдаёт dmabuf) и с unixfdsink."""
    if codec != "h264" or not has_element("unixfdsink"):
        return False
    pair = h264_elements()
    return pair is not None and pair[1] == "v4l2h264dec"


def stream_receiver_gl_args(codec, socket_path):
    """RTP из stdin (RFC 4571) → декодер → unixfdsink (ADR-0015)."""
    caps = H264_CAPS if codec == "h264" else JPEG_CAPS
    stream_caps = caps.replace("application/x-rtp,", "application/x-rtp-stream,")
    return [GST_LAUNCH, "-q", "fdsrc", "fd=0", "!", stream_caps, "!", "rtpstreamdepay", "!", caps, "!"] + \
        _depay_decode(codec) + _to_unixfd(socket_path)


def receiver_gl_args(codec, port, socket_path):
    """RTP по UDP на 127.0.0.1 → декодер → unixfdsink (ADR-0015)."""
    caps = H264_CAPS if codec == "h264" else JPEG_CAPS
    return [GST_LAUNCH, "-q", "udpsrc", f"port={port}", "address=127.0.0.1", f"caps={caps}", "!"] + \
        _depay_decode(codec) + _to_unixfd(socket_path)


def receiver_args(codec, port, width, height):
    """Вывод — сырые кадры RGBx фиксированного размера в stdout (fd=1)."""
    caps = H264_CAPS if codec == "h264" else JPEG_CAPS
    return [GST_LAUNCH, "-q", "udpsrc", f"port={port}", "address=127.0.0.1", f"caps={caps}", "!"] + \
        _depay_decode(codec) + _to_rgbx(width, height)
