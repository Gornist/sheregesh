# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Эмулятор удалённого блока — отдельный процесс (S1.5/S3.1, ADR-0007).

Режимы:
- loopback (по умолчанию): слушает только 127.0.0.1, без TLS — разработка и тесты;
- сетевой лабораторный профиль (--link-config): TLS 1.3 с взаимной аутентификацией, только адреса локальной
  подсети, вторая сторона — пульт с URI из конфигурации. Без ключей и сертификатов сервер не запускается.
Отправляет пульту hello, heartbeat, синтетическое состояние и события, по желанию — видеопоток: синтетический
или со входа: устройство V4L2 (--video-source uvc|v4l2: UVC, HDMI-захват, аналоговый CVBS) или поток RTSP
(--video-source rtsp) — через тот же конвейер и тот же канал. Сетевой профиль: видеоприём (sheregesh.videoinput,
sheregesh.videoingest) переживает отключение устройства и потерю сигнала, состояние входа — в status.video.source;
описание источника можно держать в video-input.json рядом с link.json (перечитывается по SIGHUP).
Список источников (synthetic, найденные устройства V4L2, адреса rtsp_urls из video-input.json) — в
status.video.sources; пульт выбирает источник сообщением video-select (протокол 1.2), выбор сохраняется в
video-input.json, результат — в status.video.select.
Внешний модуль (S6, ADR-0012; сетевой профиль — по умолчанию, loopback — по --module): подсистема sheregesh.module с
лабораторным эмулятором модуля дочерним процессом (sheregesh_emulators.jrmodule); состояние — в status.module,
команды — сообщением module-cmd (протокол 1.3), настройки — в remote-config/ рядом с link.json (ConfigStore).
Выхода управления нет: эмулятор моделирует только состояние модуля, радиосигналов не формирует.
Органы управления пульта (S7, ADR-0013): кадры rc (сессия ≥ 1.4) принимает сторож свежести sheregesh.control
(ControlReceiver, такт 25 мс): IDLE / ACTIVE / FAILSAFE, состояние — сообщением rc-state (10 Гц, в IDLE — 1 Гц, при
переходе — сразу) и в link-status.json (control). Значения никуда не выводятся: rc-state.output = "none".
От пульта принимает только hello, ack, bye, (сессия ≥ 1.2) video-select, (сессия ≥ 1.3) module-cmd и (сессия ≥ 1.4)
rc; остальные сообщения не исполняются, а учитываются. Команд ОС, управления устройствами и радиоинтерфейсов нет.

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

from sheregesh import PROTOCOL_VERSION, __version__, sdnotify, video, videoinput, videosources
from sheregesh.videoingest import Ingest
from sheregesh.videolink import VideoServer
from sheregesh.control import wire as control_wire
from sheregesh.control.receiver import ControlReceiver
from sheregesh.link import tls
from sheregesh.link.codec import TYPES, BadMessage, Decoder, encode
from sheregesh.module.service import ModuleService
from sheregesh.protocol import negotiate
from sheregesh_emulators.devices import FanEmulator, PowerEmulator, SimClock, ThermalEmulator

