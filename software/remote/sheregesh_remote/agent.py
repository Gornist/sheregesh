# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор удалённого блока — отдельный процесс (S1.5/S3.1, ADR-0007).

Режимы:
- loopback (по умолчанию): слушает только 127.0.0.1, без TLS — разработка и тесты;
- сетевой лабораторный профиль (--link-config): TLS 1.3 с взаимной аутентификацией, только адреса локальной
  подсети, вторая сторона — пульт с URI из конфигурации. Без ключей и сертификатов сервер не запускается.
Отправляет пульту hello, heartbeat, синтетическое состояние и события, по желанию — видеопоток: синтетический
или (S3.3, --video-source uvc) со стандартного лабораторного устройства V4L2/UVC через тот же конвейер и тот же канал.
От пульта принимает только hello, ack и bye; остальные сообщения не исполняются, а учитываются.
Команд ОС, управления устройствами и радиоинтерфейсов нет.

Неисправности для испытаний задаются расписанием (--fault AT:KIND[:ARG]), время — в секундах от старта:
  silence:DUR   не отправлять heartbeat и status DUR секунд (соединение не рвётся)
  delay:SEC     отправлять каждый heartbeat с задержкой SEC (до конца работы или до delay:0)
  corrupt:KIND  отправить повреждённое сообщение: garbage | json | fields | utf8 | long
  close         закрыть соединение
  crash         аварийно завершить процесс (код 70)
  video-off:DUR остановить видеопоток на DUR секунд (и UDP, и защищённый видеоканал сетевого профиля)
  alert:CODE    добавить код события в активные события блока; alert-clear — очистить
