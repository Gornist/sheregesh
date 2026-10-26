# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выход CRSF удалённого блока (ADR-0014, раздел 2): поток отправки кадров модулю с абсолютным расписанием.

Хост ведущий: в каждом периоде уходит не больше одного кадра (как EdgeTX), затем до следующего периода принимаются
ответы модуля. Перед отправкой разбирается всё принятое: 0x3A.0x10 (синхронизация: период и смещение), 0x29 (device
info от 0xEE), 0x14 (link statistics); любой верный кадр обновляет last_rx.

Выбор кадра периода по source() → (mode, ch[8], btn):
  служебная очередь (queue) → ping 0x28 (раз в ping_interval_s, пока нет device info, или по request_ping) → каналы;
  mode "none": каналов нет, только ping раз в ping_interval_s (признак присутствия модуля держится ответами);
  mode "neutral": каналы из source (нейтраль/failsafe_ch выбирает владелец), взведение false;
  mode "live": каналы пульта, взведение true. Каналы 9–16 — центр; кнопки в каналы не идут (S8).
Ошибка source() — как "neutral" с нулями (source_errors): линк не рвётся, ровер получает центр.

Период: до синхронизации — 4000 мкс (EdgeTX на 400K); после — период модуля из 0x3A.0x10 (короче 1 мс — кратное,
ограничение 1…50 мс) плюс поправка фазы по смещению (положительное — кадры приходят рано: следующий период
удлиняется), не больше четверти периода за цикл; синхронизация старше 2 с — назад к 4000 мкс. Расписание абсолютное:
следующий срок = прежний срок + период; опоздание больше периода — расписание от текущего момента (overruns).
Джиттер — опоздание пробуждения относительно срока (мкс), p99 по последним 512 циклам.

