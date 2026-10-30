# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор стиков (sheregesh_emulators.sticks_uinput): создание устройства uinput и сквозной путь — байты событий
эмулятора, прочитанные тем же EvdevJoystick пульта, дают заданные оси и кнопки."""
import io
import struct
import unittest

from sheregesh_console import sticks as S
from sheregesh_emulators import sticks_uinput as U


class FakeUinput:
    def __init__(self):
        self.ioctls, self.writes, self.closed = [], [], False

    def opener(self, path, flags):
        self.path = path
        return -7                                         # не настоящий fd: close() его не трогает

    def ioctl(self, fd, req, arg=0):
        self.ioctls.append((req, arg))
        return 0

    def write(self, fd, data):
        self.writes.append(bytes(data))
        return len(data)


def make(fake, profile=U.BOXER):
    return U.UinputSticks(profile, opener=fake.opener, ioctl=fake.ioctl, write=fake.write)


class Joy:
    """Ответы ioctl EvdevJoystick для устройства, которое создал эмулятор (как ядро по uinput_user_dev)."""

    def __init__(self, profile):
        self.p = profile

    def __call__(self, fd, req, buf):
        n = len(buf)
        if req == S.EVIOCGNAME(256):
            return self.p["name"].encode().ljust(n, b"\0")
        if req == S.EVIOCGBIT(S.EV_ABS, S.ABS_BYTES):
            return bytes([0xff]).ljust(n, b"\0")              # ABS 0…7
        if req == S.EVIOCGBIT(S.EV_KEY, S.KEY_BYTES):
            raw = bytearray(n)
            for code in U.BUTTONS:
                raw[code // 8] |= 1 << (code % 8)
            return bytes(raw)
        for code in range(U.AXES):
            if req == S.EVIOCGABS(code):
                lo, hi = self.p["min"], self.p["max"]
                return struct.pack(S.ABSINFO_FMT, (lo + hi) // 2, lo, hi, 0, 0, 0)
        if req == S.EVIOCGKEY(S.KEY_BYTES):
            return bytes(n)
        raise OSError("ioctl")


class SticksUinputTest(unittest.TestCase):
    def test_device_setup_matches_boxer_profile(self):
        f = FakeUinput()
        s = make(f)
        self.assertEqual(f.path, "/dev/uinput")
        self.assertIn((U.UI_SET_EVBIT, U.EV_ABS), f.ioctls)
        self.assertEqual([a for r, a in f.ioctls if r == U.UI_SET_ABSBIT], list(range(8)))
        self.assertEqual(len([a for r, a in f.ioctls if r == U.UI_SET_KEYBIT]), 24)   # как Boxer в S7 (ST-01)
        self.assertEqual(f.ioctls[-1][0], U.UI_DEV_CREATE)
        dev = struct.unpack(U.USER_DEV_FMT, f.writes[0])
        self.assertTrue(dev[0].startswith(b"Sheregesh sticks emulator"))
        absmax, absmin = dev[6:6 + 64], dev[6 + 64:6 + 128]
        self.assertEqual((absmin[:8], absmax[:8]), ((0,) * 8, (2047,) * 8))
        s.close()
        self.assertEqual(f.ioctls[-1][0], U.UI_DEV_DESTROY)

    def test_values_round_trip_through_console_reader(self):
        f = FakeUinput()
        s = make(f)
        want = [0, -500, 800, 250, -1000, 1000, -333, 667]
        s.set(want, buttons=0b101)
        joy = S.EvdevJoystick("/dev/input/event9", ioctl=Joy(U.BOXER), fd=-1)
        self.assertEqual(joy.name, U.BOXER["name"])
        self.assertTrue(joy.feed(f.writes[-1]))
        got = joy.channels()
        self.assertTrue(all(abs(g - w) <= 1 for g, w in zip(got, want)), (got, want))   # 0…2047: шаг ≈ 1 ед.
        self.assertEqual(joy.pressed, 0b101)

    def test_raw_mapping_is_inverse_of_normalize(self):
        for v in range(-1000, 1001, 7):
            raw = U.to_raw(v, 0, 2047)
            self.assertLessEqual(abs(S.normalize(raw, 0, 2047) - v), 1)
        self.assertEqual((U.to_raw(-5000, 0, 2047), U.to_raw(5000, 0, 2047)), (0, 2047))

    def test_arguments_validated(self):
        with self.assertRaises(Exception):
            U.parse_axes("1,2,3")
        with self.assertRaises(Exception):
            U.parse_axes("0,0,0,0,0,0,0,1001")
        self.assertEqual(U.parse_sweep("2:4"), (1, 4.0))

    def test_main_exits_on_stdin_close_and_destroys_device(self):
        f = FakeUinput()
        out = io.StringIO()
        import contextlib
        r, w = __import__("os").pipe()
        __import__("os").close(w)                                     # испытание закрыло канал — выход
        with open(r) as stdin, contextlib.redirect_stdout(out):
            rc = U.main(["--axes", "0,0,0,0,0,0,0,0", "--max-seconds", "5"], sticks_factory=lambda p: make(f, p),
                        stdin=stdin)
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("READY "))
        self.assertEqual(f.ioctls[-1][0], U.UI_DEV_DESTROY)


if __name__ == "__main__":
    unittest.main()


class S8EmuModeTest(unittest.TestCase):
    def test_s8_emulator_command_carries_module_source_as_root(self):
        import base64
        import re
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root / "tools/probe"))
        import s8_crsf_accept as S8
        emu = S8.SticksEmu("10.0.0.5", [0, -500, 800, 250, -1000, 1000, -333, 667])
        cmd = emu.command()
        self.assertEqual(cmd[-2], "root@10.0.0.5")                     # /dev/uinput — только root
        b64 = re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", cmd[-1]).group(1)
        self.assertEqual(base64.b64decode(b64), S8.EMU_SRC.read_bytes())   # на плате ничего не ставится
        self.assertIn("--axes 0,-500,800,250,-1000,1000,-333,667", cmd[-1])
        self.assertEqual(S8.EMU_STICKS_NAME, U.BOXER["name"])
        with self.assertRaises(Exception):
            S8.parse_emu_axes("0,0")
