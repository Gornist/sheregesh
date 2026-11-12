# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты агентского видеосеанса sheregesh_remote/videotx.py (ADR-0016, контракт v5 §3, §5, §6, §8).

Без транспорта, службы и systemctl: сокет службы (FakeCtl), разрешение nftables (allow), отправка пульту (send),
журнал (log) и часы (clock) подменяются. Асинхронные сценарии запускаются через asyncio.run (pytest-asyncio нет).
Общий журнал `events` фиксирует порядок вызовов: ('ctl', cmd, args) | ('send', mtype, body) | ('allow', target).
"""
from __future__ import annotations

import asyncio
import base64
import copy
import itertools
import json
import re

import pytest

from sheregesh.videosvc.ctl import CtlError, CtlUnavailable
from sheregesh_remote.videotx import STREAM, VideoTx, choose_transport

PEER = "192.168.1.92"
PEER_PORT = 7444
RTCP_PORT = 7445
CIPHER = "aes-128-gcm"
HELLO = {"video_transports": ["srtp"], "video_rx_port": PEER_PORT}
OTHER_SESSION = "0" * 32                              # заведомо не совпадает с сессией блока (случайной)
HEX32 = re.compile(r"[0-9a-f]{32}")
_ABSENT = object()


class FakeClock:
    """Часы для clock=: атрибут t — текущее время, вызов возвращает его."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeCtl:
    """Подмена CtlClient: request(cmd, args) пишет вызов в общий журнал; status отдаёт настраиваемый ответ."""

    def __init__(self, events: list, down: bool = False) -> None:
        self.events = events
        self.calls: list[tuple[str, dict | None]] = []
        self.down = down                              # True — любой запрос бросает CtlUnavailable("down")
        self.status_reply: dict = {"v": 1, "role": "tx", "streams": {}}
        self.start_exc: Exception | None = None       # если задано — каждый start бросает его
        self.observer = None                          # callable(cmd, args) — вызывается до записи вызова

    def request(self, cmd, args=None):
        if self.observer is not None:
            self.observer(cmd, args)
        self.calls.append((cmd, args))
        self.events.append(("ctl", cmd, args))
        if self.down:
            raise CtlUnavailable("down")
        if cmd == "start" and self.start_exc is not None:
            raise self.start_exc
        if cmd == "status":
            return copy.deepcopy(self.status_reply)
        return {"v": 1, "ok": True}


class Rig:
    """Один видеосеанс блока с подменами и общим журналом порядка вызовов."""

    def __init__(self, *, down: bool = False, enabled: bool = True, profile_fn=None) -> None:
        self.events: list = []
        self.sent: list[tuple[str, dict]] = []
        self.log_lines: list[str] = []
        self.allow_calls: list[str] = []
        self.allow_exc: Exception | None = None
        self.clock = FakeClock()
        self.ctl = FakeCtl(self.events, down=down)
        self.vtx = VideoTx(send=self.send, log=self.log_lines.append,
                           spec_fn=lambda: {"source": "synthetic"}, profile_fn=profile_fn,
                           ctl=self.ctl, allow=self.allow, clock=self.clock, enabled=enabled)

    async def send(self, mtype, body) -> None:
        self.sent.append((mtype, body))
        self.events.append(("send", mtype, body))

    def allow(self, target: str) -> None:
        self.allow_calls.append(target)
        self.events.append(("allow", target))
        if self.allow_exc is not None:
            raise self.allow_exc

    def bodies(self, mtype: str) -> list[dict]:
        return [b for t, b in self.sent if t == mtype]

    def video_keys(self) -> list[dict]:
        return self.bodies("video-key")

    def ctl_args(self, cmd: str) -> list:
        return [a for c, a in self.ctl.calls if c == cmd]

    def advance(self, seconds: float) -> None:
        self.clock.t += seconds


def set_status(rig: Rig, state: str, gen: int, **extra) -> None:
    """Ответ `status` службы: поток main с состоянием и поколением."""
    rig.ctl.status_reply = {"v": 1, "role": "tx", "streams": {STREAM: {"state": state, "gen": gen, **extra}}}


async def start_srtp(rig: Rig) -> dict:
    """probe + begin с рабочим пультом 1.5: возвращает body первого video-key."""
    assert await rig.vtx.probe() is True
    assert await rig.vtx.begin(HELLO, PEER, "1.5") == "srtp"
    return rig.video_keys()[-1]