Часы (clock) и ожидание (wait(срок)) внедряются: тесты крутят step()/run() на SimClock без потоков.
"""
import threading
import time
from collections import deque

from . import frames as F

DEFAULT_PERIOD_US = 4000            # до синхронизации, 400000 бод
MIN_PERIOD_US, MAX_PERIOD_US = 1000, 50000
SYNC_STALE_S = 2.0
LINK_STALE_S = 2.0
DEVICE_STALE_S = 2.0                # нет ответов дольше — device info забывается, ping возобновляется
PING_INTERVAL_S = 0.1
JITTER_WINDOW = 512
MODES = ("none", "neutral", "live")
AXES = 8
LOG_EVERY_S = 10.0


def _p99(samples):
    if not samples:
        return 0
    s = sorted(samples)
    return int(s[min(len(s) - 1, int(len(s) * 0.99))])


def module_period_us(interval_us: float) -> float:
    """Период модуля → период хоста: короче 1 мс — кратное периоду модуля, затем ограничение 1…50 мс."""
    p = float(interval_us)
    if p <= 0:
        return float(DEFAULT_PERIOD_US)
    if p < MIN_PERIOD_US:
        p *= -(-MIN_PERIOD_US // p)
    return max(float(MIN_PERIOD_US), min(float(MAX_PERIOD_US), p))


class CrsfOutput:
    def __init__(self, port, source, clock=time.monotonic, log=None, wait=None, on_frame=None,
                 status_byte: bool = True, ping_interval_s: float = PING_INTERVAL_S):
        self.port = port
        self.source = source
        self.clock = clock
        self.log = log or (lambda _s: None)
        self.on_frame = on_frame
        self.status_byte = bool(status_byte)
        self.ping_interval_s = float(ping_interval_s)
        self._wait = wait or self._wait_real
        self.parser = F.Parser(F.HOST_RX_SYNCS)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._service = deque()
        self._ping_req = False
        self._next_ping = None
        self._deadline = None
        self._jitter = deque(maxlen=JITTER_WINDOW)
        self._mode = "none"
        self._armed = None
        self._ch = None
        self._sync = None                # (время, Timing)
        self._phase_us = 0.0             # непотраченная поправка фазы
        self._link = None                # (время, LinkStats)
        self._device = None              # DeviceInfo
        self._last_rx = None
        self._last_log = {}
        self.frames_sent = self.pings_sent = self.service_sent = 0
        self.overruns = self.write_errors = self.source_errors = self.rx_frames = 0

    # ---------- поток ----------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="crsf-output", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        while not self._stop.is_set():
            try:
                deadline = self.step()
            except Exception as e:            # поток не должен умирать молча: журнал и пауза
                self._rate_log("step", f"crsf output step failed: {e!r}")
                deadline = self.clock() + 0.05
            self._wait(deadline)

    def _wait_real(self, deadline: float) -> None:
        delay = deadline - self.clock()
        if delay > 0:
            self._stop.wait(delay)

    def run(self, cycles: int) -> None:
        """cycles циклов в вызывающем потоке (тесты: wait продвигает SimClock до срока)."""
        for _ in range(int(cycles)):
            self._wait(self.step())

    # ---------- управление ----------
    def request_ping(self) -> None:
        """Ping 0x28 в ближайшем периоде (повторное опознание модуля)."""
        with self._lock:
            self._ping_req = True

    def queue(self, frame: bytes) -> None:
        """Служебный кадр (уже собранный, с CRC) — вместо кадра каналов в ближайшем периоде."""
        with self._lock:
            self._service.append(bytes(frame))

    def forget_device(self) -> None:
        with self._lock:
            self._device = None

    # ---------- один цикл ----------
    def step(self) -> float:
        """Принять ответы, выбрать и отправить кадр периода; вернуть срок следующего цикла (шкала clock)."""
        now = self.clock()
        self._poll_rx(now)
        mode, ch = self._read_source()
        with self._lock:
            if self._deadline is None:
                self._deadline = now
            period_s = self._period_us(now) / 1e6
            late = now - self._deadline
            if late > period_s:
                self.overruns += 1
                self._rate_log("overrun", f"crsf output overrun: {late * 1e3:.1f} ms late, schedule reset")
                self._deadline = now
                late = 0.0
            self._expire(now)
            self._jitter.append(max(0.0, late) * 1e6)
            frame = self._choose(now, mode, ch)
            adj = self._spend_phase(period_s * 1e6) / 1e6
            self._deadline += period_s + adj
            deadline = self._deadline
        if frame is not None:
            try:
                self.port.write(frame)
            except OSError as e:
                self.write_errors += 1
                self._rate_log("write", f"crsf write failed: {e}")
        return deadline

    def _read_source(self):
        try:
            mode, ch, _btn = self.source()
            if mode not in MODES:
                raise ValueError(f"bad mode {mode!r}")
            ch = [max(-1000, min(1000, int(v))) for v in list(ch)[:AXES]]
            ch += [0] * (AXES - len(ch))
            return mode, ch
        except Exception as e:
            self.source_errors += 1
            self._rate_log("source", f"crsf source failed: {e!r}; neutral")
            return "neutral", [0] * AXES

    def _choose(self, now, mode, ch):
        self._mode = mode
        if self._service:
            self.service_sent += 1
            return self._service.popleft()
        ping_due = self._next_ping is None or now >= self._next_ping
        if mode == "none" or self._ping_req or (self._device is None and ping_due):
            self._armed = None
            self._ch = None
            if self._ping_req or ping_due:
                self._ping_req = False
                self._next_ping = now + self.ping_interval_s
                self.pings_sent += 1
                return F.ping()
            return None
        armed = mode == "live"
        self._armed, self._ch = armed, list(ch)
        self.frames_sent += 1
        return F.pack_channels(list(ch) + [0] * (F.CHANNELS - AXES), armed if self.status_byte else None)

    def _period_us(self, now) -> float:
        if self._sync is not None and now - self._sync[0] <= SYNC_STALE_S:
            return module_period_us(self._sync[1].interval_us)
        if self._sync is not None:
            self._sync = None
            self._phase_us = 0.0
            self.log("crsf: sync lost (> 2 s), period 4000 us")
        return float(DEFAULT_PERIOD_US)

    def _expire(self, now):
        if self._device is not None and (self._last_rx is None or now - self._last_rx > DEVICE_STALE_S):
            self._device = None

    def _spend_phase(self, period_us) -> float:
        if not self._phase_us:
            return 0.0
        lim = period_us / 4
        adj = max(-lim, min(lim, self._phase_us))
        self._phase_us -= adj
        return adj

    # ---------- приём ----------
    def _poll_rx(self, now):
        try:
            data = self.port.read()
        except OSError as e:
            self._rate_log("read", f"crsf read failed: {e}")
            return
        if not data:
            return
        got = self.parser.feed(data)
        if not got:
            return
        with self._lock:
            for fr in got:
                self.rx_frames += 1
                self._last_rx = now
                self._handle(now, fr)
        if self.on_frame is not None:
            for fr in got:
                self.on_frame(fr)

    def _handle(self, now, fr):
        try:
            if fr.type == F.T_RADIO and fr.dest in (F.ADDR_RADIO, F.ADDR_BROADCAST):
                t = F.timing(fr.payload)
                if t is not None and t.interval_us > 0:
                    if self._sync is None:
                        self.log(f"crsf: synced, module period {t.interval_us:.1f} us")
                    self._sync = (now, t)
                    period = module_period_us(t.interval_us)
                    off = t.offset_us % period                 # фаза по модулю периода: (−период/2; период/2]
                    if off > period / 2:
                        off -= period
                    self._phase_us = off
            elif fr.type == F.T_DEVICE_INFO and fr.origin == F.ADDR_TX:
                info = F.device_info(fr.payload)
                if self._device is None or self._device != info:
                    self.log(f"crsf: device {info.name!r} sw {info.sw} serial {info.serial_text}")
                self._device = info
            elif fr.type == F.T_LINK_STATS:
                self._link = (now, F.link_stats(fr.payload))
        except F.BadFrame:
            pass

    # ---------- диагностика ----------
    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            synced = self._sync is not None and now - self._sync[0] <= SYNC_STALE_S
            link = None
            if self._link is not None and now - self._link[0] <= LINK_STALE_S:
                ls = self._link[1]
                link = {"rssi": ls.rssi, "lq": ls.up_lq, "snr": ls.up_snr, "rf_mode": ls.rf_mode,
                        "tx_power": ls.tx_power_mw}
            self._expire(now)
            dev = self._device
            return {
                "mode": self._mode,
                "period_us": int(round(self._period_us(now))),
                "synced": synced,
                "sync_age_ms": None if self._sync is None else int((now - self._sync[0]) * 1000),
                "offset_us": None if self._sync is None else round(self._sync[1].offset_us, 1),
                "frames_sent": self.frames_sent,
                "pings_sent": self.pings_sent,
                "service_sent": self.service_sent,
                "jitter_us_p99": _p99(self._jitter),
                "overruns": self.overruns,
                "crc_errors": self.parser.crc_errors,
                "crc_errors_run": self.parser.crc_run,
                "garbage_bytes": self.parser.garbage_bytes,
                "rx_frames": self.rx_frames,
                "collisions": int(getattr(self.port, "collisions", 0)),
                "write_errors": self.write_errors + int(getattr(self.port, "write_errors", 0)),
                "source_errors": self.source_errors,
                "armed": self._armed,
                "ch": None if self._ch is None else list(self._ch),
                "link": link,
                "device": None if dev is None else {"name": dev.name, "sw": dev.sw, "serial": dev.serial_text},
                "last_rx_ms": None if self._last_rx is None else int((now - self._last_rx) * 1000),
            }

    def _rate_log(self, key, text):
        now = self.clock()
        last = self._last_log.get(key)
        if last is None or now - last >= LOG_EVERY_S:
            self._last_log[key] = now
            self.log(text)
