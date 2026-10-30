#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тестовая картинка для HDMI-источника стенда: devbox (GTX 1650) → HDMI→USB (MS2130) → pi2 → пульт.

Полноэкранное окно GTK 4 (сеанс Wayland пользователя devbox, без дополнительных пакетов) рисует на каждый кадр
дисплея (часы кадров GTK):
  • счётчик кадров и время devbox HH:MM:SS.mmm — крупно, для человека;
  • полоса, проходящая ширину экрана за 1 с — рывки и пропуски кадров видны глазом;
  • штрихкод времени: 32 бита миллисекунд от полуночи (UTC) клетками 8×4 в правом нижнем углу, белое — 1, плюс
    рамка-ориентир — для автоматического замера задержки «стекло–стекло» по кадрам на пульте.

  WAYLAND_DISPLAY=wayland-0 XDG_RUNTIME_DIR=/run/user/1000 python3 hdmi_testpattern.py [--seconds N]
Запуск и остановка с Mac — tools/lab-video/hdmi_source.sh. Время — часы devbox (синхронизированы по NTP)."""
import argparse
import datetime as dt
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk, Pango  # noqa: E402

BITS_W, BITS_H = 8, 4                      # 32 бита
SWEEP_S = 1.0


def ms_of_day(now=None):
    now = dt.datetime.now(dt.timezone.utc) if now is None else now
    return (now.hour * 3600 + now.minute * 60 + now.second) * 1000 + now.microsecond // 1000


def bits(value):
    """32 бита value, старший первым (строки сверху вниз, слева направо)."""
    return [(value >> (31 - i)) & 1 for i in range(32)]


def rgba(r, g, b):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, 1.0
    return c


WHITE, BLACK, BG, BAR = rgba(1, 1, 1), rgba(0, 0, 0), rgba(0.08, 0.08, 0.10), rgba(0.95, 0.75, 0.1)


class Canvas(Gtk.Widget):
    """Отрисовка через Gtk.Snapshot (прямоугольники и текст Pango) — без pycairo (на devbox нет python3-gi-cairo)."""

    def __init__(self):
        super().__init__()
        self.frames, self.t0 = 0, time.monotonic()

    def rect(self, snap, color, x, y, w, h):
        snap.append_color(color, Graphene.Rect().init(x, y, w, h))

    def text(self, snap, s, x, y, size):
        layout = self.create_pango_layout(s)
        layout.set_font_description(Pango.FontDescription.from_string(f"monospace {int(size)}px"))
        snap.save()
        snap.translate(Graphene.Point().init(x, y))
        snap.append_layout(layout, WHITE)
        snap.restore()

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        now = dt.datetime.now(dt.timezone.utc)
        self.rect(snap, BG, 0, 0, w, h)
        x = ((time.monotonic() - self.t0) % SWEEP_S) / SWEEP_S * w        # полоса: ширина экрана за SWEEP_S
        self.rect(snap, BAR, x - w * 0.01, 0, w * 0.02, h * 0.62)
        self.text(snap, now.astimezone().strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}",
                  w * 0.05, h * 0.64, h * 0.11)
        self.text(snap, f"кадр {self.frames}   devbox → HDMI → pi2 → пульт", w * 0.05, h * 0.82, h * 0.05)
        cell = min(w, h) * 0.035              # штрихкод: рамка + 8×4 клеток; без округления — раскладка строго
        #                                       масштабируется при захвате (g2g_latency.geometry читает её так же)
        x0, y0 = w - cell * (BITS_W + 2), h - cell * (BITS_H + 2)
        self.rect(snap, WHITE, x0 - cell * 0.5, y0 - cell * 0.5, cell * (BITS_W + 1), cell * (BITS_H + 1))
        self.rect(snap, BLACK, x0 - cell * 0.25, y0 - cell * 0.25, cell * (BITS_W + 0.5), cell * (BITS_H + 0.5))
        for i, b in enumerate(bits(ms_of_day(now))):
            if b:
                self.rect(snap, WHITE, x0 + (i % BITS_W) * cell, y0 + (i // BITS_W) * cell, cell, cell)


class Pattern(Gtk.ApplicationWindow):
    def __init__(self, app, seconds):
        super().__init__(application=app, title="sheregesh-hdmi-testpattern")
        self.t0, self.seconds = time.monotonic(), seconds
        self.canvas = Canvas()
        self.set_child(self.canvas)
        self.fullscreen()
        self.add_tick_callback(self.tick)

    def tick(self, _widget, _clock):
        self.canvas.frames += 1
        self.canvas.queue_draw()
        if self.seconds and time.monotonic() - self.t0 > self.seconds:
            self.get_application().quit()
        return GLib.SOURCE_CONTINUE


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=0, help="выход через N с (0 — до остановки)")
    a = ap.parse_args(argv)
    app = Gtk.Application(application_id="org.sheregesh.HdmiTestPattern")
    app.connect("activate", lambda ap_: Pattern(ap_, a.seconds).present())
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
