# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подставной конвейер видеоприёма для испытаний без GStreamer и устройств: RTP (RFC 4571) в stdout, как
`gst-launch-1.0 … ! rtpstreampay ! fdsink fd=1`, с поведением, которое меняется на ходу через управляющий файл.

  fake_ingest_sender.py CTL PIDFILE [кадров/с] [h264|jpeg] [ширина] [высота]

CTL перечитывается перед каждым кадром:
  run    кадры идут (H.264: STAP-A с SPS + 2 среза; JPEG: заголовок RFC 2435 с размером), маркер на последнем пакете;
  pause  процесс жив, кадров нет (сигнал пропал: кабель HDMI/CVBS вынут, камера RTSP молчит);
  exit   ошибка в stderr и код 1 — как v4l2src при отключении USB (ENODEV) или rtspsrc при обрыве.
Каждый запуск дописывает свой PID в PIDFILE.
"""
import os
import sys
import time

from fake_rtp_sender import rtp, sps


def jpeg_packet(w, h):
    # type-specific, fragment offset 0, type 1, Q 50, width/8, height/8 + немного «данных»
    return bytes([0, 0, 0, 0, 1, 50, w // 8, h // 8]) + os.urandom(600)


def main():
    ctl, pidfile = sys.argv[1], sys.argv[2]
    fps = float(sys.argv[3]) if len(sys.argv) > 3 else 30.0
    codec = sys.argv[4] if len(sys.argv) > 4 else "h264"
    w, h = (int(sys.argv[5]), int(sys.argv[6])) if len(sys.argv) > 6 else (1280, 720)
    with open(pidfile, "a") as f:
        f.write(f"{os.getpid()}\n")
    s = sps(w, h)
    pps = b"\x68\xce\x38\x80"
    stap = b"\x18" + len(s).to_bytes(2, "big") + s + len(pps).to_bytes(2, "big") + pps
    out = sys.stdout.buffer
    seq, n = 0, 0
    while True:
        try:
            mode = open(ctl).read().strip()
        except OSError:
            mode = "run"
        if mode == "exit":
            sys.stderr.write("ERROR: from element /GstPipeline:pipeline0/GstV4l2Src:v4l2src0: "
                             "Could not read from resource.\n")
            sys.stderr.flush()
            sys.exit(1)
        if mode == "run":
            ts = int(n * 90000 / fps) & 0xFFFFFFFF
            if codec == "jpeg":
                pkts = [(26, jpeg_packet(w, h))]
            else:
                pkts = [(96, stap), (96, b"\x65" + os.urandom(900)), (96, b"\x65" + os.urandom(500))]
            for i, (pt, payload) in enumerate(pkts):
                pkt = bytearray(rtp(seq & 0xFFFF, ts, i == len(pkts) - 1, payload))
                pkt[1] = (pkt[1] & 0x80) | pt
                out.write(len(pkt).to_bytes(2, "big") + bytes(pkt))
                seq += 1
            out.flush()
            n += 1
        time.sleep(1 / fps)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