async def ack(rig: Rig, body: dict, ok: bool = True, error: str | None = None) -> None:
    msg = {"stream": STREAM, "session": body["session"], "gen": body["gen"], "ok": ok}
    if error is not None:
        msg["error"] = error
    await rig.vtx.on_message("video-key-ack", msg)


async def fallback(rig: Rig, session: str, gen: int) -> None:
    await rig.vtx.on_message("video-fallback",
                             {"stream": STREAM, "session": session, "gen": gen, "reason": "no-frames"})


def _first(seq, pred) -> int:
    return next(i for i, e in enumerate(seq) if pred(e))


# --- 1. choose_transport -------------------------------------------------------------------------------------------

_TRANSPORT_OPTS = {"без-поля": _ABSENT, "пусто": [], "только-tls": ["tls"], "srtp": ["srtp"],
                   "srtp-и-tls": ["srtp", "tls"]}
_PORT_OPTS = {"без-порта": _ABSENT, "порт-7444": 7444, "порт-bool": True, "порт-0": 0, "порт-70000": 70000}


def _want(srtp_ok: bool, session, hello) -> str:
    """Ожидание по контракту: srtp только при сеансе ≥ 1.5, доступном srtp, объявленном srtp и валидном порте."""
    if not srtp_ok or session != "1.5" or not isinstance(hello, dict):
        return "none"
    transports, port = hello.get("video_transports"), hello.get("video_rx_port")
    if isinstance(transports, list) and "srtp" in transports \
            and isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        return "srtp"
    return "none"


def _transport_cases():
    for srtp_ok, session, (tn, tv), (pn, pv) in itertools.product(
            [True, False], ["1.5", "1.4", None], _TRANSPORT_OPTS.items(), _PORT_OPTS.items()):
        hello = {}
        if tv is not _ABSENT:
            hello["video_transports"] = tv
        if pv is not _ABSENT:
            hello["video_rx_port"] = pv
        yield pytest.param(srtp_ok, session, hello, id=f"ok={srtp_ok}|sess={session}|{tn}|{pn}")


@pytest.mark.parametrize("srtp_ok, session, hello", list(_transport_cases()))
def test_choose_transport_table(srtp_ok, session, hello):
    """Таблица комбинаций: сессия, объявленные транспорты, порт приёма, доступность SRTP."""
    assert choose_transport(session, hello, srtp_ok) == _want(srtp_ok, session, hello)


def test_choose_transport_srtp_only_in_full_good_case():
    """Эталон: сессия 1.5, srtp доступен, пульт объявил srtp и порт — srtp; иначе видео нет (none)."""
    assert choose_transport("1.5", HELLO, True) == "srtp"
    assert choose_transport("1.5", HELLO, False) == "none"
    assert choose_transport("1.4", HELLO, True) == "none"
    assert choose_transport(None, HELLO, True) == "none"


@pytest.mark.parametrize("hello", [None, "srtp", ["srtp"], PEER_PORT], ids=["none", "str", "list", "int"])
@pytest.mark.parametrize("srtp_ok", [True, False])
def test_choose_transport_non_dict_hello_is_none(hello, srtp_ok):
    """hello, не являющийся объектом, — видео нет."""
    assert choose_transport("1.5", hello, srtp_ok) == "none"


def test_choose_transport_transports_not_a_list_is_none():
    """video_transports строкой, а не списком, — не объявление srtp."""
    assert choose_transport("1.5", {"video_transports": "srtp", "video_rx_port": PEER_PORT}, True) == "none"


def test_transports_after_successful_probe():
    """После успешного probe() агент объявляет только srtp (TLS-видео удалено)."""
    async def scenario():
        rig = Rig()
        assert rig.vtx.transports() == []                # до пробы SRTP не объявляется
        assert await rig.vtx.probe() is True
        assert rig.vtx.transports() == ["srtp"]
    asyncio.run(scenario())


def test_transports_empty_when_service_down_or_disabled():
    """Сокет службы недоступен или SRTP выключен — видеотранспортов нет."""
    async def scenario():
        down = Rig(down=True)
        assert await down.vtx.probe() is False
        assert down.vtx.transports() == []

        disabled = Rig(enabled=False)
        assert await disabled.vtx.probe() is False
        assert disabled.vtx.transports() == []
        assert disabled.ctl.calls == []                  # выключенный SRTP службу даже не опрашивает
    asyncio.run(scenario())


# --- 2. полный цикл -------------------------------------------------------------------------------------------------

