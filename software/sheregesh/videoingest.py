# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Видеоприём удалённого блока: один активный источник (sheregesh.videoinput.VideoSource), его конвейер GStreamer,
состояние и переподключение. Используется видеосервером (sheregesh.videolink.VideoServer, ADR-0011) вместо прямого
запуска генератора; защищённый канал и его кадры не меняются.

Состояния (контракт телеметрии status.video.input.state):
  off        видеоприём выключен
  idle       источник есть, потребителя (видеосеанса пульта) нет — конвейер не запущен
  no-device  устройства нет: V4L2 не найдено или исчезло (ENODEV, горячее отключение). Поиск повторяется;
             возврат — автоматически
  connect-error  сервер RTSP не принимает соединение или оборвал поток; повторяется, возврат — автоматически
  starting   конвейер запущен, первых кадров ещё нет
  streaming  кадры идут (последний — не старше no_signal_s)
  no-signal  устройство есть, кадров нет (кабель HDMI/CVBS вынут, камера RTSP не отдаёт поток) или драйвер сообщает
             V4L2_IN_ST_NO_SIGNAL/NO_POWER/NO_H_LOCK/NO_SYNC (VIDIOC_ENUMINPUT). Конвейер без кадров дольше stall_restart_s
             перезапускается (кроме случая, когда потерю сигнала сообщил драйвер — тогда ждём сигнала)
  error      конвейер завершился при наличии источника, неподдерживаемый режим, неверная конфигурация

Ограничение (честно): захваты, которые без входного сигнала продолжают слать «синее/чёрное поле» и не сообщают
статус входа (многие HDMI→UVC MS2109/MS2130, часть EasyCAP), выглядят как streaming — детектора «пустого кадра» нет
(кадры не декодируются на блоке ради этого); проверяется на стенде.

Устройство различается с сигналом так: «нет устройства» — узла /dev нет / ENODEV / сервер не принимает TCP;
«нет сигнала» — узел есть, кадров нет. После потери сигнала последний кадр не повторяется: RTP просто перестаёт
идти, и пульт через NO_VIDEO_AFTER_S (1,5 с) переходит в «нет видео» и гасит изображение.

Устройство ищется заново (opener) при каждом запуске после «нет устройства»: постоянная ссылка by-id/by-path и
VID:PID находят вернувшееся устройство и под другим /dev/videoN. Конвейер работает только пока есть видеосеанс
(attach/detach), как и раньше. При перезапуске конвейера внутри сеанса номера и метки времени RTP продолжаются
(RtpContinuity): буфер джиттера пульта видит один непрерывный поток.

Владелец процессов один — поток наблюдения (poll); attach/detach/switch только выставляют запросы. Поэтому смена
источника (switch) гарантированно останавливает старый конвейер (killpg, wait) и закрывает его каналы до запуска
нового. Архитектура рассчитана на один поток (v1.0); второй конвейер — второй экземпляр Ingest.
"""
import os
import signal
import subprocess
import threading
import time
from collections import deque

from . import video
from .videolink import Rfc4571, parse_sps, rtp_info, sps_from_rtp

STATES = ("off", "idle", "no-device", "connect-error", "starting", "streaming", "no-signal", "error")
# Состояния для пульта (контракт sheregesh_console/videosource.py: SOURCE_STATES); idle пульт видит как starting —
# видеосеанс вот-вот начнётся (пульт подключает видео сразу по телеметрии).
REPORTED_STATE = {"idle": "starting"}
RTP_CLOCK = 90000


class CommandSource:
    """Источник «готовая команда генератора» (испытания, --video-command): всегда присутствует."""
    kind, id, name, input_type, standard, device = "command", "command", "external generator", "V4L2", "", ""
    available, missing, error, mode = True, False, "", None

    def present(self):
        return True, ""

    def signal(self):
        return None


class RtpContinuity:
    """Непрерывные номер последовательности, метка времени и SSRC RTP на выходе, когда генератор перезапущен внутри
    одного видеосеанса. Первое поколение проходит без изменений (байт в байт); каждое следующее продолжает номера
    с последнего отправленного, а метку времени — на прошедшее реальное время (часы RTP 90 кГц)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.ssrc = None
        self.last = None           # (seq, ts, wall) последнего выданного пакета
        self.off = None            # (seq_off, ts_off) текущего поколения; None — ещё не определено
        self.first = True

    def new_generation(self):
        self.off = None

    def rewrite(self, pkt, wall):
        seq, ts = int.from_bytes(pkt[2:4], "big"), int.from_bytes(pkt[4:8], "big")
        if self.off is None:
            if self.first or self.last is None:
                self.off, self.ssrc, self.first = (0, 0), pkt[8:12], False
            else:
                lseq, lts, lwall = self.last
                gap = max(1, int((wall - lwall) * RTP_CLOCK))
                self.off = ((lseq + 1 - seq) & 0xFFFF, (lts + gap - ts) & 0xFFFFFFFF)
        so, to = self.off
        oseq, ots = (seq + so) & 0xFFFF, (ts + to) & 0xFFFFFFFF
        self.last = (oseq, ots, wall)
        if (so, to) == (0, 0) and pkt[8:12] == self.ssrc:
            return pkt
        return pkt[:2] + oseq.to_bytes(2, "big") + ots.to_bytes(4, "big") + self.ssrc + pkt[12:]


