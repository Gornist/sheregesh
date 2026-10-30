# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Задержка «стекло–стекло» (tools/probe/g2g_latency.py): чтение штрихкода кадра при масштабировании и шуме,
отказ на чужом кадре, смещение часов по пробе с наименьшим RTT, переход суток."""
import importlib.util
import random
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("g2g_latency", ROOT / "tools/probe/g2g_latency.py")
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


def render(w, h, value, noise=0, rng=None):
    """Кадр GRAY8 по раскладке hdmi_testpattern.Canvas (фон, рамка, клетки)."""
    img = bytearray([20] * (w * h))
    cell = min(w, h) * 0.035
    x0, y0 = w - cell * (g.BITS_W + 2), h - cell * (g.BITS_H + 2)

    def rect(x, y, rw, rh, v):
        for yy in range(int(y), int(y + rh)):
            for xx in range(int(x), int(x + rw)):
                if 0 <= xx < w and 0 <= yy < h:
                    img[yy * w + xx] = v
    rect(x0 - cell * 0.5, y0 - cell * 0.5, cell * (g.BITS_W + 1), cell * (g.BITS_H + 1), 255)
    rect(x0 - cell * 0.25, y0 - cell * 0.25, cell * (g.BITS_W + 0.5), cell * (g.BITS_H + 0.5), 0)
    for i in range(32):
        if value >> (31 - i) & 1:
            rect(x0 + (i % g.BITS_W) * cell, y0 + (i // g.BITS_W) * cell, cell, cell, 255)
    if noise:
        rng = rng or random.Random(1)
        img = bytearray(max(0, min(255, v + rng.randint(-noise, noise))) for v in img)
    return bytes(img)


class G2GTest(unittest.TestCase):
    def test_decode_at_capture_and_screen_sizes(self):
        for w, h in ((1280, 720), (1920, 1080), (640, 360)):
            for value in (0, 1, 45_678_901, g.DAY_MS - 1):
                self.assertEqual(g.decode(render(w, h, value, noise=30), w, h), value, (w, h, value))

    def test_foreign_frame_is_not_read(self):
        self.assertIsNone(g.decode(bytes([20] * (1280 * 720)), 1280, 720))        # нет рамки — не наш кадр
        self.assertIsNone(g.decode(bytes([255] * (1280 * 720)), 1280, 720))

    def test_stride_padding(self):
        w, h, pad = 1280, 720, 64
        img = render(w, h, 12_345)
        padded = b"".join(img[y * w:(y + 1) * w] + bytes(pad) for y in range(h))
        self.assertEqual(g.decode(padded, w, h, w + pad), 12_345)

    def test_rgb_frame_green_channel(self):
        w, h = 1280, 720
        gray = render(w, h, 31_337_000)
        rgb = bytes(b for v in gray for b in (0, v, 0))                     # как RGB из v4l2convert пульта
        self.assertEqual(g.decode(rgb, w, h, w * 3, bpp=3, chan=1), 31_337_000)

    def test_offset_uses_min_rtt_probe(self):
        # сервер впереди на 0,5 с; у второй пробы RTT наименьший
        samples = [(100.0, 100.52, 100.03), (200.0, 200.501, 200.002), (300.0, 300.53, 300.06)]
        offset, rtt = g.best_offset(samples)
        self.assertAlmostEqual(offset, 0.5, places=3)
        self.assertAlmostEqual(rtt, 0.002, places=6)

    def test_latency_with_offset_and_day_wrap(self):
        t = 1_793_400_000.250                                  # время получения на pi1
        code = g.ms_of_day(t + 0.5) - 80                       # кадр показан 80 мс назад по часам devbox
        self.assertEqual(g.latency_ms(t, 0.5, code % g.DAY_MS), 80)
        self.assertEqual(g.latency_ms(86_400.010, 0.0, g.DAY_MS - 30), 40)          # полночь между кадром и приёмом
        self.assertIsNone(g.latency_ms(t, 0.5, (code + 10_000) % g.DAY_MS))      # «из будущего» — неверное чтение

    def test_summary(self):
        s = g.summarize([60, 70, 80, 90, 100], 2, 0.0123, 0.0004, 10)
        self.assertEqual((s["frames_read"], s["frames_unread"], s["latency_ms"]["p50"]), (5, 2, 80))
        self.assertEqual(s["clock"]["error_bound_ms"], 0.2)


if __name__ == "__main__":
    unittest.main()
