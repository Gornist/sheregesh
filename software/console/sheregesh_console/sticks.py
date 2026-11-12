# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Стики и кнопки аппаратуры (USB-джойстик EdgeTX) на пульте и передача их удалённому блоку (ADR-0013, 4.2).

  EvdevJoystick — чтение /dev/input/eventN через os/fcntl/struct/select (без python-evdev): диапазоны осей —
                  EVIOCGABS, кнопки — EVIOCGBIT(EV_KEY), исходное состояние кнопок — EVIOCGKEY;
  StickSource   — поток: поиск устройства (by-id/*-event-joystick, иначе первое event-устройство с ABS_X и ABS_Y),
                  текущее состояние осей и кнопок, переподключение при выдёргивании (опрос раз в 1 с);
  StickSender   — поток 50 Гц: при enabled and present and connected (сеанс ≥ 1.4) — PeerClient.send_rc(кадр);
                  выключение оператором → один кадр en=false; пропажа устройства → один кадр dev.ok=false, затем
                  передача выключается (повторное включение — снова оператором, с подтверждением);
  SticksView    — представление для SticksPage.qml, локального канала (команда sticks) и ui-state.

Передача после каждого старта пульта выключена. Без устройства, без связи или со старым блоком (сеанс < 1.4) ничего не
отправляется. Свежесть команд решает только сторож блока (ControlReceiver): пульт при остановке ничего «на прощание»
не шлёт — блок сам переходит в FAILSAFE (timeout / link-lost). Радиовыход (S8, ADR-0014) — на блоке: плашка страницы
«Управление» показывает его по status.module.output (ModuleView), а не по rc-state.

Каналы «Шерегеш» не трактует: ch[i] — ось с кодом ABS i (ABS_X…ABS_RUDDER), min…max → −1000…1000, ось отсутствует — 0;
btn — бит i у i-й кнопки в порядке возрастания кода KEY (не больше 24).
"""
import collections
import errno
import fcntl
import glob
import os
import select
import statistics
import struct
import threading
import time

from PySide6.QtCore import Property, QCoreApplication, QObject, QTimer, Signal, Slot

# ---------------- evdev (linux/input.h, linux/input-event-codes.h) ----------------
EVENT_FMT = "llHHi"                         # struct input_event: timeval (long, long), type, code, value
EVENT_SIZE = struct.calcsize(EVENT_FMT)     # 24 на aarch64/x86_64, 16 на 32-битных платформах
ABSINFO_FMT = "6i"                          # struct input_absinfo: value, minimum, maximum, fuzz, flat, resolution
ABSINFO_SIZE = struct.calcsize(ABSINFO_FMT)
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT, SYN_DROPPED = 0, 3
ABS_X, ABS_Y = 0, 1
KEY_MAX, ABS_MAX = 0x2ff, 0x3f
KEY_BYTES = KEY_MAX // 8 + 1                # 96
ABS_BYTES = ABS_MAX // 8 + 1                # 8
_IOC_READ = 2


def _ioc(direction, nr, size):
    """_IOC(dir, 'E', nr, size) из asm-generic/ioctl.h (dir 2 бита, size 14 бит, type 8 бит, nr 8 бит)."""
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


def EVIOCGNAME(length):                     # noqa: N802 — имена как в linux/input.h
    return _ioc(_IOC_READ, 0x06, length)


def EVIOCGKEY(length):                      # noqa: N802
    return _ioc(_IOC_READ, 0x18, length)


def EVIOCGBIT(ev, length):                  # noqa: N802
    return _ioc(_IOC_READ, 0x20 + ev, length)


def EVIOCGABS(axis):                        # noqa: N802
    return _ioc(_IOC_READ, 0x40 + axis, ABSINFO_SIZE)


CHANNELS = 8                                # ch: ровно 8 (ADR-0013, 3.1)
MAX_BUTTONS = 24                            # btn: 0…2^24−1
RATE_HZ = 50
RESCAN_S = 1.0                              # опрос появления/пропажи устройства
INPUT_DIR = "/dev/input"
NAME_LIMIT = 64
REMOTE_STALE_S = 3.0                        # rc-state не обновлялся дольше — данных блока нет (в IDLE он шлёт 1 Гц)
LATENCY_WINDOW_S = 30.0                     # медиана и 99-й перцентиль задержки (ST-05)
LATENCY_SHOWN_S = 2.0                       # на экране — медиана за последние 2 с
MIN_SESSION = (1, 4)                        # rc / rc-state — с SHRG-LAB 1.4
CONTEXT = "SticksPage"                      # контекст перевода (имя файла QML)

# Исходные строки (английские) — те же, что qsTr в SticksPage.qml; перевод — i18n/sheregesh_ru.ts
STATE_SOURCE = {"IDLE": "No commands", "ACTIVE": "Commands are arriving", "FAILSAFE": "FAILSAFE: no fresh commands"}
SUPPORT_SOURCE = {"no-link": "No link to the remote unit", "old-remote": "Not supported by the remote unit"}
NO_DATA_SOURCE = "No data from the remote unit"


def tr(source):
    return QCoreApplication.translate(CONTEXT, source)


def scrub(text, limit=NAME_LIMIT):
    """Строка устройства для кадра и экрана: без управляющих символов, не длиннее limit."""
    return "".join(c if c.isprintable() else " " for c in str(text or "")).strip()[:limit]


def normalize(value, lo, hi):
    """Ось: lo…hi → −1000…1000 (целое, с ограничением); вырожденный диапазон — 0."""
    if hi <= lo:
        return 0
    v = round((value - lo) * 2000 / (hi - lo) - 1000)
    return max(-1000, min(1000, v))


def parse_events(buf):
    """Байты из read(2) → ([(type, code, value)], остаток). Неполное событие в конце возвращается остатком."""
    n = len(buf) // EVENT_SIZE
    out = [struct.unpack_from(EVENT_FMT, buf, i * EVENT_SIZE)[2:] for i in range(n)]
    return out, buf[n * EVENT_SIZE:]


def _bits(raw):
    return [i for i in range(len(raw) * 8) if raw[i // 8] >> (i % 8) & 1]


def parse_session(session):
    """"1.4" → (1, 4); иначе None."""
    try:
        major, minor = str(session).split(".", 1)
        return int(major), int(minor)
    except (TypeError, ValueError):
        return None


class EvdevJoystick:
    """Одно устройство evdev. ioctl подменяется в испытаниях (на macOS настоящего evdev нет).

    Состояние меняется по SYN_REPORT (события между отчётами — один кадр). SYN_DROPPED (переполнение буфера ядра):
    события до следующего SYN_REPORT отбрасываются, затем состояние перечитывается ioctl (EVIOCGABS, EVIOCGKEY)."""

    def __init__(self, path, ioctl=fcntl.ioctl, fd=None):
        self.path = path
        self._ioctl = ioctl
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK) if fd is None else fd
        try:
            self.name = scrub(self._query(EVIOCGNAME(256), 256).split(b"\0", 1)[0].decode("utf-8", "replace"))
            abs_codes = set(_bits(self._query(EVIOCGBIT(EV_ABS, ABS_BYTES), ABS_BYTES)))
            self.axes = {}                       # код ABS (0…7) → [min, max, текущее]
            for code in range(CHANNELS):
                if code in abs_codes:
                    value, lo, hi = struct.unpack(ABSINFO_FMT, self._query(EVIOCGABS(code), ABSINFO_SIZE))[:3]
                    self.axes[code] = [lo, hi, value]
            self.buttons = _bits(self._query(EVIOCGBIT(EV_KEY, KEY_BYTES), KEY_BYTES))[:MAX_BUTTONS]
            self._btn_index = {code: i for i, code in enumerate(self.buttons)}
            self.pressed = 0
            self._resync_keys()
        except OSError:
            self.close()
            raise
        self._pending = []
        self._dropped = False
        self._rest = b""

    def _query(self, request, size):
        return bytes(self._ioctl(self.fd, request, bytes(size)))

    def _resync_keys(self):
        try:
            down = set(_bits(self._query(EVIOCGKEY(KEY_BYTES), KEY_BYTES)))
        except OSError:                          # без EVIOCGKEY — кнопки отпущены до первого события
            down = set()
        self.pressed = sum(1 << i for code, i in self._btn_index.items() if code in down)

    def _resync(self):
        for code, ax in self.axes.items():
            ax[2] = struct.unpack(ABSINFO_FMT, self._query(EVIOCGABS(code), ABSINFO_SIZE))[0]
        self._resync_keys()

    def fileno(self):
        return self.fd

    def channels(self):
        """ch[8]: −1000…1000, отсутствующая ось — 0."""
        return [normalize(self.axes[c][2], self.axes[c][0], self.axes[c][1]) if c in self.axes else 0
                for c in range(CHANNELS)]

    def feed(self, data):
        """Байты событий → изменение состояния. Возвращает True, если состояние изменилось."""
        events, self._rest = parse_events(self._rest + data)
        changed = False
        for etype, code, value in events:
            if etype == EV_SYN and code == SYN_DROPPED:
                self._dropped, self._pending = True, []
            elif etype == EV_SYN and code == SYN_REPORT:
                if self._dropped:
                    self._dropped = False
                    self._resync()
                    changed = True
                    continue
                for ptype, pcode, pvalue in self._pending:
                    if ptype == EV_ABS and pcode in self.axes:
                        changed |= self.axes[pcode][2] != pvalue
                        self.axes[pcode][2] = pvalue
                    elif ptype == EV_KEY and pcode in self._btn_index:
                        bit = 1 << self._btn_index[pcode]
                        before = self.pressed
                        self.pressed = self.pressed | bit if pvalue else self.pressed & ~bit   # 2 — автоповтор
                        changed |= before != self.pressed
                self._pending = []
            elif not self._dropped and etype in (EV_ABS, EV_KEY):
                self._pending.append((etype, code, value))
        return changed

    def read(self):
        """Прочитать доступные события. Устройство выдернуто — OSError (ENODEV) или конец файла (EOFError)."""
        try:
            data = os.read(self.fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return False
        if not data:
            raise EOFError(self.path)
        return self.feed(data)

    def close(self):
        fd, self.fd = self.fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def probe_joystick(path, ioctl=fcntl.ioctl):
    """Есть ли у event-устройства оси ABS_X и ABS_Y (без чтения событий)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        bits = set(_bits(bytes(ioctl(fd, EVIOCGBIT(EV_ABS, ABS_BYTES), bytes(ABS_BYTES)))))
        return ABS_X in bits and ABS_Y in bits
    except OSError:
        return False
    finally:
        os.close(fd)


def find_device(root=INPUT_DIR, probe=probe_joystick):
    """Путь устройства: by-id/*-event-joystick (по имени, первое существующее), иначе первое eventN (по номеру) с
    ABS_X и ABS_Y; нет — None."""
    for link in sorted(glob.glob(os.path.join(root, "by-id", "*-event-joystick"))):
        path = os.path.realpath(link)
        if os.path.exists(path):
            return path

    def number(p):
        tail = os.path.basename(p)[5:]
        return int(tail) if tail.isdigit() else 1 << 30
    for path in sorted(glob.glob(os.path.join(root, "event*")), key=number):
        if probe(path):
            return path
    return None


def empty_state():
    return {"present": False, "name": "", "path": "", "axes": [0] * CHANNELS, "buttons": 0, "axis_count": 0,
            "button_count": 0, "generation": 0, "lost": 0, "error": ""}


class StickSource:
    """Поток чтения устройства. state() — копия текущего состояния (из любого потока)."""

    def __init__(self, root=INPUT_DIR, opener=EvdevJoystick, find=None, rescan_s=RESCAN_S):
        self.root = root
        self._open = opener
        self._find = find or (lambda: find_device(self.root))
        self.rescan_s = rescan_s
        self._lock = threading.Lock()
        self._state = empty_state()
        self._dev = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sticks-source", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
            self._thread = None
        self._drop("")

    def state(self):
        with self._lock:
            s = dict(self._state)
            s["axes"] = list(s["axes"])
            return s

    # ---------- шаги (вызываются потоком; в испытаниях — напрямую) ----------
    def scan(self):
        """Устройства нет — найти и открыть. True — открыто."""
        if self._dev is not None:
            return True
        path = self._find()
        if not path:
            return False
        try:
            dev = self._open(path)
        except OSError as e:
            with self._lock:
                self._state["error"] = f"{path}: {e.strerror or e}"
            return False
        self._dev = dev
        with self._lock:
            self._state.update(present=True, name=dev.name, path=path, axes=dev.channels(), buttons=dev.pressed,
                               axis_count=len(dev.axes), button_count=len(dev.buttons), error="",
                               generation=self._state["generation"] + 1)
        return True

    def poll(self, timeout=0.2):
        """Прочитать события устройства (ждать не дольше timeout). False — устройство пропало."""
        dev = self._dev
        if dev is None:
            return False
        try:
            ready, _, _ = select.select([dev], [], [], timeout)
            if ready and dev.read():
                with self._lock:
                    self._state.update(axes=dev.channels(), buttons=dev.pressed)
            return True
        except (OSError, EOFError, ValueError) as e:
            self._drop(getattr(e, "strerror", None) or type(e).__name__)
            return False

    def check(self):
        """Файл устройства исчез (udev удалил узел) — устройство пропало, даже если select молчит."""
        if self._dev is not None and not os.path.exists(self._dev.path):
            self._drop(errno.errorcode.get(errno.ENODEV, "ENODEV"))

    def _drop(self, reason):
        dev, self._dev = self._dev, None
        if dev is None:
            return
        dev.close()
        with self._lock:
            self._state.update(present=False, axes=[0] * CHANNELS, buttons=0, lost=self._state["lost"] + 1,
                               error=reason)

    def _run(self):
        last_check = 0.0
        while not self._stop.is_set():
            if self._dev is None:
                if not self.scan():
                    self._stop.wait(self.rescan_s)
                continue
            self.poll(0.2)
            now = time.monotonic()
            if now - last_check >= self.rescan_s:
                last_check = now
                self.check()


def _link_state(client):
    """Фаза, сеанс и число соединений канала: link_state() клиента (дёшево, такт 50 Гц), у прочих — snapshot()."""
    if client is None:
        return {}
    if hasattr(client, "link_state"):
        return client.link_state()
    return client.snapshot() if hasattr(client, "snapshot") else {}


def _client_connected(snap):
    """Канал с блоком установлен и сеанс ≥ 1.4 (иначе rc не отправляется)."""
    ver = parse_session(snap.get("session"))
    return snap.get("phase") == "connected", ver is not None and ver >= MIN_SESSION


VOLATILE = ("rx_age_s",)                    # меняется в каждом snapshot() без нового rc-state


def remote_key(rs):
    """Признак «новый rc-state»: время приёма клиентом (rx_at), иначе содержимое без меняющихся при опросе полей."""
    at = rs.get("rx_at")
    if isinstance(at, (int, float)) and not isinstance(at, bool):
        return at
    return tuple(sorted((k, repr(v)) for k, v in rs.items() if k not in VOLATILE))


def client_timing(snap, n):
    """Время приёма rc-state и время отправки кадра n по часам клиента канала (ADR-0013, 4.1: rc_state с временем
    получения, rc_sent с кольцом времён отправки последних 64 кадров). (rx, sent) или None, если клиент их не дал."""
    rs, sent = snap.get("rc_state"), snap.get("rc_sent")
    if not isinstance(rs, dict) or not isinstance(sent, dict):
        return None
    rx = next((rs[k] for k in ("rx_at", "received_at", "at") if isinstance(rs.get(k), (int, float))), None)
    if rx is None:
        rx = next((snap[k] for k in ("rc_state_at", "rc_state_rx") if isinstance(snap.get(k), (int, float))), None)
    if rx is None:
        return None
    if sent.get("n") == n and isinstance(sent.get("at"), (int, float)):
        return rx, sent["at"]
    ring = next((sent[k] for k in ("ring", "times", "recent", "history") if k in sent), None)
    if isinstance(ring, dict):
        at = ring.get(n, ring.get(str(n)))
    elif isinstance(ring, (list, tuple)):
        at = None
        for item in ring:
            if isinstance(item, dict) and item.get("n") == n:
                at = item.get("at")
            elif isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == n:
                at = item[1]
    else:
        at = None
    return (rx, at) if isinstance(at, (int, float)) else None


class StickSender:
    """Передача кадров rc 50 Гц. step(now) — один такт (поток вызывает его по расписанию; испытания — напрямую).

    client() — клиент канала (PeerClient с send_rc и snapshot) или None. Включение — set_enabled(True), только если
    устройство есть и канал с блоком ≥ 1.4 установлен; подтверждение оператора проверяет SticksView/локальный канал."""

    def __init__(self, source, client=lambda: None, clock=time.monotonic, rate_hz=RATE_HZ):
        self.source = source
        self._client = client
        self._clock = clock
        self.period = 1.0 / rate_hz
        self._lock = threading.Lock()
        self._enabled = False
        self._gen = 0                            # поколение устройства, при котором включено
        self._pending = None                     # кадр-завершение: "disabled" | "device-lost"
        self._n = 0
        self._connects = None                    # counters.connects клиента: новое соединение — n с 1
        self._sent = 0
        self._sent_at = collections.OrderedDict()   # n → время отправки (свои часы; запасной расчёт задержки)
        self._last = ""                          # итог последнего send_rc
        self._off_reason = ""                    # почему выключено: operator | device-lost | "" (старт)
        self._remote_seen = None
        self._samples = collections.deque(maxlen=int(LATENCY_WINDOW_S * 20))   # (время, мс)
        self._latency_src = ""
        self._stop = threading.Event()
        self._thread = None

    # ---------- управление ----------
    def can_enable(self):
        """"" — можно включить; иначе причина: no-device | no-link | old-remote."""
        client = self._client()
        if client is None:
            return "no-link"
        snap = client.snapshot() if hasattr(client, "snapshot") else {}
        connected, new = _client_connected(snap)
        if not connected:
            return "no-link"
        if not new or not callable(getattr(client, "send_rc", None)):
            return "old-remote"
        if not self.source.state()["present"]:
            return "no-device"
        return ""

    def set_enabled(self, enabled):
        """Включить (→ "ok" или причина отказа) / выключить (→ "ok"; следующий такт шлёт один кадр en=false)."""
        if not enabled:
            with self._lock:
                if self._enabled:
                    self._enabled, self._pending, self._off_reason = False, "disabled", "operator"
            return "ok"
        reason = self.can_enable()
        if reason:
            return reason
        with self._lock:
            if not self._enabled:
                self._enabled, self._pending, self._off_reason = True, None, ""
                self._gen = self.source.state()["generation"]
        return "ok"

    @property
    def enabled(self):
        with self._lock:
            return self._enabled

    def status(self):
        now = self._clock()
        with self._lock:
            recent = [ms for t, ms in self._samples if now - t <= LATENCY_WINDOW_S]
            shown = [ms for t, ms in self._samples if now - t <= LATENCY_SHOWN_S]
            return {"enabled": self._enabled, "n": self._n, "sent": self._sent, "last": self._last,
                    "off_reason": self._off_reason, "latency_ms": round(statistics.median(shown), 1) if shown else None,
                    "latency": {"samples": len(recent), "window_s": LATENCY_WINDOW_S, "source": self._latency_src,
                                "last_ms": self._samples[-1][1] if recent else None,
                                "median_ms": round(statistics.median(recent), 1) if recent else None,
                                "p99_ms": round(sorted(recent)[min(len(recent) - 1, int(len(recent) * 0.99))], 1)
                                if recent else None,
                                "recent_ms": recent[-50:]}}

    # ---------- поток ----------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sticks-sender", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        """Остановка пульта: прощального кадра нет — блок сам уйдёт в FAILSAFE (ST-06)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
            self._thread = None

    def _run(self):
        nxt = self._clock()
        while not self._stop.is_set():
            try:
                self.step(self._clock())
            except Exception as e:               # noqa: BLE001 — сбой клиента не останавливает такт (следующий кадр)
                with self._lock:
                    self._last = f"error: {type(e).__name__}: {e}"[:120]
            nxt += self.period
            delay = nxt - self._clock()
            if delay < -self.period:             # отстали больше чем на кадр — без догоняющей пачки
                nxt, delay = self._clock(), 0
            self._stop.wait(max(0.0, delay))

    def _frame(self, en, st, ok=True):
        self._n += 1
        return {"n": self._n, "en": en, "ch": list(st["axes"]) if ok else [0] * CHANNELS,
                "btn": int(st["buttons"]) if ok else 0, "dev": {"name": st["name"], "ok": ok}}

    def step(self, now):
        """Один такт: решить, какой кадр отправить (или никакого), и отправить."""
        st = self.source.state()
        client = self._client()
        snap = _link_state(client)
        connected, new = _client_connected(snap)
        send = getattr(client, "send_rc", None) if connected and new else None
        connects = (snap.get("counters") or {}).get("connects")
        with self._lock:
            if connects != self._connects:       # новое соединение: номера кадров — с 1, прежние времена не его
                self._connects, self._n = connects, 0
                self._sent_at.clear()
                self._remote_seen = None
            if self._enabled and (not st["present"] or st["generation"] != self._gen):
                self._enabled, self._pending, self._off_reason = False, "device-lost", "device-lost"
            pending, self._pending = self._pending, None
            if not callable(send):
                frame = None                     # без связи / старый блок: ничего (и завершающий кадр не копится)
            elif pending == "device-lost":
                frame = self._frame(True, st, ok=False)
            elif pending == "disabled":
                frame = self._frame(False, st, ok=st["present"])
            elif self._enabled:
                frame = self._frame(True, st)
            else:
                frame = None
        if frame is not None:
            result = send(frame)
            with self._lock:
                self._last = result if isinstance(result, str) else "queued"
                if self._last == "queued":
                    self._sent += 1
                    self._sent_at[frame["n"]] = now
                    while len(self._sent_at) > 64:
                        self._sent_at.popitem(last=False)
        if "rc_state_at" in snap:                # лёгкий срез: полный снимок только при новом rc-state (10 Гц)
            if snap["rc_state_at"] is None or snap["rc_state_at"] == self._remote_seen:
                return
            snap = client.snapshot()
        self._measure(snap, now)

    def _measure(self, snap, now):
        """Задержка «приём rc-state − отправка кадра n» по новому rc-state блока в ACTIVE (часы плат не сравниваются).
        Источник: latency_ms клиента канала (rx_at и кольцо rc_sent по его часам), иначе rx_at + rc_sent.ring; клиент
        без rc_sent — свои времена отправки (опрос 50 Гц: погрешность до одного такта)."""
        rs = snap.get("rc_state")
        if not isinstance(rs, dict) or remote_key(rs) == self._remote_seen:
            return
        self._remote_seen = remote_key(rs)
        n = rs.get("n")
        if rs.get("state") != "ACTIVE" or not isinstance(n, int):
            return
        lat = rs.get("latency_ms")
        timing = client_timing(snap, n)
        with self._lock:
            if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                ms, src = float(lat), "client"
            elif timing is not None:
                ms, src = (timing[0] - timing[1]) * 1000, "client"
            elif "rc_sent" not in snap and n in self._sent_at:
                ms, src = (now - self._sent_at[n]) * 1000, "console"
            else:
                return
            if 0 <= ms < 10000:
                self._samples.append((now, round(ms, 1)))
                self._latency_src = src


# ---------------- представление ----------------
def support_of(link, session, client_ok, present):
    """ok | no-link | old-remote | no-device (порядок: сначала канал, затем блок, затем устройство)."""
    if link != "CONNECTED":
        return "no-link"
    ver = parse_session(session)
    if ver is None:
        return "no-link"
    if ver < MIN_SESSION or not client_ok:
        return "old-remote"
    return "ok" if present else "no-device"


def _num(v, default=0):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _channels(v):
    if not isinstance(v, list):
        return [0] * CHANNELS
    out = [int(_num(x)) for x in v[:CHANNELS]]
    return out + [0] * (CHANNELS - len(out))


class SticksView(QObject):
    """Логика страницы «Управление»: свойства для QML, слот setEnabled с подтверждением, snapshot() для локального
    канала и ui-state. Все свойства уведомляются одним сигналом changed (только при изменении видимого)."""

    changed = Signal()

    def __init__(self, parent=None, source=None, sender=None, client=None, log=None, clock=time.monotonic,
                 interval_ms=50):
        super().__init__(parent)
        self.source = source if source is not None else StickSource()
        self._client_fn = client or (lambda: None)
        self.sender = sender if sender is not None else StickSender(self.source, self._client_fn, clock=clock)
        self._log = log or (lambda level, text: None)
        self._clock = clock
        self._link, self._session = "", ""
        self._remote, self._remote_key, self._remote_at = None, None, 0.0
        self._result = ""                        # итог последнего запроса включения: ok | no-device | …
        self._notice = ""                        # device-lost — передача выключена из-за пропажи устройства
        self._prev = None
        self._view = {}
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def start(self):
        """Потоки устройства и передачи, опрос представления (только в работающем пульте, не в снимках/испытаниях)."""
        self.source.start()
        self.sender.start()
        self._timer.start()

    def stop(self):
        self._timer.stop()
        self.sender.stop()
        self.source.stop()

    def update_link(self, link, session):
        """Состояние канала (из Backend._refresh, поток интерфейса)."""
        self._link, self._session = str(link or ""), str(session or "")
        self.refresh()

    def _client_snapshot(self):
        client = self._client_fn()
        if client is None or not hasattr(client, "snapshot"):
            return None, {}
        try:
            return client, client.snapshot()
        except Exception:                        # noqa: BLE001 — клиент остановлен: данных нет
            return client, {}

    @Slot()
    def refresh(self):
        now = self._clock()
        st = self.source.state()
        snd = self.sender.status()
        client, snap = self._client_snapshot()
        client_ok = client is not None and callable(getattr(client, "send_rc", None))
        support = support_of(self._link, self._session, client_ok, st["present"])
        rs = snap.get("rc_state")
        if isinstance(rs, dict) and support in ("ok", "no-device"):
            key = remote_key(rs)
            if key != self._remote_key:          # новый rc-state блока
                self._remote_key, self._remote_at = key, now
            age = rs.get("rx_age_s")
            age = age if isinstance(age, (int, float)) and not isinstance(age, bool) else now - self._remote_at
            self._remote = rs if age <= REMOTE_STALE_S else None
        else:
            self._remote = None
        if snd["off_reason"] == "device-lost":
            self._notice = "device-lost"
        elif snd["enabled"]:
            self._notice = ""
        r = self._remote or {}
        dev = r.get("dev") if isinstance(r.get("dev"), dict) else {}
        view = {
            "support": support, "present": st["present"], "deviceName": st["name"], "devicePath": st["path"],
            "axisCount": st["axis_count"], "buttonCount": st["button_count"], "enabled": snd["enabled"],
            "axes": list(st["axes"]), "buttons": int(st["buttons"]),
            "remoteState": str(r.get("state") or ""), "remoteReason": str(r.get("reason") or ""),
            "remoteAxes": _channels(r.get("ch")), "remoteButtons": int(_num(r.get("btn"))),
            "remoteDevice": scrub(dev.get("name")), "remoteOutput": str(r.get("output") or ""),
            "timeoutMs": int(_num(r.get("timeout_ms"), 500)),
            "rateHz": float(_num(r.get("rate_hz"))), "gaps": int(_num(r.get("gaps"))),
            "failsafes": int(_num(r.get("failsafes"))), "stale": int(_num(r.get("stale"))),
            "bad": int(_num(r.get("bad"))),
            "latencyMs": float(snd["latency_ms"]) if snd["latency_ms"] is not None and self._remote else -1.0,
            "sent": snd["sent"], "frame": snd["n"], "result": self._result, "notice": self._notice,
            "session": self._session,
        }
        self._journal(view)
        if view != self._view:
            self._view = view
            self.changed.emit()

    def _journal(self, v):
        """Переходы — в журнал приложения (поток интерфейса)."""
        key = (v["support"], v["present"], v["enabled"], v["remoteState"], v["remoteReason"])
        prev, self._prev = self._prev, key
        if prev is None or prev == key:
            return
        if prev[1] != v["present"]:
            self._log("info" if v["present"] else "warning",
                      f"device {'present: ' + v['deviceName'] if v['present'] else 'absent'}")
        if prev[2] != v["enabled"]:
            why = "" if v["enabled"] else f" ({self.sender.status()['off_reason'] or 'operator'})"
            self._log("info" if v["enabled"] or why == " (operator)" else "warning",
                      f"transmission {'enabled' if v['enabled'] else 'disabled'}{why}")
        if (prev[3], prev[4]) != (v["remoteState"], v["remoteReason"]) and v["remoteState"]:
            self._log("warning" if v["remoteState"] == "FAILSAFE" else "info",
                      f"remote {v['remoteState']} ({v['remoteReason'] or '-'})")
        if prev[0] != v["support"]:
            self._log("info", f"support {v['support']}")

    # ---------- свойства ----------
    def _p(name, kind, notify=changed):         # noqa: N805 — фабрика свойств класса (notify — из тела класса)
        return Property(kind, lambda self: self._view[name], notify=notify)

    support = _p("support", str)
    present = _p("present", bool)
    deviceName = _p("deviceName", str)
    devicePath = _p("devicePath", str)
    axisCount = _p("axisCount", int)
    buttonCount = _p("buttonCount", int)
    enabled = _p("enabled", bool)
    axes = _p("axes", list)
    buttons = _p("buttons", int)
    remoteState = _p("remoteState", str)
    remoteReason = _p("remoteReason", str)
    remoteAxes = _p("remoteAxes", list)
    remoteButtons = _p("remoteButtons", int)
    remoteDevice = _p("remoteDevice", str)
    remoteOutput = _p("remoteOutput", str)
    timeoutMs = _p("timeoutMs", int)
    rateHz = _p("rateHz", float)
    latencyMs = _p("latencyMs", float)
    gaps = _p("gaps", int)
    failsafes = _p("failsafes", int)
    sent = _p("sent", int)
    result = _p("result", str)
    notice = _p("notice", str)
    session = _p("session", str)
    del _p

    @Property(str, notify=changed)
    def remoteStateText(self):
        return self.state_text()

    # ---------- команды ----------
    @Slot(bool, bool, result=bool)
    def setEnabled(self, enabled, confirmed):
        """Включение передачи — только с подтверждением (диалог экрана, флаг confirmed локального канала);
        выключение — всегда и сразу (безопасное действие)."""
        if enabled and not confirmed:
            return False
        code = self.sender.set_enabled(bool(enabled))
        self._result = code
        if code == "ok":
            self._notice = ""
            self._log("info", f"operator: transmission {'enable' if enabled else 'disable'}")
        else:
            self._log("warning", f"operator: transmission enable refused ({code})")
        self.refresh()
        return code == "ok"

    # ---------- для локального канала и ui-state ----------
    def state_text(self):
        """Крупная надпись состояния блока (как sticksRemoteStateText на экране)."""
        v = self._view
        if v["support"] in SUPPORT_SOURCE:
            return tr(SUPPORT_SOURCE[v["support"]])
        if v["remoteState"] in STATE_SOURCE:
            return tr(STATE_SOURCE[v["remoteState"]])
        return tr(NO_DATA_SOURCE)

    def snapshot(self):
        """Формат S для команды sticks локального канала (ADR-0013, 4.2) и ключа sticks в ui-state."""
        v = self._view
        snd = self.sender.status()
        return {"support": v["support"], "present": v["present"], "enabled": v["enabled"],
                "device": {"name": v["deviceName"], "path": v["devicePath"], "axes": v["axisCount"],
                           "buttons": v["buttonCount"]},
                "axes": list(v["axes"]), "buttons": v["buttons"], "session": v["session"] or None,
                "remote": dict(self._remote) if self._remote else None,
                "sender": {"n": snd["n"], "sent": snd["sent"], "last": snd["last"], "off_reason": snd["off_reason"]},
                "latency": dict(snd["latency"], shown_ms=None if v["latencyMs"] < 0 else v["latencyMs"]),
                "result": v["result"], "notice": v["notice"],
                "display": {"stateText": self.state_text()}}
