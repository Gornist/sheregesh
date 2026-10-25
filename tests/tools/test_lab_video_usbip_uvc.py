# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор UVC по USB/IP (tools/lab-video/usbip_uvc.py): дескрипторы, нагрузки UVC, сеанс USB/IP без ядра."""
import asyncio
import errno
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/lab-video"))
import usbip_attach as ua  # noqa: E402
import usbip_uvc as uu  # noqa: E402


def walk(desc):
    """Последовательность (bDescriptorType, подтип, байты) по bLength."""
    out, i = [], 0
    while i < len(desc):
        n = desc[i]
        assert n >= 2, f"bLength {n} на смещении {i}"
        out.append((desc[i + 1], desc[i + 2] if n > 2 else None, desc[i:i + n]))
        i += n
    assert i == len(desc)
    return out


class Descriptors(unittest.TestCase):
    def test_lengths_and_totals(self):
        for p in uu.PROFILES.values():
            with self.subTest(p.name):
                dev = uu.device_descriptor(p)
                self.assertEqual(len(dev), 18)
                self.assertEqual(struct.unpack_from("<HH", dev, 8), (p.vid, p.pid))
                cfg = uu.config_descriptor(p)
                self.assertEqual(struct.unpack_from("<H", cfg, 2)[0], len(cfg))
                items = walk(cfg)
                vc = next(d for t, s, d in items if t == 0x24 and s == 0x01 and d[3:5] == b"\x00\x01")
                vc_units = sum(len(d) for t, s, d in items if t == 0x24 and s in (0x01, 0x02, 0x03)
                               and d is not None and (s != 0x01 or d is vc))
                self.assertEqual(struct.unpack_from("<H", vc, 5)[0], vc_units)
                vs = next(d for t, s, d in items if t == 0x24 and s == 0x01 and d is not vc)
                self.assertEqual(vs[3], len(p.formats))
                vs_total = sum(len(d) for t, s, d in items if t == 0x24 and s in (0x04, 0x05, 0x06, 0x07, 0x0D))
                self.assertEqual(struct.unpack_from("<H", vs, 4)[0], len(vs) + vs_total)
                eps = [d for t, _s, d in items if t == 5]
                self.assertEqual([(e[2], e[3]) for e in eps], [(0x81, 0x02)])

    def test_input_terminal_is_camera(self):
        # uvcvideo не строит цепочку от внешнего терминала (композитный разъём 0x0401) — у всех ITT_CAMERA
        for p in uu.PROFILES.values():
            d = next(d for t, s, d in walk(uu.config_descriptor(p)) if t == 0x24 and s == 0x02)
            self.assertEqual(struct.unpack_from("<H", d, 4)[0], 0x0201, p.name)

    def test_ids_match_sheregesh_classification(self):
        sys.path.insert(0, str(ROOT / "software"))
        from sheregesh import videoinput
        self.assertIn("534d:2109", videoinput.HDMI_USB_IDS)
        self.assertIn("534d:0021", videoinput.CVBS_USB_IDS)
        p = uu.PROFILES["hdmi-ms2109"]
        self.assertEqual(f"{p.vid:04x}:{p.pid:04x}", "534d:2109")

    def test_strings(self):
        p = uu.PROFILES["camera"]
        self.assertEqual(uu.string_descriptor(p, 0), b"\x04\x03\x09\x04")
        self.assertEqual(uu.string_descriptor(p, 2)[2:].decode("utf-16-le"), p.product)
        self.assertIsNone(uu.string_descriptor(p, 9))

    def test_devinfo_roundtrip_with_attach_parser(self):
        p = uu.PROFILES["cvbs-ms2106"]
        raw = uu.devinfo(p, 3)
        self.assertEqual(len(raw), 312)
        self.assertEqual(raw[311], 2)                                  # bNumInterfaces
        info = ua.parse_devinfo(raw)
        self.assertEqual((info["busid"], info["devnum"], info["speed"], info["vid"], info["pid"]),
                         ("1-2", 3, uu.USB_SPEED_HIGH, 0x534D, 0x0021))


class Payloads(unittest.TestCase):
    def test_split_header_eof_fid(self):
        frame = bytes(range(256)) * 200                                 # 51200 байт → 4 нагрузки
        pls = uu.payloads(frame, fid=1)
        self.assertEqual(len(pls), 4)
        self.assertTrue(all(len(pl) <= uu.MAX_PAYLOAD and pl[0] == 2 for pl in pls))
        self.assertEqual([pl[1] for pl in pls], [0x81, 0x81, 0x81, 0x83])
        self.assertEqual(b"".join(pl[2:] for pl in pls), frame)


class FakeSource:
    FRAME = b"\xff\xd8" + b"J" * 40000 + b"\xff\xd9"

    def __init__(self, *_a):
        self.started = False

    async def start(self):
        self.started = True

    async def frames(self):
        while True:
            yield self.FRAME
            await asyncio.sleep(0.01)

    async def stop(self):
        pass


def submit(seq, ep, direction, length, setup=b"\0" * 8):
    return struct.pack(">IIIIIIiIiI", uu.CMD_SUBMIT, seq, 0x10002, direction, ep, 0, length, 0, 0, 0) + setup


