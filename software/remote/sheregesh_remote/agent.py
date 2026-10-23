# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор удалённого блока — отдельный процесс (S1.5, ADR-0007).

Слушает только 127.0.0.1. Отправляет пульту hello, heartbeat и синтетическое состояние,
по желанию — синтетический видеопоток. От пульта принимает только hello и bye; остальные сообщения
не исполняются, а учитываются. Команд ОС, управления устройствами и радиоинтерфейсов нет.

Неисправности для испытаний задаются расписанием (--fault AT:KIND[:ARG]), время — в секундах от старта:
  silence:DUR   не отправлять heartbeat и status DUR секунд (соединение не рвётся)
  delay:SEC     отправлять каждый heartbeat с задержкой SEC (до конца работы или до delay:0)
  corrupt:KIND  отправить повреждённое сообщение: garbage | json | fields | utf8 | long
  close         закрыть соединение
  crash         аварийно завершить процесс (код 70)
  video-off:DUR остановить видеопоток на DUR секунд
  alert:CODE    добавить код события в активные события блока; alert-clear — очистить
"""
import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time

from sheregesh import PROTOCOL_VERSION, __version__, video
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
        self.seq = 0
        self.ignored = 0
        self.bad = 0
        self.video_proc = None
        self.video_codec = video.choose_codec(args.video_codec) if args.video else None
        self.video_port = args.video_port
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
                "video": {"enabled": bool(self.video_codec), "active": self.video_proc is not None,
                          "codec": self.video_codec, "port": self.video_port},
                "active_events": list(self.alerts)}

    # ---------- видео ----------
    def video_start(self):
        if not self.video_codec or self.video_proc is not None:
            return
        self.video_proc = video.spawn(
            video.sender_args(self.video_codec, self.video_port, self.args.video_width, self.args.video_height,
                              self.args.video_fps), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

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
        elif kind == "alert":
            self.alerts.append(arg)
        elif kind == "alert-clear":
            self.alerts.clear()

    # ---------- обмен ----------
    async def handle(self, reader, writer):
        if self.writer is not None:                 # одно соединение: новое заменяет старое
            self.writer.close()
        self.writer = writer
        await self.send("hello", self.info())
        dec = Decoder()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                for msg in dec.feed(data):
                    await self.on_message(msg)
        except ConnectionError:
            pass
        finally:
            if self.writer is writer:
                self.writer = None
            writer.close()

    async def on_message(self, msg):
        if isinstance(msg, BadMessage):
            self.bad += 1
            log(f"bad message from console: {msg.reason}")
            return
        mtype, body = msg["type"], msg["body"]
        if mtype == "hello":
            n = negotiate(self.proto, body.get("proto"), body.get("software"), body.get("hardware"))
            log(f"hello from console proto={body.get('proto')} -> {n.reason}")
            if not n.compatible:
                await self.send("error", {"code": "PEER-INCOMPAT", "reason": n.reason, "local": self.proto})
                if self.writer:
                    self.writer.close()
                    self.writer = None
        elif mtype == "bye":
            if self.writer:
                self.writer.close()
                self.writer = None
        else:
            # Ничего не исполняется: неизвестные и не предназначенные блоку типы только учитываются.
            self.ignored += 1
            if mtype not in TYPES.get(self.proto, ()):
                log(f"ignored unknown message type {mtype!r}")

    async def ticker(self):
        dt = min(0.2, self.args.heartbeat_interval)
        next_hb = 0.0
        while True:
            await asyncio.sleep(dt)
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

    async def run(self):
        server = await asyncio.start_server(self.handle, self.args.host, self.args.port)
        port = server.sockets[0].getsockname()[1]
        print(f"LISTENING port={port}", flush=True)
        if self.video_codec:
            print(f"VIDEO codec={self.video_codec} port={self.video_port}", flush=True)
        elif self.args.video:
            print("VIDEO unavailable", flush=True)
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
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение модельного времени сценария")
    ap.add_argument("--scenario", choices=["none", "demo-s1"], default="none")
    ap.add_argument("--fault", action="append", default=[], metavar="AT:KIND[:ARG]")
    ap.add_argument("--video", action="store_true", help="синтетический видеопоток на 127.0.0.1")
    ap.add_argument("--video-port", type=int, default=5600)
    ap.add_argument("--video-codec", choices=["h264", "jpeg"], default="h264")
    ap.add_argument("--video-width", type=int, default=1280)
    ap.add_argument("--video-height", type=int, default=720)
    ap.add_argument("--video-fps", type=int, default=30)
    args = ap.parse_args(argv)
    if not args.host.startswith("127."):
        ap.error("S1.5: эмулятор слушает только loopback (127.0.0.0/8)")
    agent = Agent(args)

    def stop(*_):
        agent.video_stop()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        agent.video_stop()
    return 0
