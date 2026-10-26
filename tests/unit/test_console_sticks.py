# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Стики пульта (S7, ADR-0013, 4.2) без QML: разбор потока evdev из байтов, нормализация осей, ioctl, поиск
устройства и горячее подключение (подставной каталог /dev/input и подменный ioctl — на macOS evdev нет), логика
передачи 50 Гц и её безопасность, SticksView.

Клиент канала подменный (контракт ADR-0013, 4.1: send_rc → queued | offline | unsupported, snapshot() с phase,
session, counters.connects, rc_state, rc_sent). Настоящий джойстик и блок здесь не участвуют.
"""
import os
import struct
import threading
import time

import pytest

from sheregesh_console import sticks as s

# RadioMaster Boxer (EdgeTX) по ADR-0013, раздел 1: 8 осей 0…2048, 24 кнопки 0x130…0x13f и 0x2c0…0x2c7
BOXER_NAME = "OpenTX Radiomaster Boxer Joystick"
BOXER_KEYS = list(range(0x130, 0x140)) + list(range(0x2c0, 0x2c8))


def ev(etype, code, value, sec=1, usec=0):
    return struct.pack(s.EVENT_FMT, sec, usec, etype, code, value)


def syn():
    return ev(s.EV_SYN, s.SYN_REPORT, 0)


def bitmask(codes, nbytes):
    raw = bytearray(nbytes)
    for c in codes:
        raw[c // 8] |= 1 << (c % 8)
    return bytes(raw)


class FakeIoctl:
    """ioctl evdev для подменного устройства: имя, оси (value, min, max, fuzz, flat, res), кнопки, нажатые кнопки."""

    def __init__(self, name=BOXER_NAME, axes=None, keys=BOXER_KEYS, down=(), fail=()):
        self.name = name
        self.axes = {c: [1024, 0, 2048, 8, 128, 0] for c in range(8)} if axes is None else axes
        self.keys, self.down, self.fail = list(keys), set(down), set(fail)
        self.calls = []

    def __call__(self, fd, request, buf):
        self.calls.append(request)
        if request in self.fail:
            raise OSError(25, "Inappropriate ioctl for device")
        size = len(buf)
        if request == s.EVIOCGNAME(size):
            return (self.name.encode() + b"\0").ljust(size, b"\0")[:size]
        if request == s.EVIOCGBIT(s.EV_ABS, size):
            return bitmask(self.axes, size)
        if request == s.EVIOCGBIT(s.EV_KEY, size):
            return bitmask(self.keys, size)
        if request == s.EVIOCGKEY(size):
            return bitmask(self.down, size)
        for code, info in self.axes.items():
            if request == s.EVIOCGABS(code):
                return struct.pack(s.ABSINFO_FMT, *info)
        raise OSError(22, "Invalid argument")


@pytest.fixture
def pipe():
    r, w = os.pipe()
    os.set_blocking(r, False)
    yield r, w
    for fd in (r, w):
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------- формат и ioctl ----------------
def test_ioctl_numbers_match_linux_input_h():
    assert s.EVIOCGABS(0) == 0x80184540 and s.EVIOCGABS(7) == 0x80184547      # _IOR('E', 0x40+a, input_absinfo)
    assert s.EVIOCGBIT(s.EV_KEY, 96) == 0x80604521                             # _IOC(_IOC_READ, 'E', 0x21, len)
    assert s.EVIOCGBIT(s.EV_ABS, 8) == 0x80084523
    assert s.EVIOCGNAME(256) == 0x81004506 and s.EVIOCGKEY(96) == 0x80604518
    assert s.ABSINFO_SIZE == 24
    assert s.EVENT_SIZE == struct.calcsize("llHHi")                            # 24 на 64-битных, 16 на 32-битных


def test_parse_events_from_bytes_keeps_partial_tail():
    raw = ev(s.EV_ABS, 0, 2048) + ev(s.EV_KEY, 0x130, 1) + syn()
    events, rest = s.parse_events(raw + raw[:5])
    assert events == [(s.EV_ABS, 0, 2048), (s.EV_KEY, 0x130, 1), (s.EV_SYN, s.SYN_REPORT, 0)]
    assert rest == raw[:5]
    events, rest = s.parse_events(rest + raw[5:s.EVENT_SIZE])
    assert events == [(s.EV_ABS, 0, 2048)] and rest == b""


@pytest.mark.parametrize("value,lo,hi,expected", [
    (0, 0, 2048, -1000), (2048, 0, 2048, 1000), (1024, 0, 2048, 0), (1025, 0, 2048, 1), (1023, 0, 2048, -1),
    (512, 0, 2048, -500), (-50, 0, 2048, -1000), (3000, 0, 2048, 1000), (0, -512, 511, 1), (5, 10, 10, 0),
    (-32768, -32768, 32767, -1000), (32767, -32768, 32767, 1000),
])
def test_normalize(value, lo, hi, expected):
    assert s.normalize(value, lo, hi) == expected


# ---------------- устройство ----------------
def test_open_reads_ranges_buttons_and_initial_state(pipe):
    io = FakeIoctl(axes={0: [0, 0, 2048, 8, 128, 0], 1: [2048, 0, 2048, 8, 128, 0], 2: [1024, 0, 2048, 8, 128, 0],
                         5: [512, 0, 2048, 8, 128, 0], 9: [7, 0, 255, 0, 0, 0]}, down={0x131, 0x2c7})
    dev = s.EvdevJoystick("/dev/input/event4", ioctl=io, fd=pipe[0])
    assert dev.name == BOXER_NAME
    assert sorted(dev.axes) == [0, 1, 2, 5]                      # код 9 (за пределами ABS 0…7) не канал
    assert dev.channels() == [-1000, 1000, 0, 0, 0, -500, 0, 0]  # отсутствующая ось — 0
    assert dev.buttons == BOXER_KEYS and len(dev.buttons) == 24
    assert dev.pressed == (1 << 1) | (1 << 23)                   # бит i — i-я кнопка по возрастанию кода


def test_events_apply_only_on_syn_report(pipe):
    r, w = pipe
    dev = s.EvdevJoystick("/dev/input/event4", ioctl=FakeIoctl(), fd=r)
    os.write(w, ev(s.EV_ABS, 0, 2048) + ev(s.EV_ABS, 3, 0) + ev(s.EV_KEY, 0x2c0, 1))
    assert dev.read() is False and dev.channels()[0] == 0      # кадр ещё не закрыт SYN_REPORT
    os.write(w, syn())
    assert dev.read() is True
    assert dev.channels() == [1000, 0, 0, -1000, 0, 0, 0, 0] and dev.pressed == 1 << 16
    os.write(w, ev(s.EV_KEY, 0x2c0, 2) + syn())                  # автоповтор — кнопка остаётся нажатой
    assert dev.read() is False and dev.pressed == 1 << 16
    os.write(w, ev(s.EV_KEY, 0x2c0, 0) + ev(s.EV_KEY, 0x1ff, 1) + ev(s.EV_ABS, 30, 5) + syn())
    assert dev.read() is True and dev.pressed == 0               # неизвестные коды не влияют
    assert dev.read() is False                                   # данных нет — не ошибка


def test_syn_dropped_resyncs_from_ioctl(pipe):
    r, w = pipe
    io = FakeIoctl()
    dev = s.EvdevJoystick("/dev/input/event4", ioctl=io, fd=r)
    io.axes[2][0], io.down = 2048, {0x13f}                       # состояние ядра после переполнения буфера
    os.write(w, ev(s.EV_SYN, s.SYN_DROPPED, 0) + ev(s.EV_ABS, 2, 0) + syn())
    assert dev.read() is True
    assert dev.channels()[2] == 1000 and dev.pressed == 1 << 15  # из ioctl, а не из событий до SYN_REPORT


def test_unplug_gives_eof_or_oserror(pipe):
    r, w = pipe
    dev = s.EvdevJoystick("/dev/input/event4", ioctl=FakeIoctl(), fd=r)
    os.close(w)
    with pytest.raises(EOFError):
        dev.read()


def test_open_fails_cleanly_without_evdev(tmp_path):
    p = tmp_path / "event0"
    p.write_bytes(b"")
    with pytest.raises(OSError):                                  # обычный файл: ioctl — ENOTTY
        s.EvdevJoystick(str(p))


# ---------------- поиск устройства ----------------
def test_find_prefers_by_id_joystick(tmp_path):
    (tmp_path / "by-id").mkdir()
    for n in (0, 3, 4, 10):
        (tmp_path / f"event{n}").write_bytes(b"")
    os.symlink("../event4", tmp_path / "by-id" / "usb-OpenTX_Radiomaster_Boxer_Joystick_00000000001B-event-joystick")
    os.symlink("../event9", tmp_path / "by-id" / "usb-Gone_Pad-event-joystick")       # висячая ссылка — пропуск
    os.symlink("../event0", tmp_path / "by-id" / "usb-Keyboard-event-kbd")
    probed = []
    path = s.find_device(str(tmp_path), probe=lambda p: probed.append(p) or True)
    assert path == os.path.realpath(tmp_path / "event4") and probed == []


def test_find_falls_back_to_first_event_with_xy(tmp_path):
    for n in (10, 2, 3):
        (tmp_path / f"event{n}").write_bytes(b"")
    probed = []

    def probe(p):
        probed.append(os.path.basename(p))
        return p.endswith(("event3", "event10"))
    assert s.find_device(str(tmp_path), probe=probe) == str(tmp_path / "event3")
    assert probed == ["event2", "event3"]                        # по номеру, а не по строке (event10 — после)
    assert s.find_device(str(tmp_path), probe=lambda p: False) is None
    assert s.find_device(str(tmp_path / "missing")) is None


def test_probe_rejects_non_evdev(tmp_path):
    p = tmp_path / "event0"
    p.write_bytes(b"")
    assert s.probe_joystick(str(p)) is False
    assert s.probe_joystick(str(tmp_path / "none")) is False
    assert s.probe_joystick(str(p), ioctl=lambda fd, req, buf: bitmask([0, 1], len(buf))) is True
    assert s.probe_joystick(str(p), ioctl=lambda fd, req, buf: bitmask([0], len(buf))) is False


# ---------------- горячее подключение ----------------
class Plug:
    """Подставной каталог: файл устройства (для check()), каналы pipe как его события; opener — EvdevJoystick."""

    def __init__(self, root, ioctl=None):
        self.root, self.ioctl = root, ioctl or FakeIoctl()
        self.path = str(root / "event4")
        self.w = None
        self.opened = 0

    def attach(self):
        open(self.path, "wb").close()

    def detach(self, close_pipe=True):
        os.unlink(self.path)
        if close_pipe and self.w is not None:
            os.close(self.w)
            self.w = None

    def find(self):
        return self.path if os.path.exists(self.path) else None

    def open(self, path):
        r, self.w = os.pipe()
        os.set_blocking(r, False)
        self.opened += 1
        return s.EvdevJoystick(path, ioctl=self.ioctl, fd=r)

    def send(self, data):
        os.write(self.w, data)


def test_source_hot_plug(tmp_path):
    plug = Plug(tmp_path)
    src = s.StickSource(str(tmp_path), opener=plug.open, find=plug.find, rescan_s=0.05)
    assert src.scan() is False and src.state()["present"] is False
    plug.attach()
    assert src.scan() is True
    st = src.state()
    assert st["present"] and st["name"] == BOXER_NAME and st["axis_count"] == 8 and st["button_count"] == 24
    assert st["generation"] == 1 and st["path"] == plug.path
    plug.send(ev(s.EV_ABS, 1, 0) + ev(s.EV_KEY, 0x130, 1) + syn())
    assert src.poll(0.5) and src.state()["axes"][1] == -1000 and src.state()["buttons"] == 1
    plug.detach()                                                # выдернули: EOF на дескрипторе
    assert src.poll(0.5) is False
    st = src.state()
    assert not st["present"] and st["lost"] == 1 and st["axes"] == [0] * 8 and st["buttons"] == 0
    assert st["name"] == BOXER_NAME                              # имя остаётся для кадра dev.ok=false
    assert src.scan() is False                                   # устройства нет
    plug.attach()
    assert src.scan() and src.state()["generation"] == 2 and plug.opened == 2


def test_source_check_detects_removed_node(tmp_path):
    plug = Plug(tmp_path)
    plug.attach()
    src = s.StickSource(str(tmp_path), opener=plug.open, find=plug.find)
    assert src.scan()
    plug.detach(close_pipe=False)                                # узел удалён, select молчит
    src.check()
    assert not src.state()["present"] and src.state()["lost"] == 1


def test_source_thread_reconnects(tmp_path):
    plug = Plug(tmp_path)
    src = s.StickSource(str(tmp_path), opener=plug.open, find=plug.find, rescan_s=0.05).start()
    try:
        plug.attach()
        assert wait(lambda: src.state()["present"], 2)
        plug.send(ev(s.EV_ABS, 0, 2048) + syn())
        assert wait(lambda: src.state()["axes"][0] == 1000, 2)
        plug.detach()
        assert wait(lambda: not src.state()["present"], 2)
        plug.attach()
        assert wait(lambda: src.state()["present"] and src.state()["generation"] == 2, 2)
    finally:
        src.stop()


def test_source_open_error_is_reported(tmp_path):
    def opener(path):
        raise PermissionError(13, "Permission denied")
    src = s.StickSource(str(tmp_path), opener=opener, find=lambda: "/dev/input/event4")
    assert src.scan() is False and "Permission denied" in src.state()["error"] and not src.state()["present"]


def wait(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


# ---------------- передача ----------------
class FakeSource:
    def __init__(self, present=True):
        self.st = {**s.empty_state(), "present": present, "name": BOXER_NAME if present else "",
                   "axes": [0, -12, 1000, 3, -1000, 0, 0, 1000], "buttons": 5, "axis_count": 8, "button_count": 24,
                   "generation": 1 if present else 0}

    def state(self):
        return dict(self.st, axes=list(self.st["axes"]))

    def unplug(self):
        self.st.update(present=False, axes=[0] * 8, buttons=0, lost=self.st["lost"] + 1)

    def plug(self):
        self.st.update(present=True, axes=[0, -12, 1000, 3, -1000, 0, 0, 1000], buttons=5,
                       generation=self.st["generation"] + 1)

    def start(self):
        return self

    def stop(self):
        pass


class FakeClient:
    """PeerClient по контракту ADR-0013, 4.1: send_rc хранит последний кадр; номер ставит канал при отправке."""

    def __init__(self, session="1.4", phase="connected"):
        self.session, self.phase, self.connects = session, phase, 1
        self.frames, self.rc_state, self.result = [], None, None

    def send_rc(self, body):
        if self.phase != "connected":
            return "offline"
        if self.session != "1.4":
            return "unsupported"
        self.frames.append(body)
        return self.result or "queued"

    def snapshot(self):
        return {"phase": self.phase, "session": self.session if self.phase == "connected" else None,
                "counters": {"connects": self.connects}, "rc_state": self.rc_state,
                "rc_sent": {"n": len(self.frames), "at": None, "pending": False, "ring": []}}


class OldClient:
    """Клиент канала без send_rc (пульт до слияния с агентом R)."""

    def snapshot(self):
        return {"phase": "connected", "session": "1.4", "counters": {"connects": 1}}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def rig():
    src, client, clock = FakeSource(), FakeClient(), Clock()
    box = {"client": client}
    snd = s.StickSender(src, lambda: box["client"], clock=clock)

    def tick(n=1):
        for _ in range(n):
            clock.t += 0.02
            snd.step(clock.t)
    return src, client, snd, tick, box


def test_disabled_after_start_sends_nothing(rig):
    src, client, snd, tick, _ = rig
    assert snd.enabled is False
    tick(50)
    assert client.frames == []


def test_enable_sends_frames_with_axes_buttons_and_device(rig):
    src, client, snd, tick, _ = rig
    assert snd.set_enabled(True) == "ok"
    tick(3)
    assert len(client.frames) == 3
    f = client.frames[0]
    assert set(f) == {"n", "en", "ch", "btn", "dev"}
    assert f["en"] is True and f["ch"] == [0, -12, 1000, 3, -1000, 0, 0, 1000] and f["btn"] == 5
    assert f["dev"] == {"name": BOXER_NAME, "ok": True}
    assert [x["n"] for x in client.frames] == [1, 2, 3]
    src.st["axes"][0] = 250
    tick()
    assert client.frames[-1]["ch"][0] == 250 and snd.status()["sent"] == 4


@pytest.mark.parametrize("case,expected", [("no-device", "no-device"), ("no-link", "no-link"),
                                           ("old-session", "old-remote"), ("no-send-rc", "old-remote"),
                                           ("no-client", "no-link")])
def test_enable_refused(rig, case, expected):
    src, client, snd, tick, box = rig
    if case == "no-device":
        src.unplug()
    elif case == "no-link":
        client.phase = "connecting"
    elif case == "old-session":
        client.session = "1.3"
    elif case == "no-send-rc":
        box["client"] = OldClient()
    else:
        box["client"] = None
    assert snd.set_enabled(True) == expected and snd.enabled is False
    tick(10)
    assert client.frames == []


def test_operator_disable_sends_exactly_one_en_false(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick(5)
    assert snd.set_enabled(False) == "ok" and snd.enabled is False
    tick(20)
    assert len(client.frames) == 6
    last = client.frames[-1]
    assert last["en"] is False and last["dev"]["ok"] is True and last["n"] == 6
    assert snd.status()["off_reason"] == "operator"
    assert snd.set_enabled(False) == "ok"                       # повторное выключение — без нового кадра
    tick(5)
    assert len(client.frames) == 6


def test_device_loss_sends_one_dev_ok_false_and_disables(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick(5)
    src.unplug()
    tick(20)
    assert len(client.frames) == 6
    last = client.frames[-1]
    assert last["en"] is True and last["dev"] == {"name": BOXER_NAME, "ok": False}   # FAILSAFE device-lost, не IDLE
    assert last["ch"] == [0] * 8 and last["btn"] == 0
    assert snd.enabled is False and snd.status()["off_reason"] == "device-lost"
    src.plug()                                                  # вернули — передача остаётся выключенной
    tick(20)
    assert len(client.frames) == 6 and snd.enabled is False
    assert snd.set_enabled(True) == "ok"                        # снова — только оператором
    tick()
    assert client.frames[-1]["en"] is True and client.frames[-1]["dev"]["ok"] is True


def test_fast_replug_between_ticks_is_device_loss(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick()
    src.unplug()
    src.plug()                                                  # новое поколение устройства до следующего такта
    tick(5)
    assert client.frames[-1]["dev"]["ok"] is False and snd.enabled is False and len(client.frames) == 2


def test_no_link_sends_nothing_and_resumes_with_new_numbering(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick(3)
    client.phase = "connecting"
    tick(25)
    assert len(client.frames) == 3 and snd.enabled is True      # потеря связи не выключает передачу
    client.phase, client.connects = "connected", 2
    tick(2)
    assert [f["n"] for f in client.frames] == [1, 2, 3, 1, 2]   # новое соединение — с 1


def test_device_loss_without_link_sends_nothing_but_disables(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick()
    client.phase = "lost"
    src.unplug()
    tick(5)
    client.phase = "connected"
    tick(5)
    assert len(client.frames) == 1 and snd.enabled is False


def test_old_remote_after_reconnect_sends_nothing(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick()
    client.session, client.connects = "1.3", 2
    tick(10)
    assert len(client.frames) == 1


def test_offline_result_is_not_counted(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    client.result = "offline"
    tick(3)
    assert snd.status()["sent"] == 0 and snd.status()["last"] == "offline"


def test_latency_from_client_rc_state(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick()
    client.rc_state = {"state": "ACTIVE", "n": 1, "rx_at": 5.0, "latency_ms": 12.5, "rx_age_s": 0.0}
    tick()
    client.rc_state = dict(client.rc_state, rx_age_s=0.02)      # тот же rc-state: rx_age_s меняется, отсчёта нет
    tick()
    client.rc_state = {"state": "ACTIVE", "n": 2, "rx_at": 5.1, "latency_ms": 20.0, "rx_age_s": 0.0}
    tick()
    client.rc_state = {"state": "IDLE", "n": 2, "rx_at": 6.1, "latency_ms": 900.0, "rx_age_s": 0.0}
    tick()                                                      # в IDLE — не задержка канала
    lat = snd.status()["latency"]
    assert lat["samples"] == 2 and lat["median_ms"] == 16.2 and lat["source"] == "client"
    assert snd.status()["latency_ms"] == 16.2 and lat["recent_ms"] == [12.5, 20.0]


def test_latency_from_ring_without_latency_ms(rig):
    src, client, snd, tick, _ = rig
    snd.set_enabled(True)
    tick()
    snap = {"rc_state": {"state": "ACTIVE", "n": 7, "rx_at": 10.030}, "rc_sent": {"n": 9, "at": 10.05,
                                                                                    "ring": [[6, 9.99], [7, 10.01]]}}
    assert s.client_timing(snap, 7) == (10.030, 10.01)
    assert s.client_timing({"rc_state": {"n": 7}, "rc_sent": {}}, 7) is None


def test_sender_thread_runs_at_50_hz():
    src, client = FakeSource(), FakeClient()
    snd = s.StickSender(src, lambda: client)
    snd.set_enabled(True)
    snd.start()
    try:
        time.sleep(1.0)
    finally:
        snd.stop()
    assert 44 <= len(client.frames) <= 56, len(client.frames)
    gaps = [b["n"] - a["n"] for a, b in zip(client.frames, client.frames[1:])]
    assert set(gaps) == {1}


def test_sender_stop_sends_no_farewell_frame():
    src, client = FakeSource(), FakeClient()
    snd = s.StickSender(src, lambda: client)
    snd.set_enabled(True)
    snd.start()
    time.sleep(0.1)
    snd.stop()
    assert client.frames and all(f["en"] is True and f["dev"]["ok"] for f in client.frames)   # ST-06: FAILSAFE на блоке


def test_concurrent_enable_disable_is_consistent():
    src, client = FakeSource(), FakeClient()
    snd = s.StickSender(src, lambda: client)
    snd.start()
    try:
        def flip():
            for _ in range(200):
                snd.set_enabled(True)
                snd.set_enabled(False)
        ts = [threading.Thread(target=flip) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        time.sleep(0.1)
    finally:
        snd.stop()
    assert snd.enabled is False
    assert all(isinstance(f["n"], int) for f in client.frames)


# ---------------- SticksView ----------------
@pytest.fixture
def view(qapp):
    src, client, clock = FakeSource(), FakeClient(), Clock()
    snd = s.StickSender(src, lambda: client, clock=clock)
    logs = []
    v = s.SticksView(source=src, sender=snd, client=lambda: client, clock=clock,
                     log=lambda level, text: logs.append((level, text)))
    v.update_link("CONNECTED", "1.4")
    v.t = SimpleNS(src=src, client=client, clock=clock, logs=logs, snd=snd)
    return v


class SimpleNS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.parametrize("link,session,present,client,expected", [
    ("CONNECTED", "1.4", True, True, "ok"), ("CONNECTED", "1.5", False, True, "no-device"),
    ("CONNECTED", "1.3", True, True, "old-remote"), ("CONNECTED", "1.4", True, False, "old-remote"),
    ("DISCONNECTED", "1.4", True, True, "no-link"), ("CONNECTED", None, True, True, "no-link"),
    ("", "1.4", False, True, "no-link"), ("CONNECTED", "x", True, True, "no-link"),
])
def test_support_of(link, session, present, client, expected):
    assert s.support_of(link, session, client, present) == expected


def test_view_enable_requires_confirmation(view):
    assert view.setEnabled(True, False) is False and view.enabled is False
    view.t.snd.step(100.02)
    assert view.t.client.frames == []
    assert view.setEnabled(True, True) is True and view.enabled and view.result == "ok"
    assert view.setEnabled(False, False) is True and not view.enabled   # выключение — без подтверждения
    assert ("info", "operator: transmission enable") in view.t.logs


def test_view_refusal_reason_and_device_lost_notice(view):
    view.t.src.unplug()
    view.refresh()
    assert view.support == "no-device"
    assert view.setEnabled(True, True) is False and view.result == "no-device"
    view.t.src.plug()
    assert view.setEnabled(True, True) and view.result == "ok"
    view.t.src.unplug()
    view.t.snd.step(100.02)
    view.refresh()
    assert view.enabled is False and view.notice == "device-lost"
    assert any(level == "warning" and "device-lost" in text for level, text in view.t.logs)


def test_view_remote_echo_and_staleness(view):
    client = view.t.client
    client.rc_state = {"v": 1, "state": "ACTIVE", "reason": "fresh", "n": 10, "rate_hz": 49.8, "gaps": 1,
                       "failsafes": 2, "timeout_ms": 500, "ch": [0, -12, 1000, 3, -1000, 0, 0, 1000], "btn": 5,
                       "dev": {"name": BOXER_NAME, "ok": True}, "output": "none", "rx_at": 1.0, "rx_age_s": 0.01}
    view.refresh()
    assert view.remoteState == "ACTIVE" and view.remoteReason == "fresh" and view.remoteAxes[2] == 1000
    assert view.rateHz == 49.8 and view.gaps == 1 and view.failsafes == 2 and view.remoteButtons == 5
    assert view.state_text() == "Commands are arriving"          # без переводчика — исходная строка
    client.rc_state = dict(client.rc_state, rx_age_s=s.REMOTE_STALE_S + 0.5)
    view.refresh()
    assert view.remoteState == "" and view.state_text() == "No data from the remote unit"
    client.rc_state = dict(client.rc_state, rx_age_s=0.0, rx_at=9.0)
    view.update_link("DISCONNECTED", None)
    assert view.remoteState == "" and view.support == "no-link"
    assert view.state_text() == "No link to the remote unit"


def test_view_snapshot_format(view):
    view.t.client.rc_state = {"v": 1, "state": "IDLE", "reason": "start", "n": 0, "rx_at": 1.0, "rx_age_s": 0.0}
    view.refresh()
    snap = view.snapshot()
    assert set(snap) == {"support", "present", "enabled", "device", "axes", "buttons", "session", "remote", "sender",
                         "latency", "result", "notice", "display"}
    assert snap["device"] == {"name": BOXER_NAME, "path": "", "axes": 8, "buttons": 24}
    assert snap["remote"]["state"] == "IDLE" and snap["session"] == "1.4" and snap["enabled"] is False
    assert snap["display"] == {"stateText": "No commands"}
    assert set(snap["latency"]) >= {"samples", "median_ms", "p99_ms", "last_ms", "shown_ms", "window_s"}
