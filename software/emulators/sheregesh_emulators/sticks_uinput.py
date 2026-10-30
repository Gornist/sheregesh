# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор стиков пульта: виртуальное устройство ввода Linux через /dev/uinput (испытания S7/S8 без аппаратуры).

Свои аппаратные стики пульта — основной вариант изделия; внешний USB-пульт (Boxer) — частный случай. Пульт находит
устройство так же, как настоящее: первое eventN с осями ABS_X и ABS_Y (sticks.find_device) — и читает его тем же
EvdevJoystick. Профиль по умолчанию повторяет Boxer из S7: 8 осей ABS_X…ABS_RUDDER, 0…2047, 24 кнопки.

  sudo python3 -m sheregesh_emulators.sticks_uinput --axes 0,-500,800,250,0,0,-1000,1000 [--buttons 0x5]
      [--sweep КАНАЛ:ПЕРИОД_С] [--max-seconds 600] [--name …]

Значения осей — в единицах пульта (−1000…1000; sticks.normalize делает обратное). Устройство живёт, пока жив
процесс: выход — по закрытию stdin (испытание запускает эмулятор по SSH и закрывает канал), по --max-seconds или
по SIGTERM; устройство удаляется (UI_DEV_DESTROY) и при аварийном выходе (ядро закрывает /dev/uinput).
Строка «READY <имя>» в stdout — устройство создано. Нужен root (/dev/uinput — 0600 root). Только стандартная
библиотека: эмулятор передаётся на плату текстом, без установки."""
import argparse
import fcntl
import math
import os
import select
import signal
import struct
import sys
import time

# linux/uinput.h, linux/input.h, linux/input-event-codes.h
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BUS_USB = 0x03
ABS_CNT = 64
UINPUT_MAX_NAME_SIZE = 80
EVENT_FMT = "llHHi"                                   # struct input_event (64-битная платформа: 24 байта)
USER_DEV_FMT = f"{UINPUT_MAX_NAME_SIZE}s4HI{ABS_CNT}i{ABS_CNT}i{ABS_CNT}i{ABS_CNT}i"   # struct uinput_user_dev

AXES = 8                                              # ABS_X…ABS_RUDDER (коды 0…7) — каналы 1–8 пульта
BUTTONS = tuple(range(0x120, 0x130)) + tuple(range(0x2c0, 0x2c8))   # BTN_JOYSTICK… + BTN_TRIGGER_HAPPY1…8 = 24
BOXER = {"name": "Sheregesh sticks emulator (Boxer profile)", "vendor": 0x1209, "product": 0x4f54,
         "min": 0, "max": 2047, "fuzz": 0, "flat": 0}


def to_raw(v, lo, hi):
    """Единицы пульта −1000…1000 → значение оси lo…hi (обратное sticks.normalize, с округлением)."""
    v = max(-1000, min(1000, int(v)))
    return lo + round((v + 1000) * (hi - lo) / 2000)


def user_dev(profile):
    """struct uinput_user_dev: имя, input_id (bus, vendor, product, version), ff_effects_max, absmax/min/fuzz/flat."""
    absmax, absmin, fuzz, flat = ([0] * ABS_CNT for _ in range(4))
    for c in range(AXES):
        absmax[c], absmin[c], fuzz[c], flat[c] = profile["max"], profile["min"], profile["fuzz"], profile["flat"]
    name = profile["name"].encode()[:UINPUT_MAX_NAME_SIZE - 1]
    return struct.pack(USER_DEV_FMT, name, BUS_USB, profile["vendor"], profile["product"], 1, 0,
                       *absmax, *absmin, *fuzz, *flat)


def event(etype, code, value, now=None):
    now = time.time() if now is None else now
    return struct.pack(EVENT_FMT, int(now), int((now % 1) * 1e6), etype, code, value)


class UinputSticks:
    """Виртуальное устройство. ioctl, write и open подменяются в испытаниях (на macOS uinput нет)."""

    def __init__(self, profile=BOXER, path="/dev/uinput", opener=os.open, ioctl=fcntl.ioctl, write=os.write):
        self.p, self._ioctl, self._write = profile, ioctl, write
        self.fd = opener(path, os.O_WRONLY | os.O_NONBLOCK)
        self.created = False
        self.axes = [0] * AXES
        self.buttons = 0
        try:
            self._ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            self._ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            for code in BUTTONS:
                self._ioctl(self.fd, UI_SET_KEYBIT, code)
            for code in range(AXES):
                self._ioctl(self.fd, UI_SET_ABSBIT, code)
            self._write(self.fd, user_dev(profile))
            self._ioctl(self.fd, UI_DEV_CREATE)
            self.created = True
        except OSError:
            self.close()
            raise

    def set(self, axes=None, buttons=None):
        """Новое состояние одним кадром (SYN_REPORT). axes — 8 значений −1000…1000, buttons — битовая маска 24 кнопок."""
        data = b""
        if axes is not None:
            for c, v in enumerate(axes[:AXES]):
                self.axes[c] = int(v)
                data += event(EV_ABS, c, to_raw(v, self.p["min"], self.p["max"]))
        if buttons is not None:
            for i, code in enumerate(BUTTONS):
                data += event(EV_KEY, code, (buttons >> i) & 1)
            self.buttons = buttons
        self._write(self.fd, data + event(EV_SYN, SYN_REPORT, 0))

    def close(self):
        if self.fd is None:
            return
        try:
            if self.created:
                self._ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        finally:
            os.close(self.fd) if isinstance(self.fd, int) and self.fd >= 0 else None
            self.fd, self.created = None, False


def parse_axes(text):
    vals = [int(x) for x in text.split(",")]
    if len(vals) != AXES or any(not -1000 <= v <= 1000 for v in vals):
        raise argparse.ArgumentTypeError(f"нужно {AXES} значений −1000…1000 через запятую")
    return vals


def parse_sweep(text):
    ch, period = text.split(":")
    ch, period = int(ch), float(period)
    if not 1 <= ch <= AXES or period <= 0:
        raise argparse.ArgumentTypeError(f"КАНАЛ 1…{AXES}:ПЕРИОД_С > 0")
    return ch - 1, period


def main(argv=None, sticks_factory=UinputSticks, stdin=None, clock=time.monotonic):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--axes", type=parse_axes, default=[0] * AXES, help="8 значений −1000…1000 (каналы 1–8)")
    ap.add_argument("--buttons", type=lambda s: int(s, 0), default=0, help="маска нажатых кнопок (24 бита)")
    ap.add_argument("--sweep", type=parse_sweep, help="КАНАЛ:ПЕРИОД — синус по каналу на всю шкалу")
    ap.add_argument("--max-seconds", type=float, default=600.0)
    ap.add_argument("--name", default=BOXER["name"])
    a = ap.parse_args(argv)
    stdin = sys.stdin if stdin is None else stdin
    stop = []
    prev = signal.signal(signal.SIGTERM, lambda *_: stop.append("SIGTERM"))
    s = sticks_factory({**BOXER, "name": a.name})
    try:
        s.set(a.axes, a.buttons)
        print(f"READY {a.name}", flush=True)
        t0 = clock()
        while not stop and clock() - t0 < a.max_seconds:
            r, _, _ = select.select([stdin], [], [], 0.02)
            if r and not stdin.readline():                      # stdin закрыт — испытание закончилось
                break
            if a.sweep:
                ch, period = a.sweep
                axes = list(a.axes)
                axes[ch] = round(1000 * math.sin(2 * math.pi * (clock() - t0) / period))
                s.set(axes)
        return 0
    finally:
        s.close()
        signal.signal(signal.SIGTERM, prev)


if __name__ == "__main__":
    sys.exit(main())
