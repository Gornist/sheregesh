# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Универсальный видеовход (S3.3, видеоприём): один интерфейс VideoSource для синтетического генератора, любого
устройства захвата V4L2 (UVC-камера, HDMI→USB-адаптер, аналоговый захват usbtv/stk1160/em28xx) и потока RTSP.

Только системные интерфейсы Linux, уже имеющиеся в образе: sysfs /sys/class/video4linux и ioctl V4L2 из стандартной
библиотеки Python (VIDIOC_QUERYCAP, VIDIOC_ENUM_FMT, VIDIOC_ENUM_FRAMESIZES, VIDIOC_ENUM_FRAMEINTERVALS, VIDIOC_ENUMINPUT,
VIDIOC_G_INPUT/S_INPUT, VIDIOC_ENUMSTD, VIDIOC_G_STD, VIDIOC_QUERYSTD). Без udev-правил, v4l2-ctl и сторонних модулей. Конвейеры
GStreamer строит sheregesh.video; выполнение, состояние (нет устройства / нет сигнала / …) и переподключение на блоке —
sheregesh.videoingest; здесь — только описание источника, обнаружение и проверки присутствия/сигнала.

Устройство не открывается само по себе: обнаружение с ioctl выполняется, только когда конфигурация явно выбрала
источник V4L2 (или оператор запустил диагностику `python3 -m sheregesh.videoinput`). Служебные узлы Pi (кодек и ISP
bcm2835, декодер HEVC) не открываются вовсе; узлы без V4L2_CAP_VIDEO_CAPTURE (метаданные UVC, VBI, M2M) источником
не считаются.

