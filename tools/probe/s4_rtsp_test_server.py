#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Тестовый RTSP-источник стенда S4/S5 (без камер и захватов): videotestsrc + timeoverlay/clockoverlay → RTP → RTSP.

Нужны только python3 (стандартная библиотека) и gst-launch-1.0 — есть на обеих платах (на pi2 нет python3-gi,
gst-rtsp-server нет нигде). Пакеты RTP даёт настоящий GStreamer (… ! rtpstreampay ! fdsink fd=1, RFC 4571 в stdout),
сервер отдаёт их клиенту по согласованному в SETUP транспорту (RTSP 1.0, RFC 2326):
  RTP/AVP/TCP;unicast;interleaved=0-1  — внутри соединения RTSP кадрами «$ канал длина»;
  RTP/AVP;unicast;client_port=a-b      — датаграммами UDP на порт a клиента (адрес — тот, с которого пришло
                                         соединение RTSP); RTCP сервер не шлёт, RTCP клиента на server_port+1
                                         поглощается.
Так проверяется RTSPSource блока с transport auto | tcp | udp (rtspsrc protocols=udp+tcp | tcp | udp). Транспорт,
которого нет в --transports, получает «461 Unsupported Transport» (как камера без этого транспорта). Каждый клиент
получает свой генератор; кадр несёт таймкод потока (timeoverlay), часы (clockoverlay) и метку --label — видно
задержку и замирание.

  python3 tools/probe/s4_rtsp_test_server.py [--bind 0.0.0.0] [--port 8554] [--codec mjpeg|h264]
                                             [--transports tcp,udp] [--udp-drop] [--size 640x480] [--fps 25]
                                             [--label CAM-A] [--outage 30:10] [--pidfile /tmp/s4-rtsp.pid]

  --codec mjpeg (по умолчанию; прежнее имя jpeg — тоже) jpegenc → rtpjpegpay; в video-input.json "codec": "mjpeg"
  --codec h264  первый найденный кодер: v4l2h264enc (Pi 4: есть на pi1/pi2; x264enc в образе нет), x264enc (Mac
                Homebrew, CI ubuntu с gstreamer1.0-plugins-ugly), openh264enc, vtenc_h264 (macOS) → rtph264pay;
                "codec": "h264". Выбранный кодер печатается при запуске и в --check; нет ни одного — код 2
  --transports  транспорты, принимаемые в SETUP: tcp,udp (по умолчанию) | tcp | udp
  --udp-drop    «фаервол»: SETUP по UDP принимается, но датаграммы не отправляются (rtspsrc auto переходит на TCP
                по своему timeout; transport=udp — ошибка «No other protocols to try»)
  --outage P:D  каждые P с сервер «падает» на D с (слушающий сокет и сеансы закрыты, генераторы остановлены) —
                проверка потери/возврата источника на блоке (connect-error → streaming) без ручной остановки
  --check       только проверить элементы GStreamer и вывести конвейер