def test_full_cycle_start_only_after_ack():
    """probe → begin → один video-key; start — только после ack текущего поколения; повтор ack не даёт второго start."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)

        vks = rig.video_keys()
        assert len(vks) == 1
        assert vks[0] is body
        assert body["transport"] == "srtp"
        assert body["srtp"]["cipher"] == CIPHER
        assert body["rtcp_port"] == RTCP_PORT
        assert body["gen"] == 1
        assert HEX32.fullmatch(body["session"])
        assert body["profile"] == {"width": 1280, "height": 720, "fps": 30, "bitrate": 6000}
        assert 1 <= body["ssrc"] <= 2**32 - 1
        assert len(base64.b64decode(body["srtp"]["key_b64"], validate=True)) == 28   # GCM: 16 + 12

        # до ack: служба не получила start, адрес пульта не открыт
        assert rig.ctl_args("start") == []
        assert rig.allow_calls == []

        await ack(rig, body)
        assert rig.allow_calls == [PEER]
        starts = rig.ctl_args("start")
        assert len(starts) == 1
        args = starts[0]
        assert args == {
            "stream": STREAM,
            "session": body["session"],
            "gen": body["gen"],
            "ssrc": body["ssrc"],
            "peer": {"ip": PEER, "port": PEER_PORT, "rtcp_port": RTCP_PORT},
            "srtp": {"cipher": CIPHER, "key_b64": body["srtp"]["key_b64"]},
            "source": {"source": "synthetic"},
            "profile": {"width": 1280, "height": 720, "fps": 30, "bitrate": 6000},
        }
        # адрес открыт раньше запуска службы
        assert _first(rig.events, lambda e: e == ("allow", PEER)) < \
            _first(rig.events, lambda e: e[0] == "ctl" and e[1] == "start")
        assert rig.vtx.mode == "srtp"

        # повтор того же ack не даёт второго start
        await ack(rig, body)
        assert len(rig.ctl_args("start")) == 1
        assert len(rig.video_keys()) == 1
    asyncio.run(scenario())


def test_profile_fn_feeds_video_key_and_start():
    """profile_fn задаёт профиль и в video-key, и в start."""
    prof = {"width": 640, "height": 360, "fps": 15, "bitrate": 1500}

    async def scenario():
        rig = Rig(profile_fn=lambda: dict(prof))
        body = await start_srtp(rig)
        await ack(rig, body)
        assert body["profile"] == prof
        assert rig.ctl_args("start")[0]["profile"] == prof
    asyncio.run(scenario())


# --- 3. ack чужого поколения и отказ --------------------------------------------------------------------------------

_WRONG_ACKS = {
    "чужая-session-ok": lambda b: {"session": OTHER_SESSION, "gen": b["gen"], "ok": True},
    "чужой-gen-ok": lambda b: {"session": b["session"], "gen": b["gen"] + 1, "ok": True},
    "чужой-gen-ok-false": lambda b: {"session": b["session"], "gen": b["gen"] + 1, "ok": False,
                                     "error": "stale"},
    "чужая-session-ok-false": lambda b: {"session": OTHER_SESSION, "gen": b["gen"], "ok": False,
                                         "error": "stale"},
}


@pytest.mark.parametrize("make", list(_WRONG_ACKS.values()), ids=list(_WRONG_ACKS))
def test_ack_of_another_session_or_generation_is_ignored(make):
    """ack с чужими session или gen (ok и не ok) игнорируется: start не вызван."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await rig.vtx.on_message("video-key-ack", {"stream": STREAM, **make(body)})
        assert rig.ctl_args("start") == []
        assert rig.allow_calls == []
        assert rig.vtx.mode == "srtp"
    asyncio.run(scenario())


