#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Задержка видеоконвейера по стадиям на плате: время между выходами помеченных элементов для одного и того же кадра.

Трассировщик GStreamer latency на pi2 вешает конвейеры с v4l2h264enc, поэтому здесь — пробники на src-пад каждого
элемента с именем s0, s1, … (часы monotonic), кадры сопоставляются по PTS. Стадия «sK» — путь от выхода s(K-1) до
выхода sK. Конвейер запускается отдельно от служб стенда; источник — videotestsrc (живой), устройства не открываются.
Не входит: захват устройства (буфер конвертера, USB), сеть и показ.

На плате (интерпретатор с python3-gi, жёсткий тайм-аут обязателен):
  timeout -k 2 40 python3 video_stage_latency.py --seconds 20 '<описание gst с элементами name=s0 … name=sN>'
Вывод — JSON: по стадиям p50/p95/max мс и число кадров."""
import argparse
import json
import statistics
import sys
import time


def summary(values):
    v = sorted(values)
    if not v:
        return {"p50": None, "p95": None, "max": None, "n": 0}
    return {"p50": round(statistics.median(v), 2), "p95": round(v[min(len(v) - 1, int(len(v) * 0.95))], 2),
            "max": round(v[-1], 2), "n": len(v)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pipeline")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--warmup", type=float, default=3.0, help="первые секунды не учитываются")
    a = ap.parse_args(argv)
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
    Gst.init(None)
    pipe = Gst.parse_launch(a.pipeline)
    names = []
    while pipe.get_by_name(f"s{len(names)}") is not None:
        names.append(f"s{len(names)}")
    if len(names) < 2:
        sys.exit("нужны элементы name=s0 и name=s1 (и далее)")
    seen = {n: {} for n in names}                          # PTS → время выхода
    t0 = time.monotonic()

    def probe(_pad, info, name):
        buf = info.get_buffer()
        if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE and time.monotonic() - t0 >= a.warmup:
            seen[name].setdefault(buf.pts, time.monotonic())
        return Gst.PadProbeReturn.OK

    for n in names:
        pipe.get_by_name(n).get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, probe, n)
    loop = GLib.MainLoop()
    err = []

    def on_msg(_bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            e, dbg = msg.parse_error()
            err.append(f"{e.message} | {dbg}"[:300])
            loop.quit()
        elif msg.type == Gst.MessageType.EOS:
            loop.quit()

    bus = pipe.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_msg)
    GLib.timeout_add(int(a.seconds * 1000), loop.quit)
    pipe.set_state(Gst.State.PLAYING)
    loop.run()
    pipe.set_state(Gst.State.NULL)
    out = {"stages": {}, "error": err[0] if err else ""}
    for prev, cur in zip(names, names[1:]):
        d = [(seen[cur][p] - t) * 1000 for p, t in seen[prev].items() if p in seen[cur]]
        out["stages"][f"{prev}->{cur}"] = summary(d)
    first, last = names[0], names[-1]
    out["total"] = summary([(seen[last][p] - t) * 1000 for p, t in seen[first].items() if p in seen[last]])
    out["frames"] = {n: len(seen[n]) for n in names}
    print(json.dumps(out, ensure_ascii=False))
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
