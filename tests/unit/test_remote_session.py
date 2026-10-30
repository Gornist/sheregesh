# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Прикладной сеанс агента принадлежит соединению (ADR-0007, ADR-0013): агент в процессе, соединения — поддельные
потоки asyncio (без сокетов и TLS).

Новое соединение после завершённого сеанса без собственного hello не исполняет rc, module-cmd и video-select;
сообщения и bye прежнего соединения, уже принятые до замены, не закрывают новое соединение и не меняют его
управление; новый сеанс после своего hello начинает нумерацию RC с n=1; несовместимый hello сеанса не даёт."""
import asyncio

import pytest

from sheregesh import PROTOCOL_VERSION
from sheregesh.link.codec import Decoder, encode
from sheregesh_remote import agent as agent_mod

CH = [0, -12, 1000, 3, -1000, 0, 0, 1000]
DEV = {"name": "OpenTX Radiomaster Boxer Joystick", "ok": True}


class _Args(Exception):
    pass


def make_agent(monkeypatch, *extra):
    """Аргументы — из настоящего разбора main(), агент — без run(): такты вызывает тест."""
    def capture(args):
        raise _Args(args)
    monkeypatch.setattr(agent_mod, "Agent", capture)
    with pytest.raises(_Args) as e:
        agent_mod.main(["--port", "0", "--no-video", "--module", *extra])
    monkeypatch.undo()
    a = agent_mod.Agent(e.value.args[0])
    a.handshakes = asyncio.Semaphore(4)
    return a


class Transport:
    def __init__(self, w):
        self.w = w

    def abort(self):
        self.w.closed = True
        self.w.reader.feed_eof()


class Writer:
    def __init__(self, reader):
        self.reader = reader
        self.out = bytearray()
        self.closed = False
        self.transport = Transport(self)

    def write(self, data):
        if not self.closed:
            self.out += data

    async def drain(self):
        if self.closed:
            raise ConnectionError("closed")

    def close(self):
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    def get_extra_info(self, key):
        return ("127.0.0.1", 50000) if key == "peername" else None

    def types(self):
        return [m["type"] for m in Decoder().feed(bytes(self.out)) if isinstance(m, dict)]


class Conn:
    def __init__(self, agent):
        self.reader = asyncio.StreamReader()
        self.writer = Writer(self.reader)
        self.seq = 0
        self.task = asyncio.ensure_future(agent.handle(self.reader, self.writer))

    def send(self, mtype, body, proto=PROTOCOL_VERSION):
        self.seq += 1
        self.reader.feed_data(encode(proto, mtype, self.seq, 0.0, body))

    def hello(self, proto=PROTOCOL_VERSION):
        self.send("hello", {"role": "console", "proto": proto, "software": "t", "hardware": "HW-1.0 Rev A",
                            "device_id": "t"}, proto)

    def rc(self, n, en=True):
        self.send("rc", {"n": n, "en": en, "ch": CH, "btn": 5, "dev": DEV})


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


def run(coro):
    asyncio.run(coro)


def test_new_connection_without_hello_does_not_control(monkeypatch):
    a = make_agent(monkeypatch)

    async def body():
        c1 = Conn(a)
        c1.hello()
        for n in (1, 2, 3):
            c1.rc(n)
        await settle()
        assert a.control.state() == "ACTIVE"
        c1.reader.feed_eof()                         # нормальное завершение первого сеанса
        await settle()
        assert a.control.state() == "FAILSAFE" and c1.task.done()
        frames, cmd = a.control.frames, a.module.cmd
        c2 = Conn(a)                                 # новое соединение без hello
        await settle()
        for n in (1, 2, 3):
            c2.rc(n)
        c2.send("module-cmd", {"req": 7, "op": "ping", "args": {}})
        c2.send("video-select", {"req": 3, "id": "synthetic"})
        await settle()
        assert a.control.state() == "FAILSAFE" and a.control.frames == frames
        assert a.module.cmd is cmd is None and a.conn.video_select is None
        assert "rc-state" not in c2.writer.types() and "status" not in c2.writer.types()
        c2.hello()                                   # свой hello — с этого момента сеанс есть, нумерация с n=1
        c2.rc(1)
        await settle()
        assert a.control.state() == "ACTIVE" and a.control.frames == frames + 1
        c2.reader.feed_eof()
        await settle()
    run(body())
    a.module.close()


def test_stale_connection_messages_do_not_touch_new_session(monkeypatch):
    a = make_agent(monkeypatch)

    async def body():
        c1 = Conn(a)
        c1.hello()
        c1.rc(1)
        await settle()
        assert a.control.state() == "ACTIVE"
        c2 = Conn(a)                                 # замена: прежнее соединение оборвано
        await settle()
        c2.hello()
        await settle()
        assert a.conn.writer is c2.writer and a.control.state() == "FAILSAFE"
        # Принятое до обрыва, но обработанное после замены: прежнее соединение не исполняется
        await a.on_message({"type": "rc", "body": {"n": 2, "en": True, "ch": CH, "btn": 5, "dev": DEV}},
                           c1.writer)
        await a.on_message({"type": "bye", "body": {}}, c1.writer)
        await a.on_message({"type": "hello", "body": {"proto": "1.0", "software": "x", "hardware": "y"}},
                           c1.writer)
        await settle()
        assert a.conn.writer is c2.writer and not c2.writer.closed
        assert a.control.state() == "FAILSAFE" and a.conn.version is not None and a.conn.writer is c2.writer
        c2.rc(1)
        await settle()
        assert a.control.state() == "ACTIVE"
        c2.reader.feed_eof()
        await settle()
    run(body())
    a.module.close()


def test_incompatible_hello_gives_no_session(monkeypatch):
    a = make_agent(monkeypatch)

    async def body():
        c1 = Conn(a)
        c1.hello()
        c1.rc(1)
        await settle()
        c1.reader.feed_eof()
        await settle()
        c2 = Conn(a)
        c2.hello(proto="9.0")
        await settle()
        assert "error" in c2.writer.types() and c2.writer.closed
        assert a.conn is None or a.conn.version is None
        assert a.control.state() == "FAILSAFE"
    run(body())
    a.module.close()


def test_replaced_during_video_begin_ends_stale_video(monkeypatch):
    """Соединение заменено, пока выбирается транспорт видео по его hello: после выбора видео прежнего сеанса
    закрывается и не достаётся новому соединению, у которого своего hello ещё нет."""
    a = make_agent(monkeypatch)
    gate = asyncio.Event()
    calls = []
    begin, end = a.vtx.begin, a.vtx.end

    async def slow_begin(*args):
        calls.append("begin")
        await gate.wait()
        res = await begin(*args)
        calls.append("begun")
        return res

    async def record_end(reason):
        calls.append(f"end:{reason}")
        return await end(reason)
    a.vtx.begin, a.vtx.end = slow_begin, record_end

    async def body():
        c1 = Conn(a)
        c1.hello()
        await settle()
        assert calls[-1] == "begin"                  # handle() перед ним закрывает видео прежнего соединения
        c2 = Conn(a)                                 # замена, пока begin прежнего сеанса не завершён
        await settle()
        assert a.conn.writer is c2.writer and a.conn.version is None
        gate.set()
        await settle(40)
        assert "begun" in calls and calls[-1] == "end:control session replaced"   # закрыто после выбора
        assert a.conn.writer is c2.writer and a.conn.version is None and not c2.writer.closed
        c2.reader.feed_eof()
        await settle()
    run(body())
    a.module.close()


def test_compatible_hello_on_network_sets_time_peer(monkeypatch):
    """ADR-0017: в сети стенда (mTLS) совместимый hello пульта → sheregesh-time-peer@<адрес пульта> (в потоке);
    несовместимый hello и эмуляция без TLS — без вызова."""
    a = make_agent(monkeypatch)
    calls = []
    monkeypatch.setattr(agent_mod.timepeer, "start", lambda addr: calls.append(addr) or (True, str(addr)))

    async def body():
        c0 = Conn(a)                                 # эмуляция без TLS: время не трогаем
        c0.hello()
        await settle()
        assert calls == []
        c0.reader.feed_eof()
        await settle()
        a.tls_cfg = object()                         # сетевой профиль (сама проверка TLS здесь не нужна)
        a.peer_desc = {"addr": "192.168.1.92"}
        c1 = Conn(a)
        c1.hello(proto="9.0")                        # несовместимый — сеанса нет
        await settle()
        assert calls == []
        c2 = Conn(a)
        c2.hello()
        for _ in range(50):
            await settle()
            if calls:
                break
        assert calls == ["192.168.1.92"]
        c2.reader.feed_eof()
        await settle()
    run(body())
    a.module.close()
