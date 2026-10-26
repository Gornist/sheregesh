# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты службы передатчика видео (videosvc/tx.py, ADR-0016, контракт §2, §3, §5).

Конвейер подменён фабрикой (FakeFactory), открытие источника — фейком (make_opener). Сокет управления не запускается:
команды идут напрямую через TxService.handle, цикл — через tick. Часы управляемые: тест двигает их сам.
"""
import base64
import copy
import json
import logging
import secrets

import pytest

from sheregesh.videosvc import media as M
from sheregesh.videosvc import tx as T
from sheregesh.videosvc.ctl import CtlError
from sheregesh.videosvc.state import validate_state

SESSION = "a" * 32
KEY_LEN = {"aes-128-gcm": 28, "aes-128-icm": 30}
CANARY_PREFIX = b"SHRG-CANARY-VIDEOKEY-"


# --- заглушки ---------------------------------------------------------------------------------------------------

class Clock:
    """Управляемые часы: вызов отдаёт текущее время, advance сдвигает его."""

    def __init__(self, now):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakePipe:
    """Конвейер-заглушка: start/stop считаются, события имитирует emit (как поток GStreamer)."""

    def __init__(self, cfg, on_event, *, fail_start=False):
        self.cfg = cfg
        self.on_event = on_event
        self.fail_start = fail_start
        self.started = False
        self.stops = 0
        self.keyframes = 0
        self._stats = {"packets": 5, "bitrate_kbps": 1234.0, "pli_received": 2, "keyframes_requested": 1}

    def start(self):
        if self.fail_start:
            raise RuntimeError("элемент не создан")
        self.started = True

    def stop(self):
        self.stops += 1
        self.started = False

    def request_keyframe(self):
        self.keyframes += 1
        return True

    def stats(self):
        return dict(self._stats)

    def emit(self, kind, **detail):
        self.on_event(kind, detail)


class FakeFactory:
    """Фабрика конвейеров; запоминает созданные конвейеры в .pipes."""

    def __init__(self, *, create_error=None, fail_start=False):
        self.create_error = create_error
        self.fail_start = fail_start
        self.pipes = []

    def __call__(self, cfg, on_event):
        if self.create_error is not None:
            raise self.create_error
        pipe = FakePipe(cfg, on_event, fail_start=self.fail_start)
        self.pipes.append(pipe)
        return pipe


def make_opener(*, passthrough=False, errors=()):
    """Фейк открытия источника: по одной из errors за вызов (по порядку), затем отдаёт Media."""
    pending = list(errors)
    calls = []

    def opener(spec, profile):
        calls.append((spec, profile))
        if pending:
            raise pending.pop(0)
        return M.Media(fragment="videotestsrc ! fakeout", passthrough=passthrough,
                       info={"kind": "synthetic", "id": "synthetic", "name": "x", "encoder": "x264enc"})

    opener.calls = calls
    return opener


def build(tmp_path, clock, wall, *, factory=None, opener=None):
    return T.TxService(str(tmp_path), factory=factory if factory is not None else FakeFactory(),
                       media_opener=opener if opener is not None else make_opener(),
                       mono=clock, clock=wall, version="t")


# --- фикстуры и помощники ---------------------------------------------------------------------------------------

@pytest.fixture
def clock():
    return Clock(1000.0)


@pytest.fixture
def wall():
    return Clock(1_793_000_000.0)


@pytest.fixture
def factory():
    return FakeFactory()


@pytest.fixture
def opener():
    return make_opener()


@pytest.fixture
def svc(tmp_path, clock, wall, factory, opener):
    return build(tmp_path, clock, wall, factory=factory, opener=opener)


def key_for(cipher="aes-128-gcm"):
    return secrets.token_bytes(KEY_LEN[cipher])


def start_args(*, stream="main", gen=1, session=SESSION, ssrc=123456789, cipher="aes-128-gcm", key=None):
    if key is None:
        key = key_for(cipher)
    return {
        "stream": stream, "session": session, "gen": gen, "ssrc": ssrc,
        "peer": {"ip": "127.0.0.1", "port": 7444, "rtcp_port": 7445},
        "srtp": {"cipher": cipher, "key_b64": base64.b64encode(key).decode("ascii")},
        "source": {"source": "synthetic"},
        "profile": {"width": 1280, "height": 720, "fps": 30, "bitrate": 6000},
    }


def main_state(svc, stream="main"):
    return svc.state.snapshot()["streams"][stream]


def expect(code, fn, *args):
    """Вызов fn(*args) должен бросить CtlError с кодом code; исключение возвращается для проверок."""
    with pytest.raises(CtlError) as ei:
        fn(*args)
    assert ei.value.code == code, f"ожидали {code}, получили {ei.value.code}: {ei.value.detail}"
    return ei.value


# --- 1. start: результат, конфигурация конвейера, состояние ----------------------------------------------------

@pytest.mark.parametrize("cipher", ["aes-128-gcm", "aes-128-icm"])
def test_старт_собирает_конфигурацию_конвейера(svc, factory, cipher):
    key = key_for(cipher)
    args = start_args(gen=3, ssrc=123456789, cipher=cipher, key=key)
    r = svc.handle("start", args)
    assert r == {"stream": "main", "state": "starting", "session": SESSION, "gen": 3,
                 "ssrc": 123456789, "passthrough": False}
    (pipe,) = factory.pipes
    assert pipe.started
    cfg = pipe.cfg
    assert cfg.ssrc == 123456789
    assert cfg.peer_ip == "127.0.0.1" and cfg.peer_port == 7444
    assert cfg.rtcp_peer_port == 7445
    assert cfg.rtcp_local_port == 7445
    assert cfg.media == "videotestsrc ! fakeout"
    assert cfg.srtp.cipher == cipher
    assert cfg.srtp.key == key


def test_rtcp_local_port_берётся_из_peer_если_задан(svc, factory):
    args = start_args()
    args["peer"]["rtcp_local_port"] = 9001
    svc.handle("start", args)
    cfg = factory.pipes[0].cfg
    assert cfg.rtcp_local_port == 9001
    assert cfg.rtcp_peer_port == 7445


def test_старт_состояние_и_схема_проходят(svc, tmp_path):
    svc.handle("start", start_args(gen=2, ssrc=77))
    st = main_state(svc)
    assert st["state"] == "starting"
    assert st["session"] == SESSION and st["gen"] == 2 and st["ssrc"] == 77 and st["peer"] == "127.0.0.1"
    assert validate_state(svc.state.snapshot()) == []
    svc.state.write()
    doc = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert validate_state(doc) == []


# --- 2. ошибки start -----------------------------------------------------------------------------------------

BAD_START_ARGS = [
    ("лишнее поле", lambda a: a.update(extra=1)),
    ("нет поля profile", lambda a: a.pop("profile")),
    ("нет поля srtp", lambda a: a.pop("srtp")),
    ("stream с заглавной", lambda a: a.update(stream="Main")),
    ("session не hex", lambda a: a.update(session="g" * 32)),
    ("session короче 32", lambda a: a.update(session="a" * 31)),
    ("gen 0", lambda a: a.update(gen=0)),
    ("gen bool", lambda a: a.update(gen=True)),
    ("gen строка", lambda a: a.update(gen="1")),
    ("ssrc 0", lambda a: a.update(ssrc=0)),
    ("ssrc 2**32", lambda a: a.update(ssrc=2**32)),
    ("peer.ip не адрес", lambda a: a["peer"].update(ip="999.1.1.1")),
    ("peer.ip не строка", lambda a: a["peer"].update(ip=5)),
    ("peer.port 0", lambda a: a["peer"].update(port=0)),
    ("peer.port 70000", lambda a: a["peer"].update(port=70000)),
    ("peer без rtcp_port", lambda a: a["peer"].pop("rtcp_port")),
    ("peer лишнее поле", lambda a: a["peer"].update(extra=1)),
    ("profile лишнее поле", lambda a: a["profile"].update(extra=1)),
    ("profile.width строка", lambda a: a["profile"].update(width="1280")),
    ("profile.fps bool", lambda a: a["profile"].update(fps=True)),
    ("profile.bitrate вне диапазона", lambda a: a["profile"].update(bitrate=10)),
    ("source не объект", lambda a: a.update(source="synthetic")),
    ("srtp лишнее поле", lambda a: a["srtp"].update(extra=1)),
    ("srtp.key_b64 не строка", lambda a: a["srtp"].update(key_b64=5)),
]


@pytest.mark.parametrize("mutate", [m for _, m in BAD_START_ARGS], ids=[n for n, _ in BAD_START_ARGS])
def test_плохие_аргументы_старта_bad_request(svc, factory, mutate):
    args = start_args()
    mutate(args)
    expect("bad-request", svc.handle, "start", args)
    assert factory.pipes == []


def test_неизвестный_шифр_unsupported(svc, factory):
    args = start_args()
    args["srtp"]["cipher"] = "aes-256-gcm"
    expect("unsupported", svc.handle, "start", args)
    assert factory.pipes == []


@pytest.mark.parametrize("cipher,n", [("aes-128-gcm", 16), ("aes-128-gcm", 30), ("aes-128-icm", 28)])
def test_длина_ключа_не_та_bad_request(svc, factory, cipher, n):
    args = start_args(cipher=cipher, key=secrets.token_bytes(n))
    expect("bad-request", svc.handle, "start", args)
    assert factory.pipes == []


def test_ключ_не_base64_bad_request(svc, factory):
    args = start_args()
    args["srtp"]["key_b64"] = "!!не base64!!"
    expect("bad-request", svc.handle, "start", args)
    assert factory.pipes == []


@pytest.mark.parametrize("err_code,expected", [
    ("no-device", "pipeline"),
    ("unsupported", "unsupported"),
    ("bad-request", "bad-request"),
])
def test_ошибка_источника_переводится_в_код_команды(tmp_path, clock, wall, factory, err_code, expected):
    opener = make_opener(errors=[M.MediaError(err_code, "x")])
    svc = build(tmp_path, clock, wall, factory=factory, opener=opener)
    expect(expected, svc.handle, "start", start_args())
    st = main_state(svc)
    assert st["state"] == "error"
    assert st["error"] == err_code
    assert factory.pipes == []


def test_нет_устройства_повтор_того_же_поколения_разрешён(tmp_path, clock, wall, factory):
    opener = make_opener(errors=[M.MediaError("no-device", "gone")])
    svc = build(tmp_path, clock, wall, factory=factory, opener=opener)
    args = start_args(gen=1)
    expect("pipeline", svc.handle, "start", args)
    assert main_state(svc)["state"] == "error"
    # устройство так и не запускали: пара (session, gen) не сожжена, повтор проходит
    r = svc.handle("start", args)
    assert r["state"] == "starting" and r["gen"] == 1
    assert len(factory.pipes) == 1


@pytest.mark.parametrize("where", ["create", "start"])
def test_сбой_конвейера_pipeline_и_поколение_сожжено(tmp_path, clock, wall, where):
    if where == "create":
        factory = FakeFactory(create_error=RuntimeError("нет gst"))
    else:
        factory = FakeFactory(fail_start=True)
    svc = build(tmp_path, clock, wall, factory=factory)
    args = start_args(gen=1)
    expect("pipeline", svc.handle, "start", args)
    st = main_state(svc)
    assert st["state"] == "error" and st["error"] == "pipeline"
    # пара (ключ, SSRC) уже ушла в конвейер: тот же gen больше не принимается
    expect("stale", svc.handle, "start", args)
    if where == "start":
        assert factory.pipes[0].stops == 1


# --- 3. идемпотентность ---------------------------------------------------------------------------------------

def test_повтор_start_идемпотентен(svc, factory):
    args = start_args(gen=1)
    r1 = svc.handle("start", args)
    r2 = svc.handle("start", copy.deepcopy(args))
    assert r1 == r2
    assert len(factory.pipes) == 1
    assert factory.pipes[0].stops == 0


@pytest.mark.parametrize("change", ["ssrc", "key"])
def test_то_же_поколение_с_другими_параметрами_bad_request(svc, factory, change):
    args = start_args(gen=1)
    svc.handle("start", args)
    other = copy.deepcopy(args)
    if change == "ssrc":
        other["ssrc"] = args["ssrc"] + 1
    else:
        other["srtp"]["key_b64"] = base64.b64encode(key_for()).decode("ascii")
    expect("bad-request", svc.handle, "start", other)
    assert len(factory.pipes) == 1
    assert factory.pipes[0].stops == 0
    assert main_state(svc)["ssrc"] == args["ssrc"]


# --- 4. stale и вытеснение --------------------------------------------------------------------------------------

def test_gen_меньше_текущего_stale(svc):
    svc.handle("start", start_args(gen=2))
    expect("stale", svc.handle, "start", start_args(gen=1))


def test_gen_строго_больше_вытесняет_прежний_конвейер(svc, factory):
    svc.handle("start", start_args(gen=1))
    r = svc.handle("start", start_args(gen=2))
    assert r["gen"] == 2
    assert factory.pipes[0].stops == 1
    assert len(factory.pipes) == 2 and factory.pipes[1].started
    assert main_state(svc)["gen"] == 2


def test_новый_сеанс_вытесняет_и_старый_сеанс_stale(svc, factory):
    svc.handle("start", start_args(session=SESSION, gen=1))
    other = "b" * 32
    r = svc.handle("start", start_args(session=other, gen=1))
    assert r["session"] == other
    assert factory.pipes[0].stops == 1
    expect("stale", svc.handle, "start", start_args(session=SESSION, gen=5))


def test_старый_сеанс_после_stop_и_нового_сеанса_stale(svc, factory):
    svc.handle("start", start_args(session=SESSION, gen=1))
    svc.handle("stop", {"stream": "main"})
    svc.handle("start", start_args(session="b" * 32, gen=1))
    svc.handle("stop", {"stream": "main"})
    expect("stale", svc.handle, "start", start_args(session=SESSION, gen=2))


def test_неверный_ключ_не_гасит_работающий_поток(svc, factory):
    svc.handle("start", start_args(gen=1))
    expect("bad-request", svc.handle, "start", start_args(gen=2, key=secrets.token_bytes(16)))
    assert factory.pipes[0].stops == 0
    assert main_state(svc)["state"] == "starting"


# --- 5. автомат: первый кадр и таймаут ------------------------------------------------------------------------

def test_первый_кадр_переводит_в_streaming(svc, factory):
    svc.handle("start", start_args())
    factory.pipes[0].emit("first-frame")
    svc.tick()
    st = main_state(svc)
    assert st["state"] == "streaming"
    assert any(t[1] == "streaming" and len(t) == 3 for t in st["transitions"])


def test_нет_первого_кадра_за_5_секунд_need_new_gen(svc, factory, clock):
    args = start_args(gen=1)
    svc.handle("start", args)
    clock.advance(4.0)
    svc.tick()
    assert main_state(svc)["state"] == "starting"
    clock.advance(1.1)
    svc.tick()
    st = main_state(svc)
    assert st["state"] == "error" and st["error"] == "need-new-gen"
    assert factory.pipes[0].stops >= 1
    expect("stale", svc.handle, "start", args)
    r = svc.handle("start", start_args(gen=2))
    assert r["gen"] == 2 and r["state"] == "starting"


# --- 6. T2: ошибка конвейера не перезапускается службой ----------------------------------------------------

@pytest.mark.parametrize("kind,detail", [("error", {"message": "boom"}), ("eos", {})])
@pytest.mark.parametrize("first_frame", [False, True], ids=["до-кадра", "после-кадра"])
def test_ошибка_конвейера_не_перезапускается_сама(svc, factory, clock, kind, detail, first_frame):
    svc.handle("start", start_args(gen=1))
    pipe = factory.pipes[0]
    if first_frame:
        pipe.emit("first-frame")
        svc.tick()
    pipe.emit(kind, **detail)
    svc.tick()
    st = main_state(svc)
    assert st["state"] == "error" and st["error"] == "need-new-gen" and st["restarts"] == 1
    assert pipe.stops == 1
    for dt in (1, 60, 3600):
        clock.advance(dt)
        svc.tick()
        svc.handle("status", {})
    pipe.emit("first-frame")
    svc.tick()
    assert len(factory.pipes) == 1
    st = main_state(svc)
    assert st["state"] == "error" and st["restarts"] == 1


def test_событие_остановленного_конвейера_игнорируется(svc, factory):
    svc.handle("start", start_args(gen=1))
    old = factory.pipes[0]
    svc.handle("start", start_args(gen=2))
    new = factory.pipes[1]
    old.emit("error", message="старый сбой")
    old.emit("eos")
    old.emit("first-frame")
    svc.tick()
    st = main_state(svc)
    assert st["state"] == "starting" and st["error"] == "" and st["restarts"] == 0 and st["gen"] == 2
    assert new.stops == 0 and len(factory.pipes) == 2
    new.emit("first-frame")
    svc.tick()
    assert main_state(svc)["state"] == "streaming"


# --- 7. stop -------------------------------------------------------------------------------------------------

def test_stop_останавливает_и_идемпотентен(svc, factory):
    args = start_args(gen=1)
    svc.handle("start", args)
    pipe = factory.pipes[0]
    r = svc.handle("stop", {"stream": "main", "reason": "operator"})
    assert r == {"stream": "main", "state": "idle"}
    assert pipe.stops == 1
    assert main_state(svc)["state"] == "idle"
    assert svc.handle("stop", {"stream": "main"}) == {"stream": "main", "state": "idle"}
    assert pipe.stops == 1
    assert svc._act == {}
    expect("stale", svc.handle, "start", args)


def test_stop_неизвестный_поток_unknown_stream(svc):
    expect("unknown-stream", svc.handle, "stop", {"stream": "other"})


@pytest.mark.parametrize("reason", ["перезапуск", "x" * 65, "tab\there"])
def test_stop_плохая_причина_bad_request_и_поток_работает(svc, factory, reason):
    svc.handle("start", start_args())
    expect("bad-request", svc.handle, "stop", {"stream": "main", "reason": reason})
    assert factory.pipes[0].stops == 0
    assert main_state(svc)["state"] == "starting"


def test_stop_причина_64_символа_принимается(svc, factory):
    svc.handle("start", start_args())
    assert svc.handle("stop", {"stream": "main", "reason": "x" * 64}) == {"stream": "main", "state": "idle"}


# --- 8. select-source ----------------------------------------------------------------------------------------

def test_select_source_останавливает_и_требует_новое_поколение(svc, factory):
    args = start_args(gen=1)
    svc.handle("start", args)
    r = svc.handle("select-source", {"stream": "main", "source": {"source": "synthetic"}})
    assert r == {"stream": "main", "state": "idle", "need_new_gen": True}
    assert factory.pipes[0].stops == 1
    assert main_state(svc)["state"] == "idle"
    expect("stale", svc.handle, "start", args)
    r2 = svc.handle("start", start_args(gen=2))
    assert r2["gen"] == 2 and r2["state"] == "starting"


@pytest.mark.parametrize("bad", [
    {"stream": "main"},
    {"stream": "main", "source": "synthetic"},
    {"stream": "main", "source": {"source": "synthetic"}, "extra": 1},
])
def test_select_source_плохие_аргументы_bad_request(svc, bad):
    expect("bad-request", svc.handle, "select-source", bad)


def test_select_source_неизвестный_поток(svc):
    expect("unknown-stream", svc.handle, "select-source", {"stream": "other", "source": {"source": "synthetic"}})


# --- 9. keyframe ---------------------------------------------------------------------------------------------

def test_keyframe_в_streaming_запрошен(svc, factory):
    svc.handle("start", start_args())
    factory.pipes[0].emit("first-frame")
    svc.tick()
    r = svc.handle("keyframe", {"stream": "main"})
    assert r == {"stream": "main", "requested": True, "keyframe_unsupported": False}
    assert factory.pipes[0].keyframes == 1


def test_keyframe_в_starting_busy(svc):
    svc.handle("start", start_args())
    expect("busy", svc.handle, "keyframe", {"stream": "main"})


def test_keyframe_после_stop_pipeline(svc):
    svc.handle("start", start_args())
    svc.handle("stop", {"stream": "main"})
    expect("pipeline", svc.handle, "keyframe", {"stream": "main"})


def test_keyframe_неизвестный_поток(svc):
    expect("unknown-stream", svc.handle, "keyframe", {"stream": "other"})


def test_keyframe_сквозной_h264_помечается_unsupported(tmp_path, clock, wall, factory):
    svc = build(tmp_path, clock, wall, factory=factory, opener=make_opener(passthrough=True))
    r = svc.handle("start", start_args())
    assert r["passthrough"] is True
    assert main_state(svc)["rtcp"]["keyframe_unsupported"] is True
    factory.pipes[0].emit("first-frame")
    svc.tick()
    k = svc.handle("keyframe", {"stream": "main"})
    assert k["keyframe_unsupported"] is True
    assert svc.state.snapshot()["streams"]["main"]["rtcp"]["keyframe_unsupported"] is True


# --- 10. status, catalog, неизвестная команда ----------------------------------------------------------------

def test_status_совпадает_с_состоянием(svc, factory):
    svc.handle("start", start_args())
    factory.pipes[0].emit("first-frame")
    svc.tick()
    st = svc.handle("status", {})
    assert st == svc.state.snapshot()
    assert st["v"] == 1 and st["role"] == "tx"
    assert st["streams"]["main"]["state"] == "streaming"


def test_status_лишние_аргументы_bad_request(svc):
    expect("bad-request", svc.handle, "status", {"x": 1})


def test_catalog_unsupported(svc):
    expect("unsupported", svc.handle, "catalog", {})


def test_неизвестная_команда_unknown_cmd(svc):
    expect("unknown-cmd", svc.handle, "frobnicate", {})


# --- 11. tick: статистика не чаще раза в секунду -------------------------------------------------------------

def test_tick_подтягивает_статистику_не_чаще_раза_в_секунду(svc, factory, clock):
    svc.handle("start", start_args())
    pipe = factory.pipes[0]
    clock.advance(1.25)
    svc.tick()
    st = main_state(svc)
    assert st["rtp"]["packets"] == 5
    assert st["rtcp"]["pli_received"] == 2
    assert st["rtcp"]["keyframes_requested"] == 1
    assert st["bitrate_kbps"] == 1234.0
    pipe._stats["packets"] = 9
    clock.advance(0.5)
    svc.tick()
    assert main_state(svc)["rtp"]["packets"] == 5
    clock.advance(0.5)
    svc.tick()
    assert main_state(svc)["rtp"]["packets"] == 9


# --- 12. канарейка: ключ не попадает в состояние, журнал, ответы и repr ----------------------------------------

def _canary(cipher="aes-128-gcm"):
    raw = (CANARY_PREFIX + secrets.token_hex(3).encode()).ljust(KEY_LEN[cipher], b"\x00")
    return raw, base64.b64encode(raw).decode("ascii")


def test_канарейка_ключа_нигде_не_встречается(svc, factory, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    raw, key_b64 = _canary()
    raw2, key_b64_2 = _canary()
    seen = []

    def run(fn, *a):
        try:
            r = fn(*a)
        except CtlError as e:
            seen.append(str(e))
            seen.append(e.detail)
            seen.append(repr(e))
            return None
        seen.append(json.dumps(r, ensure_ascii=False))
        return r

    run(svc.handle, "start", start_args(key=raw))
    pipe = factory.pipes[0]
    pipe.emit("first-frame")
    svc.tick()
    run(svc.handle, "status", {})
    run(svc.handle, "keyframe", {"stream": "main"})
    pipe.emit("error", message="сбой конвейера")
    svc.tick()
    svc.state.write()
    run(svc.handle, "start", start_args(gen=1, key=raw))          # stale: ключ не должен всплыть в ответе
    run(svc.handle, "start", start_args(gen=2, key=raw2))         # новое поколение с новым ключом
    run(svc.handle, "select-source", {"stream": "main", "source": {"source": "synthetic"}})
    run(svc.handle, "stop", {"stream": "main", "reason": "operator"})
    bad_cipher = start_args(gen=3, key=raw)
    bad_cipher["srtp"]["cipher"] = "aes-256-gcm"
    run(svc.handle, "start", bad_cipher)
    run(svc.handle, "start", start_args(gen=4, key=raw[:-5]))     # не та длина
    bad_b64 = start_args(gen=5)
    bad_b64["srtp"]["key_b64"] = key_b64 + "!"                    # не base64
    run(svc.handle, "start", bad_b64)

    blob = "\n".join(seen)
    blob += json.dumps(svc.state.snapshot(), ensure_ascii=False)
    blob += (tmp_path / "state.json").read_text(encoding="utf-8")
    blob += caplog.text
    blob += repr(factory.pipes[0].cfg) + repr(factory.pipes[1].cfg)
    for needle in (CANARY_PREFIX.decode("ascii"), key_b64, key_b64_2):
        assert needle not in blob


# --- 13. shutdown_streams -------------------------------------------------------------------------------------

def test_shutdown_streams_останавливает_все_потоки(svc, factory):
    svc.handle("start", start_args(stream="main"))
    svc.handle("start", start_args(stream="aux"))
    factory.pipes[0].emit("first-frame")
    svc.tick()
    svc.shutdown_streams()
    assert len(factory.pipes) == 2
    assert all(p.stops == 1 for p in factory.pipes)
    snap = svc.state.snapshot()
    assert snap["streams"]["main"]["state"] == "idle"
    assert snap["streams"]["aux"]["state"] == "idle"
    assert validate_state(snap) == []
    assert svc._act == {}


# --- 14. status_line -----------------------------------------------------------------------------------------

def test_status_line_начинается_с_tx(svc, factory):
    line = svc.status_line()
    assert line.startswith("tx ") and "main=idle" in line
    svc.handle("start", start_args())
    assert svc.status_line().startswith("tx ") and "main=starting" in svc.status_line()
