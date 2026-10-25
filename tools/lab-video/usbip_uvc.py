# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор USB-видеоустройств (UVC 1.0) по USB/IP: HDMI→USB, CVBS→USB и обычная камера с картинкой shcam.

  python3 usbip_uvc.py serve [--listen 0.0.0.0:3240] [--source rtsp://127.0.0.1:8554/cam | test]
                             [--profiles hdmi-ms2109,cvbs-ms2106,camera] [--state-dir /run/shcam-uvc]
  python3 usbip_uvc.py descriptors hdmi-ms2109        дескрипторы профиля (hex) — для проверки

На стороне, где устройство должно «появиться» (root, модуль vhci-hcd): usbip_attach.py attach HOST BUSID.
Для хоста это обычное USB-устройство: настоящий драйвер uvcvideo, sysfs, VID:PID, подключение и отключение.
Шерегеш (videoinput.py) классифицирует профили по VID:PID: 534d:2109 → HDMI (HDMI_USB_IDS), 534d:0021 → CVBS
(CVBS_USB_IDS), 1d6b:0102 → USB (UVC-камера).

| профиль      | busid | VID:PID   | форматы                                                      |
|--------------|-------|-----------|--------------------------------------------------------------|
| hdmi-ms2109  | 1-1   | 534d:2109 | MJPEG 1920x1080@30, 1280x720@30, 640x480@30; YUYV 640x480@30 |
| cvbs-ms2106  | 1-2   | 534d:0021 | MJPEG/YUYV 720x576@25, 720x480@30                            |
| camera       | 1-3   | 1d6b:0102 | MJPEG 1280x720@30, 640x480@30; YUYV 640x480@30               |

Вход UVC у всех — терминал «камера» (ITT_CAMERA): внешний терминал «композитный разъём» (0x0401) uvcvideo в цепочку
не берёт («No valid video chain found»), а тип входа шерегеш всё равно определяет по VID:PID.

Сбои — файл <state-dir>/<профиль>.signal (читается на каждом кадре):
  ok       картинка камеры              blue | black   однотонное поле (как MS2109 без сигнала)
  freeze   повтор последнего кадра      unplug         разорвать соединение USB/IP = «выдернуть» устройство

Видео идёт через bulk-endpoint 0x81: каждый ответ на URB — одна полезная нагрузка UVC (заголовок 2 байта + данные,
не больше dwMaxPayloadTransferSize = 16384 = размер URB uvcvideo для bulk 512×32), последняя нагрузка кадра — с EOF,
FID переключается от кадра к кадру. Протокол USB/IP — Documentation/usb/usbip_protocol.rst ядра (версия 0x0111).
"""
import argparse
import asyncio
import errno
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

USBIP_VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
USB_SPEED_HIGH = 3
EP_IN = 0x81
BULK_MAX_PACKET = 512
MAX_PAYLOAD = 16384                     # = urb_size uvcvideo для bulk (UVC_MAX_PACKETS 32 × 512)
UVC_HEADER = 2
PROBE_LEN = 26                          # UVC 1.0: VS_PROBE/COMMIT
YUY2_GUID = b"YUY2" + bytes.fromhex("00001000800000aa00389b71")
FMT_MJPEG, FMT_YUYV = "MJPEG", "YUYV"


@dataclass
class Frame:
    width: int
    height: int
    fps: tuple                          # частоты по убыванию; первая — по умолчанию


@dataclass
class Profile:
    name: str
    busid: str
    vid: int
    pid: int
    manufacturer: str
    product: str
    formats: list                       # [(FMT_*, [Frame, ...]), ...]
    bcd_device: int = 0x0100
    serial: str = ""


PROFILES = {p.name: p for p in (
    Profile("hdmi-ms2109", "1-1", 0x534D, 0x2109, "MACROSILICON", "USB Video",
            [(FMT_MJPEG, [Frame(1920, 1080, (30,)), Frame(1280, 720, (30,)), Frame(640, 480, (30,))]),
             (FMT_YUYV, [Frame(640, 480, (30,))])], bcd_device=0x2100),
    Profile("cvbs-ms2106", "1-2", 0x534D, 0x0021, "MACROSILICON", "AV TO USB2.0",
            [(FMT_MJPEG, [Frame(720, 576, (25,)), Frame(720, 480, (30,))]),
             (FMT_YUYV, [Frame(720, 576, (25,)), Frame(720, 480, (30,))])], bcd_device=0x0121),
    Profile("camera", "1-3", 0x1D6B, 0x0102, "sheregesh", "shcam UVC camera",
            [(FMT_MJPEG, [Frame(1280, 720, (30,)), Frame(640, 480, (30,))]),
             (FMT_YUYV, [Frame(640, 480, (30,))])], serial="shcam-0001"),
)}


# --------------------------------------------------------------------------- дескрипторы

def frame_size(fmt, fr):
    return fr.width * fr.height * 2     # YUYV точно; для MJPEG — верхняя граница буфера


def device_descriptor(p):
    return struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0xEF, 0x02, 0x01, 64, p.vid, p.pid, p.bcd_device,
                       1, 2, 3 if p.serial else 0, 1)


def config_descriptor(p):
    # ITT_CAMERA: фокусные расстояния 0, bControlSize 3, управлений нет
    it = struct.pack("<BBBBHBBHHHB3s", 18, 0x24, 0x02, 1, 0x0201, 0, 0, 0, 0, 0, 3, b"\0\0\0")
    ot = struct.pack("<BBBBHBBB", 9, 0x24, 0x03, 2, 0x0101, 0, 1, 0)
    vc_len = 13 + len(it) + len(ot)
    vc_header = struct.pack("<BBBHHIBB", 13, 0x24, 0x01, 0x0100, vc_len, 48_000_000, 1, 1)

    vs_body = b""
    for fi, (fmt, frames) in enumerate(p.formats, 1):
        if fmt == FMT_MJPEG:
            vs_body += struct.pack("<BBBBBBBBBBB", 11, 0x24, 0x06, fi, len(frames), 1, 1, 0, 0, 0, 0)
            sub = 0x07
        else:
            vs_body += struct.pack("<BBBBB16sBBBBBB", 27, 0x24, 0x04, fi, len(frames), YUY2_GUID, 16, 1, 0, 0, 0, 0)
            sub = 0x05
        for ri, fr in enumerate(frames, 1):
            ivals = [10_000_000 // f for f in fr.fps]
            size = frame_size(fmt, fr)
            bitrate = size * 8 * fr.fps[0]
            vs_body += struct.pack("<BBBBBHHIIIIB", 26 + 4 * len(ivals), 0x24, sub, ri, 0, fr.width, fr.height,
                                   bitrate, bitrate, size, ivals[0], len(ivals))
            vs_body += b"".join(struct.pack("<I", i) for i in ivals)
        vs_body += struct.pack("<BBBBBB", 6, 0x24, 0x0D, 1, 1, 4)
    nfmt = len(p.formats)
    vs_header_len = 13 + nfmt
    vs_header = struct.pack("<BBBBHBBBBBBB", vs_header_len, 0x24, 0x01, nfmt, vs_header_len + len(vs_body),
                            EP_IN, 0, 2, 0, 0, 0, 1) + bytes(nfmt)

    body = (struct.pack("<BBBBBBBB", 8, 0x0B, 0, 2, 0x0E, 0x03, 0, 2)              # IAD
            + struct.pack("<BBBBBBBBB", 9, 4, 0, 0, 0, 0x0E, 0x01, 0, 2)           # VideoControl
            + vc_header + it + ot
            + struct.pack("<BBBBBBBBB", 9, 4, 1, 0, 1, 0x0E, 0x02, 0, 0)           # VideoStreaming, alt 0
            + vs_header + vs_body
            + struct.pack("<BBBBHB", 7, 5, EP_IN, 0x02, BULK_MAX_PACKET, 0))        # bulk IN
    return struct.pack("<BBHBBBBB", 9, 2, 9 + len(body), 2, 1, 0, 0x80, 250) + body


def string_descriptor(p, index):
    if index == 0:
        return bytes((4, 3, 0x09, 0x04))
    s = {1: p.manufacturer, 2: p.product, 3: p.serial}.get(index)
    if s is None:
        return None
    data = s.encode("utf-16-le")
    return bytes((2 + len(data), 3)) + data


def qualifier_descriptor(p):
    return struct.pack("<BBHBBBBBB", 10, 6, 0x0200, 0xEF, 0x02, 0x01, 64, 1, 0)


# --------------------------------------------------------------------------- USB/IP: обмен

def devinfo(p, devnum):
    path = f"/sys/devices/platform/shcam-uvc/usb1/{p.busid}".encode().ljust(256, b"\0")
    return (path + p.busid.encode().ljust(32, b"\0")
            + struct.pack(">IIIHHHBBBBBB", 1, devnum, USB_SPEED_HIGH, p.vid, p.pid, p.bcd_device,
                          0xEF, 0x02, 0x01, 1, 1, 2))


def devlist_reply(profiles):
    out = struct.pack(">HHII", USBIP_VERSION, OP_REP_DEVLIST, 0, len(profiles))
    for n, p in enumerate(profiles, 2):
        out += devinfo(p, n) + struct.pack(">BBBB", 0x0E, 0x01, 0, 0) + struct.pack(">BBBB", 0x0E, 0x02, 0, 0)
    return out


def ret_submit(seqnum, status=0, data=b"", actual=None):
    actual = len(data) if actual is None else actual
    return struct.pack(">IIIIIiIIII8x", RET_SUBMIT, seqnum, 0, 0, 0, status, actual, 0, 0, 0) + data


def ret_unlink(seqnum, status):
    return struct.pack(">IIIIIi24x", RET_UNLINK, seqnum, 0, 0, 0, status)


def payloads(frame, fid, max_payload=MAX_PAYLOAD):
    """Кадр → полезные нагрузки UVC: [заголовок 2 байта: длина, bmHeaderInfo (EOH | EOF | FID)] + данные."""
    chunk = max_payload - UVC_HEADER
    parts = [frame[i:i + chunk] for i in range(0, len(frame), chunk)] or [b""]
    return [bytes((UVC_HEADER, 0x80 | fid | (0x02 if i == len(parts) - 1 else 0))) + part
            for i, part in enumerate(parts)]


# --------------------------------------------------------------------------- источник кадров

class Source:
    """gst-launch → stdout: MJPEG (multipartmux с Content-Length) или сырой YUYV фиксированного размера."""

    def __init__(self, url, fmt, fr, fps):
        self.url, self.fmt, self.fr, self.fps = url, fmt, fr, fps
        self.proc = None

    def _pipeline(self, pattern=None, count=None):
        w, h, f = self.fr.width, self.fr.height, self.fps
        if pattern:
            src = f"videotestsrc pattern={pattern}" + (f" num-buffers={count}" if count else " is-live=true")
        elif self.url == "test":
            src = "videotestsrc is-live=true pattern=smpte"
        else:
            src = (f"rtspsrc location={self.url} protocols=tcp latency=0 ! rtph264depay ! h264parse ! avdec_h264 "
                   "! videoconvert ! videoscale add-borders=false ! videorate")
        caps = f"video/x-raw,width={w},height={h},framerate={f}/1,pixel-aspect-ratio=1/1"
        tail = ("! videoconvert ! jpegenc quality=80 ! multipartmux boundary=shcamframe ! fdsink fd=1"
                if self.fmt == FMT_MJPEG else "! videoconvert ! video/x-raw,format=YUY2 ! fdsink fd=1")
        label = "" if pattern else (f"! timeoverlay text=\"USB {self.fmt} {w}x{h}\" halignment=right "
                                    "valignment=bottom font-desc=\"Sans 18\" ")
        return f"{src} ! {caps} {label}{tail}"

    async def start(self):
        self.proc = await asyncio.create_subprocess_shell(
            "exec gst-launch-1.0 -q " + self._pipeline(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1 << 24)

    async def frames(self):
        out = self.proc.stdout
        if self.fmt == FMT_YUYV:
            n = self.fr.width * self.fr.height * 2
            while True:
                yield await out.readexactly(n)
        while True:
            line = await out.readline()
            if not line:
                raise asyncio.IncompleteReadError(b"", None)
            if not line.startswith(b"--shcamframe"):
                continue
            length = None
            while (line := await out.readline()) not in (b"\r\n", b"\n", b""):
                k, _, v = line.decode("latin-1").partition(":")
                if k.strip().lower() == "content-length":
                    length = int(v)
            if length is None:
                raise ValueError("multipartmux: нет Content-Length")
            yield await out.readexactly(length)

    async def still(self, pattern):
        """Один кадр однотонного поля (blue | black) в формате потока."""
        proc = await asyncio.create_subprocess_shell(
            "exec gst-launch-1.0 -q " + self._pipeline(pattern=pattern, count=1), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1 << 24)
        data, _ = await proc.communicate()
        if self.fmt == FMT_YUYV:
            return data[:self.fr.width * self.fr.height * 2]
        head, _, rest = data.partition(b"\r\n\r\n")
        length = next(int(v) for k, _, v in (ln.decode("latin-1").partition(":") for ln in head.split(b"\r\n"))
                      if k.strip().lower() == "content-length")
        return rest[:length]

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None


# --------------------------------------------------------------------------- одно устройство на соединение

@dataclass
class Device:
    profile: Profile
    source_url: str
    state_dir: Path
    writer: asyncio.StreamWriter = None
    probe: dict = field(default_factory=dict)
    committed: dict = None
    pending: list = field(default_factory=list)        # [(seqnum, length)] — URB bulk IN в ожидании данных
    urb_ready: asyncio.Event = field(default_factory=asyncio.Event)
    stream_task: asyncio.Task = None
    unplug: asyncio.Event = field(default_factory=asyncio.Event)
    log: callable = print

    def __post_init__(self):
        self.probe = self.default_probe()
        self.config = config_descriptor(self.profile)

    # -- UVC probe/commit
    def default_probe(self, fi=1, ri=1):
        fmt, frames = self.profile.formats[fi - 1]
        fr = frames[ri - 1]
        return {"hint": 1, "format": fi, "frame": ri, "interval": 10_000_000 // fr.fps[0],
                "size": frame_size(fmt, fr), "payload": MAX_PAYLOAD}

    def negotiate(self, data):
        hint, fi, ri, interval = struct.unpack_from("<HBBI", data)
        fi = fi if 1 <= fi <= len(self.profile.formats) else 1
        frames = self.profile.formats[fi - 1][1]
        ri = ri if 1 <= ri <= len(frames) else 1
        p = self.default_probe(fi, ri)
        ivals = [10_000_000 // f for f in frames[ri - 1].fps]
        p["interval"] = min(ivals, key=lambda i: abs(i - interval)) if interval else ivals[0]
        p["hint"] = hint
        return p

    @staticmethod
    def pack_probe(p, length):
        raw = struct.pack("<HBBIHHHHHII", p["hint"], p["format"], p["frame"], p["interval"], 0, 0, 0, 0, 0,
                          p["size"], p["payload"])
        return raw.ljust(length, b"\0")[:length]

    def signal(self):
        try:
            return (self.state_dir / f"{self.profile.name}.signal").read_text().strip() or "ok"
        except OSError:
            return "ok"

    # -- запросы ep0
    def control(self, setup, data):
        """→ (status, данные ответа). Остановка (STALL) — status -EPIPE."""
        rtype, req, value, index, length = struct.unpack("<BBHHH", setup)
        stall = (-errno.EPIPE, b"")
        if rtype == 0x80 and req == 6:                                  # GET_DESCRIPTOR
            kind, idx = value >> 8, value & 0xFF
            desc = {1: lambda: device_descriptor(self.profile), 2: lambda: self.config,
                    3: lambda: string_descriptor(self.profile, idx), 6: lambda: qualifier_descriptor(self.profile)
                    }.get(kind, lambda: None)()
            return stall if desc is None else (0, desc[:length])
        if rtype == 0x00 and req == 9:                                  # SET_CONFIGURATION
            return 0, b""
        if rtype == 0x80 and req == 8:                                  # GET_CONFIGURATION
            return 0, b"\x01"
        if rtype == 0x01 and req == 11:                                 # SET_INTERFACE (alt 0 — bulk)
            return 0, b""
        if rtype == 0x81 and req == 10:                                 # GET_INTERFACE
            return 0, b"\x00"
        if rtype in (0x80, 0x81, 0x82) and req == 0:                    # GET_STATUS
            return 0, b"\0\0"[:length]
        if rtype == 0x02 and req == 1 and value == 0 and index & 0xFF == EP_IN:   # CLEAR_FEATURE(ENDPOINT_HALT)
            self.stop_stream("clear halt")
            return 0, b""
        if rtype & 0x60 == 0x20 and index & 0xFF == 1 and index >> 8 == 0:        # класс: VideoStreaming
            cs = value >> 8
            if cs not in (1, 2):                                        # только VS_PROBE / VS_COMMIT
                return stall
            if rtype == 0x21 and req == 0x01:                           # SET_CUR
                p = self.negotiate(data)
                self.probe = p
                if cs == 2:
                    self.commit(p)
                return 0, b""
            if rtype == 0xA1:
                if req == 0x86:                                         # GET_INFO
                    return 0, b"\x03"
                if req == 0x85:                                         # GET_LEN
                    return 0, struct.pack("<H", PROBE_LEN)
                if req in (0x81, 0x82, 0x83, 0x87, 0x84):               # GET_CUR/MIN/MAX/DEF/RES
                    p = self.probe if req == 0x81 else self.default_probe()
                    return 0, self.pack_probe(p, length)
        return stall                                                    # VideoControl и прочее — нет управлений

    # -- поток
    def commit(self, p):
        fmt, frames = self.profile.formats[p["format"] - 1]
        fr = frames[p["frame"] - 1]
        fps = round(10_000_000 / p["interval"])
        self.stop_stream("recommit")
        self.committed = dict(p, fmt=fmt, fr=fr, fps=fps)
        self.log(f"{self.profile.name}: commit {fmt} {fr.width}x{fr.height}@{fps}")
        self.stream_task = asyncio.get_running_loop().create_task(self.stream())

    def stop_stream(self, why):
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()
            self.log(f"{self.profile.name}: stream stop ({why})")
        self.stream_task = None

    async def next_urb(self):
        while not self.pending:
            self.urb_ready.clear()
            await self.urb_ready.wait()
        return self.pending.pop(0)

    async def send_frame(self, frame, fid):
        for pl in payloads(frame, fid):
            seq, length = await self.next_urb()
            if len(pl) > length:                # URB меньше нагрузки (не ожидается при 16384): режем, EOF — в последней
                pl = pl[:length]
            self.writer.write(ret_submit(seq, 0, pl))
        await self.writer.drain()

    async def stream(self):
        c = self.committed
        src = Source(self.source_url, c["fmt"], c["fr"], c["fps"])
        latest = {"frame": None}
        fresh = asyncio.Event()
        stills = {}

        async def produce():                    # источник упал (RTSP недоступен и т. п.) — перезапуск через 2 с
            while True:
                try:
                    await src.start()
                    async for f in src.frames():
                        latest["frame"] = f
                        fresh.set()
                except (asyncio.IncompleteReadError, ValueError, OSError) as e:
                    self.log(f"{self.profile.name}: source restart ({type(e).__name__})")
                await src.stop()
                await asyncio.sleep(2)

        producer = asyncio.get_running_loop().create_task(produce())
        fid, last = 0, None
        period = 1 / c["fps"]
        try:
            while True:
                mode = self.signal()
                if mode in ("blue", "black"):
                    if mode not in stills:
                        stills[mode] = await src.still(mode)
                    frame = stills[mode]
                    await asyncio.sleep(period)
                elif mode == "freeze" and last is not None:
                    frame = last
                    await asyncio.sleep(period)
                else:
                    try:
                        await asyncio.wait_for(fresh.wait(), 5)
                    except TimeoutError:
                        continue                                        # источник молчит — ждём (как без сигнала)
                    fresh.clear()
                    frame = last = latest["frame"]
                await self.send_frame(frame, fid)
                fid ^= 1
        finally:
            producer.cancel()
            await src.stop()


# --------------------------------------------------------------------------- сервер

class Server:
    def __init__(self, profiles, source_url, state_dir, log=print):
        self.profiles = {p.busid: p for p in profiles}
        self.source_url, self.state_dir, self.log = source_url, Path(state_dir), log
        self.busy = set()

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            version, code, _status = struct.unpack(">HHI", await reader.readexactly(8))
            if code == OP_REQ_DEVLIST:
                free = [p for b, p in self.profiles.items() if b not in self.busy]
                writer.write(devlist_reply(free))
                await writer.drain()
                return
            if code != OP_REQ_IMPORT:
                self.log(f"{peer}: неизвестная операция 0x{code:04x}")
                return
            busid = (await reader.readexactly(32)).rstrip(b"\0").decode()
            p = self.profiles.get(busid)
            if p is None or busid in self.busy:
                writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 1))
                await writer.drain()
                return
            self.busy.add(busid)
            writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 0) + devinfo(p, 2 + list(self.profiles).index(busid)))
            await writer.drain()
            self.log(f"{p.name} ({busid}) imported by {peer}")
            dev = Device(p, self.source_url, self.state_dir, writer=writer, log=self.log)
            urbs = asyncio.get_running_loop().create_task(self.urb_loop(reader, writer, dev))
            unplug = asyncio.get_running_loop().create_task(self.watch_unplug(dev))
            await asyncio.wait({urbs, unplug}, return_when=asyncio.FIRST_COMPLETED)
            for t in (urbs, unplug):
                t.cancel()
            dev.stop_stream("disconnect")
            self.busy.discard(busid)
            self.log(f"{p.name} ({busid}) released")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def watch_unplug(self, dev):
        """signal = unplug → разорвать соединение (хост видит отключение USB); файл возвращается в ok,
        чтобы следующий attach снова подключил устройство («вставили обратно»)."""
        while dev.signal() != "unplug":
            await asyncio.sleep(0.3)
        self.log(f"{dev.profile.name}: unplug")
        (self.state_dir / f"{dev.profile.name}.signal").write_text("ok\n")

    async def urb_loop(self, reader, writer, dev):
        try:
            while True:
                hdr = await reader.readexactly(48)
                command, seq, _devid, direction, ep = struct.unpack_from(">IIIII", hdr)
                if command == CMD_SUBMIT:
                    _flags, length, _sf, npk, _iv = struct.unpack_from(">IiIiI", hdr, 20)
                    setup = hdr[40:48]
                    data = await reader.readexactly(length) if direction == 0 and length > 0 else b""
                    if npk not in (0, -1, 0xFFFFFFFF) and npk > 0:      # изохронных endpoint'ов нет
                        await reader.readexactly(16 * npk)
                        writer.write(ret_submit(seq, -errno.EPIPE))
                    elif ep == 0:
                        status, out = dev.control(setup, data)
                        writer.write(ret_submit(seq, status, out if direction == 1 else b"",
                                                None if direction == 1 else (len(data) if status == 0 else 0)))
                    elif ep == (EP_IN & 0x0F) and direction == 1:
                        dev.pending.append((seq, length))
                        dev.urb_ready.set()
                    else:
                        writer.write(ret_submit(seq, -errno.EPIPE))
                elif command == CMD_UNLINK:
                    victim = struct.unpack_from(">I", hdr, 20)[0]
                    before = len(dev.pending)
                    dev.pending = [u for u in dev.pending if u[0] != victim]
                    writer.write(ret_unlink(seq, -errno.ECONNRESET if len(dev.pending) < before else 0))
                else:
                    raise ConnectionError(f"неизвестная команда USB/IP {command}")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--listen", default="0.0.0.0:3240")
    s.add_argument("--source", default="rtsp://127.0.0.1:8554/cam", help="rtsp://… или test")
    s.add_argument("--profiles", default=",".join(PROFILES))
    s.add_argument("--state-dir", default="/run/shcam-uvc")
    d = sub.add_parser("descriptors")
    d.add_argument("profile", choices=sorted(PROFILES))
    args = ap.parse_args(argv)

    if args.cmd == "descriptors":
        p = PROFILES[args.profile]
        print("device", device_descriptor(p).hex())
        print("config", config_descriptor(p).hex())
        return 0

    profiles = [PROFILES[n] for n in args.profiles.split(",") if n]
    host, _, port = args.listen.rpartition(":")
    Path(args.state_dir).mkdir(parents=True, exist_ok=True)
    server = Server(profiles, args.source, args.state_dir, log=lambda m: print(m, flush=True))

    async def run():
        srv = await asyncio.start_server(server.handle, host or None, int(port))
        print(f"usbip-uvc: {args.listen}, профили: "
              + ", ".join(f"{p.busid}={p.name} {p.vid:04x}:{p.pid:04x}" for p in profiles), flush=True)
        async with srv:
            await srv.serve_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