Классов под изготовителей нет: устройство выбирается по постоянному идентификатору (/dev/v4l/by-id, by-path,
VID:PID[:serial], имя карты или bus_info из VIDIOC_QUERYCAP), форматы и режимы — те, что сообщает драйвер.
Единственное изменение состояния устройства отсюда — VIDIOC_S_INPUT, если конфигурация явно выбрала вход
(composite / s-video / номер): v4l2src входы не переключает, а драйверы usbtv/stk1160/em28xx помнят вход между
открытиями узла. Стандарт PAL/NTSC задаёт сам v4l2src (norm=…, VIDIOC_S_STD при открытии).
"""
import errno
import fcntl
import os
import re
import socket
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

# ---------- V4L2 (linux/videodev2.h) ----------
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_VIDEO_M2M_MPLANE = 0x00004000
V4L2_CAP_VIDEO_M2M = 0x00008000
V4L2_CAP_META_CAPTURE = 0x00800000
V4L2_CAP_STREAMING = 0x04000000
V4L2_CAP_DEVICE_CAPS = 0x80000000
V4L2_BUF_TYPE_VIDEO_CAPTURE = 1
V4L2_FRMSIZE_TYPE_DISCRETE = 1
V4L2_FRMSIZE_TYPE_STEPWISE = 3
V4L2_FRMIVAL_TYPE_DISCRETE = 1
# struct v4l2_input: status и capabilities
V4L2_IN_ST_NO_POWER = 0x00000001
V4L2_IN_ST_NO_SIGNAL = 0x00000002
V4L2_IN_ST_NO_H_LOCK = 0x00000100        # аналоговые входы: нет строчной синхронизации (нет сигнала CVBS)
V4L2_IN_ST_NO_SYNC = 0x00010000           # цифровые входы (HDMI): нет синхронизации
NO_SIGNAL_FLAGS = V4L2_IN_ST_NO_POWER | V4L2_IN_ST_NO_SIGNAL | V4L2_IN_ST_NO_H_LOCK | V4L2_IN_ST_NO_SYNC
V4L2_IN_CAP_DV_TIMINGS = 0x00000002       # вход с DV timings — HDMI/DVI (tc358743, adv7604 …)
V4L2_IN_CAP_STD = 0x00000004              # аналоговый вход со стандартом PAL/NTSC/SECAM
# v4l2_std_id
V4L2_STD_PAL = 0x000000FF                 # PAL B/B1/G/H/I/D/D1/K
V4L2_STD_NTSC = 0x0000B000                # NTSC M/M-JP/M-KR
V4L2_STD_525_60 = 0x0000F900              # PAL-M | PAL-60 | NTSC | NTSC-443 — 480 строк, 29,97 к/с
V4L2_STD_625_50 = 0x00FF06FF              # PAL | PAL-N | PAL-Nc | SECAM — 576 строк, 25 к/с


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("V") << 8) | nr


def _iowr(nr, size, read_only=False):
    return _ioc(2 if read_only else 3, nr, size)


CAPABILITY = struct.Struct("16s32s32sIII12x")            # struct v4l2_capability, 104 байта
FMTDESC = struct.Struct("III32sII12x")                    # struct v4l2_fmtdesc, 64 байта
FRMSIZE = struct.Struct("III6I8x")                        # struct v4l2_frmsizeenum, 44 байта
FRMIVAL = struct.Struct("IIIII6I8x")                      # struct v4l2_frmivalenum, 52 байта
# В v4l2_input и v4l2_standard есть __u64 (v4l2_std_id): выравнивание 8 на arm64 и x86_64 → 80 и 72 байта
INPUT = struct.Struct("<I32sIIIQII12x4x")                 # struct v4l2_input, 80 байт
STANDARD = struct.Struct("<I4xQ24sIII16x4x")              # struct v4l2_standard, 72 байта
STD_ID = struct.Struct("<Q")
INT = struct.Struct("<i")
VIDIOC_QUERYCAP = _iowr(0, CAPABILITY.size, read_only=True)        # 0x80685600
VIDIOC_ENUM_FMT = _iowr(2, FMTDESC.size)                            # 0xc0405602
VIDIOC_G_STD = _ioc(2, 23, STD_ID.size)                             # 0x80085617
VIDIOC_ENUMSTD = _iowr(25, STANDARD.size)                           # 0xc0485619
VIDIOC_ENUMINPUT = _iowr(26, INPUT.size)                            # 0xc050561a
VIDIOC_G_INPUT = _ioc(2, 38, INT.size)                              # 0x80045626
VIDIOC_S_INPUT = _iowr(39, INT.size)                                # 0xc0045627
VIDIOC_QUERYSTD = _ioc(2, 63, STD_ID.size)                          # 0x8008563f
VIDIOC_ENUM_FRAMESIZES = _iowr(74, FRMSIZE.size)                    # 0xc02c564a
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, FRMIVAL.size)                # 0xc034564b

# Служебные узлы платформы: не открываются никогда (кодеки, ISP, декодер HEVC Pi 4/5 — M2M или узлы конвейера ISP).
AUX_DRIVERS = frozenset({"bcm2835-codec", "bcm2835-isp", "rpi-hevc-dec", "rpivid", "hevc-dec", "pispbe"})
# Драйверы аналогового захвата (CVBS/S-Video) — для эвристики типа входа, если драйвер не сообщает V4L2_IN_CAP_STD.
ANALOG_DRIVERS = frozenset({"usbtv", "stk1160", "em28xx", "em28xx_v4l", "cx231xx", "saa7134", "bttv", "cx88",
                            "cx23885", "tw686x", "tw5864", "go7007", "hdpvr"})
# HDMI→USB-адаптеры класса UVC, которые представляются безликим «USB Video» (MacroSilicon MS2109/MS2130).
HDMI_USB_IDS = frozenset({"534d:2109", "534d:2130", "345f:2130", "345f:2131"})
# Аналоговые захваты, которые работают как обычный UVC без стандартов (MacroSilicon MS210x «EasyCAP»).
CVBS_USB_IDS = frozenset({"534d:0021"})


def fourcc(code: str) -> int:
    return struct.unpack("<I", code.encode("ascii"))[0]


def fourcc_str(value: int) -> str:
    return struct.pack("<I", value).decode("ascii", errors="replace").rstrip("\0 ")


# Форматы, которые подсистема принимает без программного перекодирования в источнике. Остальные показываются
# в списке форматов устройства, но не выбираются. UYVY — аналоговые захваты (stk1160).
FORMATS = {"YUYV": "yuyv", "UYVY": "uyvy", "MJPG": "mjpeg", "H264": "h264"}
FORMAT_ORDER = ("YUYV", "UYVY", "MJPG", "H264")    # «auto»: без декодирования → дешёвое декодирование → H.264
MODES = ("640x480@30", "640x480@60", "1280x720@30", "1280x720@60", "1920x1080@30", "1920x1080@60")
_SIZES = ((640, 480), (1280, 720), (1920, 1080))     # проверяются в устройствах с непрерывным диапазоном размеров
_RATES = (30, 60)
# Аналоговые стандарты: режим захвата определяется стандартом, а не перечнем размеров.
STANDARDS = {"PAL": (V4L2_STD_625_50, 720, 576, 25, ""), "NTSC": (V4L2_STD_525_60, 720, 480, 30, "30000/1001")}
# «standard: auto», когда VIDIOC_QUERYSTD ответа не дал (usbtv его не реализует; нет сигнала; ответ не сужен до
# одного семейства). VIDIOC_G_STD для этого не годится: он возвращает умолчание драйвера (usbtv, stk1160 — NTSC),
# а не стандарт сигнала. Источник NTSC на usbtv задаётся явно: «standard»: «ntsc».
DEFAULT_STANDARD = "PAL"
# «mode: auto» — 720p30 раньше 1080p30: MJPEG HDMI-захвата (MS2109 даёт YUYV ≥ 720p только 5–10 к/с) на блоке
# декодируется программно (jpegdec, одно ядро Cortex-A72), а 1080p30 — 2,25× пикселей 720p30 и на пределе одного
# ядра; канал 2 Мбит/с и экран пульта 1280×800 выигрыша от 1080p не показывают. 1080p30 — если 720p30 у устройства
# нет или он задан явно (docs/hardware/VIDEO_CAPTURE_COMPAT.md §0.3).
_AUTO_PREFERENCE = ((1280, 720, 30), (1920, 1080, 30), (1280, 720, 60), (1920, 1080, 60), (1280, 720, 25),
                    (1920, 1080, 25), (720, 576, 25), (720, 480, 30), (640, 480, 30))


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

    @property
    def nominal_fps(self):
        if not self.rate:
            return float(self.fps)
        num, den = self.rate.split("/")
        return round(int(num) / int(den), 3)

    def __str__(self):
        size = f"{self.width}×{self.height}@{self.fps}" if self.width else "(size from stream)"
        return f"{FORMATS.get(self.pixfmt, self.pixfmt).upper()} {size}"


def parse_mode(text: str):
    """«WxH@fps» → (w, h, fps); дробная частота (29.97) округляется, как Mode.fps режима NTSC 30000/1001."""
    m = re.fullmatch(r"(\d+)x(\d+)@(\d+(?:\.\d+)?)", text or "")
    if not m:
        raise ValueError(f"mode {text!r}: expected WxH@fps")
    return int(m[1]), int(m[2]), round(float(m[3]))


# ---------- ioctl ----------
def _dec(b):
    return b.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def querycap(fd, ioctl=fcntl.ioctl):
    """(driver, card, bus_info, device_caps) — возможности именно этого узла (V4L2_CAP_DEVICE_CAPS)."""
    raw = ioctl(fd, VIDIOC_QUERYCAP, bytes(CAPABILITY.size))
    driver, card, bus, _version, caps, device_caps = CAPABILITY.unpack(raw)
    return _dec(driver), _dec(card), _dec(bus), device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps


def _enum(ioctl, fd, request, st, *head, optional=False):
    """Перебор index = 0, 1, … до EINVAL (так перечисляют все VIDIOC_ENUM_*). head — входные поля после index.
    optional — драйвер может не реализовывать запрос (ENOTTY): пустой перечень, а не ошибка узла."""
    blank = st.unpack(bytes(st.size))[1 + len(head):]          # остальные поля — нули нужного типа
    out = []
    for index in range(256):
        try:
            raw = ioctl(fd, request, st.pack(index, *head, *blank))
        except OSError as e:
            if e.errno == errno.EINVAL or (optional and e.errno == errno.ENOTTY):
                break
            raise
        out.append(st.unpack(raw))
    return out


def enum_formats(fd, ioctl=fcntl.ioctl):
    return [fourcc_str(pf) for _i, _t, _f, _d, pf, *_ in _enum(ioctl, fd, VIDIOC_ENUM_FMT, FMTDESC,
                                                               V4L2_BUF_TYPE_VIDEO_CAPTURE)]


def enum_modes(fd, ioctl=fcntl.ioctl):
    """Все режимы захвата: (FourCC, ширина, высота, к/с). Дискретные размеры и интервалы — ровно как у драйвера
    (HDMI-захваты: фактические режимы адаптера). Непрерывные и ступенчатые диапазоны — только стандартные размеры
    и частоты (640×480, 1280×720, 1920×1080; 30 и 60 к/с). Драйверы без VIDIOC_ENUM_FRAMESIZES (аналоговые usbtv,
    stk1160) режимов не дают: их режим задаёт стандарт (std_modes)."""
    modes = set()
    for _i, _t, _flags, _desc, pixfmt, *_ in _enum(ioctl, fd, VIDIOC_ENUM_FMT, FMTDESC, V4L2_BUF_TYPE_VIDEO_CAPTURE):
        code = fourcc_str(pixfmt)
        for _i, _pf, kind, *u in _enum(ioctl, fd, VIDIOC_ENUM_FRAMESIZES, FRMSIZE, pixfmt, optional=True):
            if kind == V4L2_FRMSIZE_TYPE_DISCRETE:
                sizes = [(u[0], u[1])]
            else:                                    # min_w, max_w, step_w, min_h, max_h, step_h
                step = kind == V4L2_FRMSIZE_TYPE_STEPWISE   # ступенчатый: размер = min + k·step
                sizes = [(w, h) for w, h in _SIZES if u[0] <= w <= u[1] and u[3] <= h <= u[4] and
                         not (step and ((u[2] > 1 and (w - u[0]) % u[2]) or (u[5] > 1 and (h - u[3]) % u[5])))]
            for w, h in sizes:
                for _i, _pf, _w, _h, ikind, *iv in _enum(ioctl, fd, VIDIOC_ENUM_FRAMEINTERVALS, FRMIVAL, pixfmt, w, h,
                                                         optional=True):
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


def enum_inputs(fd, ioctl=fcntl.ioctl):
    """Входы узла (VIDIOC_ENUMINPUT): [{index, name, type, std, status, capabilities}]; без поддержки — []."""
    return [{"index": i, "name": _dec(name), "type": typ, "std": std, "status": status, "capabilities": cap}
            for i, name, typ, _audio, _tuner, std, status, cap in _enum(ioctl, fd, VIDIOC_ENUMINPUT, INPUT, optional=True)]


def current_input(fd, ioctl=fcntl.ioctl):
    try:
        return INT.unpack(ioctl(fd, VIDIOC_G_INPUT, bytes(INT.size)))[0]
    except OSError as e:
        if e.errno in (errno.ENOTTY, errno.EINVAL):
            return 0
        raise


def enum_standards(fd, ioctl=fcntl.ioctl):
    """Поддерживаемые стандарты (VIDIOC_ENUMSTD): [(id, имя, кадр/с)]; цифровые входы — [] (ENODATA/ENOTTY)."""
    try:
        rows = _enum(ioctl, fd, VIDIOC_ENUMSTD, STANDARD, optional=True)
    except OSError as e:
        if e.errno == errno.ENODATA:
            return []
        raise
    return [(sid, _dec(name), round(den / num, 2) if num else 0) for _i, sid, name, num, den, _lines in rows]


def _std_ioctl(fd, request, ioctl):
    try:
        return STD_ID.unpack(ioctl(fd, request, bytes(STD_ID.size)))[0]
    except OSError:
        return None


def std_family(std_id):
    """v4l2_std_id → «PAL» (625 строк/50 Гц, вкл. SECAM) или «NTSC» (525/60); 0/None → «»."""
    if not std_id:
        return ""
    if std_id & V4L2_STD_525_60 and not std_id & V4L2_STD_625_50:
        return "NTSC"
    if std_id & V4L2_STD_625_50:
        return "PAL"
    return ""


def detected_family(std_id):
    """Ответ VIDIOC_QUERYSTD → семейство, только если он сужен до одного (525/60 или 625/50). Ядро заполняет ответ
    всеми стандартами устройства и даёт драйверу сузить; несуженный (обе группы) — сигнал не распознан, «»."""
    if not std_id or (std_id & V4L2_STD_525_60 and std_id & V4L2_STD_625_50):
        return ""
    return std_family(std_id)


def set_input(fd, index, ioctl=fcntl.ioctl):
    """VIDIOC_S_INPUT: вход узла (composite / s-video у аналоговых захватов). Ошибка — OSError."""
    ioctl(fd, VIDIOC_S_INPUT, INT.pack(index))


_INPUT_ALIASES = {"cvbs": "composite", "av": "composite", "sv": "svideo", "yc": "svideo"}


def input_index(inputs, want):
    """Номер входа по конфигурации: число («0», «1») или имя без учёта регистра и знаков («composite», «s-video»,
    «svideo», «cvbs»); имя сравнивается целиком, затем как начало имени входа (usbtv «Composite», stk1160
    «Composite0»…«Composite3» → первый). None — такого входа нет."""
    text = str(want).strip()
    if re.fullmatch(r"\d+", text):
        return int(text) if any(i["index"] == int(text) for i in inputs) else None
    key = re.sub(r"[^a-z0-9]", "", text.lower())
    key = _INPUT_ALIASES.get(key, key)
    names = [(i["index"], re.sub(r"[^a-z0-9]", "", i["name"].lower())) for i in inputs]
    for match in (lambda n: n == key, lambda n: n.startswith(key)):
        found = [idx for idx, n in names if key and match(n)]
        if found:
            return found[0]
    return None


# ---------- обнаружение (sysfs) ----------
@dataclass
class Node:
    """Узел /dev/videoN по данным sysfs (и ioctl, если узел — кандидат в устройства захвата)."""
    name: str                 # video0
    dev: str                  # постоянная ссылка /dev/v4l/by-id/… (или by-path/…), иначе /dev/videoN
    card: str                 # имя устройства (sysfs name / VIDIOC_QUERYCAP card)
    driver: str
    usb_id: str = ""          # vvvv:pppp[:serial] — постоянный идентификатор для конфигурации
    capture: bool = False     # устройство захвата V4L2 (а не служебный узел)
    reason: str = ""          # почему не устройство захвата / ошибка опроса
    modes: list = field(default_factory=list)
    bus_info: str = ""        # VIDIOC_QUERYCAP bus_info: usb-0000:01:00.0-1.2 и т. п.
    by_id: str = ""           # /dev/v4l/by-id/… (udev), если есть
    by_path: str = ""         # /dev/v4l/by-path/… (udev, по порту), если есть
    formats: list = field(default_factory=list)      # все FourCC узла, включая неиспользуемые
    inputs: list = field(default_factory=list)       # enum_inputs
    input: int = 0                                   # текущий вход (VIDIOC_G_INPUT)
    standards: list = field(default_factory=list)    # enum_standards: [(id, имя, к/с)]
    std: int = 0                                     # текущий стандарт (VIDIOC_G_STD), 0 — нет

    def _current(self):
        return next((i for i in self.inputs if i["index"] == self.input), None)

    @property
    def analog(self):
        """Аналоговый вход: драйвер перечисляет стандарты или вход объявляет V4L2_IN_CAP_STD."""
        cur = self._current()
        return bool(self.standards) or bool(cur and cur["capabilities"] & V4L2_IN_CAP_STD)

    @property
    def input_type(self):
        """Тип входа для оператора — честная эвристика: HDMI | CVBS | USB | V4L2 (неизвестно)."""
        cur = self._current()
        caps = cur["capabilities"] if cur else 0
        text = f"{self.card} {cur['name'] if cur else ''}".lower()
        if caps & V4L2_IN_CAP_DV_TIMINGS or "hdmi" in text or self.usb_id[:9] in HDMI_USB_IDS:
            return "HDMI"
        if self.analog or self.driver in ANALOG_DRIVERS or self.usb_id[:9] in CVBS_USB_IDS or \
                any(k in text for k in ("composite", "cvbs", "s-video", "av to usb")):
            return "CVBS"
        if self.driver == "uvcvideo":
            return "USB"              # UVC-камера или неопознанный UVC-адаптер
        return "V4L2"

    def selectors(self):
        """Все значения, по которым устройство можно выбрать в конфигурации."""
        out = {self.name, f"/dev/{self.name}", self.card, self.bus_info}
        for link in (self.by_id, self.by_path):
            if link:
                out |= {link, os.path.basename(link)}
        if self.usb_id:
            out |= {self.usb_id, self.usb_id[:9]}         # vvvv:pppp[:serial] и vvvv:pppp
        return {x for x in out if x}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _link_to(dev_root: Path, kind: str, node: str) -> str:
    """Постоянная ссылка udev /dev/v4l/<kind>/…, указывающая на node, или «»."""
    d = dev_root / "v4l" / kind
    try:
        for link in sorted(d.iterdir()):
            if os.path.basename(os.readlink(link)) == node:
                return str(link)
    except OSError:
        pass
    return ""


def _by_id(dev_root: Path, node: str) -> str:
    """Постоянная ссылка udev /dev/v4l/by-id/…, если есть (переживает переподключение); иначе /dev/videoN."""
    return _link_to(dev_root, "by-id", node) or str(dev_root / node)


def _usb_id(dev_dir: Path) -> str:
    """vvvv:pppp[:serial] USB-устройства, к которому относится узел (интерфейс → устройство); не USB — «»."""
    try:
        d = dev_dir.resolve()
    except OSError:
        return ""
    for cand in (d, d.parent, d.parent.parent):
        vid, pid = _read(cand / "idVendor"), _read(cand / "idProduct")
        if vid and pid:
            serial = re.sub(r"[^A-Za-z0-9._-]", "", _read(cand / "serial"))
            return ":".join(x for x in (vid, pid, serial) if x)
    return ""


def scan(sys_root="/sys", dev_root="/dev", ioctl=fcntl.ioctl, opener=os.open, probe=True, drivers=None):
    """Все узлы video4linux с классификацией. probe=False — только sysfs, устройства не открываются.
    drivers — открывать только узлы этих драйверов (пульт S3.3: {"uvcvideo"}); None — любые, кроме служебных."""
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
        if not driver or driver in AUX_DRIVERS:
            node.reason = f"auxiliary node (driver {driver or 'unknown'})"
            nodes.append(node)
            continue
        if drivers is not None and driver not in drivers:
            node.reason = f"driver {driver} not selected"
            nodes.append(node)
            continue
        node.usb_id = _usb_id(d / "device")
        node.by_id, node.by_path = _link_to(dev_root, "by-id", name), _link_to(dev_root, "by-path", name)
        if driver == "uvcvideo" and _read(d / "index") not in ("", "0"):
            node.reason = "UVC metadata node"
        if probe:
            _probe(node, dev_root, ioctl, opener)
        else:
            node.capture = not node.reason and _read(d / "index") in ("", "0")
        nodes.append(node)
    return nodes


def _probe(node, dev_root, ioctl, opener):
    try:
        fd = opener(str(dev_root / node.name), os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as e:
        node.reason = node.reason or f"cannot open {dev_root / node.name}: {e.strerror or e}"
        return
    try:
        _driver, card, bus, caps = querycap(fd, ioctl)
        node.card, node.bus_info = card or node.card, bus
        m2m = caps & (V4L2_CAP_VIDEO_M2M | V4L2_CAP_VIDEO_M2M_MPLANE)
        if not caps & V4L2_CAP_VIDEO_CAPTURE or m2m:
            node.reason = "UVC metadata node" if caps & V4L2_CAP_META_CAPTURE else \
                "memory-to-memory node" if m2m else "no V4L2_CAP_VIDEO_CAPTURE"
            return
        node.reason = ""
        node.modes = enum_modes(fd, ioctl)
        node.formats = enum_formats(fd, ioctl)
        node.inputs = enum_inputs(fd, ioctl)
        node.input = current_input(fd, ioctl) if node.inputs else 0
        node.standards = enum_standards(fd, ioctl)
        node.std = (_std_ioctl(fd, VIDIOC_G_STD, ioctl) or 0) if node.standards else 0
        node.capture = True
        node.dev = node.by_id or node.by_path or str(dev_root / node.name)
    except OSError as e:
        node.reason = f"V4L2 query failed: {e.strerror or e}"
    finally:
        os.close(fd)


def stable_id(node):
    """Постоянная часть идентификатора устройства: VID:PID[:serial], иначе имя ссылки by-path (порт), bus_info,
    в крайнем случае videoN. Одна и та же для V4L2Source.id и списка источников (sheregesh.videosources)."""
    return node.usb_id or (os.path.basename(node.by_path) if node.by_path else "") or node.bus_info or node.name


def capture_devices(**kw):
    return [n for n in scan(**kw) if n.capture]


def std_modes(node, standard):
    """Режимы аналогового стандарта: 720×576@25 (PAL) или 720×480@29,97 (NTSC) в каждом используемом формате узла
    (если драйвер сам не перечислил такой размер)."""
    _mask, w, h, fps, rate = STANDARDS[standard]
    listed = [m for m in node.modes if (m.width, m.height) == (w, h)]
    if listed:
        return listed
    return [Mode(w, h, fps, f, rate) for f in node.formats if f in FORMATS and f != "H264"]


def _open_dev(opener, dev):
    return opener(dev, os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))


def probe_signal(dev, input_index=0, ioctl=fcntl.ioctl, opener=os.open):
    """Состояние входа без захвата: «no-device» (узла нет / ENODEV), «no-signal» (драйвер сообщает
    V4L2_IN_ST_NO_SIGNAL / NO_POWER / NO_H_LOCK / NO_SYNC через VIDIOC_ENUMINPUT) или None (сигнал есть или драйвер не сообщает —
    тогда решает поток кадров). Второй дескриптор допустим и во время захвата: ENUMINPUT владения не требует."""
    try:
        fd = _open_dev(opener, dev)
    except OSError as e:
        return "no-device" if e.errno in (errno.ENOENT, errno.ENODEV, errno.ENXIO) else None
    try:
        raw = ioctl(fd, VIDIOC_ENUMINPUT, INPUT.pack(input_index, b"", 0, 0, 0, 0, 0, 0))
        return "no-signal" if INPUT.unpack(raw)[6] & NO_SIGNAL_FLAGS else None
    except OSError as e:
        return "no-device" if e.errno in (errno.ENODEV, errno.ENXIO) else None
    finally:
        os.close(fd)


def query_std(dev, ioctl=fcntl.ioctl, opener=os.open):
    """Стандарт сигнала на входе (VIDIOC_QUERYSTD) — для «standard: auto». «» — нет сигнала, не поддерживается
    (usbtv: ENOTTY) или ответ не сужен до одного семейства."""
    try:
        fd = _open_dev(opener, dev)
    except OSError:
        return ""
    try:
        return detected_family(_std_ioctl(fd, VIDIOC_QUERYSTD, ioctl))
    finally:
        os.close(fd)


# ---------- VideoSource ----------
class VideoSource:
    """Общий интерфейс видеовхода.

    Описание (здесь): kind, id (постоянный), name, input_type, formats (режимы), mode (текущий), available/error.
    Конвейер GStreamer собирает sheregesh.video из двух частей источника: capture() — элементы до первого «!»
    включительно (кадры режима mode), decode() — до сырых кадров. Проверки без захвата: present() — источник есть
    (узел устройства, достижимый сервер RTSP), signal() — «no-device» | «no-signal» | None (не известно).
    Выполнение, состояние (no-device / no-signal / starting / streaming / error), фактическая частота и переподключение
    ведёт один исполнитель на сторону: пульт — VideoReceiver (локальный вход), блок — sheregesh.videoingest.Ingest."""
    kind = ""
    id = ""
    name = ""
    input_type = "V4L2"
    available = False
    missing = False
    error = ""
    mode = None              # Mode — текущий режим
    formats = ()             # поддерживаемые режимы (Mode)
    standard = ""            # аналоговый стандарт PAL | NTSC, если выбран
    device = ""              # постоянный путь устройства / адрес потока без учётных данных

    def capture(self):
        raise NotImplementedError

    def decode(self, decoder=None):
        """Декодирование формата режима до сырых кадров: YUYV/UYVY — ничего, MJPEG — jpegdec, H.264 — h264parse и
        декодер decoder (выбранный пробой кодеков; аппаратный v4l2h264dec на Pi 4)."""
        code = self.mode.pixfmt if self.mode else ""
        return {"MJPG": ["jpegdec", "!"], "H264": ["h264parse", "!", decoder or "avdec_h264", "!"]}.get(code, [])

    def present(self):
        """(True, «») или (False, причина) — без захвата и без длительной блокировки."""
        return (self.available, self.error)

    def signal(self):
        return None

    def info(self):
        m = self.mode
        return {"id": self.id, "kind": self.kind, "name": self.name, "available": self.available,
                "error": self.error, "format": FORMATS.get(m.pixfmt, m.pixfmt.lower()) if m else "",
                "resolution": f"{m.width}x{m.height}" if m and m.width else "", "fps": m.fps if m else 0,
                "formats": [str(f) for f in self.formats], "input_type": self.input_type,
                "standard": self.standard, "device": self.device}


class SyntheticSource(VideoSource):
    """Встроенный генератор videotestsrc (существующий синтетический режим, без изменений)."""
    kind = "synthetic"
    id = "synthetic"
    name = "SHEREGESH synthetic test pattern"
    input_type = "Synthetic"
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


class V4L2Source(VideoSource):
    """Любое устройство захвата V4L2 (uvcvideo, usbtv, stk1160, em28xx, …). Формат устройства не перекодируется
    в источнике (декодирование — VideoSource.decode). Аналоговый стандарт задаёт v4l2src norm=… (VIDIOC_S_STD).

    standard_source: «config» (задан явно), «detected» (VIDIOC_QUERYSTD), «default» (auto без ответа QUERYSTD —
    DEFAULT_STANDARD). При «auto» стандарт перепроверяется перед каждым запуском конвейера (capture): блок часто
    включается раньше камеры, и первый опрос идёт без сигнала. want — запрошенный режим ("auto" | (w, h, fps)) и формат."""
    kind = "v4l2"

    def __init__(self, node: Node, mode: Mode, standard="", ioctl=fcntl.ioctl, opener=os.open,
                 standard_source="", want=("auto", "auto")):
        self.node = node
        self.id = f"{self.kind}:{stable_id(node)}"
        self.name = node.card
        self.mode = mode
        modes = list(node.modes)
        if node.analog:
            modes += [m for s in STANDARDS for m in std_modes(node, s) if m not in modes]
        self.formats = tuple(modes)
        self.available = True
        self.standard = standard
        self.standard_source = standard_source or ("config" if standard else "")
        self.device = node.dev
        self.input_type = node.input_type
        self._ioctl, self._opener = ioctl, opener
        self._want = want

    def _redetect_standard(self):
        """«standard: auto»: QUERYSTD перед запуском; другой стандарт — другой режим (тот же запрос режима)."""
        if self.standard_source not in ("detected", "default"):
            return
        fam = query_std(self.node.dev, self._ioctl, self._opener)
        if not fam or fam == self.standard:
            if fam:
                self.standard_source = "detected"
            return
        mode, fmt = self._want
        modes = std_modes(self.node, fam)
        m = auto_mode(modes, fmt) if mode == "auto" else select_mode(modes, *mode, fmt)
        if m is not None:
            self.standard, self.mode, self.standard_source = fam, m, "detected"

    def capture(self):
        self._redetect_standard()
        m = self.mode
        size = f"width={m.width},height={m.height},framerate={m.framerate}"
        caps = {"YUYV": f"video/x-raw,format=YUY2,{size}", "UYVY": f"video/x-raw,format=UYVY,{size}",
                "MJPG": f"image/jpeg,{size}", "H264": f"video/x-h264,stream-format=byte-stream,{size}"}[m.pixfmt]
        norm = [f"norm={self.standard}"] if self.standard else []
        return ["v4l2src", f"device={self.node.dev}", *norm, "do-timestamp=true", "!", caps, "!"]

    def decode(self, decoder=None):
        """Аналоговые захваты (usbtv, stk1160, em28xx) отдают чересстрочные кадры (V4L2_FIELD_INTERLACED →
        interlace-mode=interleaved), а аппаратный кодер Pi 4 принимает только построчные (bcm2835-codec приводит поле
        к V4L2_FIELD_NONE, v4l2h264enc отказывает: «does not support interleaved interlacing»). deinterlace mode=auto
        построчный поток пропускает без обработки; fields=top сохраняет частоту стандарта (25 / 29,97 к/с)."""
        if not self.node.analog:
            return super().decode(decoder)
        return ["deinterlace", "mode=auto", "method=linear", "fields=top", "!", *super().decode(decoder)]

    def present(self):
        if os.path.exists(self.node.dev):
            return True, ""
        return False, f"device {self.node.dev} disappeared"

    def signal(self):
        if not os.path.exists(self.node.dev):
            return "no-device"
        return probe_signal(self.node.dev, self.node.input, self._ioctl, self._opener)

    def info(self):
        cur = self.node._current()
        return {**super().info(), "bus_info": self.node.bus_info, "driver": self.node.driver,
                "usb_id": self.node.usb_id, "input_name": cur["name"] if cur else "",
                "standard_source": self.standard_source}


class UVCSource(V4L2Source):
    """Устройство драйвера uvcvideo (S3.3): прежние kind/id «uvc» — их видит пульт (локальный вход, настройки)."""
    kind = "uvc"


def redact(url):
    """rtsp://user:pass@host/… → rtsp://host/…"""
    return re.sub(r"(?<=://)[^/@]*@", "", url or "")