def test_refused_ack_of_current_generation_is_reported_and_not_started():
    """ok:false с верными session и gen пишет ошибку в status, start не вызывается."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body, ok=False, error="unsupported")
        assert rig.ctl_args("start") == []
        assert rig.vtx.status()["srtp"]["error"] == "ack refused: unsupported"
    asyncio.run(scenario())


# --- 4. ошибка allow --------------------------------------------------------------------------------------------------

def test_allow_failure_does_not_block_start():
    """allow бросает исключение — start всё равно вызывается, ошибка видна в status."""
    async def scenario():
        rig = Rig()
        rig.allow_exc = RuntimeError("boom")
        body = await start_srtp(rig)
        await ack(rig, body)
        assert rig.allow_calls == [PEER]
        assert len(rig.ctl_args("start")) == 1
        assert rig.vtx.status()["srtp"]["allow_error"] == "allow: RuntimeError"
        assert rig.vtx.mode == "srtp"
    asyncio.run(scenario())


# --- 5. fallback ------------------------------------------------------------------------------------------------------

def test_fallback_of_current_generation_stops_service_and_leaves_session_without_video():
    """video-fallback (старый пульт) текущего поколения: stop службе, allow@none, режим off — TLS-видео нет."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)

        # чужой gen и чужая session — игнорируются
        await fallback(rig, body["session"], body["gen"] + 1)
        await fallback(rig, OTHER_SESSION, body["gen"])
        assert rig.vtx.mode == "srtp"
        assert rig.ctl_args("stop") == []
        assert rig.allow_calls == [PEER]

        # текущее поколение — служба остановлена, сеанс управления без видео
        await fallback(rig, body["session"], body["gen"])
        assert rig.ctl_args("stop") == [{"stream": STREAM, "reason": "fallback"}]
        stop_i = _first(rig.events, lambda e: e[0] == "ctl" and e[1] == "stop")
        none_i = _first(rig.events, lambda e: e == ("allow", "none"))
        assert stop_i < none_i
        assert rig.allow_calls[-1] == "none"
        assert rig.vtx.mode == "off"
        st = rig.vtx.status()
        assert st["active"] is False and st["srtp"]["state"] == "off" and "fallback" in st["srtp"]["error"]

        # после отката: ack, tick, select и повторный fallback ничего не делают, SRTP не возвращается
        n_calls, n_sent = len(rig.ctl.calls), len(rig.sent)
        await ack(rig, body)
        await rig.vtx.tick()
        await rig.vtx.on_select()
        await fallback(rig, body["session"], body["gen"])
        assert len(rig.ctl.calls) == n_calls
        assert len(rig.sent) == n_sent
        assert rig.vtx.mode == "off"
    asyncio.run(scenario())


# --- 6. video-select --------------------------------------------------------------------------------------------------

def test_video_select_starts_new_generation_with_new_key_and_ssrc():
    """video-select: новое поколение (gen+1, новый ключ и SSRC), второй start — после его ack."""
    async def scenario():
        rig = Rig()
        b1 = await start_srtp(rig)
        await ack(rig, b1)
        await rig.vtx.on_select()

        vks = rig.video_keys()
        assert len(vks) == 2
        b2 = vks[1]
        assert b2["gen"] == 2
        assert b2["session"] == b1["session"]
        assert b2["srtp"]["key_b64"] != b1["srtp"]["key_b64"]
        assert b2["ssrc"] != b1["ssrc"]
        assert len(rig.ctl_args("start")) == 1           # второй start — только после ack

        await ack(rig, b2)
        starts = rig.ctl_args("start")
        assert len(starts) == 2
        assert starts[1]["gen"] == 2
        assert starts[1]["ssrc"] == b2["ssrc"]
        assert starts[1]["srtp"]["key_b64"] == b2["srtp"]["key_b64"]
        assert rig.allow_calls == [PEER]                 # адрес уже открыт, повторно не открывается
    asyncio.run(scenario())


def test_video_select_without_srtp_session_sends_nothing():
    """video-select вне SRTP-сеанса ничего не отправляет."""
    async def scenario():
        rig = Rig()
        await rig.vtx.on_select()
        assert rig.sent == []
    asyncio.run(scenario())


# --- 7. tick ----------------------------------------------------------------------------------------------------------

