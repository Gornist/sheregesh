# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Лабораторный эмулятор передающего модуля ELRS (ADR-0014, раздел 3.1) — «RM Nomad X-Band (emu)» на псевдотерминале.

Модель поведения ELRS TX со стороны хоста (по описанию в docs/hardware/EDGETX-EXTERNAL-MODULE.md, своими словами):
модуль ведомый — отвечает только после принятого кадра хоста с верной CRC, не больше max_reply байт целыми кадрами за
период; на ping 0x28 (адресат 0x00 или 0xEE) — device info 0x29; раз в 200 мс — синхронизация 0x3A.0x10 (период
радиопакетов и смещение в 0,1 мкс) и link statistics 0x14. Смещение — среднее по последним 4 кадрам каналов времени
от прихода кадра до ближайшего радиопакета минус запас 100 мкс, по модулю периода (> 0 — хост рано). Кадры каналов
0x16 проверяются по CRC, хранятся последние значения и взведение (байт состояния; длина 24 — None), считаются частота
и джиттер интервалов (p99 отклонения от медианы). Без верных кадров хоста дольше 1 с — «uart-disconnected»: синхронизация и статистика не формируются.

Лабораторные сценарии: silent — модуль молчит (кадры хоста принимаются, ответов нет); garbage — перед каждым ответом
случайные байты (детерминированно по seed), кадры остаются верными; crc — CRC каждого ответа испорчена; heal — норма.

Ядро ElrsTxEmulator — чистая модель на внедрённых часах: одинаковые входы и seed дают одинаковые байты. LoopbackPort —
порт CrsfOutput поверх ядра в одном процессе (unit-тесты без pty). Дочерний процесс агента (main):

  python3 -m sheregesh_emulators.elrs_tx [--tty PATH] [--period-us 20000] [--seed 0] [--state-ms 200]