Адрес: rtsp://<адрес платы>:<port>/cam (путь любой). Остановка — Ctrl+C / SIGTERM (генераторы снимаются).
"""
import argparse
import os
import re
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
            ("openh264enc", ["openh264enc", "!"]),
            ("vtenc_h264", ["vtenc_h264", "realtime=true", "allow-frame-reordering=false", "max-keyframe-interval=30",
                            "!"]))
CODECS = {"mjpeg": "jpeg", "jpeg": "jpeg", "h264": "h264"}      # --codec → внутреннее имя (jpeg — прежнее)
TRANSPORTS = ("tcp", "udp")


def has_element(name):
    if not shutil.which("gst-inspect-1.0"):
        return False
    return subprocess.run(["gst-inspect-1.0", "--exists", name], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def h264_encoder(have=has_element):
    """Имя первого доступного кодера H.264 из ENCODERS или None."""
    return next((name for name, _args in ENCODERS if have(name)), None)


def producer(codec="jpeg", width=640, height=480, fps=25, label="SHEREGESH RTSP TEST", have=has_element):
    """Команда генератора: RTP в RFC 4571 в stdout. Надписи — только если есть элементы pango."""
    codec = CODECS.get(codec, codec)
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
            raise RuntimeError("no H.264 encoder (v4l2h264enc / x264enc / openh264enc / vtenc_h264); use --codec mjpeg")
        cmd += [*enc, "h264parse", "config-interval=-1", "!", "rtph264pay", "pt=96", "config-interval=-1", "!"]
    return cmd + ["rtpstreampay", "!", "fdsink", "fd=1"]


def sdp(codec):
    codec = CODECS.get(codec, codec)
    media = ("m=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\n" if codec == "jpeg" else
             "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1\r\n")
    return ("v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=sheregesh-s4-test\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n" + media +
            "a=control:track0\r\n")


def parse_transports(text):
    """«tcp,udp» → ("tcp", "udp"); пусто или неизвестное — ValueError."""
    out = tuple(dict.fromkeys(x.strip().lower() for x in str(text).split(",") if x.strip()))
    if not out or any(x not in TRANSPORTS for x in out):
        raise ValueError(f"transports {text!r}: expected tcp, udp or tcp,udp")
    return out


def choose_transport(header, allowed=TRANSPORTS):
    """Заголовок Transport из SETUP → ("tcp", None) | ("udp", (rtp_port, rtcp_port)) | None (ответ 461).
    Клиент может перечислить варианты через запятую — берётся первый допустимый. Только unicast RTP/AVP."""
    for spec in header.split(","):
        parts = [x.strip() for x in spec.split(";")]
        proto = parts[0].upper()
        params = {k.lower(): v for k, _, v in (x.partition("=") for x in parts[1:])}
        if "multicast" in params:
            continue
        if proto == "RTP/AVP/TCP":
            if "tcp" in allowed:
                return "tcp", None
        elif proto in ("RTP/AVP", "RTP/AVP/UDP") and "udp" in allowed:
            m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", params.get("client_port", ""))
            if m and 0 < int(m.group(1)) < 65536:
                rtp = int(m.group(1))
                return "udp", (rtp, int(m.group(2) or rtp + 1))
    return None


def udp_pair(host):
    """Сокеты UDP сервера: RTP (чётный порт) — с него идут датаграммы; RTCP (следующий) — поглощает RR клиента
    (иначе на каждый RR — ICMP «порт недоступен»)."""
    for _ in range(50):
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        a.bind((host, 0))
        port = a.getsockname()[1]
        if port % 2 == 0 and port < 65535:
            b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                b.bind((host, port + 1))
                return a, b
            except OSError:
                b.close()
        a.close()
    raise OSError("no free even/odd UDP port pair")


def log(text):
    print(f"[rtsp-test] {time.strftime('%H:%M:%S')} {text}", file=sys.stderr, flush=True)


class Server:
    def __init__(self, bind="0.0.0.0", port=8554, codec="jpeg", command=None, transports=TRANSPORTS, udp_drop=False):
        self.bind, self.port, self.codec = bind, port, CODECS.get(codec, codec)
        self.transports, self.udp_drop = tuple(transports), udp_drop
        self.command = command or producer(self.codec)
        self.sessions = []                  # транспорты принятых SETUP (по порядку) — для испытаний и журнала
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
        with self.lock:                     # поток _stream уберёт их позже сам; к возврату stop() учёт уже пуст
            self.procs.difference_update(procs)

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
        sess = {"transport": None, "dest": None, "udp": (), "closed": threading.Event()}
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
                    self._answer(c, parts[0], parts[1] if len(parts) > 1 else "", hdr, sess)
        except (OSError, ValueError):
            pass
        finally:
            sess["closed"].set()                            # поток UDP узнаёт о конце сеанса отсюда
            with self.lock:
                self.clients.discard(c)
            c.close()

    def _setup(self, c, hdr, sess):
        """SETUP: (статус, доп. заголовки). Выбор транспорта — choose_transport по --transports."""
        chosen = choose_transport(hdr.get("transport", ""), self.transports)
        if chosen is None:
            log(f"SETUP refused (461): {hdr.get('transport', '')[:120]}")
            return "461 Unsupported Transport", ""
        for s in sess["udp"]:                               # повторный SETUP — прежние сокеты не нужны
            s.close()
        if chosen[0] == "tcp":
            sess.update(transport="tcp", dest=None, udp=())
            tr = "RTP/AVP/TCP;unicast;interleaved=0-1"
        else:
            rtp, rtcp = udp_pair(c.getsockname()[0])
            sess.update(transport="udp", dest=(c.getpeername()[0], chosen[1][0]), udp=(rtp, rtcp))
            sp = rtp.getsockname()[1]
            tr = f"RTP/AVP;unicast;client_port={chosen[1][0]}-{chosen[1][1]};server_port={sp}-{sp + 1}"
        with self.lock:
            self.sessions.append(chosen[0])
        log(f"SETUP {chosen[0]}" + (f" -> {sess['dest'][0]}:{sess['dest'][1]}" if sess["dest"] else ""))
        return "200 OK", f"Transport: {tr}\r\nSession: 5348524753;timeout=60\r\n"

    def _answer(self, c, method, uri, hdr, sess):
        cseq = hdr.get("cseq", "0")
        extra, body, status = "", "", "200 OK"
        if method == "OPTIONS":
            extra = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN\r\n"
        elif method == "DESCRIBE":
            body = sdp(self.codec)
            extra = f"Content-Base: {uri.rstrip('/')}/\r\nContent-Type: application/sdp\r\n"
        elif method == "SETUP":
            status, extra = self._setup(c, hdr, sess)
        elif method in ("PLAY", "GET_PARAMETER", "TEARDOWN"):
            extra = "Session: 5348524753\r\n"
        else:
            status = "405 Method Not Allowed"
        c.sendall(f"RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{extra}Content-Length: {len(body)}\r\n\r\n{body}"
                  .encode("latin-1"))
        if method == "PLAY" and sess["transport"]:
            threading.Thread(target=self._stream, args=(c, sess), daemon=True).start()
        elif method == "TEARDOWN":
            c.shutdown(socket.SHUT_RDWR)

    def _stream(self, c, sess):
        p = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        with self.lock:
            self.procs.add(p)
        udp = sess["udp"][0] if sess["transport"] == "udp" else None
        dest = sess["dest"]
        buf = b""
        try:
            while self.up.is_set() and not sess["closed"].is_set():
                data = p.stdout.read1(65536)
                if not data:
                    err = p.stderr.read(400).decode("utf-8", "replace").strip() if p.poll() is not None else ""
                    if err:
                        log(f"generator exited: {err}")
                    return
                buf += data
                while len(buf) >= 2 and len(buf) >= 2 + int.from_bytes(buf[:2], "big"):
                    n = int.from_bytes(buf[:2], "big")
                    if udp is None:
                        c.sendall(b"$\x00" + buf[:2] + buf[2:2 + n])
                    elif not self.udp_drop:
                        try:
                            udp.sendto(buf[2:2 + n], dest)
                        except OSError:                     # ICMP «порт недоступен» на прежнюю датаграмму — не обрыв
                            pass
                    buf = buf[2 + n:]
        except OSError:
            pass
        finally:
            self._kill(p)
            with self.lock:
                self.procs.discard(p)
            for s in sess["udp"]:
                s.close()
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
    ap.add_argument("--codec", choices=["mjpeg", "jpeg", "h264"], default="mjpeg")
    ap.add_argument("--transports", default="tcp,udp", help="принимаемые транспорты: tcp,udp | tcp | udp")
    ap.add_argument("--udp-drop", action="store_true", help="SETUP по UDP принять, датаграммы не слать («фаервол»)")
    ap.add_argument("--size", default="640x480", help="WxH")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--label", default="SHEREGESH RTSP TEST")
    ap.add_argument("--outage", default=None, metavar="PERIOD:DURATION", help="периодическое «падение» сервера, с")
    ap.add_argument("--check", action="store_true", help="проверить элементы и вывести конвейер, сервер не запускать")
    ap.add_argument("--pidfile", default=None, help="записать PID (остановка с Mac: kill $(cat PIDFILE)); удаляется при выходе")
    a = ap.parse_args(argv)
    if not shutil.which("gst-launch-1.0"):
        print("нет gst-launch-1.0 (пакет gstreamer1.0-tools)", file=sys.stderr)
        return 2
    w, h = (int(x) for x in a.size.lower().split("x"))
    codec = CODECS[a.codec]
    try:
        transports = parse_transports(a.transports)
        cmd = producer(codec, w, h, a.fps, a.label)
    except (RuntimeError, ValueError) as e:
        print(e, file=sys.stderr)
        return 2
    encoder = "jpegenc" if codec == "jpeg" else h264_encoder()
    missing = [e for e in ("videotestsrc", "videoconvert", "rtpstreampay", "fdsink",
                           *(("jpegenc", "rtpjpegpay") if codec == "jpeg" else ("h264parse", "rtph264pay")))
               if not has_element(e)]
    if a.check or missing:
        print(" ".join(cmd))
        print(f"encoder: {encoder}; transports: {','.join(transports)}")
        print("missing elements: " + (", ".join(missing) or "none"))
        return 1 if missing else 0
    srv = Server(a.bind, a.port, codec, cmd, transports, a.udp_drop).start()
    if a.pidfile:
        with open(a.pidfile, "w") as f:
            f.write(f"{os.getpid()}\n")
    host = local_addresses()[0] if a.bind in ("0.0.0.0", "") else a.bind
    name = "mjpeg" if codec == "jpeg" else "h264"
    mode = transports[0] if len(transports) == 1 else "auto"
    print(f"RTSP url=rtsp://{host}:{srv.port}/cam codec={name} encoder={encoder} transports={','.join(transports)}"
          f"{' udp-drop' if a.udp_drop else ''}  video-input.json: "
          f'{{"rtsp_urls": [{{"url": "rtsp://{host}:{srv.port}/cam", "codec": "{name}", "transport": "{mode}"}}]}}',
          flush=True)
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
    if a.pidfile:
        try:
            os.unlink(a.pidfile)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