# Демонстрационный сценарий удалённого блока (секунды модельного времени, как у пульта — sheregesh_emulators.demo)
DEMO_FAULTS = ["60:video-off:15", "95:silence:20"]
# Внеочередной status при смене состояния модуля — не чаще (ADR-0012, раздел 6.2)
MODULE_STATUS_MIN_S = 0.5
REMOTE_CONFIG_DIR = "remote-config"
# Пульт не забирает данные дольше — соединение зависло (буфер отправки полон): разрыв, а не ожидание в основном цикле,
# который же шлёт WATCHDOG (WatchdogSec=15 у службы) и heartbeat новому сеансу.
DRAIN_TIMEOUT_S = 3.0
# Сторож команд органов управления (ADR-0013): такт не реже 50 мс — FAILSAFE не позже timeout + 50 мс после последнего
# кадра; отдельная сопрограмма, чтобы ожидания основного тикера (delay, drain) не задерживали сторож.
CONTROL_TICK_S = 0.025
RC_STATE_ACTIVE_S = 0.1                             # rc-state в ACTIVE/FAILSAFE — 10 Гц
RC_STATE_IDLE_S = 1.0                               # … в IDLE — 1 Гц


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
        # Видеоисточник: синтетический (по умолчанию), устройство V4L2 (S3.3 «uvc» и любой захват «v4l2») или RTSP —
        # описание из аргументов или файла --video-config (video_spec). Сетевой профиль: источник ведёт видеоприём
        # (sheregesh.videoingest) — устройство ищется заново после отключения, состояние входа — в телеметрии.
        # Локальный профиль (--video, UDP 127.0.0.1): источник находится один раз при запуске, как в S3.3.
        self.video_spec = video_spec(args)
        self.video_rtsp = videosources.rtsp_items(read_video_config(args), self.video_spec)
        self.video_select = None                    # результат последнего video-select (status.video.select)
        self.scan_kw = {k: v for k, v in (("sys_root", args.video_sys_root), ("dev_root", args.video_dev_root)) if v}
        self.catalog = videosources.Catalog(lambda: (self.video_spec, self.video_rtsp), self.scan_kw)
        self.video_src, self.video_src_error = None, ""
        if self.video_spec["source"] != "synthetic":
            src = self.open_video_source()
            self.video_src, self.video_src_error = (src, "") if src.available else (None, src.error)
        uvc_ok = self.video_spec["source"] == "synthetic" or self.video_src is not None
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
            # Источник может появиться позже (горячее подключение): канал объявляется при любом его состоянии.
            self.net_video_codec = video.choose_codec(args.video_codec) if net_video and video.available() \
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
        # Внешний модуль (ADR-0012, раздел 7.4): дочерний процесс эмулятора, настройки — ConfigStore remote-1.
        self.module = ModuleService(module_config_dir(args), log=lambda s: log(f"module: {s}"),
                                    emulator_command=args.module_emulator_command) if module_enabled(args) else None
        self.module_sent = None                     # (seq, итог команды) модуля в последнем отправленном status
        self.module_seq_written = None              # … и в последнем записанном файле состояния
        self.last_status = 0.0                      # monotonic последней отправки status
        # Команды органов управления (ADR-0013): сторож свежести, rc-state пульту, control в link-status.json
        self.control = ControlReceiver(time.monotonic, timeout_ms=args.rc_timeout_ms,
                                       log=lambda s: log(f"control: {s}"))
        self.rc_state_at = 0.0                      # monotonic последней отправки rc-state
        self.hello_writer = None                    # сеанс, чей hello согласовал self.session
        self.rc_state_sent = None                   # control.transitions в последнем отправленном rc-state
        self.rc_state_task = None                   # отправка rc-state не задерживает такт сторожа
        self.control_written = None                 # control.transitions в последнем записанном файле состояния

    def open_video_source(self, spec=None):
        a = self.args
        return videoinput.open_source(spec or self.video_spec,
                                      synthetic=lambda: video.synthetic(a.video_width, a.video_height, a.video_fps),
                                      **self.scan_kw)

    def video_ingest(self):
        """Видеоприём сетевого профиля: источник по описанию, конвейер — тот же stream_sender_args (сквозной или
        с кодером), что и раньше."""
        a = self.args
        if a.video_command:                       # испытания: источник — по описанию, конвейер — подставной
            return Ingest(self.open_video_source, lambda _src: list(a.video_command), log)
        return Ingest(self.open_video_source, lambda src: video.stream_sender_args(
            self.net_video_codec, a.video_width, a.video_height, a.video_fps, src=src), log)

    def reload_video(self):
        """SIGHUP: перечитать --video-config и сменить источник (старый конвейер останавливается до запуска нового)."""
        try:
            spec = video_spec(self.args, reload=True)
        except ValueError as e:
            log(f"video config not reloaded: {e}")
            return
        self.video_rtsp = videosources.rtsp_items(read_video_config(self.args), spec)
        if spec == self.video_spec:
            log("video config unchanged")
            return
        log(f"video source: {describe_spec(self.video_spec)} -> {describe_spec(spec)}")
        self.apply_video_spec(spec)

    def apply_video_spec(self, spec):
        """Сменить источник: сетевой профиль — видеоприём (старый конвейер останавливается до запуска нового),
        локальный (UDP) — источник открывается заново, отправитель перезапускается следующим тиком."""
        self.video_spec = spec
        if self.vserver is not None:
            self.vserver.ingest.switch(lambda: self.open_video_source(spec))
        elif self.args.video:
            self.video_stop()
            src = self.open_video_source(spec) if spec["source"] != "synthetic" else None
            self.video_src, self.video_src_error = (src, "") if src is None or src.available else (None, src.error)
            if self.video_codec is None and (src is None or src.available):
                self.video_codec = video.choose_codec(self.args.video_codec)

    def select_video(self, body):
        """video-select (протокол 1.2): id из status.video.sources → смена источника и сохранение выбора в
        video-input.json. Неизвестный id — ошибка в status.video.select, источник не меняется."""
        sid, req = body.get("id"), body.get("req")
        req = req if isinstance(req, int) and not isinstance(req, bool) and 0 <= req < 2 ** 31 else 0
        shown = sid[:96] if isinstance(sid, str) else ""
        if self.vserver is None and not self.args.video:
            res = {"result": "error", "error": "video disabled on remote unit"}
        else:
            spec = self.catalog.resolve(sid)
            if spec is None:
                res = {"result": "unknown-id", "error": f"unknown video source id {shown!r}"}
            else:
                res = {"result": "ok", "error": ""}
                if spec != self.video_spec:
                    log(f"video source (console): {describe_spec(self.video_spec)} -> {describe_spec(spec)}")
                    self.apply_video_spec(spec)
                path = video_config_path(self.args)
                try:
                    if path is None:
                        raise ValueError("no video config file (--video-config / --link-config)")
                    videosources.save_spec(path, spec, SPEC_KEYS)
                    if self.args.video_source is not None:
                        res["error"] = "saved, but --video-source overrides video-input.json at start"
                except (OSError, ValueError) as e:
                    res["error"] = f"selection not saved: {e}"
        self.video_select = {"req": req, "id": shown, **res}
        log(f"video select {shown!r} (req {req}): {res['result']}{': ' + res['error'] if res['error'] else ''}")

    def video_status(self):
        """status.video: прежние поля + необязательный объект source (видеоприём; протокол 1.1 без изменений: старый
        пульт его отбрасывает). Видео выключено — source нет (пульт показывает прежний синтетический режим)."""
        out = ({"enabled": True, "active": self.vserver.active, "codec": self.net_video_codec,
                "port": self.vserver.port, "transport": "tls"} if self.vserver is not None else
               {"enabled": bool(self.video_codec), "active": self.video_proc is not None,
                "codec": self.video_codec, "port": self.video_port})
        if self.vserver is not None or self.video_codec or self.video_src_error:
            out["source"] = self.video_source_status()
            out["sources"] = self.catalog.snapshot()     # S4: список источников (необязательное поле)
        if self.video_select is not None:                # результат video-select этого сеанса (протокол 1.2)
            out["select"] = dict(self.video_select)
        return out

    def video_source_status(self):
        """Контракт status.video.source (Ingest.status). Локальный профиль (UDP): источник находится один раз при
        запуске — состояние грубое (streaming | starting | no-device)."""
        if self.vserver is not None:
            return self.vserver.ingest.status()
        a = self.args
        src = self.video_src or (videoinput.SyntheticSource(a.video_width, a.video_height, a.video_fps)
                                 if self.video_spec["source"] == "synthetic" else None)
        st = "streaming" if self.video_proc is not None else "no-device" if self.video_src_error else "starting"
        mode = getattr(src, "mode", None) if src else None
        return {"type": getattr(src, "kind", self.video_spec["source"]) if src else self.video_spec["source"],
                "input": (getattr(src, "input_type", "") if src else
                          videoinput.Unavailable(self.video_spec["source"], "").input_type).lower(),
                "name": getattr(src, "name", "") if src else "", "id": getattr(src, "id", "") if src else "",
                "device": getattr(src, "device", "") if src else "", "state": st, "error": self.video_src_error,
                "format": videoinput.FORMATS.get(mode.pixfmt, mode.pixfmt.lower()) if mode else "",
                "width": mode.width if mode else 0, "height": mode.height if mode else 0,
                "fps": mode.nominal_fps if mode else 0.0, "fps_actual": 0.0, "stream_width": 0, "stream_height": 0,
                "standard": getattr(src, "standard", "") if src else "", "restarts": 0, "since_s": 0.0}

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
        await self.send_raw(encode(self.proto, mtype, self.seq, time.time(), body))

    async def send_raw(self, data: bytes):
        writer = self.writer
        if writer is None:
            return
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), DRAIN_TIMEOUT_S)
        except (ConnectionError, RuntimeError):
            if self.writer is writer:
                self.writer = None
        except (asyncio.TimeoutError, TimeoutError):
            log(f"console does not read for {DRAIN_TIMEOUT_S:.0f} s (send buffer full): connection dropped")
            self.stats["peer_timeouts"] += 1
            writer.transport.abort()
            if self.writer is writer:
                self.writer = None

    def status_body(self):
        p = self.power.read()
        body = {"state": "alert" if self.alerts else "normal", "uptime_s": round(self.now(), 1),
                "temperatures": self.thermal.read(),
                "power": {"source": p.source, "percent": p.percent, "low_battery": p.low_battery},
                "video": self.video_status(),
                "active_events": list(self.alerts),
                "link": {k: v for k, v in self.stats.items() if isinstance(v, (int, float))}}
        if self.module is not None and "module-cmd" in TYPES.get(self.session or "", ()):
            body["module"] = self.module.status()     # только сессия ≥ 1.3: старый пульт модуль не видит
        return body

    async def send_status(self):
        self.last_status = time.monotonic()
        if self.module is not None:
            self.module_sent = self.module_key()
        await self.send("status", self.status_body())

    def module_key(self):
        """Смена состояния модуля (seq) или итога команды (configure: pending → ok) — повод для внеочередного status."""
        cmd = self.module.cmd
        return self.module.seq, (cmd["req"], cmd["result"]) if cmd else None

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
        self.video_select = None                    # результат выбора — только для сеанса, который его запросил
        self.control.on_link_lost()                 # прежний сеанс (если был) кончился; нумерация кадров — заново
        self.rc_state_sent = None
        if self.module is not None:
            self.module.new_session()               # и итог команды модулю — тоже
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
                    if self.writer is not writer and isinstance(msg, dict) and msg.get("type") == "rc":
                        continue                    # кадр закрытого или заменённого сеанса не исполняется
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
            if self.writer is None:                 # не заменён новым: команды органов управления — FAILSAFE сразу
                self.control.on_link_lost()
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
        self.control.on_link_lost()
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
            self.hello_writer = self.writer
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
        elif mtype == "video-select" and mtype in TYPES.get(self.session or "", ()):
            self.select_video(body)
        elif mtype == "module-cmd" and mtype in TYPES.get(self.session or "", ()):
            if self.module is not None:
                self.module.command(body)
                if self.writer is not None:         # итог (или pending) — сразу, не ждать heartbeat
                    await self.send_status()
            else:                                   # подсистема выключена: status.module нет, пульт видит «absent»
                self.ignored += 1
                log("module-cmd ignored: module subsystem disabled (--no-module)")
        elif mtype == "rc" and mtype in TYPES.get(self.session or "", ()):
            mono = time.monotonic()
            try:
                self.control.on_rc(control_wire.check_rc(body), mono)
            except control_wire.BadRc as e:
                self.control.on_bad(e.text, mono)
            self.control_tick(mono)                 # переход — rc-state сразу
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

    # ---------- органы управления ----------
    def control_tick(self, mono):
        """Такт сторожа и, если пора, rc-state пульту: при переходе — сразу, иначе 10 Гц (IDLE — 1 Гц). Отправка — в
        отдельной задаче: пульт, не забирающий данные, не задерживает сторож."""
        self.control.tick(mono)
        if self.writer is None or self.hello_writer is not self.writer \
                or "rc-state" not in TYPES.get(self.session or "", ()) or self.now() < self.silent_until:
            return                                  # нет сеанса, его hello ещё не принят, сессия < 1.4 или silence
        if self.rc_state_task is not None and not self.rc_state_task.done():
            return
        period = RC_STATE_IDLE_S if self.control.state() == "IDLE" else RC_STATE_ACTIVE_S
        if self.control.transitions != self.rc_state_sent or mono - self.rc_state_at >= period:
            self.rc_state_at, self.rc_state_sent = mono, self.control.transitions
            self.rc_state_task = asyncio.ensure_future(self.send("rc-state", self.control.snapshot(mono)))

    async def control_ticker(self):
        while True:
            await asyncio.sleep(CONTROL_TICK_S)
            self.control_tick(time.monotonic())

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
            if self.module is not None:
                self.module.tick()                    # миллисекунды: события эмулятора и таймеры, без ожиданий
            if self.args.status_file and (mono >= next_status or (self.writer is not None) != self.written_connected
                                          or (self.module is not None and self.module.seq != self.module_seq_written)
                                          or self.control.transitions != self.control_written):
                self.write_status()                   # смена «есть/нет пульта», состояния модуля и команд — сразу,
                # иначе раз в 2 с
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
            if self.video_codec and self.video_proc is None and t >= self.video_off_until and \
                    (self.video_src is not None or self.video_spec["source"] == "synthetic"):
                self.video_start()
            if self.video_proc is not None and self.video_proc.poll() is not None:
                log("video sender exited; restarting")
                self.video_proc = None
            if time.monotonic() >= next_hb and t >= self.silent_until:
                if self.hb_delay:
                    await asyncio.sleep(self.hb_delay)
                await self.send("heartbeat", {"uptime_s": round(t, 1)})
                await self.send_status()
                next_hb = time.monotonic() + self.args.heartbeat_interval
            elif self.module is not None and self.writer is not None and t >= self.silent_until \
                    and self.module_key() != self.module_sent and mono - self.last_status >= MODULE_STATUS_MIN_S:
                await self.send_status()              # смена состояния модуля или итога команды — внеочередной status

    def write_status(self):
        """Состояние канала для sheregesh-health (RuntimeDirectory службы), атомарно."""
        st = {"role": "remote", "time": time.time(), "software": __version__, "proto": self.proto,
              "tls": self.ctx is not None, "listen": f"{self.args.host}:{self.port}", "connected": self.writer is not None,
              "session": self.session if self.writer is not None else None, "peer": self.peer_desc, "stats": self.stats,
              "own_uri": self.tls_cfg.own_uri if self.tls_cfg else None,
              "video": self.vserver.stats() if self.vserver is not None else None,
              "module": self.module.health() if self.module is not None else None,
              "control": self.control.health()}
        path = Path(self.args.status_file)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False))
        os.replace(tmp, path)
        self.written_connected = st["connected"]
        self.module_seq_written = self.module.seq if self.module is not None else None
        self.control_written = self.control.transitions

    async def run(self):
        self.handshakes = asyncio.Semaphore(4)
        server = await asyncio.start_server(self.handle, self.args.host, self.args.port, limit=128 * 1024)
        self.port = port = server.sockets[0].getsockname()[1]
        print(f"LISTENING port={port}" + (f" tls=1.3 mutual peer={self.tls_cfg.peer_uri}" if self.ctx else ""), flush=True)
        if self.net_video_codec:
            self.vserver = VideoServer(self.tls_cfg, self.net_video_codec, self.authorize_video, log, bind=self.args.host,
                                       port=self.args.net_video_port, width=self.args.video_width,
                                       height=self.args.video_height, fps=self.args.video_fps,
                                       ingest=self.video_ingest()).start()
            print(f"VIDEO codec={self.net_video_codec} port={self.vserver.port} transport=tls1.3-mutual", flush=True)
            asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, self.reload_video)
        elif self.video_codec:
            print(f"VIDEO codec={self.video_codec} port={self.video_port}", flush=True)
        if self.video_src is not None:
            print(f"VIDEO source={self.video_src.id} mode={self.video_src.mode} device={self.video_src.device}"
                  f"{' standard=' + self.video_src.standard if self.video_src.standard else ''}", flush=True)
        if self.video_src_error and (self.args.video or self.tls_cfg is not None):
            print(f"VIDEO unavailable: {self.video_src_error}", flush=True)
        elif self.args.video and not self.video_codec and not self.net_video_codec:
            why = "; ".join(video.PROBE_FAILURES[-4:]) or ("no gst-launch-1.0/videotestsrc/udpsrc" if not video.available()
                                                         else "no working codec")
            print(f"VIDEO unavailable: {why}", flush=True)
        if self.vserver is not None or self.args.video:
            self.catalog.start()
        if self.module is not None:
            self.module.start()                       # эмулятора не ждёт: READY придёт по событиям
        sdnotify.ready(f"listening {self.args.host}:{port}" + (" mTLS" if self.ctx else " loopback"))
        async with server:
            await asyncio.gather(server.serve_forever(), self.ticker(), self.control_ticker())


