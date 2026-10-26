# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Конвейеры gst-launch-1.0 для лабораторных источников видео — единственное место, где они собираются.

Возвращают списки аргументов для exec (без shell): gst_parse_launchv сам экранирует пробелы внутри аргумента,
поэтому «text=CVBS PAL» — один аргумент, а адрес источника не проходит через shell.

Источники (src):
  rtsp://…  поток камеры shcam (H.264): rtspsrc → депакетизация → декодер (avdec_h264 | v4l2h264dec)
  test      videotestsrc (таблица)
  shm:PATH  уже декодированные кадры из shmsink (decoder_hub): одно декодирование /cam на всех потребителей

Потребители:
  vivid_cvbs / vivid_hdmi  в выход vivid (shcam-feed, sheregesh-lab-vivid-feed) — tools/lab-video/pi2/bin/lab-vivid
  uvc_stream / uvc_still   в stdout для эмулятора USB/IP (MJPEG multipart или сырой YUYV) — usbip_uvc.py
  decoder_hub              /cam → I420 HUB_W×HUB_H@HUB_FPS → shmsink
"""
import subprocess

HUB_W, HUB_H, HUB_FPS = 1280, 720, 30           # формат shm-кадров = формат потока /cam (не менять без /cam)
HUB_SHM_SIZE = 32 * 1024 * 1024                 # ~20 кадров I420 1280×720
MJPEG_BOUNDARY = "shcamframe"


def h264_decoder():
    """v4l2h264dec, если есть (Pi 4), иначе avdec_h264 (Pi 5 — аппаратного декодера H.264 нет; x86)."""
    found = subprocess.run(["gst-inspect-1.0", "v4l2h264dec"], capture_output=True).returncode == 0
    return "v4l2h264dec" if found else "avdec_h264"


def framerate(fps):
    """30 → «30/1», 29.97 → «30000/1001»."""
    r = round(fps)
    return f"{r * 1000}/1001" if r - fps > 0.005 else f"{r}/1"


def src(url, decoder="avdec_h264"):
    if url == "test":
        return ["videotestsrc", "is-live=true", "pattern=smpte"]
    if url.startswith("shm:"):
        # leaky-очередь сразу за shmsrc: буферы shm освобождаются немедленно, даже если кодирование отстаёт
        # (shmsink в хабе падал на «sp_shmbuf_dec: Assertion had_client», пока потребитель держал буферы)
        return ["shmsrc", f"socket-path={url[4:]}", "is-live=true", "do-timestamp=true", "!",
                f"video/x-raw,format=I420,width={HUB_W},height={HUB_H},framerate={HUB_FPS}/1,pixel-aspect-ratio=1/1",
                "!", "queue", "leaky=downstream", "max-size-buffers=2", "max-size-bytes=0", "max-size-time=0"]
    if url.startswith("rtsp://"):
        return ["rtspsrc", f"location={url}", "protocols=tcp", "latency=0", "!", "rtph264depay", "!", "h264parse",
                "!", decoder]
    raise ValueError(f"источник — rtsp://…, shm:ПУТЬ или test: {url!r}")


def scaled(source, w, h, rate, par="1/1"):
    return [*source, "!", "videoconvert", "!", "videoscale", "add-borders=false", "!", "videorate", "!",
            f"video/x-raw,width={w},height={h},framerate={rate},pixel-aspect-ratio={par}"]


def label(text, size):
    return ["!", "timeoverlay", f"text={text}", "halignment=right", "valignment=bottom", f"font-desc=Sans {size}"]


def launch(*parts):
    return ["gst-launch-1.0", "-q", *[a for p in parts for a in p]]


def vivid_cvbs(url, out_dev, pal, decoder="avdec_h264"):
    """Аналог в выход vivid: чересстрочно по стандарту (PAL 720×576i25, верхнее поле первым, PAR 54/59;
    NTSC 720×480i29.97, нижнее первым, PAR 11/10). capssetter убирает field-order: иначе v4l2sink ставит выходу
    Interlaced-TB/BT, а вход vivid — Interlaced, поля не совпадают и петля отдаёт шум."""
    w, h, fields, rate, par, tff, text = ((720, 576, "50/1", "25/1", "54/59", "true", "CVBS PAL") if pal else
                                          (720, 480, "60000/1001", "30000/1001", "11/10", "false", "CVBS NTSC"))
    return launch(scaled(src(url, decoder), w, h, fields, par), label(text, 20),
                  ["!", "videoconvert", "!", "video/x-raw,format=YUY2", "!", "interlace", "field-pattern=1:1",
                   f"top-field-first={tff}", "!", "capssetter", "replace=true",
                   f"caps=video/x-raw,format=YUY2,width={w},height={h},framerate={rate},interlace-mode=interleaved,"
                   f"pixel-aspect-ratio={par}", "!", "v4l2sink", f"device={out_dev}", "sync=false"])


def vivid_hdmi(url, out_dev, w, h, fps, decoder="avdec_h264"):
    return launch(scaled(src(url, decoder), w, h, framerate(fps)), label(f"HDMI {w}x{h}", 24),
                  ["!", "videoconvert", "!", "video/x-raw,format=YUY2", "!", "v4l2sink", f"device={out_dev}",
                   "sync=false"])


def _uvc_tail(fmt):
    if fmt == "MJPEG":
        return ["!", "videoconvert", "!", "jpegenc", "quality=80", "!", "multipartmux", f"boundary={MJPEG_BOUNDARY}",
                "!", "fdsink", "fd=1"]
    return ["!", "videoconvert", "!", "video/x-raw,format=YUY2", "!", "fdsink", "fd=1"]


def uvc_stream(url, fmt, w, h, fps, decoder="avdec_h264"):
    return launch(scaled(src(url, decoder), w, h, f"{fps}/1"), label(f"USB {fmt} {w}x{h}", 18), _uvc_tail(fmt))


def uvc_still(pattern, fmt, w, h, fps):
    """Один кадр однотонного поля (blue | black) в формате потока."""
    return launch(["videotestsrc", f"pattern={pattern}", "num-buffers=1", "!",
                   f"video/x-raw,width={w},height={h},framerate={fps}/1,pixel-aspect-ratio=1/1"], _uvc_tail(fmt))


def decoder_hub(url, sock, decoder="avdec_h264"):
    """/cam → I420 HUB_W×HUB_H@HUB_FPS → shmsink sock: декодируется один раз на всех потребителей src('shm:…')."""
    return launch(scaled(src(url, decoder), HUB_W, HUB_H, f"{HUB_FPS}/1"),
                  ["!", "videoconvert", "!", "video/x-raw,format=I420", "!", "shmsink", f"socket-path={sock}",
                   f"shm-size={HUB_SHM_SIZE}", "wait-for-connection=false", "sync=false"])