RTSP_TRANSPORTS = ("auto", "tcp", "udp")
# transport описания → rtspsrc protocols. auto: SETUP сначала по UDP; 461 Unsupported Transport — сразу TCP; UDP
# принят, но датаграмм нет timeout_s (фаервол, NAT) — rtspsrc сам переподключается по TCP (его timeout = «retry TCP
# after UDP timeout»). udp без данных timeout_s — ошибка конвейера («No other protocols to try»).
RTSP_PROTOCOLS = {"auto": "udp+tcp", "tcp": "tcp", "udp": "udp"}


class RTSPSource(VideoSource):
    """Поток RTSP (IP-камера, кодер): rtspsrc, транспорт RTP/UDP или RTP/TCP (interleaved) — transport auto | tcp |
    udp, депакетизация без декодирования. Формат — из конфигурации (h264 | mjpeg); разрешение и частота — фактические,
    из потока (SPS / заголовок JPEG и маркеры RTP на блоке). Учётные данные из адреса не попадают ни в id, ни в info,
    ни в журнал.

    transport — фактический транспорт, если он однозначен: при tcp/udp — он и есть (другого rtspsrc не пробует), при
    auto — «» (gst-launch -q не сообщает, на чём rtspsrc договорился; ограничение — S3.3 §13). rtspt:// — только
    TCP (схема rtspsrc), поэтому auto там означает tcp, а udp — ошибка описания."""
    kind = "rtsp"
    input_type = "RTSP"
    available = True

    def __init__(self, url, codec="h264", latency_ms=200, timeout_s=5.0, connect=socket.create_connection,
                 transport="auto"):
        parts = urlsplit(url)
        if parts.scheme not in ("rtsp", "rtsps", "rtspt") or not parts.hostname:
            raise ValueError(f"RTSP address {redact(url)!r}: expected rtsp://host[:port]/path")
        if codec not in ("h264", "mjpeg"):
            raise ValueError(f"RTSP codec {codec!r}: expected h264 or mjpeg")
        if transport not in RTSP_TRANSPORTS:
            raise ValueError(f"RTSP transport {transport!r}: expected auto, tcp or udp")
        if parts.scheme == "rtspt":
            if transport == "udp":
                raise ValueError("RTSP transport 'udp': rtspt:// is TCP only")
            transport = "tcp"
        self.transport_mode = transport
        self.transport = "" if transport == "auto" else transport
        self.url, self.codec = url, codec
        self.host, self.port = parts.hostname, parts.port or (322 if parts.scheme == "rtsps" else 554)
        self.device = redact(url)
        self.id = f"rtsp:{self.device.split('://', 1)[1]}"
        self.name = f"RTSP {self.host}:{self.port}"
        self.mode = Mode(0, 0, 0, "H264" if codec == "h264" else "MJPG")
        self.formats = (self.mode,)
        self.latency_ms, self.timeout_s = int(latency_ms), float(timeout_s)
        self._connect = connect

    def capture(self):
        us = int(self.timeout_s * 1_000_000)
        depay = "rtph264depay" if self.codec == "h264" else "rtpjpegdepay"
        return ["rtspsrc", f"location={self.url}", f"protocols={RTSP_PROTOCOLS[self.transport_mode]}",
                f"latency={self.latency_ms}", f"tcp-timeout={us}", f"timeout={us}", "!", depay, "!"]

    def info(self):
        return {**super().info(), "transport": self.transport, "transport_mode": self.transport_mode}

    def present(self):
        """Сервер принимает TCP-соединения (без RTSP-сеанса: дёшево, не занимает камеру)."""
        try:
            self._connect((self.host, self.port), timeout=min(self.timeout_s, 2.0)).close()
            return True, ""
        except OSError as e:
            return False, f"RTSP server {self.host}:{self.port} unreachable: {e.strerror or e}"