def jpeg_size(payload):
    """(ширина, высота) из заголовка RTP/JPEG (RFC 2435: размеры в блоках 8×8), только первый фрагмент кадра."""
    if len(payload) >= 8 and int.from_bytes(payload[1:4], "big") == 0:
        return payload[6] * 8, payload[7] * 8
    return None


class Ingest:
    def __init__(self, opener, command_for, log=lambda m: None, spawn=video.spawn, clock=time.monotonic,
                 no_signal_s=2.0, start_timeout_s=8.0, stall_restart_s=10.0, presence_s=2.0, signal_s=1.0,
                 max_backoff_s=8.0, queue=4096):
        self._opener, self._command_for, self._log, self._spawn_fn, self._clock = opener, command_for, log, spawn, clock
        self.no_signal_s, self.start_timeout_s, self.stall_restart_s = no_signal_s, start_timeout_s, stall_restart_s
        self.presence_s, self.signal_s, self.max_backoff_s = presence_s, signal_s, max_backoff_s
        self.src = None
        self._shown = None                           # последний найденный источник (в т. ч. Unavailable) — для status()
        self.state, self.error, self._since = "idle", "", clock()
        self.restarts = 0
        self.transitions = deque(maxlen=50)          # (время, состояние, причина) — для журнала и испытаний
        self._want = self._owner = None              # запрошенный и текущий видеосеанс (токен)
        self._token = 0
        self._pending = None                         # новый opener (switch)
        self._proc = None
        self._readers = []
        self._gen = 0
        self._q = deque(maxlen=queue)
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        os.set_blocking(self._w, False)
        self._frames = deque(maxlen=1000)
        self._last_frame = None
        self._started_at = 0.0
        self._next_start = self._next_presence = self._next_signal = 0.0
        self._backoff = 0
        self._sig = None
        self._stderr = deque(maxlen=12)
        self._continuity = RtpContinuity()
        self.stream = {}                             # фактический размер потока (SPS H.264 / заголовок JPEG)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._monitor = None
        self._closed = False

    @classmethod
    def fixed(cls, command, log=lambda m: None, **kw):
        """Ingest с готовой командой генератора (испытания, прежний параметр VideoServer.command)."""
        return cls(CommandSource, lambda _src: command, log, **kw)

    # ---------- запросы (любой поток) ----------
    def start(self):
        if self._monitor is None:
            self._monitor = threading.Thread(target=self._run, name="video-ingest", daemon=True)
            self._monitor.start()
        return self

    def attach(self):
        """Видеосеанс начался: нужен поток. Возвращает токен для detach."""
        self._token += 1
        self._want = self._token
        self._wake.set()
        return self._token

    def detach(self, token):
        if self._want == token:
            self._want = None
            self._wake.set()

    def switch(self, opener):
        """Сменить источник: старый конвейер останавливается, его каналы закрываются, затем — новый источник."""
        self._pending = opener
        self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._monitor is not None and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=10)
        self._kill()
        if not self._closed:
            self._closed = True
            for fd in (self._r, self._w):
                try:
                    os.close(fd)
                except OSError:
                    pass

    @property
    def wake_fd(self):
        return self._r

    @property
    def pid(self):
        """PID группы процессов текущего конвейера (None — не запущен): диагностика и испытания отказов."""
        p = self._proc
        return p.pid if p is not None else None

    def take(self):
        """Пакеты RTP для отправки: [(time.time() приёма, пакет)] — читает видеосеанс после пробуждения wake_fd."""
        try:
            while os.read(self._r, 4096):
                pass
        except (BlockingIOError, OSError):
            pass
        out = []
        while True:
            try:
                out.append(self._q.popleft())
            except IndexError:
                return out

    # ---------- наблюдение (один поток) ----------
    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as e:                            # noqa: BLE001 — поток наблюдения не должен умирать
                self._set("error", f"ingest: {type(e).__name__}: {e}")
            self._wake.wait(0.2)
            self._wake.clear()
        self._kill()

    def _set(self, state, error=""):
        if state != self.state:
            self._since = self._clock()
            self.transitions.append((time.time(), state, error))
            self._log(f"video input: {self.state} -> {state}{': ' + error if error else ''}")
        self.state, self.error = state, error

    @staticmethod
    def _absent(src):
        return "connect-error" if getattr(src, "kind", "") == "rtsp" else "no-device"

    def _resolve(self):
        if self.src is None:
            self.src = self._shown = self._opener()
        return self.src

    def _schedule(self, now, delay=None):
        if delay is None:
            delay = min(self.max_backoff_s, 2 ** self._backoff)
            self._backoff += 1
        self._next_start = now + delay

    def poll(self):
        now = self._clock()
        if self._pending is not None:
            opener, self._pending = self._pending, None
            self._kill()
            self._opener, self.src, self._shown = opener, None, None
            self._backoff, self._next_start, self._next_presence = 0, 0.0, 0.0
            self.stream = {}
            self._set("starting" if self._owner is not None else "idle")
        want = self._want
        if want != self._owner:                               # видеосеанс начался, сменился или кончился
            self._kill()
            self._owner, self._backoff, self._next_start, self._next_presence = want, 0, 0.0, 0.0
            self._q.clear()
            self._continuity.reset()
            if want is not None:
                self._set("starting")
        if self._owner is None:
            self._poll_idle(now)
        else:
            self._poll_attached(now)

    def _poll_idle(self, now):
        if now < self._next_presence:
            return
        self._next_presence = now + self.presence_s
        src = self._resolve()
        ok, why = src.present()
        if ok:
            self._set("idle")
        else:
            self._set(self._absent(src) if src.missing or src.available else "error", why or src.error)
            if src.missing or src.available:
                self.src = None                               # искать заново (by-id / VID:PID)

    def _poll_attached(self, now):
        proc = self._proc
        if proc is not None and proc.poll() is not None:     # конвейер завершился
            rc = proc.returncode
            self._kill()
            self.restarts += 1
            ok, why = self.src.present() if self.src is not None else (False, "")
            if not ok:
                self._set(self._absent(self.src), why or "source disappeared")
                self.src = None
                self._schedule(now, self.presence_s)
            else:
                tail = " | ".join(x for x in self._stderr if x)[-200:]
                self._set("error", f"pipeline exited with code {rc}" + (f": {tail}" if tail else ""))
                self._schedule(now)
            return
        if proc is None:
            if now < self._next_start:
                return
            src = self._resolve()
            if not src.available:
                self._set("no-device" if src.missing else "error", src.error)
                self.src = None
                self._schedule(now, self.presence_s)
                return
            ok, why = src.present()
            if not ok:
                self._set(self._absent(src), why)
                self.src = None
                self._schedule(now, self.presence_s)
                return
            try:
                cmd = self._command_for(src)
            except Exception as e:                            # noqa: BLE001 — нет кодека/элемента: причина оператору
                self._set("error", f"pipeline: {e}")
                self._schedule(now)
                return
            self._spawn(cmd, now)
            if self.state != "no-signal":
                self._set("starting")
            return
        last = self._last_frame
        if last is not None and now - last <= self.no_signal_s:
            self._sig = None
            self._set("streaming")
            self._backoff = 0
            return
        if now >= self._next_signal:
            self._next_signal = now + self.signal_s
            self._sig = self.src.signal() if self.src is not None else None
        if self._sig == "no-device":
            self._kill()
            dev = getattr(self.src, "device", "")
            self._set("no-device", f"device {dev} disappeared" if dev else "device disappeared")
            self.src = None
            self._schedule(now, self.presence_s)
            return
        since = last if last is not None else self._started_at
        if last is None and now - self._started_at <= self.start_timeout_s and self._sig != "no-signal" and \
                self.state != "no-signal":
            self._set("starting")
            return
        self._set("no-signal", "driver reports no signal on input" if self._sig == "no-signal" else
                  f"no frames for {now - since:.0f} s")
        if self._sig != "no-signal" and now - since > self.stall_restart_s:
            ok, why = self.src.present() if self.src is not None else (False, "")
            self._kill()
            self.restarts += 1
            if not ok:
                self._set(self._absent(self.src), why)
                self.src = None
                self._schedule(now, self.presence_s)
            else:
                self._started_at = now
                self._schedule(now)

    # ---------- процесс ----------
    def _spawn(self, cmd, now):
        self._stderr.clear()
        proc = self._spawn_fn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._gen += 1
        self._continuity.new_generation()
        self._proc, self._started_at, self._last_frame = proc, now, None
        self._readers = [threading.Thread(target=self._read_rtp, args=(proc, self._gen), name="video-ingest-rtp",
                                          daemon=True),
                         threading.Thread(target=self._read_err, args=(proc,), name="video-ingest-err", daemon=True)]
        for t in self._readers:
            t.start()

    def _kill(self):
        p, self._proc = self._proc, None
        readers, self._readers = self._readers, []
        if p is None:
            return
        self._gen += 1                                        # пакеты убитого поколения больше не принимаются
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=3)
        except Exception:                                    # noqa: BLE001 — процесс уже снят killpg
            pass
        for t in readers:
            if t is not threading.current_thread():
                t.join(timeout=3)
        for f in (p.stdout, p.stderr):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass

    def _read_err(self, proc):
        try:
            for line in iter(proc.stderr.readline, b""):
                self._stderr.append(line.decode("utf-8", "replace").strip()[:200])
        except (OSError, ValueError):
            pass

    def _read_rtp(self, proc, gen):
        parser = Rfc4571()
        try:
            fd = proc.stdout.fileno()
            while True:
                data = os.read(fd, 65536)
                if not data:
                    return
                if gen != self._gen:
                    continue
                now_m, wall = self._clock(), time.time()
                queued = False
                for pkt in parser.feed(data):
                    info = rtp_info(pkt)
                    if info is None:
                        continue
                    _seq, marker, payload = info
                    if marker:
                        self._frames.append(now_m)
                        self._last_frame = now_m
                    if "width" not in self.stream:
                        self._detect_size(pkt[1] & 0x7F, payload)
                    if self._owner is not None and gen == self._gen:
                        self._q.append((wall, self._continuity.rewrite(pkt, wall)))
                        queued = True
                if queued:
                    try:
                        os.write(self._w, b"\x01")
                    except (BlockingIOError, OSError):
                        pass
        except (OSError, ValueError):
            pass

    def _detect_size(self, pt, payload):
        try:
            if pt == 26:
                size = jpeg_size(payload)
                if size:
                    self.stream = {"width": size[0], "height": size[1]}
            else:
                nal = sps_from_rtp(payload)
                if nal is not None:
                    sps = parse_sps(nal)
                    self.stream = {"width": sps["width"], "height": sps["height"]}
        except (IndexError, ValueError):
            pass

    # ---------- состояние ----------
    def fps(self, window=3.0):
        now = self._clock()
        if self.state != "streaming":
            return 0.0
        span = min(window, max(0.5, now - self._started_at))
        return round(sum(1 for t in list(self._frames) if now - t <= span) / span, 1)

    def status(self):
        """Контракт для пульта (status.video.source, sheregesh_console/videosource.py::remote_fields) и
        sheregesh-health. Основные поля пульта: input (synthetic|hdmi|cvbs|usb|v4l2|rtsp), state (no-device|
        no-signal|starting|streaming|connect-error|error), device, error, width, height, fps (номинальные режима;
        у RTSP — из потока). Дополнительно: fps_actual (фактическая за 3 с), stream_width/height (из SPS/JPEG),
        type, name, id, format, standard, restarts, since_s, transport, transport_mode (RTSP). Без учётных данных."""
        src = self._shown
        mode = getattr(src, "mode", None)
        fmt = ""
        if mode is not None:
            from .videoinput import FORMATS
            fmt = FORMATS.get(mode.pixfmt, mode.pixfmt.lower())
        width = (mode.width if mode is not None else 0) or self.stream.get("width", 0)
        height = (mode.height if mode is not None else 0) or self.stream.get("height", 0)
        actual = self.fps()
        return {"type": getattr(src, "kind", "") or "", "input": (getattr(src, "input_type", "") or "").lower(),
                "name": getattr(src, "name", "") or "", "id": getattr(src, "id", "") or "",
                "device": getattr(src, "device", "") or "", "state": REPORTED_STATE.get(self.state, self.state),
                "error": self.error, "format": fmt, "width": width, "height": height,
                "fps": (mode.nominal_fps if mode is not None and hasattr(mode, "nominal_fps") else 0.0) or actual,
                "fps_actual": actual, "stream_width": self.stream.get("width", 0),
                "stream_height": self.stream.get("height", 0), "standard": getattr(src, "standard", "") or "",
                "restarts": self.restarts, "since_s": round(self._clock() - self._since, 1),
                # RTSP (S5): фактический транспорт tcp|udp, пока поток идёт и транспорт однозначен, иначе «»;
                # transport_mode — заданный auto|tcp|udp (у прочих источников оба «»)
                "transport": (getattr(src, "transport", "") or "") if self.state == "streaming" else "",
                "transport_mode": getattr(src, "transport_mode", "") or ""}
