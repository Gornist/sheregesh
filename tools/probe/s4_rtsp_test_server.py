#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тестовый RTSP-источник стенда S4 (без камер и захватов): videotestsrc + timeoverlay/clockoverlay → RTP → RTSP.

Нужны только python3 (стандартная библиотека) и gst-launch-1.0 — есть на обеих платах (на pi2 нет python3-gi,
gst-rtsp-server нет нигде). Пакеты RTP даёт настоящий GStreamer (… ! rtpstreampay ! fdsink fd=1, RFC 4571 в stdout),
сервер перекладывает их в RTSP interleaved («$ канал длина», RTP/AVP/TCP) — так подключается RTSPSource блока
(rtspsrc protocols=tcp). Каждый клиент получает свой генератор; кадр несёт таймкод потока (timeoverlay), часы
(clockoverlay) и метку --label — видно задержку и замирание.

  python3 tools/probe/s4_rtsp_test_server.py [--bind 0.0.0.0] [--port 8554] [--codec jpeg|h264]
                                             [--size 640x480] [--fps 25] [--label CAM-A] [--outage 30:10]

  --codec jpeg  (по умолчанию) jpegenc → rtpjpegpay; в video-input.json: {"url": …, "codec": "mjpeg"}
  --codec h264  v4l2h264enc (Pi 4), иначе x264enc / openh264enc → rtph264pay; {"url": …, "codec": "h264"}
  --outage P:D  каждые P с сервер «падает» на D с (слушающий сокет и сеансы закрыты, генераторы остановлены) —
                проверка потери/возврата источника на блоке (connect-error → streaming) без ручной остановки
  --check       только проверить элементы GStreamer и вывести конвейер