class Unavailable(VideoSource):
    """Источник выбран, но использовать его нельзя (нет устройства, неподдерживаемый режим). Конвейера нет."""

    def __init__(self, kind, error, missing=False, formats=(), name=""):
        self.kind, self.id, self.error, self.missing, self.formats, self.name = kind, kind, error, missing, \
            tuple(formats), name
        self.input_type = {"rtsp": "RTSP", "synthetic": "Synthetic", "uvc": "USB"}.get(kind, "V4L2")


def select_mode(modes, width, height, fps, fmt="auto"):
    """Точное совпадение размера и частоты; «auto» — в порядке FORMAT_ORDER (без декодирования, затем MJPEG, H.264).
    Без совпадения — None (режим не подменяется молча другим)."""
    order = FORMAT_ORDER if fmt == "auto" else tuple(k for k, v in FORMATS.items() if v == fmt)
    for code in order:
        for m in sorted(modes, key=lambda m: m.rate != ""):     # целая частота устройства — в первую очередь
            if (m.pixfmt, m.width, m.height, m.fps) == (code, width, height, fps):
                return m
    return None


def auto_mode(modes, fmt="auto"):
    """«mode: auto»: первый из типичных режимов (_AUTO_PREFERENCE), иначе наибольший размер при ≥ 25 к/с, иначе
    любой. Только используемые форматы."""
    usable = [m for m in modes if m.pixfmt in FORMATS and (fmt == "auto" or FORMATS[m.pixfmt] == fmt)]
    for w, h, fps in _AUTO_PREFERENCE:
        m = select_mode(usable, w, h, fps, fmt)
        if m is not None:
            return m
    good = [m for m in usable if m.fps >= 25] or usable
    if not good:
        return None
    rank = {c: i for i, c in enumerate(FORMAT_ORDER)}
    return sorted(good, key=lambda m: (-m.width * m.height, -m.fps, rank.get(m.pixfmt, 9)))[0]


