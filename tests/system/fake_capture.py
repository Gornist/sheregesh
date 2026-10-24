# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подставной процесс захвата для испытаний видеовхода S3.3 без GStreamer и устройств.

Пишет кадры RGBx 640×360 в stdout с частотой FPS, как `gst-launch-1.0 v4l2src … ! fdsink`.
  fake_capture.py PIDFILE FRAMES AFTER [FPS]
FRAMES — сколько кадров (-1 — бесконечно); AFTER — что делать потом: exit (код 1, как v4l2src при отключении
устройства), hang (процесс жив, кадров нет — зависшее устройство), closeout (stdout закрыт, процесс жив — обрыв
канала кадров) или hang-child (как hang, но ещё и порождён внук в той же группе процессов — как элементы GStreamer
с собственными процессами; его PID тоже пишется в PIDFILE). Каждый запуск дописывает свой PID в PIDFILE.
"""
import os
import subprocess
import sys
import time

W, H = 640, 360

pidfile, frames, after = sys.argv[1], int(sys.argv[2]), sys.argv[3]
fps = float(sys.argv[4]) if len(sys.argv) > 4 else 30.0
with open(pidfile, "a") as f:
    f.write(f"{os.getpid()}\n")
frame = bytes([40, 160, 220, 255]) * (W * H)
out = sys.stdout.buffer
n = 0
try:
    while frames < 0 or n < frames:
        out.write(frame)
        out.flush()
        n += 1
        time.sleep(1 / fps)
except BrokenPipeError:
    sys.exit(0)
if after == "closeout":
    out.close()
    try:
        os.close(1)
    except OSError:
        pass
    time.sleep(3600)
if after == "hang-child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
    with open(pidfile, "a") as f:
        f.write(f"{child.pid}\n")
    time.sleep(3600)
if after == "hang":
    time.sleep(3600)
sys.exit(1)