def test_tick_streaming_clears_failures_and_passes_service_fields():
    """streaming текущего поколения: fails сброшен, поля статуса переданы."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)
        # один провал: stuck → новое поколение, fails = 1
        set_status(rig, "stuck", 1)
        rig.advance(2.1)
        await rig.vtx.tick()
        assert rig.vtx.fails == 1
        b2 = rig.video_keys()[-1]
        assert b2["gen"] == 2
        await ack(rig, b2)

        set_status(rig, "streaming", 2, fps=30, bitrate_kbps=5900,
                   rtcp={"keyframes_requested": 3, "keyframe_unsupported": True})
        await rig.vtx.tick()
        assert rig.vtx.fails == 0
        st = rig.vtx.status()
        assert st["transport"] == "srtp"
        assert st["active"] is True
        s = st["srtp"]
        assert s["state"] == "streaming"
        assert s["gen"] == 2
        assert s["fps"] == 30
        assert s["bitrate_kbps"] == 5900
        assert s["keyframes_requested"] == 3
        assert s["keyframe_unsupported"] is True
        assert s["error"] == ""
        assert len(rig.video_keys()) == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("state, extra", [("error", {"error": "need-new-gen"}), ("stuck", {})],
                         ids=["need-new-gen", "stuck"])
def test_tick_new_generation_on_need_new_gen_or_stuck(state, extra):
    """need-new-gen / stuck текущего поколения → новое поколение, но не раньше 2 с; чужое поколение игнорируется."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)

        # состояние чужого поколения игнорируется (часы не сдвигаем: интервал 2 с отсчитывается от текущего оффера)
        set_status(rig, state, body["gen"] + 1, **extra)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 1
        assert rig.vtx.status()["srtp"]["state"] != state

        # текущее поколение: сразу и через 1.9 с — не шлём; через 2.1 с — шлём
        set_status(rig, state, body["gen"], **extra)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 1
        rig.advance(1.9)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 1
        rig.advance(0.2)
        await rig.vtx.tick()
        vks = rig.video_keys()
        assert len(vks) == 2
        assert vks[1]["gen"] == 2
        assert vks[1]["srtp"]["key_b64"] != vks[0]["srtp"]["key_b64"]
    asyncio.run(scenario())


