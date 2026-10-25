# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Минимальный сервер RTSP для испытаний видеоприёма (без gst-rtsp-server и python3-gi — их нет ни на платах, ни в CI).

Один поток RTP/JPEG (PT 26), транспорт — только RTP/AVP/TCP interleaved (так подключается RTSPSource: protocols=tcp).
Пакеты RTP даёт настоящий GStreamer: videotestsrc ! jpegenc ! rtpjpegpay ! rtpstreampay ! fdsink (RFC 4571 в stdout),
сервер перекладывает их в кадры «$ канал длина». Методы: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN.
"""
import os
import signal
import socket
import subprocess
import threading

SDP = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=sheregesh-test\r\nc=IN IP4 0.0.0.0\r\nt=0 0\r\n"
       "m=video 0 RTP/AVP 26\r\na=rtpmap:26 JPEG/90000\r\na=control:track0\r\n")
PRODUCER = ["gst-launch-1.0", "-q", "videotestsrc", "is-live=true", "!",
            "video/x-raw,width=320,height=240,framerate=15/1", "!", "videoconvert", "!", "video/x-raw,format=I420", "!",
            "jpegenc", "!", "rtpjpegpay", "!", "rtpstreampay", "!", "fdsink", "fd=1"]


class MiniRTSP:
    def __init__(self, port=0):
        self.lsock = socket.create_server(("127.0.0.1", port))
        self.port = self.lsock.getsockname()[1]
        self.url = f"rtsp://127.0.0.1:{self.port}/cam"
        self.clients, self.procs, self.stopped = [], [], threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                c, _ = self.lsock.accept()
            except OSError:
                return
            self.clients.append(c)
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
                    if buf[:1] == b"$":                       # RTCP от клиента по каналу interleaved — пропустить
                        if len(buf) < 4 or len(buf) < 4 + int.from_bytes(buf[2:4], "big"):
                            break
                        buf = buf[4 + int.from_bytes(buf[2:4], "big"):]
                        continue
                    end = buf.find(b"\r\n\r\n")
                    if end < 0:
                        break
                    head, buf = buf[:end].decode("latin-1"), buf[end + 4:]
                    lines = head.split("\r\n")
                    method = lines[0].split(" ")[0]
                    hdr = {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in lines[1:] if ":" in x)}
                    n = int(hdr.get("content-length", 0))
                    buf = buf[n:]
                    self._answer(c, method, hdr)
        except OSError:
            pass

    def _answer(self, c, method, hdr):
        cseq = hdr.get("cseq", "0")
        extra, body = "", ""
        if method == "OPTIONS":
            extra = "Public: OPTIONS, DESCRIBE, SETUP, PLAY, GET_PARAMETER, TEARDOWN\r\n"
        elif method == "DESCRIBE":
            body = SDP
            extra = f"Content-Base: {self.url}/\r\nContent-Type: application/sdp\r\n"
        elif method == "SETUP":
            extra = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: 5348524753;timeout=60\r\n"
        elif method in ("PLAY", "GET_PARAMETER", "TEARDOWN"):
            extra = "Session: 5348524753\r\n"
        c.sendall((f"RTSP/1.0 200 OK\r\nCSeq: {cseq}\r\n{extra}Content-Length: {len(body)}\r\n\r\n{body}")
                  .encode("latin-1"))
        if method == "PLAY":
            threading.Thread(target=self._stream, args=(c,), daemon=True).start()

    def _stream(self, c):
        p = subprocess.Popen(PRODUCER, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(p)
        buf = b""
        try:
            while not self.stopped.is_set():
                data = p.stdout.read1(65536)
                if not data:
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

    @staticmethod
    def _kill(p):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()

    def close(self):
        """Сервер «упал»: слушающий сокет и все сеансы закрыты, генераторы остановлены."""
        self.stopped.set()
        for s in [self.lsock, *self.clients]:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()
        for p in self.procs:
            self._kill(p)