Адрес: rtsp://<адрес платы>:<port>/cam (путь любой). Остановка — Ctrl+C / SIGTERM (генераторы снимаются).
"""
import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

ENCODERS = (("v4l2h264enc", ["v4l2h264enc", "extra-controls=controls,repeat_sequence_header=1", "!",
                             "video/x-h264,level=(string)4", "!"]),
            ("x264enc", ["x264enc", "tune=zerolatency", "speed-preset=ultrafast", "key-int-max=30", "!"]),
            ("openh264enc", ["openh264enc", "!"]))


def has_element(name):
    if not shutil.which("gst-inspect-1.0"):
        return False
    return subprocess.run(["gst-inspect-1.0", "--exists", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def producer(codec="jpeg", width=640, height=480, fps=25, label="SHEREGESH RTSP TEST", have=has_element):
    """Команда генератора: RTP в RFC 4571 в stdout. Надписи — только если есть элементы pango."""
    cmd = ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true", "pattern=ball", "!",
           f"video/x-raw,width={width},height={height},framerate={fps}/1", "!"]
    if have("timeoverlay"):
        cmd += ["timeoverlay", "halignment=left", "valignment=bottom", "font-desc=Sans 20", "!"]
    if have("clockoverlay"):
        cmd += ["clockoverlay", "halignment=right", "valignment=bottom", "time-format=%H:%M:%S", "font-desc=Sans 20",
                "!"]
    if label and have("textoverlay"):
        cmd += ["textoverlay", f"text={label}", "valignment=top", "font-desc=Sans 20", "!"]
    cmd += ["videoconvert", "!", "video/x-raw,format=I420", "!"]
    if codec == "jpeg":
        cmd += ["jpegenc", "quality=80", "!", "rtpjpegpay", "pt=26", "!"]
    else:
        enc = next((args for name, args in ENCODERS if have(name)), None)
        if enc is None:
            raise RuntimeError("no H.264 encoder (v4l2h264enc / x264enc / openh264enc); use --codec jpeg")
        cmd += [*enc, "h264parse", "config-interval=-1", "!", "rtph264pay", "pt=96", "config-interval=-1", "!"]
    return cmd + ["rtpstreampay", "!", "fdsink", "fd=1"]


def sdp(codec):
    media = ("m=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\n" if codec == "jpeg" else
             "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1\r\n")
    return ("v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=sheregesh-s4-test\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n" + media +
            "a=control:track0\r\n")


def log(text):
    print(f"[rtsp-test] {time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)


class Server:
    def __init__(self, bind="0.0.0.0", port=8554, codec="jpeg", command=None):
        self.bind, self.port, self.codec = bind, port, codec
        self.command = command or producer(codec)
        self.lsock = None
        self.clients, self.procs = set(), set()
        self.lock = threading.Lock()
        self.up = threading.Event()

    # ---------- жизненный цикл ----------
    def start(self):
        self.lsock = socket.create_server((self.bind, self.port), reuse_port=False)
        self.port = self.lsock.getsockname()[1]
        self.up.set()
        threading.Thread(target=self._accept, args=(self.lsock,), daemon=True).start()
        return self

    def stop(self):
        """«Падение» сервера: сокеты закрыты, генераторы остановлены (start() — снова поднять на том же порту)."""
        self.up.clear()
        with self.lock:
            socks, procs = [self.lsock, *self.clients], list(self.procs)
            self.clients.clear()
        for s in socks:
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        for p in procs:
            self._kill(p)

    # ---------- RTSP ----------
    def _accept(self, lsock):
        while self.up.is_set():
            try:
                c, addr = lsock.accept()
            except OSError:
                return
            with self.lock:
                self.clients.add(c)
            log(f"client {addr[0]}:{addr[1]}")
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        try:
            while True:
                data = c.recv(65536)
                if not data:
                    return
                buf += data
                while buf:
                    if buf[:1] == b"$":                     # RTCP клиента по каналу interleaved — пропустить
                        if len(buf) < 4 or len(buf) < 4 + int.from_bytes(buf[2:4], "big"):
                            break
                        buf = buf[4 + int.from_bytes(buf[2:4], "big"):]
                        continue
                    end = buf.find(b"\r\n\r\n")
                    if end < 0:
                        if len(buf) > 65536:
                            return
                        break
                    head, buf = buf[:end].decode("latin-1"), buf[end + 4:]
                    lines = head.split("\r\n")
                    parts = lines[0].split(" ")
                    hdr = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
                    buf = buf[int(hdr.get("content-length", 0) or 0):]
                    self._answer(c, parts[0], parts[1] if len(parts) > 1 else "", hdr)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.clients.discard(c)
            c.close()

    def _answer(self, c, method, uri, hdr):
        cseq = hdr.get("cseq", "0")
        extra, body, status = "", "", "200 OK"
        if method == "OPTIONS":
            extra = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN\r\n"
        elif method == "DESCRIBE":
            body = sdp(self.codec)
            extra = f"Content-Base: {uri.rstrip('/')}/\r\nContent-Type: application/sdp\r\n"
        elif method == "SETUP":
            if "tcp" not in hdr.get("transport", "").lower():
                status = "461 Unsupported Transport"      # только RTP/AVP/TCP (rtspsrc protocols=tcp)
            else:
                extra = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: 5348524753;timeout=60\r\n"
        elif method in ("PLAY", "GET_PARAMETER", "TEARDOWN"):
            extra = "Session: 5348524753\r\n"
        else:
            status = "405 Method Not Allowed"
        c.sendall(f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{extra}Content-Length: {len(body)}\r\n\r\n{body}"
                  .encode("latin-1"))
        if method == "PLAY":
            threading.Thread(target=self._stream, args=(c,), daemon=True).start()
        elif method == "TEARDOWN":
            c.shutdown(socket.SHUT_RDWR)

    def _stream(self, c):
        p = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        with self.lock:
            self.procs.add(p)
        buf = b""
        try:
            while self.up.is_set():
                data = p.stdout.read1(65536)
                if not data:
                    err = p.stderr.read(400).decode("utf-8", "replace").strip() if p.poll() is not None else ""
                    if err:
                        log(f"generator exited: {err}")
                    return
                buf += data
                while len(buf) >= 2 and len(buf) >= 2 + int.from_bytes(buf[:2], "big"):
                    n = int.from_bytes(buf[:2], "big")
                    c.sendall(b"$\x00" + buf[:2] + buf[2:2 + n])
                    buf = buf[2 + n:]
        except OSError:
            pass
        finally:
            self._kill(p)
            with self.lock:
                self.procs.discard(p)
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    @staticmethod
    def _kill(p):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except (OSError, AttributeError):
                pass


def local_addresses():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.77.1", 9))                 # маршрут в сторону стенда; пакетов не отправляет
            return [s.getsockname()[0]]
    except OSError:
        return ["127.0.0.1"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="s4_rtsp_test_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8554)
    ap.add_argument("--codec", choices=["jpeg", "h264"], default="jpeg")
    ap.add_argument("--size", default="640x480", help="WxH")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--label", default="SHEREGESH RTSP TEST")
    ap.add_argument("--outage", default=None, metavar="PERIOD:DURATION", help="периодическое «падение» сервера, с")
    ap.add_argument("--check", action="store_true", help="проверить элементы и вывести конвейер, сервер не запускать")
    a = ap.parse_args(argv)
    if not shutil.which("gst-launch-1.0"):
        print("нет gst-launch-1.0 (пакет gstreamer1.0-tools)", file=sys.stderr)
        return 2
    w, h = (int(x) for x in a.size.lower().split("x"))
    try:
        cmd = producer(a.codec, w, h, a.fps, a.label)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    missing = [e for e in ("videotestsrc", "videoconvert", "rtpstreampay", "fdsink",
                           *(("jpegenc", "rtpjpegpay") if a.codec == "jpeg" else ("h264parse", "rtph264pay")))
               if not has_element(e)]
    if a.check or missing:
        print(" ".join(cmd))
        print("missing elements: " + (", ".join(missing) or "none"))
        return 1 if missing else 0
    srv = Server(a.bind, a.port, a.codec, cmd).start()
    host = local_addresses()[0] if a.bind in ("0.0.0.0", "") else a.bind
    codec = "mjpeg" if a.codec == "jpeg" else "h264"
    print(f"RTSP url=rtsp://{host}:{srv.port}/cam codec={codec}  video-input.json: "
          f'{{"rtsp_urls": [{{"url": "rtsp://{host}:{srv.port}/cam", "codec": "{codec}"}}]}}', flush=True)
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    period, dur = (float(x) for x in a.outage.split(":")) if a.outage else (0.0, 0.0)
    next_down = time.monotonic() + period if period else None
    while not stop.wait(0.2):
        if next_down is not None and time.monotonic() >= next_down:
            log(f"outage: server down for {dur:g} s")
            srv.stop()
            if stop.wait(dur):
                break
            srv.start()
            log("outage over: server up")
            next_down = time.monotonic() + period
    srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