def log(text):
    print(f"[remote] {text}", file=sys.stderr, flush=True)


VIDEO_CONFIG_NAME = "video-input.json"
SPEC_KEYS = {"source", "device", "mode", "format", "standard", "input", "url", "codec", "transport", "latency_ms",
             "timeout_s"}


def module_enabled(args):
    """Подсистема модуля: по умолчанию только в сетевом профиле (pi2); loopback — по --module; --no-module — выкл."""
    return not args.no_module and (args.module or bool(getattr(args, "link_config", None)))


def module_config_dir(args):
    """Хранилище настроек модуля: --remote-config-dir или remote-config/ рядом с link.json; без них — в памяти."""
    if args.remote_config_dir:
        return Path(args.remote_config_dir).expanduser()
    if getattr(args, "link_config", None):
        return Path(args.link_config).expanduser().with_name(REMOTE_CONFIG_DIR)
    return None


def video_config_path(args):
    if args.video_config:
        return Path(args.video_config)
    if getattr(args, "link_config", None):
        return Path(args.link_config).expanduser().with_name(VIDEO_CONFIG_NAME)
    return None


def read_video_config(args):
    """Содержимое video-input.json (словарь) или {} — для списка адресов rtsp_urls (ошибки журналирует video_spec)."""
    path = video_config_path(args)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def video_spec(args, reload=False):
    """Описание источника (словарь для videoinput.open_source). Явный --video-source — из аргументов; иначе файл
    --video-config / video-input.json рядом с link.json (если есть); иначе синтетический. reload=True — только файл
    (SIGHUP): нет файла — синтетический источник; ошибка — ValueError."""
    path = video_config_path(args)
    if args.video_source is None and path is not None and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            if reload:
                raise ValueError(f"{path}: {e}") from None
            log(f"video config {path} ignored: {e}")
            raw = None
        if isinstance(raw, dict):
            spec = {k: v for k, v in raw.items() if k in SPEC_KEYS}
            spec["source"] = str(spec.get("source") or "synthetic").lower()
            return spec
        if reload:
            raise ValueError(f"{path}: expected a JSON object")
    elif reload:
        if path is None or args.video_source is not None:
            raise ValueError("no --video-config file" if path is None else "--video-source given: file not used")
        return {"source": "synthetic"}               # файл удалён — источник по умолчанию (синтетический)
    source = args.video_source or "synthetic"
    mode = args.video_mode or (f"{args.video_width}x{args.video_height}@{args.video_fps}" if source == "uvc" else "auto")
    spec = {"source": source}
    if source in ("uvc", "v4l2"):
        spec.update(device=args.video_device, mode=mode, format=args.video_format, standard=args.video_standard)
    elif source == "rtsp":
        spec.update(url=args.video_url, codec=args.video_rtsp_codec)
    return spec


