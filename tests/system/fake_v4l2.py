# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Поддельные sysfs /sys/class/video4linux, /dev и ioctl V4L2 для испытаний видеовхода S3.3 без устройств.

Дерево повторяет Raspberry Pi 4 (системные узлы сняты с pi1/pi2, docs/acceptance/s3.3-video-input/hw/v4l2-discovery.txt):
bcm2835-codec /dev/video10, 11, 12, 18, 31; два экземпляра bcm2835-isp /dev/video13–16 и /dev/video20–23; rpi-hevc-dec
/dev/video19 — и, по желанию, HDMI→USB-адаптер класса UVC: узел захвата uvcvideo (index 0) и узел метаданных (index 1).
ioctl отвечает настоящими структурами videodev2.h, поэтому разбор в sheregesh.videoinput проверяется полностью.
Открытие служебного узла (драйверы videoinput.AUX_DRIVERS) — ошибка испытания: такой узел записывается в aux_opened,
и open() падает. Аналоговые захваты (usbtv, stk1160): inputs — входы VIDIOC_ENUMINPUT (status меняется на ходу:
потеря сигнала), standards — VIDIOC_ENUMSTD, std/querystd — VIDIOC_G_STD/QUERYSTD; без frame-размеров (как usbtv)
VIDIOC_ENUM_FRAMESIZES отвечает ENOTTY.
Сбои: open_errors {videoN: errno} — отказ open(); hooks — функции hook(videoN, request), вызываемые перед каждым ioctl
(отключить устройство посреди опроса, вернуть ошибку).
"""
import errno
import os
import shutil
from pathlib import Path

from sheregesh import videoinput as vi

MJPEG_HDMI = [("MJPG", "discrete", [((640, 480), [30, 60]), ((1280, 720), [30, 60]), ((1920, 1080), [30, 60])]),
              ("YUYV", "discrete", [((640, 480), [30, 60]), ((1280, 720), [10]), ((1920, 1080), [5])])]


_M2M, _CAP, _OUT, _META = 0x00004000, vi.V4L2_CAP_VIDEO_CAPTURE, 0x00000002, vi.V4L2_CAP_META_CAPTURE
# (N, драйвер, имя sysfs, index, возможности) — /dev/videoN pi1 и pi2 (одинаковы). Номера, драйверы и имена сняты с плат;
# возможности приблизительные (узлы не открывались и не должны открываться).
PI4_SYSTEM_NODES = (
    (10, "bcm2835-codec", "bcm2835-codec-decode", 0, _M2M),
    (11, "bcm2835-codec", "bcm2835-codec-encode", 0, _M2M),
    (12, "bcm2835-codec", "bcm2835-codec-isp", 0, _M2M),
    (13, "bcm2835-isp", "bcm2835-isp-output0", 0, _OUT),
    (14, "bcm2835-isp", "bcm2835-isp-capture0", 1, _CAP),
    (15, "bcm2835-isp", "bcm2835-isp-capture1", 2, _CAP),
    (16, "bcm2835-isp", "bcm2835-isp-stats2", 3, _META),
    (18, "bcm2835-codec", "bcm2835-codec-image_fx", 0, _M2M),
    (19, "rpi-hevc-dec", "rpi-hevc-dec", 0, _M2M),
    (20, "bcm2835-isp", "bcm2835-isp-output0", 0, _OUT),
    (21, "bcm2835-isp", "bcm2835-isp-capture0", 1, _CAP),
    (22, "bcm2835-isp", "bcm2835-isp-capture1", 2, _CAP),
    (23, "bcm2835-isp", "bcm2835-isp-stats2", 3, _META),
    (31, "bcm2835-codec", "bcm2835-codec-encode_image", 0, _M2M),
)


class FakeV4L2:
    """Дерево и ioctl. devices: {videoN: dict(driver, name, index, usb=(vid, pid, serial), caps, formats)}."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.sys = self.root / "sys"
        self.dev = self.root / "dev"
        self.devices = {}
        self._usb = {}
        self.fds = {}
        self.opened = []
        self.deny = set()
        self.open_errors = {}
        self.aux_opened = []
        self.hooks = []
        self.dev.mkdir(parents=True, exist_ok=True)
        (self.sys / "class" / "video4linux").mkdir(parents=True, exist_ok=True)

    # ---------- дерево ----------
    def add(self, node, driver, name, index=0, usb=None, caps=vi.V4L2_CAP_VIDEO_CAPTURE | vi.V4L2_CAP_STREAMING,
            formats=(), by_id=None, bus="usb-0000:01:00.0-1.2", by_path=None, inputs=None, standards=None, std=0,
            querystd=None, sizes_ioctl=True):
        if usb:
            port = self._usb.setdefault(usb, f"1-1.{len(self._usb) + 2}")    # узлы одного устройства — один порт
            usbdev = self.sys / "devices" / "platform" / "scb" / "usb1" / "1-1" / port
            parent = usbdev / f"{port}:1.0"
            vid, pid, serial = usb
            usbdev.mkdir(parents=True, exist_ok=True)
            (usbdev / "idVendor").write_text(vid + "\n")
            (usbdev / "idProduct").write_text(pid + "\n")
            if serial:
                (usbdev / "serial").write_text(serial + "\n")
        else:
            parent = self.sys / "devices" / "platform" / "soc" / f"{driver}.{len(self.devices)}"
        parent.mkdir(parents=True, exist_ok=True)
        drv = self.sys / "bus" / ("usb" if usb else "platform") / "drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        if not (parent / "driver").is_symlink():
            (parent / "driver").symlink_to(drv)
        real = parent / "video4linux" / node
        real.mkdir(parents=True)
        (real / "name").write_text(name + "\n")
        (real / "index").write_text(f"{index}\n")
        (real / "device").symlink_to(parent)
        (self.sys / "class" / "video4linux" / node).symlink_to(real)
        (self.dev / node).write_bytes(b"")
        for kind, link in (("by-id", by_id), ("by-path", by_path)):
            if link:
                (self.dev / "v4l" / kind).mkdir(parents=True, exist_ok=True)
                (self.dev / "v4l" / kind / link).symlink_to(f"../../{node}")
        self.devices[node] = {"driver": driver, "name": name, "caps": caps, "formats": list(formats), "bus": bus,
                              "real": real, "by_id": by_id, "by_path": by_path, "inputs": inputs, "input": 0,
                              "standards": standards, "std": std, "querystd": querystd, "sizes_ioctl": sizes_ioctl}

    def remove(self, node):
        """Отключение USB: узел исчезает из sysfs и /dev."""
        d = self.devices.pop(node)
        (self.sys / "class" / "video4linux" / node).unlink()
        (self.dev / node).unlink()
        shutil.rmtree(d["real"])
        for kind in ("by-id", "by-path"):
            if d[kind.replace("-", "_")]:
                (self.dev / "v4l" / kind / d[kind.replace("-", "_")]).unlink()

    def pi4(self, with_uvc=True, formats=MJPEG_HDMI, usb=("534d", "2109", "")):
        """Ровно 14 системных узлов Pi 4 (как на pi1/pi2) и, по желанию, HDMI→USB-адаптер. Возможности служебных
        узлов нарочно «опасные» (у ISP — V4L2_CAP_VIDEO_CAPTURE): классификация обязана опираться на драйвер."""
        if with_uvc:
            self.add("video0", "uvcvideo", "USB Video: USB Video", 0, usb, formats=formats,
                     by_id="usb-MACROSILICON_USB_Video-video-index0")
            self.add("video1", "uvcvideo", "USB Video: USB Video", 1, usb, caps=vi.V4L2_CAP_META_CAPTURE,
                     by_id="usb-MACROSILICON_USB_Video-video-index1")
        for n, driver, name, index, caps in PI4_SYSTEM_NODES:
            self.add(f"video{n}", driver, name, index, caps=caps)
        return self

    def scan_kw(self):
        return {"sys_root": self.sys, "dev_root": self.dev, "ioctl": self.ioctl, "opener": self.open}

    # ---------- ioctl ----------
    def open(self, path, flags):
        name = os.path.basename(os.path.realpath(path)) if os.path.exists(path) else os.path.basename(path)
        d = self.devices.get(name)
        if d is not None and d["driver"] in vi.AUX_DRIVERS:
            self.aux_opened.append(name)
            raise AssertionError(f"auxiliary V4L2 node {name} ({d['driver']}) must not be opened")
        if name in self.deny:
            raise PermissionError(errno.EACCES, "Permission denied", path)
        if name in self.open_errors:
            code = self.open_errors[name]
            raise OSError(code, os.strerror(code), path)
        fd = os.open(path, os.O_RDONLY)        # настоящий дескриптор: sheregesh.videoinput закрывает его сам
        self.fds[fd] = name
        self.opened.append(name)
        return fd

    def ioctl(self, fd, req, buf):
        for hook in list(self.hooks):
            hook(self.fds.get(fd), req)
        d = self.devices.get(self.fds.get(fd))
        if d is None:
            raise OSError(errno.ENODEV, "No such device")
        if req == vi.VIDIOC_QUERYCAP:
            return vi.CAPABILITY.pack(d["driver"].encode(), d["name"].encode(), d["bus"].encode(), 0x60000,
                                      d["caps"] | vi.V4L2_CAP_DEVICE_CAPS, d["caps"])
        if req == vi.VIDIOC_ENUM_FMT:
            index, typ, *_ = vi.FMTDESC.unpack(buf)
            if typ != vi.V4L2_BUF_TYPE_VIDEO_CAPTURE or index >= len(d["formats"]):
                raise OSError(errno.EINVAL, "Invalid argument")
            code = d["formats"][index][0]
            return vi.FMTDESC.pack(index, typ, 0, code.encode(), vi.fourcc(code), 0)
        if req == vi.VIDIOC_ENUMINPUT:
            index = vi.INPUT.unpack(buf)[0]
            if d["inputs"] is None:
                raise OSError(errno.ENOTTY, "Inappropriate ioctl")
            if index >= len(d["inputs"]):
                raise OSError(errno.EINVAL, "Invalid argument")
            i = d["inputs"][index]                  # {name, status, capabilities, std}
            return vi.INPUT.pack(index, i["name"].encode(), 2, 0, 0, i.get("std", 0), i.get("status", 0),
                                 i.get("capabilities", 0))
        if req == vi.VIDIOC_G_INPUT:
            if d["inputs"] is None:
                raise OSError(errno.ENOTTY, "Inappropriate ioctl")
            return vi.INT.pack(d["input"])
        if req == vi.VIDIOC_ENUMSTD:
            index = vi.STANDARD.unpack(buf)[0]
            if not d["standards"]:
                raise OSError(errno.ENODATA if d["standards"] == [] else errno.ENOTTY, "no standards")
            if index >= len(d["standards"]):
                raise OSError(errno.EINVAL, "Invalid argument")
            sid, sname, num, den, lines = d["standards"][index]
            return vi.STANDARD.pack(index, sid, sname.encode(), num, den, lines)
        if req in (vi.VIDIOC_G_STD, vi.VIDIOC_QUERYSTD):
            value = d["std"] if req == vi.VIDIOC_G_STD else d["querystd"]
            if not value:
                raise OSError(errno.ENODATA if req == vi.VIDIOC_QUERYSTD else errno.ENOTTY, "no standard")
            return vi.STD_ID.pack(value)
        if req == vi.VIDIOC_ENUM_FRAMESIZES and not d["sizes_ioctl"]:
            raise OSError(errno.ENOTTY, "Inappropriate ioctl")
        if req == vi.VIDIOC_ENUM_FRAMESIZES:
            index, pixfmt, *_ = vi.FRMSIZE.unpack(buf)
            fmt = self._fmt(d, pixfmt)
            code, kind, sizes = fmt
            if kind in ("stepwise", "continuous"):    # (min_w, max_w, step_w, min_h, max_h, step_h)
                if index:
                    raise OSError(errno.EINVAL, "Invalid argument")
                return vi.FRMSIZE.pack(0, pixfmt, 3 if kind == "stepwise" else 2, *sizes[0][0])
            if index >= len(sizes):
                raise OSError(errno.EINVAL, "Invalid argument")
            (w, h), _ = sizes[index]
            return vi.FRMSIZE.pack(index, pixfmt, vi.V4L2_FRMSIZE_TYPE_DISCRETE, w, h, 0, 0, 0, 0)
        if req == vi.VIDIOC_ENUM_FRAMEINTERVALS:
            index, pixfmt, w, h, *_ = vi.FRMIVAL.unpack(buf)
            code, kind, sizes = self._fmt(d, pixfmt)
            if kind in ("stepwise", "continuous"):   # по умолчанию непрерывный интервал 1/60…1/5 с
                if index:
                    raise OSError(errno.EINVAL, "Invalid argument")
                ival = sizes[0][1] or (2, 1, 60, 1, 5, 1, 1)     # (тип, min n/d, max n/d, step n/d)
                return vi.FRMIVAL.pack(0, pixfmt, w, h, *ival)
            rates = next((r for (sw, sh), r in sizes if (sw, sh) == (w, h)), None)
            if rates is None or index >= len(rates):
                raise OSError(errno.EINVAL, "Invalid argument")
            rate = rates[index]                    # к/с или (числитель, знаменатель) интервала, напр. (1001, 30000)
            num, den = rate if isinstance(rate, tuple) else (1, rate)
            return vi.FRMIVAL.pack(index, pixfmt, w, h, vi.V4L2_FRMIVAL_TYPE_DISCRETE, num, den, 0, 0, 0, 0)
        raise OSError(errno.ENOTTY, "Inappropriate ioctl")

    @staticmethod
    def _fmt(d, pixfmt):
        for f in d["formats"]:
            if vi.fourcc(f[0]) == pixfmt:
                return f
        raise OSError(errno.EINVAL, "Invalid argument")