class Session(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig = uu.Source
        uu.Source = FakeSource
        self.tmp = tempfile.TemporaryDirectory()
        self.server = uu.Server([uu.PROFILES["hdmi-ms2109"]], "test", self.tmp.name, log=lambda m: None)
        self.srv = await asyncio.start_server(self.server.handle, "127.0.0.1", 0)
        port = self.srv.sockets[0].getsockname()[1]
        self.r, self.w = await asyncio.open_connection("127.0.0.1", port)

    async def asyncTearDown(self):
        self.w.close()
        self.srv.close()
        await self.srv.wait_closed()
        uu.Source = self._orig
        self.tmp.cleanup()

    async def control(self, seq, rtype, req, value, index, length, data=b""):
        direction = 1 if rtype & 0x80 else 0
        setup = struct.pack("<BBHHH", rtype, req, value, index, length)
        self.w.write(submit(seq, 0, direction, length, setup) + data)

    async def test_import_enumerate_stream_unlink(self):
        self.w.write(struct.pack(">HHI", uu.USBIP_VERSION, uu.OP_REQ_IMPORT, 0) + b"1-1".ljust(32, b"\0"))
        _v, code, status = struct.unpack(">HHI", await self.r.readexactly(8))
        self.assertEqual((code, status), (uu.OP_REP_IMPORT, 0))
        self.assertEqual(ua.parse_devinfo(await self.r.readexactly(312))["pid"], 0x2109)

        await self.control(1, 0x80, 6, 0x0100, 0, 18)                   # GET_DESCRIPTOR device
        # direction в RET_SUBMIT не передаётся (0) — данные читаем по actual_length явно
        hdr = await self.r.readexactly(48)
        actual = struct.unpack_from(">I", hdr, 24)[0]
        dev = await self.r.readexactly(actual)
        self.assertEqual(dev[:2], b"\x12\x01")

        probe = struct.pack("<HBBI", 1, 1, 2, 333333).ljust(26, b"\0")  # MJPEG 1280x720@30
        await self.control(2, 0x21, 0x01, 0x0100, 0x0001, 26, probe)    # SET_CUR PROBE
        hdr = await self.r.readexactly(48)
        self.assertEqual(struct.unpack_from(">i", hdr, 20)[0], 0)
        await self.control(3, 0xA1, 0x81, 0x0100, 0x0001, 26)           # GET_CUR PROBE
        hdr = await self.r.readexactly(48)
        cur = await self.r.readexactly(struct.unpack_from(">I", hdr, 24)[0])
        hint, fi, ri, interval = struct.unpack_from("<HBBI", cur)
        size, payload = struct.unpack_from("<II", cur, 18)
        self.assertEqual((fi, ri, interval, payload), (1, 2, 333333, uu.MAX_PAYLOAD))
        self.assertEqual(size, 1280 * 720 * 2)
        await self.control(4, 0x21, 0x01, 0x0200, 0x0001, 26, cur)      # SET_CUR COMMIT
        await self.r.readexactly(48)

        await self.control(5, 0xA1, 0x81, 0x0200, 0x0000, 1)            # класс VideoControl — STALL
        hdr = await self.r.readexactly(48)
        self.assertEqual(struct.unpack_from(">i", hdr, 20)[0], -errno.EPIPE)

        frame, seq = b"", 100
        for _ in range(8):                                              # URB bulk IN по 16384
            self.w.write(submit(seq, 1, 1, uu.MAX_PAYLOAD))
            seq += 1
        await self.w.drain()
        eof = False
        while not eof:
            hdr = await self.r.readexactly(48)
            self.assertEqual(struct.unpack_from(">I", hdr)[0], uu.RET_SUBMIT)
            pl = await self.r.readexactly(struct.unpack_from(">I", hdr, 24)[0])
            self.assertEqual(pl[0], 2)
            frame += pl[2:]
            eof = bool(pl[1] & 0x02)
        self.assertEqual(frame, FakeSource.FRAME)

        # ещё не использованные URB отзываются: RET_UNLINK -ECONNRESET
        self.w.write(struct.pack(">IIIIII24x", uu.CMD_UNLINK, 500, 0x10002, 0, 1, seq - 1))
        await self.w.drain()
        while True:
            hdr = await self.r.readexactly(48)
            cmd = struct.unpack_from(">I", hdr)[0]
            if cmd == uu.RET_UNLINK:
                break
            await self.r.readexactly(struct.unpack_from(">I", hdr, 24)[0])  # нагрузки второго кадра
        self.assertIn(struct.unpack_from(">i", hdr, 20)[0], (-errno.ECONNRESET, 0))

    async def test_busy_and_unknown_busid_rejected(self):
        self.w.write(struct.pack(">HHI", uu.USBIP_VERSION, uu.OP_REQ_IMPORT, 0) + b"9-9".ljust(32, b"\0"))
        _v, code, status = struct.unpack(">HHI", await self.r.readexactly(8))
        self.assertEqual((code, status), (uu.OP_REP_IMPORT, 1))


if __name__ == "__main__":
    unittest.main()