def describe_spec(spec):
    """Для журнала: без учётных данных RTSP."""
    return ", ".join(f"{k}={videoinput.redact(str(v)) if k == 'url' else v}" for k, v in sorted(spec.items()))


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
    ap.add_argument("--video-source", choices=list(videoinput.SOURCE_KINDS), default=None,
                    help="источник видео: synthetic — videotestsrc (по умолчанию); uvc — устройство uvcvideo (S3.3); "
                         "v4l2 — любое устройство захвата V4L2 (UVC, HDMI-захват, аналоговый usbtv/stk1160); rtsp — поток "
                         "RTSP (--video-url). Без аргумента — из --video-config, если файл есть")
    ap.add_argument("--video-device", default="", help="устройство V4L2: /dev/v4l/by-id/… или by-path/… (или имя ссылки), "
                                                       "vvvv:pppp[:serial], имя карты, bus_info, videoN; по умолчанию — "
                                                       "единственное найденное")
    ap.add_argument("--video-format", choices=["auto", "yuyv", "uyvy", "mjpeg", "h264"], default="auto",
                    help="формат устройства; совпадающий с кодеком канала передаётся без перекодирования")
    ap.add_argument("--video-mode", default=None, help="режим устройства V4L2: WxH@fps или auto (по умолчанию: uvc — "
                                                       "--video-width/height/fps, v4l2 — auto)")
    ap.add_argument("--video-standard", choices=["auto", "pal", "ntsc"], default="auto",
                    help="аналоговый стандарт (CVBS): PAL 720×576@25, NTSC 720×480@29,97; auto — по сигналу")
    ap.add_argument("--video-url", default="", help="адрес RTSP: rtsp://host[:port]/path")
    ap.add_argument("--video-rtsp-codec", choices=["h264", "mjpeg"], default="h264", help="кодек потока RTSP")
    ap.add_argument("--video-config", default=None,
                    help="описание источника в JSON (ключи как в sheregesh.videoinput.open_source); по умолчанию — "
                         "video-input.json рядом с --link-config, если есть; перечитывается по SIGHUP")
    ap.add_argument("--net-video-port", type=int, default=None, help="порт видеоканала (по умолчанию video_port профиля)")
    ap.add_argument("--video-command", nargs="+", default=None, help=argparse.SUPPRESS)   # испытания: подмена генератора
    ap.add_argument("--video-sys-root", default=None, help=argparse.SUPPRESS)  # испытания: поддельные sysfs и /dev
    ap.add_argument("--video-dev-root", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--link-config", help="сетевой лабораторный профиль: TLS 1.3 + взаимная аутентификация (S3.1)")
    ap.add_argument("--event-interval", type=float, default=0, help="синтетическое событие EMU-TEST каждые N с (0 — нет)")
    ap.add_argument("--status-file", help="состояние канала в JSON для sheregesh-health")
    ap.add_argument("--bind", help="адрес прослушивания в сетевом профиле (по умолчанию 0.0.0.0; тесты — 127.0.0.1)")
    ap.add_argument("--module", action="store_true",
                    help="внешний модуль (лабораторный эмулятор) в loopback; в сетевом профиле включён по умолчанию")
    ap.add_argument("--no-module", action="store_true", help="без подсистемы внешнего модуля")
    ap.add_argument("--remote-config-dir", default=None,
                    help="настройки модуля (ConfigStore, схема remote-1); по умолчанию remote-config/ рядом с "
                         "--link-config, без него — только в памяти")
    ap.add_argument("--module-emulator-command", nargs="+", default=None, help=argparse.SUPPRESS)  # испытания
    ap.add_argument("--rc-timeout-ms", type=int, default=500,
                    help="сторож команд органов управления: нет кадра rc дольше N мс — FAILSAFE (50…10000, ADR-0013)")
    args = ap.parse_args(argv)
    if not 50 <= args.rc_timeout_ms <= 10000:
        ap.error("--rc-timeout-ms: ожидается 50…10000")
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
        agent.catalog.close()
        if agent.vserver is not None:
            agent.vserver.close()
        if agent.module is not None:
            agent.module.close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        agent.video_stop()
        if agent.module is not None:
            agent.module.close()
    return 0