# Стандарты, как их перечисляет usbtv/stk1160 (id, имя, кадровый период n/d, строк)
PAL_STD = (vi.V4L2_STD_PAL, "PAL", 1, 25, 625)
NTSC_STD = (vi.V4L2_STD_NTSC, "NTSC", 1001, 30000, 525)
CVBS_INPUTS = [{"name": "Composite", "capabilities": vi.V4L2_IN_CAP_STD, "std": vi.V4L2_STD_PAL | vi.V4L2_STD_NTSC},
               {"name": "S-Video", "capabilities": vi.V4L2_IN_CAP_STD, "std": vi.V4L2_STD_PAL | vi.V4L2_STD_NTSC}]


def add_usbtv(v4l2, node="video0", querystd=vi.V4L2_STD_PAL, by_id="usb-Fushicai_USBTV007_0001-video-index0"):
    """Аналоговый захват usbtv (Fushicai USBTV007, 1b71:3002): YUYV, без ENUM_FRAMESIZES, PAL/NTSC, два входа."""
    v4l2.add(node, "usbtv", "usbtv", 0, ("1b71", "3002", ""), formats=[("YUYV", "discrete", [])], by_id=by_id,
             by_path="platform-fd500000.pcie-pci-0000:01:00.0-usb-0:1.3:1.0-video-index0",
             inputs=[dict(i) for i in CVBS_INPUTS], standards=[PAL_STD, NTSC_STD], std=vi.V4L2_STD_PAL,
             querystd=querystd, sizes_ioctl=False)
    return v4l2
