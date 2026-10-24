# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подставной генератор для испытаний видеоканала без GStreamer: RTP H.264 в stdout с кадрированием RFC 4571.

Каждый кадр — STAP-A с SPS (1280×720, baseline) + 2 пакета «среза»; маркер на последнем. 30 кадров/с.
  python3 fake_rtp_sender.py [кадров/с] [ширина] [высота]
"""
import os
import sys
import time


def bits_to_bytes(bits):
    bits += "1"                                    # rbsp_stop_one_bit
    bits += "0" * (-len(bits) % 8)
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))


def ue(v):
    v += 1
    n = v.bit_length()
    return "0" * (n - 1) + format(v, "b")


def sps(width, height):
    w_mbs, h_mbs = (width + 15) // 16, (height + 15) // 16
    crop_b = (h_mbs * 16 - height) // 2
    bits = (format(66, "08b") + "11000000" + format(31, "08b") + ue(0) + ue(0) + ue(2) + ue(1) + "0"
            + ue(w_mbs - 1) + ue(h_mbs - 1) + "1" + "1")
    bits += ("1" + ue(0) + ue(0) + ue(0) + ue(crop_b)) if crop_b else "0"
    bits += "0"                                     # vui_parameters_present_flag
    return b"\x67" + bits_to_bytes(bits)


def rtp(seq, ts, marker, payload):
    return bytes([0x80, (0x80 if marker else 0) | 96]) + seq.to_bytes(2, "big") + ts.to_bytes(4, "big") + \
        (0x5348).to_bytes(4, "big") + payload


def main():
    fps = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    w, h = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1280, 720)
    s = sps(w, h)
    pps = b"\x68\xce\x38\x80"
    stap = b"\x18" + len(s).to_bytes(2, "big") + s + len(pps).to_bytes(2, "big") + pps
    seq, out = 0, sys.stdout.buffer
    t0 = time.monotonic()
    n = 0
    while True:
        ts = int(n * 90000 / fps) & 0xFFFFFFFF
        for i, payload in enumerate((stap, b"\x65" + os.urandom(1200), b"\x65" + os.urandom(800))):
            pkt = rtp(seq & 0xFFFF, ts, i == 2, payload)
            out.write(len(pkt).to_bytes(2, "big") + pkt)
            seq += 1
        out.flush()
        n += 1
        time.sleep(max(0.0, t0 + n / fps - time.monotonic()))


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