def open_v4l2(device="", mode="auto", fmt="auto", standard="auto", drivers=None, label="V4L2", input="", **scan_kw):
    """Источник V4L2 по конфигурации.

    device — постоянный идентификатор: /dev/v4l/by-id/… или by-path/… (полный путь или имя ссылки), vvvv:pppp[:serial],
    имя карты или bus_info (VIDIOC_QUERYCAP), в крайнем случае videoN; «» — единственное устройство захвата.
    mode — «WxH@fps» (точное совпадение) или «auto». standard — для аналоговых входов: pal | ntsc | auto
    (VIDIOC_QUERYSTD, если драйвер его поддерживает и сигнал распознан, иначе DEFAULT_STANDARD — PAL; текущий
    VIDIOC_G_STD не используется: это умолчание драйвера, а не стандарт сигнала). input — вход: «» | «auto» — не
    менять (у usbtv/stk1160/em28xx после загрузки — 0, композитный), номер или имя («composite», «s-video»);
    выбирается VIDIOC_S_INPUT до опроса стандарта. drivers — ограничить драйверами (open_uvc: uvcvideo)."""
    scanned = scan(drivers=drivers, **scan_kw)

    def match(n):
        return not device or device in n.selectors() or bool(n.usb_id and n.usb_id.startswith(device + ":"))
    nodes = [n for n in scanned if n.capture and match(n)]
    kind = label.lower()
    if not nodes:
        # Узел есть, но не опрошен (нет прав, занят, исчез во время опроса) — причина видна оператору.
        why = "; ".join(f"{n.name}: {n.reason}" for n in scanned if match(n) and n.reason.startswith(("cannot", "V4L2")))
        return Unavailable(kind, f"{label} device {device + ' ' if device else ''}not found" +
                           (f" ({why})" if why else ""), missing=True, name=device)
    if len(nodes) > 1:
        return Unavailable(kind, f"several {label} devices found (" +
                           ", ".join(f"{n.usb_id or n.by_path or n.name} {n.card}" for n in nodes) + "); set video_device")
    node = nodes[0]
    ioctl, opener = scan_kw.get("ioctl", fcntl.ioctl), scan_kw.get("opener", os.open)
    if str(input or "auto").strip().lower() != "auto":
        index = input_index(node.inputs, input)
        if index is None:
            offered = ", ".join(f"{i['index']} {i['name']}" for i in node.inputs) or "none (VIDIOC_ENUMINPUT)"
            return Unavailable(kind, f"input {input!r} not found on {node.card}; inputs: {offered}",
                               formats=node.modes, name=node.card)
        if index != node.input:
            try:
                fd = _open_dev(opener, node.dev)
                try:
                    set_input(fd, index, ioctl)
                finally:
                    os.close(fd)
            except OSError as e:
                return Unavailable(kind, f"input {input!r} of {node.card} cannot be selected: {e.strerror or e}",
                                   missing=e.errno in (errno.ENOENT, errno.ENODEV, errno.ENXIO),
                                   formats=node.modes, name=node.card)
        node.input = index
    std, how, modes = "", "", node.modes
    if node.analog:
        std = (standard or "auto").upper()
        how = "config"
        if std == "AUTO":
            std = query_std(node.dev, ioctl, opener)
            how = "detected" if std else "default"
            std = std or DEFAULT_STANDARD
        if std not in STANDARDS:
            return Unavailable(kind, f"standard {standard!r} not supported: expected pal, ntsc or auto",
                               formats=modes, name=node.card)
        modes = std_modes(node, std)
    elif node.usb_id[:9] in CVBS_USB_IDS and (standard or "auto").upper() in STANDARDS:
        # UVC-захват CVBS (MS210x): стандарта в V4L2 нет (S_STD/QUERYSTD → ENOTTY), PAL/NTSC — это режим кадра
        _mask, w, h, _fps, _rate = STANDARDS[standard.upper()]
        modes = [m for m in node.modes if (m.width, m.height) == (w, h)]
    if (mode or "auto") == "auto":
        chosen, want, request = auto_mode(modes, fmt), "auto", "auto"
    else:
        width, height, fps = parse_mode(mode)
        chosen = select_mode(modes, width, height, fps, fmt)
        want, request = f"{'' if fmt == 'auto' else fmt.upper() + ' '}{width}×{height}@{fps}", (width, height, fps)
    if chosen is None:
        offered = ", ".join(str(m) for m in modes if m.pixfmt in FORMATS) or "no YUYV/UYVY/MJPEG/H.264"
        return Unavailable(kind, f"mode {want}{' (' + std + ')' if std else ''} not supported by {node.card}; "
                           f"offered: {offered}", formats=modes, name=node.card)
    cls = UVCSource if node.driver == "uvcvideo" else V4L2Source
    return cls(node, chosen, std, ioctl, opener, how, (request, fmt))