def test_retry_gap_grows_two_four_eight_sixteen_then_thirty():
    """Нарастающая пауза после неудач подряд: 2, 4, 8, 16, 30, 30 с (прежние «5 раз по 2 с, затем 30 с» —
    видео-renew пульта обходил паузу, стенд 2026-10-31)."""
    from sheregesh_remote.videotx import retry_gap
    assert [retry_gap(n) for n in range(0, 8)] == [2.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_failures_in_a_row_back_off_and_streaming_resets():
    """stuck подряд: каждое следующее поколение — не раньше растущей паузы; streaming сбрасывает её до 2 с."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)
        for gap in (2.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0):
            set_status(rig, "stuck", body["gen"])
            n = len(rig.video_keys())
            rig.advance(gap - 0.2)
            await rig.vtx.tick()
            assert len(rig.video_keys()) == n, f"раньше паузы {gap} с"
            rig.advance(0.3)
            await rig.vtx.tick()
            assert len(rig.video_keys()) == n + 1, f"после паузы {gap} с"
            error = rig.vtx.status()["srtp"]["error"]
            body = rig.video_keys()[-1]
            await ack(rig, body)
        assert error.startswith("video failed 7 times in a row")

        set_status(rig, "streaming", body["gen"])
        await rig.vtx.tick()
        assert rig.vtx.fails == 0
        rig.advance(30.1)                                 # пауза за последнюю неудачу истекла
        set_status(rig, "stuck", body["gen"])
        await rig.vtx.tick()
        n = len(rig.video_keys())
        body = rig.video_keys()[-1]
        await ack(rig, body)
        set_status(rig, "stuck", body["gen"])
        rig.advance(2.1)                                  # после сброса — снова 2 с
        await rig.vtx.tick()
        assert len(rig.video_keys()) == n + 1
    asyncio.run(scenario())


def test_renew_obeys_the_same_retry_pause():
    """video-renew не выводит блок из паузы: пока она не истекла — игнорируется."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)
        for _ in range(3):                                # три неудачи: следующая пауза 8 с
            set_status(rig, "stuck", body["gen"])
            rig.advance(16.1)
            await rig.vtx.tick()
            body = rig.video_keys()[-1]
            await ack(rig, body)
        n = len(rig.video_keys())
        renew = {"stream": STREAM, "session": body["session"], "gen": body["gen"], "reason": "no first frame"}
        rig.advance(5)
        await rig.vtx.on_message("video-renew", renew)
        assert len(rig.video_keys()) == n
        assert any("within the retry pause" in line for line in rig.log_lines)
        rig.advance(3.1)
        await rig.vtx.on_message("video-renew", renew)
        assert len(rig.video_keys()) == n + 1
    asyncio.run(scenario())


class Source:
    """Состояние входа для source_fn (status.video.source.state)."""

    def __init__(self, state: str = "starting") -> None:
        self.state = state

    def __call__(self):
        return self.state


def _rig_with_source(state: str) -> tuple[Rig, Source]:
    rig = Rig()
    src = Source(state)
    rig.vtx._source_fn = src
    return rig, src


def test_no_device_at_session_start_issues_no_generation_until_source_is_back():
    """Входа нет при начале сеанса: video-key не шлётся, тики и renew поколений не дают; вход вернулся — одно."""
    async def scenario():
        rig, src = _rig_with_source("no-device")
        assert await rig.vtx.probe() is True
        assert await rig.vtx.begin(HELLO, PEER, "1.5") == "srtp"
        assert rig.video_keys() == []
        st = rig.vtx.status()
        assert st["active"] is False and st["srtp"]["state"] == "waiting-source"
        assert st["srtp"]["error"] == "waiting for source: no-device"
        for _ in range(120):                              # 2 минуты без устройства
            rig.advance(1)
            await rig.vtx.tick()
        assert rig.video_keys() == []
        src.state = "starting"
        rig.advance(1)
        await rig.vtx.tick()
        keys = rig.video_keys()
        assert len(keys) == 1 and keys[0]["gen"] == 1
        assert rig.vtx.status()["srtp"]["state"] == "negotiating"
        rig.advance(1)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 1
    asyncio.run(scenario())


def test_device_lost_while_streaming_stops_generation_and_waits():
    """Устройство пропало во время потока (need-new-gen): stop службе, video-stop пульту, новых поколений нет,
    renew игнорируется; вернулось — новое поколение, пауза сброшена."""
    async def scenario():
        rig, src = _rig_with_source("starting")
        body = await start_srtp(rig)
        await ack(rig, body)
        set_status(rig, "streaming", body["gen"])
        await rig.vtx.tick()
        src.state = "no-device"
        set_status(rig, "error", body["gen"], error="need-new-gen")
        rig.advance(1)
        await rig.vtx.tick()
        stops = rig.bodies("video-stop")
        assert len(stops) == 1 and stops[0]["gen"] == body["gen"]
        assert stops[0]["reason"] == "source unavailable: no-device"
        assert [a for a in rig.ctl_args("stop")][-1]["reason"] == "source unavailable"
        renew = {"stream": STREAM, "session": body["session"], "gen": body["gen"], "reason": "no frames (error)"}
        for _ in range(60):
            rig.advance(1)
            await rig.vtx.tick()
            await rig.vtx.on_message("video-renew", renew)
        assert len(rig.video_keys()) == 1
        src.state = "starting"
        rig.advance(1)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 2 and rig.vtx.fails == 0
    asyncio.run(scenario())


def test_failed_start_with_device_gone_waits_instead_of_retrying():
    """start отказал, вход пропал: агент ждёт источник, а не перебирает поколения (стенд: 126 за 10 мин)."""
    async def scenario():
        rig, src = _rig_with_source("starting")
        rig.ctl.start_exc = CtlError("pipeline")
        body = await start_srtp(rig)
        await ack(rig, body)
        src.state = "no-device"
        for _ in range(600):
            rig.advance(1)
            await rig.vtx.tick()
        assert len(rig.video_keys()) == 1
        assert len(rig.bodies("video-stop")) == 1
    asyncio.run(scenario())


def test_source_fn_failure_is_treated_as_unknown():
    """Сбой наблюдения за входом не останавливает видео: состояние неизвестно — как без source_fn."""
    async def scenario():
        rig = Rig()
        def boom():
            raise RuntimeError("ingest")
        rig.vtx._source_fn = boom
        await start_srtp(rig)
        assert len(rig.video_keys()) == 1
    asyncio.run(scenario())


def test_failed_start_triggers_new_generation_after_two_seconds():
    """start провалился (busy) → тик не раньше 2 с даёт новое поколение; после успешного start ошибка сброшена."""
    async def scenario():
        rig = Rig()
        rig.ctl.start_exc = CtlError("busy")
        body = await start_srtp(rig)
        await ack(rig, body)
        assert rig.vtx.status()["srtp"]["error"] == "tx start: busy"

        await rig.vtx.tick()                              # без времени: новое поколение ещё рано
        assert len(rig.video_keys()) == 1
        rig.advance(2.1)
        rig.ctl.start_exc = None
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 2
        await ack(rig, rig.video_keys()[-1])
        assert len(rig.ctl_args("start")) == 2
        assert rig.vtx.status()["srtp"]["error"] == ""
    asyncio.run(scenario())


def test_no_ack_for_ten_seconds_triggers_new_generation():
    """Пульт не ответил на video-key дольше 10 с — новое поколение; раньше — нет."""
    async def scenario():
        rig = Rig()
        await start_srtp(rig)
        rig.advance(9)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 1
        rig.advance(1.1)
        await rig.vtx.tick()
        assert len(rig.video_keys()) == 2
        assert rig.video_keys()[1]["gen"] == 2
    asyncio.run(scenario())


def test_refused_ack_retries_after_backoff_not_ack_timeout():
    """Ревью 2026-11-12: отказ пульта («unsupported» — в том числе сбой его службы rx) ждал 10 с таймаута ack.
    Теперь поколение неудачно сразу: следующее — после обычной паузы 2 с."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body, ok=False, error="unsupported")
        rig.advance(0.5)
        await rig.vtx.tick()
        rig.advance(2.1)
        await rig.vtx.tick()
        keys = rig.video_keys()
        assert len(keys) == 2 and keys[1]["gen"] == 2
        assert rig.vtx.status()["srtp"]["state"] != "streaming"
    asyncio.run(scenario())


# --- 8. конец сеанса --------------------------------------------------------------------------------------------------

def test_end_stops_service_notifies_peer_and_closes_allow():
    """end из srtp: stop службе, video-stop пульту с текущим поколением, allow@none; повторный end — ничего."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)
        mark = len(rig.events)

        await rig.vtx.end("session-end")
        tail = rig.events[mark:]
        assert tail == [
            ("ctl", "stop", {"stream": STREAM, "reason": "session-end"}),
            ("send", "video-stop", {"stream": STREAM, "session": body["session"], "gen": body["gen"],
                                    "reason": "session-end"}),
            ("allow", "none"),
        ]
        assert rig.vtx.mode == "none"

        n = len(rig.events)
        await rig.vtx.end("session-end")
        assert len(rig.events) == n
    asyncio.run(scenario())


def test_end_from_none_or_off_does_nothing():
    """end без сеанса и в режиме off (сеанс без видео) не отправляет ничего и не вызывает службу."""
    async def scenario():
        idle = Rig()
        await idle.vtx.end("session-end")
        assert idle.events == []

        off = Rig(down=True)
        assert await off.vtx.begin(HELLO, PEER, "1.5") == "none"
        n_events, n_calls = len(off.events), len(off.ctl.calls)
        await off.vtx.end("session-end")
        assert len(off.events) == n_events
        assert len(off.ctl.calls) == n_calls
        assert off.vtx.mode == "none"
    asyncio.run(scenario())


# --- 9. служба недоступна, сессия 1.4 ---------------------------------------------------------------------------------

def test_service_down_begin_is_no_video_without_video_key():
    """Сокет службы недоступен: видео нет (off, причина в status), video-key не отправляется, адрес не открывается."""
    async def scenario():
        rig = Rig(down=True)
        assert await rig.vtx.probe() is False
        assert await rig.vtx.begin(HELLO, PEER, "1.5") == "none"
        assert rig.vtx.mode == "off"
        assert rig.video_keys() == []
        assert rig.allow_calls == []
        assert rig.vtx.status() == {"active": False,
                                    "srtp": {"state": "off", "error": "no video: video-tx service unavailable"}}
    asyncio.run(scenario())


def test_session_14_begin_is_no_video_even_when_service_available():
    """Сессия 1.4 (без video-key) при доступной службе — видео нет, SRTP не запускается."""
    async def scenario():
        rig = Rig()
        assert await rig.vtx.probe() is True
        assert await rig.vtx.begin(HELLO, PEER, "1.4") == "none"
        assert rig.vtx.mode == "off"
        assert rig.video_keys() == []
        assert rig.ctl_args("start") == []
        assert rig.allow_calls == []
    asyncio.run(scenario())


# --- 10. канарейка ключа ----------------------------------------------------------------------------------------------

def test_key_never_appears_in_log_status_repr_or_error_text():
    """Ключ SRTP есть в video-key и в start, но не в журнале, status, repr и тексте ошибок (start, allow)."""
    async def scenario():
        rig = Rig()
        b1 = await start_srtp(rig)
        k1 = b1["srtp"]["key_b64"]
        rig.allow_exc = RuntimeError("allow failed, key " + k1)   # текст исключения несёт ключ
        await ack(rig, b1)                                        # allow падает, start идёт
        allow_err = rig.vtx.status()["srtp"]["allow_error"]
        rig.allow_exc = None

        await rig.vtx.on_select()
        b2 = rig.video_keys()[-1]
        k2 = b2["srtp"]["key_b64"]
        rig.ctl.start_exc = CtlError("busy", detail="key " + k2)  # текст ошибки службы несёт ключ
        await ack(rig, b2)                                        # allow повторно ок, start падает
        start_err = rig.vtx.status()["srtp"]["error"]
        rig.ctl.start_exc = None

        await rig.vtx.on_select()
        b3 = rig.video_keys()[-1]
        k3 = b3["srtp"]["key_b64"]
        await ack(rig, b3)

        keys = [k1, k2, k3]
        assert len(set(keys)) == 3
        # непустая проверка: ключ действительно в video-key и в аргументах start
        for body, key in zip(rig.video_keys(), keys):
            assert key in json.dumps(body)
        start_keys = [a["srtp"]["key_b64"] for a in rig.ctl_args("start")]
        assert start_keys == [k1, k2, k3]
        assert allow_err and start_err                            # ошибки действительно были

        log_text = "\n".join(rig.log_lines)
        assert "***" in log_text                                  # маска в журнале есть
        status_text = json.dumps(rig.vtx.status())
        for key in keys:
            assert key not in log_text
            assert key not in status_text
            assert key not in repr(rig.vtx)
            assert key not in allow_err
            assert key not in start_err
            assert key not in str(rig.vtx.status())
    asyncio.run(scenario())


# --- 11. повторный hello ----------------------------------------------------------------------------------------------

def test_second_hello_tears_down_previous_session_then_starts_new_one():
    """hello при mode != none: сначала stop, video-stop и allow@none, затем новый session id и gen 1."""
    async def scenario():
        rig = Rig()
        b1 = await start_srtp(rig)
        await ack(rig, b1)
        old_session = b1["session"]
        mark = len(rig.events)

        assert await rig.vtx.begin(HELLO, PEER, "1.5") == "srtp"
        tail = rig.events[mark:]
        i_stop = _first(tail, lambda e: e == ("ctl", "stop", {"stream": STREAM, "reason": "hello-again"}))
        i_vstop = _first(tail, lambda e: e[0] == "send" and e[1] == "video-stop")
        i_allow = _first(tail, lambda e: e == ("allow", "none"))
        i_key = _first(tail, lambda e: e[0] == "send" and e[1] == "video-key")
        assert i_stop < i_vstop < i_allow < i_key
        assert tail[i_vstop][2] == {"stream": STREAM, "session": old_session, "gen": 1,
                                    "reason": "hello-again"}

        b2 = rig.video_keys()[-1]
        assert b2["session"] != old_session
        assert HEX32.fullmatch(b2["session"])
        assert b2["gen"] == 1

        await ack(rig, b2)
        assert rig.allow_calls == [PEER, "none", PEER]           # адрес снова открыт для нового сеанса
        assert len(rig.ctl_args("start")) == 2
    asyncio.run(scenario())


# --- video-renew (контракт v8) -------------------------------------------------------------------------------

def test_renew_of_current_generation_issues_new_key_and_ssrc():
    """video-renew текущего поколения → новое поколение (новый ключ и SSRC); чужое и слишком частое — игнорируются."""
    async def scenario():
        rig = Rig()
        body = await start_srtp(rig)
        await ack(rig, body)
        rig.advance(3)
        await rig.vtx.on_message("video-renew", {"stream": STREAM, "session": OTHER_SESSION, "gen": body["gen"],
                                                 "reason": "rx lost generation"})
        await rig.vtx.on_message("video-renew", {"stream": STREAM, "session": body["session"], "gen": body["gen"] + 7,
                                                 "reason": "rx lost generation"})
        assert len(rig.video_keys()) == 1
        await rig.vtx.on_message("video-renew", {"stream": STREAM, "session": body["session"], "gen": body["gen"],
                                                 "reason": "rx lost generation"})
        keys = rig.video_keys()
        assert len(keys) == 2 and keys[1]["gen"] == body["gen"] + 1
        assert keys[1]["ssrc"] != body["ssrc"] and keys[1]["srtp"]["key_b64"] != body["srtp"]["key_b64"]
        await ack(rig, keys[1])
        await rig.vtx.on_message("video-renew", {"stream": STREAM, "session": body["session"], "gen": keys[1]["gen"],
                                                 "reason": "again"})
        assert len(rig.video_keys()) == 2                 # раньше 2 с после прошлого поколения — игнорируется
    asyncio.run(scenario())

