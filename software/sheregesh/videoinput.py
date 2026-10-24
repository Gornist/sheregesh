# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Универсальный видеовход (S3.3): один интерфейс VideoSource для синтетического генератора и стандартного
устройства захвата V4L2/UVC (USB-камера, HDMI→USB-адаптер класса UVC).

Только системные интерфейсы Linux, уже имеющиеся в образе: sysfs /sys/class/video4linux и ioctl V4L2 из стандартной
библиотеки Python (VIDIOC_QUERYCAP, VIDIOC_ENUM_FMT, VIDIOC_ENUM_FRAMESIZES, VIDIOC_ENUM_FRAMEINTERVALS). Без udev-правил,
v4l2-ctl и сторонних модулей. Конвейеры GStreamer строит sheregesh.video; здесь — только описание источника.

Устройство не открывается само по себе: обнаружение с ioctl выполняется, только когда конфигурация явно выбрала
источник «uvc» (или оператор запустил диагностику `python3 -m sheregesh.videoinput`). Служебные узлы V4L2 (метаданные
UVC, кодек и ISP bcm2835, unicam и т. п.) источником не считаются.

Классов под изготовителей нет: любое устройство, которое ядро обслуживает драйвером uvcvideo и которое отдаёт
YUYV, MJPEG или H.264, — обычный UVCSource. Устройства с собственным протоколом вывода (не UVC) не поддерживаются.
"""
import errno
import fcntl
import os
import re
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

# ---------- V4L2 (linux/videodev2.h) ----------
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_META_CAPTURE = 0x00800000
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMSIZE_TYPE_STEPWISE = 3
V4L2_FRMIVAL_TYPE_DISCRETE = 1


def _iowr(nr, size, read_only=False):
    return ((2 if read_only else 3) << 30) | (size << 16) | (ord("V") << 8) | nr


CAPABILITY = struct.Struct("16s32s32sIII12x")            # struct v4l2_capability, 104 байта
FMTDESC = struct.Struct("III32sII12x")                    # struct v4l2_fmtdesc, 64 байта
FRMSIZE = struct.Struct("III6I8x")                        # struct v4l2_frmsizeenum, 44 байта
FRMIVAL = struct.Struct("IIIII6I8x")                      # struct v4l2_frmivalenum, 52 байта
VIDIOC_QUERYCAP = _iowr(0, CAPABILITY.size, read_only=True)        # 0x80685600
VIDIOC_ENUM_FMT = _iowr(2, FMTDESC.size)                            # 0xc0405602
VIDIOC_ENUM_FRAMESIZES = _iowr(74, FRMSIZE.size)                    # 0xc02c564a
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, FRMIVAL.size)                # 0xc034564b


def fourcc(code: str) -> int:
    return struct.unpack("<I", code.encode("ascii"))[0]


def fourcc_str(value: int) -> str:
    return struct.pack("<I", value).decode("ascii", errors="replace").rstrip("\0 ")


# Форматы, которые подсистема принимает без программного перекодирования в источнике. Остальные показываются
# в списке форматов устройства, но не выбираются.
FORMATS = {"YUYV": "yuyv", "MJPG": "mjpeg", "H264": "h264"}
FORMAT_ORDER = ("YUYV", "MJPG", "H264")    # «auto»: без декодирования → дешёвое декодирование → H.264
MODES = ("640x480@30", "640x480@60", "1280x720@30", "1280x720@60", "1920x1080@30", "1920x1080@60")
_SIZES = ((640, 480), (1280, 720), (1920, 1080))     # проверяются в устройствах с непрерывным диапазоном размеров
_RATES = (30, 60)


@dataclass(frozen=True, order=True)
class Mode:
    width: int
    height: int
    fps: int
    pixfmt: str = ""      # FourCC: YUYV | MJPG | H264 | …
    rate: str = ""        # точная частота «числитель/знаменатель», если не целая (NTSC 30000/1001); иначе fps/1

    @property
    def framerate(self):
        return self.rate or f"{self.fps}/1"

    def __str__(self):
        return f"{FORMATS.get(self.pixfmt, self.pixfmt).upper()} {self.width}×{self.height}@{self.fps}"


def parse_mode(text: str):
    m = re.fullmatch(r"(\d+)x(\d+)@(\d+)", text or "")
    if not m:
        raise ValueError(f"mode {text!r}: expected WxH@fps")
    return int(m[1]), int(m[2]), int(m[3])


# ---------- ioctl ----------
def querycap(fd, ioctl=fcntl.ioctl):
    """(driver, card, bus_info, device_caps) — возможности именно этого узла (V4L2_CAP_DEVICE_CAPS)."""
    raw = ioctl(fd, VIDIOC_QUERYCAP, bytes(CAPABILITY.size))
    driver, card, bus, _version, caps, device_caps = CAPABILITY.unpack(raw)
    dec = lambda b: b.split(b"\0", 1)[0].decode("utf-8", errors="replace")     # noqa: E731
    return dec(driver), dec(card), dec(bus), device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps


def _enum(ioctl, fd, request, st, *head):
    """Перебор index = 0, 1, … до EINVAL (так перечисляют все VIDIOC_ENUM_*). head — входные поля после index."""
    blank = st.unpack(bytes(st.size))[1 + len(head):]          # остальные поля — нули нужного типа
    out = []
    for index in range(256):
        try:
            raw = ioctl(fd, request, st.pack(index, *head, *blank))
        except OSError as e:
            if e.errno == errno.EINVAL:
                break
            raise
        out.append(st.unpack(raw))
    return out


def enum_modes(fd, ioctl=fcntl.ioctl):
    """Все режимы захвата: (FourCC, ширина, высота, к/с). Непрерывные и ступенчатые диапазоны — только стандартные
    размеры и частоты из задания (640×480, 1280×720, 1920×1080; 30 и 60 к/с)."""
    modes = set()
    for _i, _t, _flags, _desc, pixfmt, *_ in _enum(ioctl, fd, VIDIOC_ENUM_FMT, FMTDESC, V4L2_BUF_TYPE_VIDEO_CAPTURE):
        code = fourcc_str(pixfmt)
        for _i, _pf, kind, *u in _enum(ioctl, fd, VIDIOC_ENUM_FRAMESIZES, FRMSIZE, pixfmt):
            if kind == V4L2_FRMSIZE_TYPE_DISCRETE:
                sizes = [(u[0], u[1])]
            else:                                    # min_w, max_w, step_w, min_h, max_h, step_h
                step = kind == V4L2_FRMSIZE_TYPE_STEPWISE   # ступенчатый: размер = min + k·step
                sizes = [(w, h) for w, h in _SIZES if u[0] <= w <= u[1] and u[3] <= h <= u[4] and
                         not (step and ((u[2] > 1 and (w - u[0]) % u[2]) or (u[5] > 1 and (h - u[3]) % u[5])))]
            for w, h in sizes:
                for _i, _pf, _w, _h, ikind, *iv in _enum(ioctl, fd, VIDIOC_ENUM_FRAMEINTERVALS, FRMIVAL, pixfmt, w, h):
                    if ikind == V4L2_FRMIVAL_TYPE_DISCRETE:
                        if iv[0]:
                            r = Fraction(iv[1], iv[0])     # 1001/30000 с → 30000/1001 к/с: caps — ровно как у устройства
                            modes.add(Mode(w, h, round(r), code, "" if r.denominator == 1 else f"{r.numerator}/{r.denominator}"))
                    else:                            # интервал min..max: частота = 1/интервал
                        lo = iv[0] / iv[1] if iv[1] else 0
                        hi = iv[2] / iv[3] if iv[3] else 0
                        modes.update(Mode(w, h, r, code) for r in _RATES if lo <= 1 / r <= hi)
                    if ikind != V4L2_FRMIVAL_TYPE_DISCRETE:
                        break
            if kind != V4L2_FRMSIZE_TYPE_DISCRETE:
                break
    return sorted(modes, key=lambda m: (m.pixfmt, m.width, m.height, m.fps))


# ---------- обнаружение (sysfs) ----------
@dataclass
class Node:
    """Узел /dev/videoN по данным sysfs (и ioctl, если узел — кандидат в устройства захвата)."""
    name: str                 # video0
    dev: str                  # /dev/video0 или постоянная ссылка /dev/v4l/by-id/…
    card: str                 # имя устройства (sysfs name / VIDIOC_QUERYCAP card)
    driver: str
    usb_id: str = ""          # vvvv:pppp[:serial] — постоянный идентификатор для конфигурации
    capture: bool = False     # устройство захвата V4L2/UVC (а не служебный узел)
    reason: str = ""          # почему не устройство захвата / ошибка опроса
    modes: list = field(default_factory=list)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _by_id(dev_root: Path, node: str) -> str:
    """Постоянная ссылка udev /dev/v4l/by-id/…, если есть (переживает переподключение); иначе /dev/videoN."""
    d = dev_root / "v4l" / "by-id"
    try:
        for link in sorted(d.iterdir()):
            if os.path.basename(os.readlink(link)) == node:
                return str(link)
    except OSError:
        pass
    return str(dev_root / node)


def scan(sys_root="/sys", dev_root="/dev", ioctl=fcntl.ioctl, opener=os.open, probe=True):
    """Все узлы video4linux с классификацией. probe=False — только sysfs, устройства не открываются."""
    sys_root, dev_root = Path(sys_root), Path(dev_root)
    base = sys_root / "class" / "video4linux"
    try:
        names = sorted((p.name for p in base.iterdir() if re.fullmatch(r"video\d+", p.name)),
                       key=lambda n: int(n[5:]))
    except OSError:
        return []
    nodes = []
    for name in names:
        d = base / name
        try:
            driver = os.path.basename(os.readlink(d / "device" / "driver"))
        except OSError:
            driver = ""
        node = Node(name, str(dev_root / name), _read(d / "name"), driver)
        if driver != "uvcvideo":
            node.reason = f"auxiliary node (driver {driver or 'unknown'})"
            nodes.append(node)
            continue
        usb = (d / "device").resolve().parent          # интерфейс USB → устройство USB
        vid, pid, serial = _read(usb / "idVendor"), _read(usb / "idProduct"), _read(usb / "serial")
        node.usb_id = ":".join(x for x in (vid, pid, re.sub(r"[^A-Za-z0-9._-]", "", serial)) if x)
        if _read(d / "index") not in ("", "0"):
            node.reason = "UVC metadata node"
        if probe:
            _probe(node, dev_root, ioctl, opener)
        else:
            node.capture = not node.reason
        nodes.append(node)
    return nodes


def _probe(node, dev_root, ioctl, opener):
    try:
        fd = opener(str(dev_root / node.name), os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        node.reason = node.reason or f"cannot open {dev_root / node.name}: {e.strerror or e}"
        return
    try:
        _driver, card, _bus, caps = querycap(fd, ioctl)
        node.card = card or node.card
        if not caps & V4L2_CAP_VIDEO_CAPTURE:
            node.reason = "UVC metadata node" if caps & V4L2_CAP_META_CAPTURE else \
                "no V4L2_CAP_VIDEO_CAPTURE"
            return
        node.reason = ""
        node.modes = enum_modes(fd, ioctl)
        node.capture = True
        node.dev = _by_id(dev_root, node.name)
    except OSError as e:
        node.reason = f"V4L2 query failed: {e.strerror or e}"
    finally:
        os.close(fd)


def capture_devices(**kw):
    return [n for n in scan(**kw) if n.capture]


# ---------- VideoSource ----------
class VideoSource:
    """Общий интерфейс видеовхода. Конвейер GStreamer собирает sheregesh.video из двух частей источника:
    capture() — элементы до первого «!» включительно (кадры режима mode), decode() — до сырых кадров.
    Состояние выполнения, ошибки и статистику кадров ведёт один общий приёмник (VideoReceiver.metrics)."""
    kind = ""
    id = ""
    name = ""
    available = False
    error = ""
    mode = None              # Mode — текущий режим
    formats = ()             # поддерживаемые режимы (Mode)

    def capture(self):
        raise NotImplementedError

    def decode(self, decoder=None):
        """Декодирование формата режима до сырых кадров: YUYV — ничего, MJPEG — jpegdec, H.264 — h264parse и
        декодер decoder (выбранный пробой кодеков; аппаратный v4l2h264dec на Pi 4)."""
        code = self.mode.pixfmt if self.mode else ""
        return {"MJPG": ["jpegdec", "!"], "H264": ["h264parse", "!", decoder or "avdec_h264", "!"]}.get(code, [])

    def info(self):
        m = self.mode
        return {"id": self.id, "kind": self.kind, "name": self.name, "available": self.available,
                "error": self.error, "format": FORMATS.get(m.pixfmt, m.pixfmt) if m else "",
                "resolution": f"{m.width}x{m.height}" if m else "", "fps": m.fps if m else 0,
                "formats": [str(f) for f in self.formats]}


class SyntheticSource(VideoSource):
    """Встроенный генератор videotestsrc (существующий синтетический режим, без изменений)."""
    kind = "synthetic"
    id = "synthetic"
    name = "SHEREGESH synthetic test pattern"
    available = True

    def __init__(self, width=1280, height=720, fps=30, pattern="smpte", overlays=()):
        self.mode = Mode(width, height, fps, "I420")
        self.formats = (self.mode,)
        self.pattern = pattern
        self.overlays = list(overlays)     # элементы надписей (наличие pango проверяет sheregesh.video)

    def capture(self):
        m = self.mode
        return ["videotestsrc", "is-live=true", f"pattern={self.pattern}", "!",
                f"video/x-raw,width={m.width},height={m.height},framerate={m.fps}/1", "!", *self.overlays]


class UVCSource(VideoSource):
    """Стандартное устройство V4L2/UVC. Формат устройства не перекодируется в источнике (декодирование —
    VideoSource.decode)."""
    kind = "uvc"

    def __init__(self, node: Node, mode: Mode):
        self.node = node
        self.id = f"uvc:{node.usb_id}" if node.usb_id else f"uvc:{node.name}"
        self.name = node.card
        self.mode = mode
        self.formats = tuple(node.modes)
        self.available = True

    def capture(self):
        m = self.mode
        size = f"width={m.width},height={m.height},framerate={m.framerate}"
        caps = {"YUYV": f"video/x-raw,format=YUY2,{size}", "MJPG": f"image/jpeg,{size}",
                "H264": f"video/x-h264,stream-format=byte-stream,{size}"}[m.pixfmt]
        return ["v4l2src", f"device={self.node.dev}", "do-timestamp=true", "!", caps, "!"]


class Unavailable(VideoSource):
    """Источник выбран, но использовать его нельзя (нет устройства, неподдерживаемый режим). Конвейера нет."""

    def __init__(self, kind, error, missing=False, formats=(), name=""):
        self.kind, self.id, self.error, self.missing, self.formats, self.name = kind, kind, error, missing, \
            tuple(formats), name


def select_mode(modes, width, height, fps, fmt="auto"):
    """Точное совпадение размера и частоты; «auto» — в порядке FORMAT_ORDER (без декодирования, затем MJPEG, H.264).
    Без совпадения — None (режим не подменяется молча другим)."""
    order = FORMAT_ORDER if fmt == "auto" else tuple(k for k, v in FORMATS.items() if v == fmt)
    for code in order:
        for m in sorted(modes, key=lambda m: m.rate != ""):     # целая частота устройства — в первую очередь
            if (m.pixfmt, m.width, m.height, m.fps) == (code, width, height, fps):
                return m
    return None


def open_uvc(device="", mode="1280x720@30", fmt="auto", **scan_kw):
    """Источник UVC по конфигурации. device — vvvv:pppp[:serial], имя videoN или «» (единственное устройство)."""
    width, height, fps = parse_mode(mode)
    scanned = scan(**scan_kw)
    match = lambda n: not device or device in (n.usb_id, n.name) or n.usb_id.startswith(device + ":")  # noqa: E731
    nodes = [n for n in scanned if n.capture and match(n)]
    if not nodes:
        # Узел UVC есть, но не опрошен (нет прав, занят, исчез во время опроса) — причина видна оператору.
        why = "; ".join(f"{n.name}: {n.reason}" for n in scanned if n.driver == "uvcvideo" and match(n) and
                        n.reason and "metadata" not in n.reason)
        return Unavailable("uvc", f"UVC device {device + ' ' if device else ''}not found" + (f" ({why})" if why else ""),
                           missing=True)
    if len(nodes) > 1:
        return Unavailable("uvc", "several UVC devices found (" +
                           ", ".join(f"{n.usb_id or n.name} {n.card}" for n in nodes) + "); set video_device")
    node = nodes[0]
    chosen = select_mode(node.modes, width, height, fps, fmt)
    if chosen is None:
        offered = ", ".join(str(m) for m in node.modes if m.pixfmt in FORMATS) or "no YUYV/MJPEG/H.264"
        want = f"{'' if fmt == 'auto' else fmt.upper() + ' '}{width}×{height}@{fps}"
        return Unavailable("uvc", f"mode {want} not supported by {node.card}; offered: {offered}",
                           formats=node.modes, name=node.card)
    return UVCSource(node, chosen)


def main(argv=None):
    """Диагностика стенда: узлы video4linux, классификация и режимы. Устройства только опрашиваются (ioctl),
    захват не запускается."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh.videoinput", description=main.__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="только sysfs, устройства не открывать")
    args = ap.parse_args(argv)
    nodes = scan(probe=not args.no_probe)
    if args.json:
        print(json.dumps([{**n.__dict__, "modes": [str(m) for m in n.modes]} for n in nodes], ensure_ascii=False,
                         indent=1))
        return 0
    for n in nodes:
        kind = "CAPTURE" if n.capture else "other  "
        print(f"{kind} {n.name:8} {n.driver:16} {n.usb_id or '-':24} {n.card} {n.dev if n.capture else ''}"
              f"{' — ' + n.reason if n.reason else ''}")
        for m in n.modes:
            print(f"         {m}{'' if m.pixfmt in FORMATS else ' (not used)'}")
    if not nodes:
        print("no video4linux nodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