def open_uvc(device="", mode="1280x720@30", fmt="auto", **scan_kw):
    """Источник UVC по конфигурации (S3.3, пульт). device — vvvv:pppp[:serial], имя videoN или «» (единственное)."""
    return open_v4l2(device, mode, fmt, drivers={"uvcvideo"}, label="UVC", **scan_kw)


SOURCE_KINDS = ("synthetic", "v4l2", "uvc", "rtsp")


def open_source(spec, synthetic=None, **scan_kw):
    """Источник по описанию конфигурации блока (словарь):
      {"source": "synthetic"}
      {"source": "v4l2" | "uvc", "device": "...", "mode": "auto" | "WxH@fps", "format": "auto" | "yuyv" | "uyvy" |
       "mjpeg" | "h264", "standard": "auto" | "pal" | "ntsc", "input": "auto" | "composite" | "s-video" | N}
      {"source": "rtsp", "url": "rtsp://host:554/path", "codec": "h264" | "mjpeg", "transport": "auto" | "tcp" | "udp",
       "latency_ms": 200, "timeout_s": 5}
    synthetic — фабрика синтетического источника (sheregesh.video.synthetic: с надписями). Ошибка описания —
    Unavailable с причиной, а не исключение."""
    kind = str(spec.get("source") or "synthetic").lower()
    try:
        if kind == "synthetic":
            return (synthetic or SyntheticSource)()
        if kind in ("v4l2", "uvc"):
            return open_v4l2(str(spec.get("device") or ""), str(spec.get("mode") or "auto"),
                             str(spec.get("format") or "auto"), str(spec.get("standard") or "auto"),
                             drivers={"uvcvideo"} if kind == "uvc" else None,
                             label="UVC" if kind == "uvc" else "V4L2", input=str(spec.get("input") or "auto"),
                             **scan_kw)
        if kind == "rtsp":
            return RTSPSource(spec["url"], spec.get("codec", "h264"), spec.get("latency_ms", 200),
                              spec.get("timeout_s", 5.0), transport=spec.get("transport", "auto"))
    except (KeyError, ValueError, TypeError) as e:
        return Unavailable(kind, f"bad video input configuration: {e}")
    return Unavailable(kind, f"unknown video source {kind!r}: expected {', '.join(SOURCE_KINDS)}")