--tty — ведомая сторона PtyPort хоста; без него эмулятор открывает свой псевдотерминал и сообщает путь в hello.
stdout — JSON-строки: hello, state (раз в --state-ms и при смене состояния линии), ответы на команды; stdin — команды
JSON-строками: {"op":"lab","scenario":…}, {"op":"state"}, {"op":"keepalive"}, {"op":"period","us":N}. EOF stdin —
выход 0. Журнал — stderr с префиксом [elrs_tx].
"""
import argparse
import json
import os
import random
import select
import sys
import time
from collections import deque

from sheregesh.crsf import frames as F

NAME = "RM Nomad X-Band (emu)"
SW = (4, 1, 0)
PERIOD_US = 20000                  # 50 Гц радиопакетов
SYNC_INTERVAL_S = 0.2
STATS_INTERVAL_S = 0.2
DISCONNECT_S = 1.0
MAX_REPLY = 64                     # байт ответа за период (кадр максимальной длины, спецификация CRSF)
LEAD_MARGIN_US = 100.0
LEAD_WINDOW = 4
RATE_WINDOW_S = 1.0
INTERVAL_WINDOW = 100
LAB_SCENARIOS = ("silent", "garbage", "crc", "heal")
MAX_LINE = 4096
CONNECTED, DISCONNECTED = "connected", "uart-disconnected"


def _p99(values):
    if not values:
        return 0
    s = sorted(values)
    return int(s[min(len(s) - 1, int(len(s) * 0.99))])


def _median(values):
    s = sorted(values)
    n = len(s)
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) if n else None


class ElrsTxEmulator:
    """Модель ELRS TX: feed(байты хоста) → ответы в drain_tx(); step() — сторож линии; state() — снимок."""

    def __init__(self, clock, period_us: float = PERIOD_US, name: str = NAME, seed: int = 0,
                 max_reply: int = MAX_REPLY, sync_interval_s: float = SYNC_INTERVAL_S,
                 stats_interval_s: float = STATS_INTERVAL_S, disconnect_s: float = DISCONNECT_S):
        self.clock = clock
        self.period_us = float(period_us)
        self.name = name
        self.max_reply = int(max_reply)
        self.sync_interval_s = float(sync_interval_s)
        self.stats_interval_s = float(stats_interval_s)
        self.disconnect_s = float(disconnect_s)
        self.rng = random.Random(seed)
        self.parser = F.Parser(F.MODULE_RX_SYNCS)
        self.t0 = clock()                       # фаза радиопакетов: t0 + k × период
        self.scenario = "normal"
        self.host = DISCONNECTED
        self._last_host = None
        self._next_sync = self._next_stats = None
        self._queue = deque()                   # (вид, байты) к отправке хосту
        self._tx = bytearray()
        self._events = []
        self._leads = deque(maxlen=LEAD_WINDOW)
        self._rc_times = deque()
        self._intervals = deque(maxlen=INTERVAL_WINDOW)
        self._last_rc = None
        self.ch = None                          # последние 16 отсчётов
        self.armed = None
        self.frame_len = None
        self.rc_frames = self.pings = self.host_frames = self.other_frames = 0
        self.sync_sent = self.stats_sent = self.info_sent = 0

    # ---------- линия ----------
    def feed(self, data: bytes) -> None:
        for fr in self.parser.feed(data):
            self._on_frame(self.clock(), fr)

    def drain_tx(self) -> bytes:
        out, self._tx = bytes(self._tx), bytearray()
        return out

    def step(self) -> list:
        """Сторож линии: без кадров хоста дольше disconnect_s — uart-disconnected. Возвращает накопленные события."""
        now = self.clock()
        if self.host == CONNECTED and now - self._last_host > self.disconnect_s:
            self.host = DISCONNECTED
            self._queue.clear()
            self._events.append({"ev": "host", "state": DISCONNECTED})
        ev, self._events = self._events, []
        return ev

    def _on_frame(self, now, fr):
        self.host_frames += 1
        self._last_host = now
        if self.host != CONNECTED:
            self.host = CONNECTED
            self._next_sync = now
            self._next_stats = now + self.stats_interval_s / 2
            self._events.append({"ev": "host", "state": CONNECTED})
        if fr.type == F.T_CHANNELS:
            try:
                raw, armed = F.unpack_channels(fr.payload)
            except F.BadFrame:
                self.other_frames += 1
            else:
                self.ch, self.armed, self.frame_len = raw, armed, len(fr.payload) + 2
                self.rc_frames += 1
                if self._last_rc is not None:
                    self._intervals.append((now - self._last_rc) * 1e6)
                self._last_rc = now
                self._rc_times.append(now)
                self._leads.append(self._deviation(now))
        elif fr.type == F.T_PING and fr.dest in (F.ADDR_BROADCAST, F.ADDR_TX):
            self.pings += 1
            if not any(kind == "info" for kind, _ in self._queue):
                self._queue.appendleft(("info", F.device_info_frame(self.name, sw=SW)))
        else:
            self.other_frames += 1
        self._reply(now)

    def _reply(self, now):
        """Окно ответа после верного кадра хоста: созревшие sync/stats в очередь, затем ≤ max_reply байт."""
        if now >= self._next_sync:
            self._next_sync = max(self._next_sync + self.sync_interval_s, now)
            self._queue.append(("sync", F.timing_frame(self.period_us, self.offset_us())))
        if now >= self._next_stats:
            self._next_stats = max(self._next_stats + self.stats_interval_s, now)
            self._queue.append(("stats", F.link_stats_frame()))
        if self.scenario == "silent":
            self._queue.clear()
            return
        out = bytearray()
        while self._queue and (not out or len(out) + len(self._queue[0][1]) <= self.max_reply):
            kind, data = self._queue.popleft()
            if self.scenario == "crc":
                data = data[:-1] + bytes((data[-1] ^ 0xFF,))
            out += data
            self.sync_sent += kind == "sync"
            self.stats_sent += kind == "stats"
            self.info_sent += kind == "info"
        if out and self.scenario == "garbage":
            out[:0] = bytes(self.rng.randrange(256) for _ in range(self.rng.randint(4, 16)))
        self._tx += out

    def _deviation(self, now) -> float:
        """Опережение кадра до ближайшего радиопакета минус 100 мкс, приведённое к (−период/2; период/2] мкс:
        кадр сразу после радиопакета — «чуть поздно», а не «почти на период рано» (усреднение без скачка на стыке)."""
        p = self.period_us
        lead = (p - ((now - self.t0) * 1e6) % p) % p
        d = (lead - LEAD_MARGIN_US) % p
        return d - p if d > p / 2 else d

    def offset_us(self) -> float:
        """Смещение для 0x3A.0x10: среднее отклонение последних 4 кадров каналов от «за 100 мкс до радиопакета»."""
        if not self._leads:
            return 0.0
        return sum(self._leads) / len(self._leads)

    # ---------- сценарии ----------
    def lab(self, scenario: str) -> bool:
        if scenario not in LAB_SCENARIOS:
            return False
        self.scenario = "normal" if scenario == "heal" else scenario
        return True

    def set_period(self, us) -> bool:
        us = float(us)
        if not 1000 <= us <= 100000:
            return False
        self.period_us = us
        return True

    # ---------- состояние ----------
    def state(self) -> dict:
        now = self.clock()
        while self._rc_times and self._rc_times[0] <= now - RATE_WINDOW_S:
            self._rc_times.popleft()
        n = len(self._rc_times)
        span = self._rc_times[-1] - self._rc_times[0] if n >= 2 else 0.0
        iv = list(self._intervals)
        med = _median(iv)
        return {
            "ev": "state", "t": round(now, 3), "host": self.host, "scenario": self.scenario,
            "period_us": int(self.period_us), "rc_frames": self.rc_frames,
            "rate_hz": round((n - 1) / span, 1) if span > 0 else 0.0,
            "interval_ms": None if med is None else round(med / 1000, 3),
            "jitter_us_p99": _p99([abs(x - med) for x in iv]) if iv else 0,
            "ch": None if self.ch is None else list(self.ch),
            "axes": None if self.ch is None else [F.crsf_to_axis(v) for v in self.ch[:8]],
            "armed": self.armed, "frame_len": self.frame_len, "pings": self.pings,
            "host_frames": self.host_frames, "crc_errors": self.parser.crc_errors,
            "garbage_bytes": self.parser.garbage_bytes, "sync_sent": self.sync_sent, "stats_sent": self.stats_sent,
            "info_sent": self.info_sent, "offset_us": round(self.offset_us(), 1),
            "last_rc_ms": None if self._last_rc is None else int((now - self._last_rc) * 1000),
            "last_host_ms": None if self._last_host is None else int((now - self._last_host) * 1000),
        }


class LoopbackPort:
    """Порт CrsfOutput поверх ядра эмулятора в одном процессе: write → feed, read → drain_tx."""

    collisions = 0
    write_errors = 0

    def __init__(self, emu: ElrsTxEmulator):
        self.emu = emu

    def write(self, data: bytes) -> int:
        self.emu.feed(bytes(data))
        return len(data)

    def read(self) -> bytes:
        return self.emu.drain_tx()

    def close(self) -> None:
        pass


# ---------- дочерний процесс ----------
def _log(text):
    print(f"[elrs_tx] {text}", file=sys.stderr, flush=True)


def _write(msg):
    data = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("message exceeds 4096 bytes")
    view = memoryview(data)
    while view:
        n = os.write(1, view)
        view = view[n:]


def handle_command(emu: ElrsTxEmulator, msg) -> list:
    """Команда stdin → ответы (JSON-объекты). Неизвестное — без ответа."""
    op = msg.get("op") if isinstance(msg, dict) else None
    if op == "keepalive":
        return [{"ev": "alive"}]
    if op == "state":
        return [emu.state()]
    if op == "lab":
        scenario = msg.get("scenario")
        ok = emu.lab(scenario)
        return [{"ev": "lab", "scenario": scenario if ok else "", "ok": ok, "error": "" if ok else "unknown scenario"},
                emu.state()]
    if op == "period":
        try:
            ok = emu.set_period(msg.get("us"))
        except (TypeError, ValueError):
            ok = False
        return [{"ev": "period", "us": int(emu.period_us), "ok": ok}]
    return []


def _open_line(path):
    from sheregesh.crsf.port import configure_termios
    keep = None
    if path:
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        configure_termios(fd)
    else:
        fd, keep = os.openpty()
        configure_termios(keep)
        os.set_blocking(fd, False)
        path = os.ttyname(keep)
    return fd, keep, path


def main(argv=None, clock=time.monotonic) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m sheregesh_emulators.elrs_tx", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tty", default="", help="псевдотерминал хоста (ведомая сторона PtyPort); пусто — свой")
    ap.add_argument("--period-us", type=float, default=PERIOD_US, help="период радиопакетов, мкс")
    ap.add_argument("--seed", type=int, default=0, help="seed сценария garbage")
    ap.add_argument("--name", default=NAME)
    ap.add_argument("--state-ms", type=int, default=200, help="период строк state, мс")
    ap.add_argument("--scenario", default="heal", choices=LAB_SCENARIOS, help="начальный сценарий")
    a = ap.parse_args(argv)
    emu = ElrsTxEmulator(clock, period_us=a.period_us, name=a.name, seed=a.seed)
    emu.lab(a.scenario)
    fd, keep, path = _open_line(a.tty)
    sin = sys.stdin.fileno()
    buf = b""
    line_ok = True
    next_state = clock()
    try:
        _write({"ev": "hello", "emulator": "elrs-tx-lab", "version": 1, "tty": path, "pid": os.getpid(),
                "name": emu.name, "period_us": int(emu.period_us)})
        _log(f"started pid={os.getpid()} tty={path} period={emu.period_us:g}us (laboratory emulator, no radio)")
        while True:
            rlist = [sin] + ([fd] if line_ok else [])
            ready, _, _ = select.select(rlist, [], [], 0.01)
            if fd in ready:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    data = b""
                except OSError as e:
                    _log(f"line closed: {e}")
                    line_ok, data = False, b""
                if data:
                    emu.feed(data)
                    out = emu.drain_tx()
                    if out:
                        try:
                            os.write(fd, out)
                        except OSError as e:
                            _log(f"line write failed: {e}")
            if sin in ready:
                data = os.read(sin, 65536)
                if not data:
                    return 0                      # EOF stdin: родитель закрыл канал
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        _log("bad line from parent ignored")
                        continue
                    for m in handle_command(emu, msg):
                        _write(m)
                    if isinstance(msg, dict) and msg.get("op") == "lab":
                        _log(f"lab {str(msg.get('scenario'))[:32]}")
                if len(buf) > MAX_LINE:
                    _log("line from parent exceeds 4096 bytes: dropped")
                    buf = b""
            events = emu.step()
            for e in events:
                _log(f"host {e['state']}")
            now = clock()
            if events or now >= next_state:
                next_state = now + a.state_ms / 1000.0
                _write(emu.state())
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        os.close(fd)
        if keep is not None:
            os.close(keep)


if __name__ == "__main__":
    sys.exit(main())
