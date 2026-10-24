# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Поддельные sysfs /sys/class/video4linux, /dev и ioctl V4L2 для испытаний видеовхода S3.3 без устройств.

Дерево повторяет устройство Raspberry Pi 4 с подключённым HDMI→USB-адаптером класса UVC: узел захвата uvcvideo
(index 0), узел метаданных UVC (index 1), кодек bcm2835-codec (/dev/video10–12), ISP (/dev/video13–16) и unicam.
ioctl отвечает настоящими структурами videodev2.h, поэтому разбор в sheregesh.videoinput проверяется полностью.
"""
import errno
import os
import shutil
from pathlib import Path

from sheregesh import videoinput as vi

MJPEG_HDMI = [("MJPG", "discrete", [((640, 480), [30, 60]), ((1280, 720), [30, 60]), ((1920, 1080), [30, 60])]),
              ("YUYV", "discrete", [((640, 480), [30, 60]), ((1280, 720), [10]), ((1920, 1080), [5])])]


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
        self.dev.mkdir(parents=True, exist_ok=True)
        (self.sys / "class" / "video4linux").mkdir(parents=True, exist_ok=True)

    # ---------- дерево ----------
    def add(self, node, driver, name, index=0, usb=None, caps=vi.V4L2_CAP_VIDEO_CAPTURE | vi.V4L2_CAP_STREAMING,
            formats=(), by_id=None, bus="usb-0000:01:00.0-1.2"):
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
        if by_id:
            (self.dev / "v4l" / "by-id").mkdir(parents=True, exist_ok=True)
            (self.dev / "v4l" / "by-id" / by_id).symlink_to(f"../../{node}")
        self.devices[node] = {"driver": driver, "name": name, "caps": caps, "formats": list(formats), "bus": bus,
                              "real": real, "by_id": by_id}

    def remove(self, node):
        """Отключение USB: узел исчезает из sysfs и /dev."""
        d = self.devices.pop(node)
        (self.sys / "class" / "video4linux" / node).unlink()
        (self.dev / node).unlink()
        shutil.rmtree(d["real"])
        if d["by_id"]:
            (self.dev / "v4l" / "by-id" / d["by_id"]).unlink()

    def pi4(self, with_uvc=True, formats=MJPEG_HDMI, usb=("534d", "2109", "")):
        """Типичный набор узлов Pi 4 (Raspberry Pi OS / Armbian) и, по желанию, HDMI→USB-адаптер."""
        cap = vi.V4L2_CAP_VIDEO_CAPTURE
        if with_uvc:
            self.add("video0", "uvcvideo", "USB Video: USB Video", 0, usb, formats=formats,
                     by_id="usb-MACROSILICON_USB_Video-video-index0")
            self.add("video1", "uvcvideo", "USB Video: USB Video", 1, usb, caps=vi.V4L2_CAP_META_CAPTURE,
                     by_id="usb-MACROSILICON_USB_Video-video-index1")
        for n, name in ((10, "bcm2835-codec-decode"), (11, "bcm2835-codec-encode"), (12, "bcm2835-codec-isp")):
            self.add(f"video{n}", "bcm2835-codec", name, 0, caps=0x00004000)     # V4L2_CAP_VIDEO_M2M_MPLANE
        for n in (13, 14, 15, 16):
            self.add(f"video{n}", "bcm2835-isp", "bcm2835-isp", n - 13, caps=cap)
        self.add("video19", "unicam", "unicam-image", 0, caps=cap)
        return self

    def scan_kw(self):
        return {"sys_root": self.sys, "dev_root": self.dev, "ioctl": self.ioctl, "opener": self.open}

    # ---------- ioctl ----------
    def open(self, path, flags):
        name = os.path.basename(path)
        if name in self.deny:
            raise PermissionError(errno.EACCES, "Permission denied", path)
        fd = os.open(path, os.O_RDONLY)        # настоящий дескриптор: sheregesh.videoinput закрывает его сам
        self.fds[fd] = name
        self.opened.append(name)
        return fd

    def ioctl(self, fd, req, buf):
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
        if req == vi.VIDIOC_ENUM_FRAMESIZES:
            index, pixfmt, *_ = vi.FRMSIZE.unpack(buf)
            fmt = self._fmt(d, pixfmt)
            code, kind, sizes = fmt
            if kind == "stepwise":                 # (min_w, max_w, step_w, min_h, max_h, step_h)
                if index:
                    raise OSError(errno.EINVAL, "Invalid argument")
                return vi.FRMSIZE.pack(0, pixfmt, 3, *sizes[0][0])
            if index >= len(sizes):
                raise OSError(errno.EINVAL, "Invalid argument")
            (w, h), _ = sizes[index]
            return vi.FRMSIZE.pack(index, pixfmt, vi.V4L2_FRMSIZE_TYPE_DISCRETE, w, h, 0, 0, 0, 0)
        if req == vi.VIDIOC_ENUM_FRAMEINTERVALS:
            index, pixfmt, w, h, *_ = vi.FRMIVAL.unpack(buf)
            code, kind, sizes = self._fmt(d, pixfmt)
            if kind == "stepwise":                 # непрерывный интервал 1/60…1/5 с
                if index:
                    raise OSError(errno.EINVAL, "Invalid argument")
                return vi.FRMIVAL.pack(0, pixfmt, w, h, 2, 1, 60, 1, 5, 1, 1)
            rates = next((r for (sw, sh), r in sizes if (sw, sh) == (w, h)), None)
            if rates is None or index >= len(rates):
                raise OSError(errno.EINVAL, "Invalid argument")
            return vi.FRMIVAL.pack(index, pixfmt, w, h, vi.V4L2_FRMIVAL_TYPE_DISCRETE, 1, rates[index], 0, 0, 0, 0)
        raise OSError(errno.ENOTTY, "Inappropriate ioctl")

    @staticmethod
    def _fmt(d, pixfmt):
        for f in d["formats"]:
            if vi.fourcc(f[0]) == pixfmt:
                return f
        raise OSError(errno.EINVAL, "Invalid argument")