"""
import argparse
import asyncio
import json
import os
import signal
import ssl
import subprocess
import sys
import time
from pathlib import Path

from sheregesh import PROTOCOL_VERSION, __version__, sdnotify, video, videoinput
from sheregesh.videolink import VideoServer
from sheregesh.link import tls
from sheregesh.link.codec import TYPES, BadMessage, Decoder, encode
from sheregesh.protocol import negotiate
from sheregesh_emulators.devices import FanEmulator, PowerEmulator, SimClock, ThermalEmulator

# Демонстрационный сценарий удалённого блока (секунды модельного времени, как у пульта — sheregesh_emulators.demo)
DEMO_FAULTS = ["60:video-off:15", "95:silence:20"]


class Agent:
    def __init__(self, args):
        self.args = args
        self.proto = args.protocol
        self.t0 = time.monotonic()
        self.clock = SimClock()
        self.power = PowerEmulator(percent=92.0, source="battery", discharge_pct_h=8.0)
        self.thermal = ThermalEmulator(FanEmulator(), ambient=25.0, heat_w=4.5, start_c=40.0, k0=0.15, kf=0.0)
        self.thermal.fan.set_duty(0)
        self.silent_until = 0.0
        self.hb_delay = 0.0
        self.video_off_until = 0.0
        self.alerts = []
        self.writer = None
        self.hb_wait = None                          # monotonic первого heartbeat, на который пульт ещё не ответил
        self.written_connected = None                # «connected» в последнем записанном файле состояния
        self.seq = 0
        self.ignored = 0
        self.bad = 0
        self.video_proc = None
        # Видеоисточник (S3.3): синтетический (по умолчанию) или стандартное устройство UVC, выбранное явно.
        # Устройство находится один раз при запуске; постоянная ссылка /dev/v4l/by-id переживает переподключение.
        self.video_src, self.video_src_error = None, ""
        if args.video_source == "uvc":
            src = videoinput.open_uvc(args.video_device, f"{args.video_width}x{args.video_height}@{args.video_fps}",
                                      args.video_format)
            self.video_src, self.video_src_error = (src, "") if src.available else (None, src.error)
        uvc_ok = args.video_source != "uvc" or self.video_src is not None
        self.video_codec = video.choose_codec(args.video_codec) if args.video and uvc_ok else None
        self.video_port = args.video_port
        self.tls_cfg = getattr(args, "tls_cfg", None)
        self.ctx = tls.make_context(self.tls_cfg, server=True) if self.tls_cfg else None
        # Сетевой профиль: видео — только по защищённому каналу (S3.1-VIDEO, ADR-0011), без UDP-отправителя.
        self.vserver = None
        net_video = self.tls_cfg is not None and self.tls_cfg.video and not args.no_video
        if net_video and args.video_command:          # испытания: подставной генератор, без пробы GStreamer
            self.net_video_codec = args.video_codec
        else:
            self.net_video_codec = video.choose_codec(args.video_codec) if net_video and uvc_ok and video.available() \
                else None
        self.handshakes = None                       # asyncio.Semaphore: не более 4 рукопожатий одновременно
        self.sent = {}                               # seq heartbeat/event → (тип, monotonic) для ack
        self.session = None
        self.peer_desc = None
        self.event_k = 0
        self.next_event = time.monotonic() + (args.event_interval or 0)
        self.stats = {"sessions": 0, "auth_failures": 0, "rejected_addr": 0, "peer_timeouts": 0, "acks": 0,
                      "events_sent": 0,
                      "events_acked": 0, "rtt_last_ms": None, "rtt_avg_ms": None, "rtt_max_ms": None}
        faults = list(args.fault) + (DEMO_FAULTS if args.scenario == "demo-s1" else [])
        self.faults = sorted((self._parse_fault(f) for f in faults), key=lambda f: f[0])

    @staticmethod
    def _parse_fault(spec):
        at, kind, *arg = spec.split(":")
        return float(at), kind, arg[0] if arg else None

    def now(self):
        """Модельное время блока: реальное × speed (совпадает со шкалой сценария пульта)."""
        return (time.monotonic() - self.t0) * self.args.speed

    def info(self):
        return {"role": "remote", "proto": self.proto, "software": self.args.software, "hardware": self.args.hardware,
                "device_id": self.args.device_id}

    async def send(self, mtype, body):
        if self.writer is None:
            return
        self.seq += 1
        if mtype == "heartbeat" and self.hb_wait is None:
            self.hb_wait = time.monotonic()
        if mtype in ("heartbeat", "event"):
            self.sent[self.seq] = (mtype, time.monotonic())
            if len(self.sent) > 256:
                self.sent.pop(next(iter(self.sent)))
        try:
            self.writer.write(encode(self.proto, mtype, self.seq, time.time(), body))
            await self.writer.drain()
        except (ConnectionError, RuntimeError):
            self.writer = None

    async def send_raw(self, data: bytes):
        if self.writer is not None:
            self.writer.write(data)
            await self.writer.drain()

    def status_body(self):
        p = self.power.read()
        return {"state": "alert" if self.alerts else "normal", "uptime_s": round(self.now(), 1),
                "temperatures": self.thermal.read(),
                "power": {"source": p.source, "percent": p.percent, "low_battery": p.low_battery},
                "video": ({"enabled": True, "active": self.vserver.active, "codec": self.net_video_codec,
                           "port": self.vserver.port, "transport": "tls"} if self.vserver is not None else
                          {"enabled": bool(self.video_codec), "active": self.video_proc is not None,
                           "codec": self.video_codec, "port": self.video_port}),
                "active_events": list(self.alerts),
                "link": {k: v for k, v in self.stats.items() if isinstance(v, (int, float))}}

    # ---------- видео ----------
    def video_start(self):
        if not self.video_codec or self.video_proc is not None:
            return
        self.video_proc = video.spawn(
            video.sender_args(self.video_codec, self.video_port, self.args.video_width, self.args.video_height,
                              self.args.video_fps, src=self.video_src), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)

    def video_stop(self):
        if self.video_proc is not None:
            try:
                os.killpg(self.video_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.video_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.video_proc.pid, signal.SIGKILL)
            self.video_proc = None

    # ---------- неисправности ----------
    async def apply_fault(self, kind, arg):
        log(f"fault {kind} {arg or ''}")
        if kind == "silence":
            self.silent_until = self.now() + float(arg)
        elif kind == "delay":
            self.hb_delay = float(arg)
        elif kind == "corrupt":
            raw = {"garbage": b"\x00\x01garbage-not-json\n", "json": b'{"proto":"1.0","type":"status",\n',
                   "fields": b'{"proto":"1.0","type":"status","body":{}}\n', "utf8": b'{"x":"\xff\xfe"}\n',
                   "long": b'{"pad":"' + b"x" * (70 * 1024) + b'"}\n'}[arg]
            await self.send_raw(raw)
        elif kind == "close":
            if self.writer:
                self.writer.close()
                self.writer = None
        elif kind == "crash":
            self.video_stop()
            os._exit(70)
        elif kind == "video-off":
            self.video_stop()
            self.video_off_until = self.now() + float(arg)
            if self.vserver is not None:            # и защищённый видеоканал: сеанс закрыт, новые отклоняются до конца
                self.vserver.drop("неисправность video-off")
        elif kind == "alert":
            self.alerts.append(arg)
        elif kind == "alert-clear":
            self.alerts.clear()

    # ---------- обмен ----------
    async def handle(self, reader, writer):
        if self.ctx is not None and not await self.secure(writer):
            writer.close()
            return
        if self.writer is not None:                 # одно соединение: новое заменяет старое (старое могло «повиснуть»)
            self.writer.transport.abort()
        if self.vserver is not None:                # видеосеанс привязан к сеансу управления — новый сеанс, новое видео
            self.vserver.drop("сеанс управления заменён")
        self.writer = writer
        self.hb_wait = None
        await self.send("hello", self.info())
        dec = Decoder()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                if self.writer is writer:
                    self.hb_wait = None             # пульт жив: любой приём от него
                for msg in dec.feed(data):
                    await self.on_message(msg)
        except ConnectionError:
            pass
        finally:
            if self.writer is writer:
                self.writer = None
            # Сеанс управления кончился любым путём (обрыв, EOF, «bye» — тот уже обнулил writer): если он не заменён
            # новым, видео закрывается. Новый сеанс сам закрыл видео при подключении.
            if self.writer is None and self.vserver is not None:
                self.vserver.drop("сеанс управления завершён")
            writer.close()

    def authorize_video(self, addr):
        """Видеосеанс — только тому же пульту (URI проверен TLS) и с того же адреса, что текущий сеанс управления."""
        if self.writer is None or self.peer_desc is None:
            return "no active control session"
        if self.now() < self.video_off_until:
            return "video off (fault)"
        if self.peer_desc.get("addr") != addr:
            return f"control session is from {self.peer_desc.get('addr')}"
        return None

    async def secure(self, writer) -> bool:
        """Сетевой профиль: адрес из локальной подсети → TLS 1.3 с сертификатом пульта → URI пульта из конфигурации."""
        addr = (writer.get_extra_info("peername") or ("?",))[0]
        if not tls.peer_allowed(addr):
            self.stats["rejected_addr"] += 1
            log(f"reject {addr}: outside lab network")
            return False
        async with self.handshakes:
            try:
                await writer.start_tls(self.ctx, ssl_handshake_timeout=tls.HANDSHAKE_TIMEOUT_S)
            except (ssl.SSLError, OSError, asyncio.TimeoutError, TimeoutError) as e:
                self.stats["auth_failures"] += 1
                log(f"auth: TLS handshake from {addr} failed: {tls.describe_ssl_error(e)}")
                return False
        why = tls.check_peer(writer.get_extra_info("peercert"), self.tls_cfg.peer_uri)
        if why:
            self.stats["auth_failures"] += 1
            log(f"auth: reject {addr}: {why}")
            try:          # цепочка верна (CA стенда), но устройство не то: причина — без данных блока
                writer.write(encode(self.proto, "error", 0, time.time(), {"code": "LINK-AUTH", "reason": "identity"}))
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            return False
        self.stats["sessions"] += 1
        version = writer.get_extra_info("ssl_object").version()
        self.peer_desc = {"addr": addr, "uri": self.tls_cfg.peer_uri, "tls": version, "since": time.time()}
        log(f"session from {addr}: {self.tls_cfg.peer_uri} ({version})")
        return True

    def check_peer_alive(self, mono):
        """Тихая потеря пульта (кабель, зависание): ядро может держать соединение минутами, а TCP не сообщит об обрыве,
        пока нечего подтверждать. В сессии с ack пульт отвечает на каждый heartbeat; нет ни одного байта от пульта дольше
        --peer-timeout после отправленного heartbeat — соединение разрывается (REQ-SYS-002: «нет связи» ≤ 5 с на обеих
        сторонах). abort, а не close: неотправленный буфер на мёртвое соединение не держит сокет до таймаута TCP."""
        limit = self.args.peer_timeout
        if not (limit and self.writer is not None and self.hb_wait is not None
                and "ack" in TYPES.get(self.session or "", ()) and mono - self.hb_wait > limit):
            return
        self.stats["peer_timeouts"] += 1
        log(f"console silent {mono - self.hb_wait:.1f} s (no ack): connection dropped")
        self.writer.transport.abort()
        self.writer = None
        self.hb_wait = None
        if self.vserver is not None:
            self.vserver.drop("пульт не отвечает")

    async def on_message(self, msg):
        if isinstance(msg, BadMessage):
            self.bad += 1
            log(f"bad message from console: {msg.reason}")
            return
        mtype, body = msg["type"], msg["body"]
        if mtype == "hello":
            n = negotiate(self.proto, body.get("proto"), body.get("software"), body.get("hardware"))
            log(f"hello from console proto={body.get('proto')} -> {n.reason}")
            self.session = n.session
            if self.peer_desc is not None:          # что сообщил пульт — для проверки версий в sheregesh-health
                self.peer_desc["hello"] = {k: body.get(k) for k in ("proto", "software", "hardware")}
            if not n.compatible:
                await self.send("error", {"code": "PEER-INCOMPAT", "reason": n.reason, "local": self.proto})
                if self.writer:
                    self.writer.close()
                    self.writer = None
        elif mtype == "bye":
            if self.writer:
                self.writer.close()
                self.writer = None
        elif mtype == "ack" and mtype in TYPES.get(self.session or self.proto, ()):
            sent = self.sent.pop(body.get("seq"), None) if isinstance(body.get("seq"), int) else None
            if sent:
                rtt = (time.monotonic() - sent[1]) * 1000
                st = self.stats
                st["acks"] += 1
                st["rtt_last_ms"] = round(rtt, 2)
                st["rtt_avg_ms"] = round(rtt if st["rtt_avg_ms"] is None else st["rtt_avg_ms"] * 0.95 + rtt * 0.05, 2)
                st["rtt_max_ms"] = round(max(rtt, st["rtt_max_ms"] or 0), 2)
                if sent[0] == "event":
                    st["events_acked"] += 1
        else:
            # Ничего не исполняется: неизвестные и не предназначенные блоку типы только учитываются.
            self.ignored += 1
            if mtype not in TYPES.get(self.proto, ()):
                log(f"ignored unknown message type {mtype!r}")

    async def ticker(self):
        dt = min(0.2, self.args.heartbeat_interval)
        next_hb = 0.0
        wd = sdnotify.watchdog_interval()
        next_ping = next_status = 0.0
        while True:
            await asyncio.sleep(dt)
            mono = time.monotonic()
            if wd and mono >= next_ping:              # основной цикл жив — подтверждение для WatchdogSec
                sdnotify.ping()
                next_ping = mono + wd
            self.check_peer_alive(mono)
            if self.args.status_file and (mono >= next_status or (self.writer is not None) != self.written_connected):
                self.write_status()                   # смена «есть/нет пульта» — сразу, иначе раз в 2 с
                next_status = mono + 2.0
            if self.args.event_interval and self.writer is not None and mono >= self.next_event:
                self.event_k += 1
                await self.send("event", {"code": "EMU-TEST", "detail": str(self.event_k)})
                self.stats["events_sent"] += 1
                self.next_event = mono + self.args.event_interval
            self.clock.advance(dt * self.args.speed)
            self.power.step(dt * self.args.speed)
            self.thermal.step(dt * self.args.speed)
            t = self.now()
            while self.faults and self.faults[0][0] <= t:
                _, kind, arg = self.faults.pop(0)
                await self.apply_fault(kind, arg)
            if self.video_codec and self.video_proc is None and t >= self.video_off_until:
                self.video_start()
            if self.video_proc is not None and self.video_proc.poll() is not None:
                log("video sender exited; restarting")
                self.video_proc = None
            if time.monotonic() >= next_hb and t >= self.silent_until:
                if self.hb_delay:
                    await asyncio.sleep(self.hb_delay)
                await self.send("heartbeat", {"uptime_s": round(t, 1)})
                await self.send("status", self.status_body())
                next_hb = time.monotonic() + self.args.heartbeat_interval

    def write_status(self):
        """Состояние канала для sheregesh-health (RuntimeDirectory службы), атомарно."""
        st = {"role": "remote", "time": time.time(), "software": __version__, "proto": self.proto,
              "tls": self.ctx is not None, "listen": f"{self.args.host}:{self.port}", "connected": self.writer is not None,
              "session": self.session if self.writer is not None else None, "peer": self.peer_desc, "stats": self.stats,
              "own_uri": self.tls_cfg.own_uri if self.tls_cfg else None,
              "video": self.vserver.stats() if self.vserver is not None else None}
        path = Path(self.args.status_file)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, path)
        self.written_connected = st["connected"]

    async def run(self):
        self.handshakes = asyncio.Semaphore(4)
        server = await asyncio.start_server(self.handle, self.args.host, self.args.port, limit=128 * 1024)
        self.port = port = server.sockets[0].getsockname()[1]
        print(f"LISTENING port={port}" + (f" tls=1.3 mutual peer={self.tls_cfg.peer_uri}" if self.ctx else ""), flush=True)
        if self.net_video_codec:
            self.vserver = VideoServer(self.tls_cfg, self.net_video_codec, self.authorize_video, log, bind=self.args.host,
                                       port=self.args.net_video_port, width=self.args.video_width,
                                       height=self.args.video_height, fps=self.args.video_fps,
                                       command=self.args.video_command or (video.stream_sender_args(
                                           self.net_video_codec, self.args.video_width, self.args.video_height,
                                           self.args.video_fps, src=self.video_src) if self.video_src else None)
                                       ).start()
            print(f"VIDEO codec={self.net_video_codec} port={self.vserver.port} transport=tls1.3-mutual", flush=True)
        elif self.video_codec:
            print(f"VIDEO codec={self.video_codec} port={self.video_port}", flush=True)
        if self.video_src is not None:
            print(f"VIDEO source={self.video_src.id} mode={self.video_src.mode} device={self.video_src.node.dev}",
                  flush=True)
        if self.video_src_error and (self.args.video or self.tls_cfg is not None):
            print(f"VIDEO unavailable: {self.video_src_error}", flush=True)
        elif self.args.video and not self.video_codec and not self.net_video_codec:
            why = "; ".join(video.PROBE_FAILURES[-4:]) or ("no gst-launch-1.0/videotestsrc/udpsrc" if not video.available()
                                                         else "no working codec")
            print(f"VIDEO unavailable: {why}", flush=True)
        sdnotify.ready(f"listening {self.args.host}:{port}" + (" mTLS" if self.ctx else " loopback"))
        async with server:
            await asyncio.gather(server.serve_forever(), self.ticker())


def log(text):
    print(f"[remote] {text}", file=sys.stderr, flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheregesh_remote", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7700, help="0 — выбрать свободный порт")
    ap.add_argument("--protocol", default=PROTOCOL_VERSION)
    ap.add_argument("--software", default=__version__)
    ap.add_argument("--hardware", default="HW-1.0 Rev A (emulator)")
    ap.add_argument("--device-id", default="remote-emu-01")
    ap.add_argument("--heartbeat-interval", type=float, default=1.0)
    ap.add_argument("--peer-timeout", type=float, default=3.0,
                    help="нет ответа пульта на heartbeat дольше N с (сессия с ack) — соединение потеряно; 0 — не проверять")
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение модельного времени сценария")
    ap.add_argument("--scenario", choices=["none", "demo-s1"], default="none")
    ap.add_argument("--fault", action="append", default=[], metavar="AT:KIND[:ARG]")
    ap.add_argument("--video", action="store_true", help="синтетический видеопоток на 127.0.0.1")
    ap.add_argument("--video-port", type=int, default=5600)
    ap.add_argument("--video-codec", choices=["h264", "jpeg"], default="h264")
    ap.add_argument("--video-width", type=int, default=1280)
    ap.add_argument("--video-height", type=int, default=720)
    ap.add_argument("--video-fps", type=int, default=30)
    ap.add_argument("--no-video", action="store_true", help="сетевой профиль без видеоканала")
    ap.add_argument("--video-source", choices=["synthetic", "uvc"], default="synthetic",
                    help="источник видео (S3.3): synthetic — videotestsrc, uvc — стандартное устройство V4L2/UVC")
    ap.add_argument("--video-device", default="", help="устройство UVC: vvvv:pppp[:serial] или videoN (по умолчанию — "
                                                       "единственное найденное)")
    ap.add_argument("--video-format", choices=["auto", "yuyv", "mjpeg", "h264"], default="auto",
                    help="формат устройства UVC; совпадающий с кодеком канала передаётся без перекодирования")
    ap.add_argument("--net-video-port", type=int, default=None, help="порт видеоканала (по умолчанию video_port профиля)")
    ap.add_argument("--video-command", nargs="+", default=None, help=argparse.SUPPRESS)   # испытания: подмена генератора
    ap.add_argument("--link-config", help="сетевой лабораторный профиль: TLS 1.3 + взаимная аутентификация (S3.1)")
    ap.add_argument("--event-interval", type=float, default=0, help="синтетическое событие EMU-TEST каждые N с (0 — нет)")
    ap.add_argument("--status-file", help="состояние канала в JSON для sheregesh-health")
    ap.add_argument("--bind", help="адрес прослушивания в сетевом профиле (по умолчанию 0.0.0.0; тесты — 127.0.0.1)")
    args = ap.parse_args(argv)
    args.tls_cfg = None
    if args.link_config:
        try:
            args.tls_cfg = tls.LinkConfig.load(args.link_config)
        except tls.LinkConfigError as e:
            print(f"сетевой профиль не настроен — сервер не запускается: {e}", file=sys.stderr, flush=True)
            return 2
        if args.tls_cfg.role != "remote":
            ap.error("--link-config: ожидается роль remote")
        # Все IPv4-адреса (адрес eth0 меняет DHCP); доступ ограничен подсетью интерфейса, mTLS и IPAddressAllow службы.
        args.host = args.bind or "0.0.0.0"
        if args.port == 7700:
            args.port = args.tls_cfg.port
    elif not args.host.startswith("127."):
        ap.error("без --link-config эмулятор слушает только loopback (127.0.0.0/8)")
    agent = Agent(args)

    def stop(*_):
        agent.video_stop()
        if agent.vserver is not None:
            agent.vserver.close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        agent.video_stop()
    return 0