def main(argv=None):
    """Диагностика стенда: узлы video4linux, классификация, режимы, входы и стандарты. Устройства только
    опрашиваются (ioctl), захват не запускается."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh.videoinput", description=main.__doc__)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-probe", action="store_true", help="только sysfs, устройства не открывать")
    args = ap.parse_args(argv)
    nodes = scan(probe=not args.no_probe)
    if args.json:
        print(json.dumps([{**n.__dict__, "modes": [str(m) for m in n.modes], "input_type": n.input_type}
                          for n in nodes], ensure_ascii=False, indent=1))
        return 0
    for n in nodes:
        kind = "CAPTURE" if n.capture else "other  "
        print(f"{kind} {n.name:8} {n.driver:16} {n.usb_id or '-':24} {n.card} {n.dev if n.capture else ''}"
              f"{' — ' + n.reason if n.reason else ''}")
        if n.capture:
            print(f"         type {n.input_type}; bus {n.bus_info or '-'}; by-path {n.by_path or '-'}")
            for i in n.inputs:
                st = "no signal" if i["status"] & NO_SIGNAL_FLAGS else "ok/unknown"
                print(f"         input {i['index']}{'*' if i['index'] == n.input else ' '} {i['name']}: {st}")
            if n.standards:
                print(f"         standards: {', '.join(s[1] for s in n.standards)}; current {std_family(n.std) or '-'}")
        for m in n.modes:
            print(f"         {m}{'' if m.pixfmt in FORMATS else ' (not used)'}")
    if not nodes:
        print("no video4linux nodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
