# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""state.json видеослужбы (ADR-0016, контракт §2 и §4): схема v1, атомарная запись, троттлинг, кольцо переходов,
запрет ключей и проверка схемы `validate_state`.

Фейковые часы передаются через параметры `clock=` и `mono=` конструктора StateWriter, поэтому тесты не зависят
от реального времени (кроме теста атомарности, где реальные потоки и реальная файловая система).
"""
import copy
import itertools
import json
import logging
import os
import stat
import threading

import pytest

from sheregesh.videosvc import state as ST


def _writer(path, role="tx", *, clock=None, mono=None):
    """StateWriter с фиксированными часами по умолчанию: без них троттлинг update() не пишет повторно."""
    return ST.StateWriter(str(path), role, "1.0",
                          clock=clock or (lambda: 1793000000.0),
                          mono=mono or (lambda: 0.0))


def _read(path):
    return json.loads(path.read_bytes())


def _good_doc(tmp_path):
    """Корректный документ с непустыми значениями всех полей потока (для проверки validate_state)."""
    w = _writer(tmp_path / "good.json")
    w.transition("main", "streaming", "first frame")
    w.update("main", session="ab" * 16, gen=3, ssrc=123456789, peer="192.168.1.92", frames=3600,
             fps=30.0, bitrate_kbps=6000, last_frame_age_s=0.03, restarts=1, error="",
             source={"kind": "synthetic"}, nft_dropped=0,
             rtp={"packets": 10, "lost": 1, "jitter_ms": 0.5, "reordered": 0},
             srtp={"auth_fail": 0, "replay_fail": 0, "unknown_ssrc": 0},
             rtcp={"pli_sent": 1, "pli_received": 0, "keyframes_requested": 0, "keyframe_unsupported": False},
             latency_ms={"p50": 12.5, "p95": 30.0, "max": 41.0, "samples": 100})
    return w.snapshot()


# --- 1. значения по умолчанию и схема файла ---------------------------------------------------------------------

def test_file_has_schema_v1_defaults(tmp_path):
    path = tmp_path / "state.json"
    w = ST.StateWriter(str(path), "tx", "1.0")
    w.update("main", frames=1)
    w.write()

    doc = _read(path)
    assert ST.validate_state(doc) == []
    assert doc["v"] == 1
    assert doc["role"] == "tx"
    assert doc["version"] == "1.0"
    assert isinstance(doc["time"], float)
    assert set(doc["streams"]) == {"main"}

    main = doc["streams"]["main"]
    assert isinstance(main["since_s"], float) and main["since_s"] >= 0
    expected = {
        "state": "idle", "transport": "srtp", "session": "", "gen": 0, "ssrc": 0, "peer": "",
        "since_s": main["since_s"], "frames": 1, "fps": None, "bitrate_kbps": 0,       # tx: fps null (v11)
        "rtp": {"packets": None, "lost": None, "jitter_ms": None, "reordered": None},   # контракт v9: не измерено
        "srtp": {"auth_fail": None, "replay_fail": None, "unknown_ssrc": 0},
        "rtcp": {"pli_sent": 0, "pli_received": 0, "keyframes_requested": 0, "keyframe_unsupported": False},
        "latency_ms": {"p50": None, "p95": None, "max": None, "samples": 0},
        "last_frame_age_s": None, "restarts": 0, "error": "", "source": {}, "nft_dropped": None,
        "transitions": [],
    }
    assert main == expected


# --- 2. права файла и отсутствие временных файлов ---------------------------------------------------------------

def test_file_mode_is_0644_even_with_restrictive_umask(tmp_path):
    path = tmp_path / "state.json"
    old = os.umask(0o077)
    try:
        w = _writer(path)
        w.update("main", frames=1)
        w.write()
    finally:
        os.umask(old)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    assert os.listdir(tmp_path) == ["state.json"]   # ни *.tmp, ни иных остатков


# --- 3. update: слияние вложенных полей, замена прочих, отказы --------------------------------------------------

def test_update_merges_nested_subfields(tmp_path):
    w = _writer(tmp_path / "state.json")
    w.update("main", rtp={"lost": 3})
    w.update("main", rtp={"packets": 5})
    rtp = w.snapshot()["streams"]["main"]["rtp"]
    assert rtp == {"packets": 5, "lost": 3, "jitter_ms": None, "reordered": None}   # не обновлённые — не 0


def test_update_merges_all_nested_groups(tmp_path):
    w = _writer(tmp_path / "state.json")
    w.update("main", srtp={"auth_fail": 2}, rtcp={"pli_sent": 4}, latency_ms={"p50": 9.0})
    w.update("main", srtp={"replay_fail": 1}, rtcp={"keyframe_unsupported": True}, latency_ms={"samples": 7})
    s = w.snapshot()["streams"]["main"]
    assert s["srtp"] == {"auth_fail": 2, "replay_fail": 1, "unknown_ssrc": 0}
    assert s["rtcp"] == {"pli_sent": 4, "pli_received": 0, "keyframes_requested": 0, "keyframe_unsupported": True}
    assert s["latency_ms"] == {"p50": 9.0, "p95": None, "max": None, "samples": 7}


def test_update_replaces_plain_fields(tmp_path):
    w = _writer(tmp_path / "state.json")
    w.update("main", source={"a": 1}, peer="192.168.1.1", frames=5)
    w.update("main", source={"b": 2}, peer="192.168.1.2")
    s = w.snapshot()["streams"]["main"]
    assert s["source"] == {"b": 2}          # не слияние, а замена
    assert s["peer"] == "192.168.1.2"
    assert s["frames"] == 5                 # не затронутое поле сохраняется


@pytest.mark.parametrize("field", ["state", "since_s", "transitions", "bogus"])
def test_update_rejects_unknown_or_protected_fields(tmp_path, field):
    w = _writer(tmp_path / "state.json")
    w.update("main", frames=1)
    with pytest.raises(ValueError):
        w.update("main", frames=2, **{field: "x"})
    # отказ происходит до применения: frames из того же вызова не применился
    assert w.snapshot()["streams"]["main"]["frames"] == 1


@pytest.mark.parametrize("name", ["Main", "", "a_b", "x" * 17, "главный"])
def test_update_rejects_bad_stream_name(tmp_path, name):
    w = _writer(tmp_path / "state.json")
    with pytest.raises(ValueError):
        w.update(name, frames=1)
    assert w.snapshot()["streams"] == {}


# --- 4. запрет ключей ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"key": "x"},
    {"key_b64": "AAAA"},
    {"srtp": {"key_b64": "AAAA"}},
    {"source": {"srtp_key": "x"}},
    {"source": {"a": {"key": 1}}},
    {"source": {"blob": b"\x00\x01"}},
    {"peer": b"raw"},
])
def test_key_material_is_refused_and_state_unchanged(tmp_path, kwargs):
    w = _writer(tmp_path / "state.json")
    w.update("main", frames=1, peer="192.168.1.92")
    before = w.snapshot()
    with pytest.raises(ValueError):
        w.update("main", frames=99, **kwargs)
    assert w.snapshot() == before


def test_keyframe_fields_are_allowed(tmp_path):
    w = _writer(tmp_path / "state.json")
    w.update("main", rtcp={"keyframes_requested": 2, "keyframe_unsupported": True})
    rtcp = w.snapshot()["streams"]["main"]["rtcp"]
    assert rtcp["keyframes_requested"] == 2
    assert rtcp["keyframe_unsupported"] is True


def test_validate_finds_key_material_in_document(tmp_path):
    doc = _good_doc(tmp_path)
    doc["streams"]["main"]["srtp"]["key_b64"] = "AAAA"
    doc["streams"]["main"]["source"]["key"] = "x"
    errs = ST.validate_state(doc)
    assert any(e.startswith("$.streams.main.srtp.key_b64") for e in errs), errs
    assert any(e.startswith("$.streams.main.source.key") for e in errs), errs


# --- 5. transition: кольцо, формат, запись, since_s ------------------------------------------------------------

def test_transition_ring_keeps_last_20_in_order(tmp_path):
    path = tmp_path / "state.json"
    now = [0.0]
    w = _writer(path, clock=lambda: now[0], mono=lambda: 0.0)
    for i in range(30):
        now[0] = 1000.0 + i
        w.transition("main", ST.STATES[i % len(ST.STATES)], f"r{i}")

    expected = [[1000.0 + i, ST.STATES[i % len(ST.STATES)], f"r{i}"] for i in range(10, 30)]
    assert w.snapshot()["streams"]["main"]["transitions"] == expected
    assert _read(path)["streams"]["main"]["transitions"] == expected


def test_transition_rejects_unknown_state(tmp_path):
    w = _writer(tmp_path / "state.json")
    with pytest.raises(ValueError):
        w.transition("main", "running", "bad")


def test_transition_writes_file_right_after_previous_write(tmp_path):
    """mono не меняется: update() уже записал, transition() всё равно обязан записать новое состояние."""
    path = tmp_path / "state.json"
    w = _writer(path, clock=lambda: 1.0, mono=lambda: 0.0)
    w.update("main", frames=1)
    assert _read(path)["streams"]["main"]["state"] == "idle"
    w.transition("main", "streaming", "first frame")
    doc = _read(path)
    assert doc["streams"]["main"]["state"] == "streaming"
    assert doc["streams"]["main"]["transitions"] == [[1.0, "streaming", "first frame"]]
    assert ST.validate_state(doc) == []


def test_since_s_resets_on_state_change_and_grows_with_mono(tmp_path):
    m = [100.0]
    w = _writer(tmp_path / "state.json", clock=lambda: 1.0, mono=lambda: m[0])
    w.update("main", frames=1)                 # поток создан при mono=100
    m[0] = 101.5
    w.transition("main", "starting", "start")  # смена состояния сбрасывает отсчёт
    assert w.snapshot()["streams"]["main"]["since_s"] == 0.0
    m[0] = 103.0
    assert w.snapshot()["streams"]["main"]["since_s"] == 1.5
    m[0] = 104.0
    w.transition("main", "streaming", "first frame")
    assert w.snapshot()["streams"]["main"]["since_s"] == 0.0
    m[0] = 105.0
    assert w.snapshot()["streams"]["main"]["since_s"] == 1.0
    w.transition("main", "streaming", "again")  # то же состояние: отсчёт не сбрасывается
    assert w.snapshot()["streams"]["main"]["since_s"] == 1.0


# --- 6. троттлинг ----------------------------------------------------------------------------------------------

def test_update_is_throttled_to_one_write_per_0_2s(tmp_path):
    path = tmp_path / "state.json"
    m = [10.0]
    w = _writer(path, clock=lambda: 1.0, mono=lambda: m[0])

    w.update("main", frames=1)                 # первый update пишет
    assert _read(path)["streams"]["main"]["frames"] == 1

    w.update("main", frames=2)                 # сразу после — не пишет
    assert _read(path)["streams"]["main"]["frames"] == 1

    w.write()                                  # write() пишет всегда, троттлинга не знает
    assert _read(path)["streams"]["main"]["frames"] == 2

    m[0] = 10.25
    w.update("main", frames=3)                 # прошло 0.25 с — пишет
    assert _read(path)["streams"]["main"]["frames"] == 3


def test_write_ignores_throttle(tmp_path):
    path = tmp_path / "state.json"
    w = _writer(path, clock=lambda: 1.0, mono=lambda: 10.0)
    w.update("main", frames=1)
    w.update("main", frames=2)
    assert _read(path)["streams"]["main"]["frames"] == 1
    w.write()
    assert _read(path)["streams"]["main"]["frames"] == 2


# --- 7. атомарность: читатель никогда не видит частичный файл --------------------------------------------------

def _source_for(i):
    """Чередуем мелкие и крупные (50 КБ) значения; source всегда согласован с frames этой же записи."""
    return {"i": i, "blob": "p" * 50_000 if i % 4 == 0 else "s"}


def test_concurrent_reader_never_sees_partial_file(tmp_path):
    path = tmp_path / "state.json"
    ticks = itertools.count()
    # каждый вызов mono даёт +1: троттлинг не мешает, каждый update() реально пишет файл
    w = ST.StateWriter(str(path), "rx", "1.0", mono=lambda: float(next(ticks)))
    w.update("main", frames=0, source=_source_for(0))
    w.write()                                  # файл с потоком main есть до старта читателя

    done = threading.Event()
    writer_errors = []

    def produce():
        try:
            for i in range(1, 1001):
                w.update("main", frames=i, source=_source_for(i))
                w.write()                      # 2000 операций update/write подряд
        except BaseException as e:             # noqa: BLE001 — передаём ошибку в основной поток
            writer_errors.append(repr(e))
        finally:
            done.set()

    t = threading.Thread(target=produce)
    t.start()

    problems = []
    last = -1
    reads = 0
    while not done.is_set() and not problems:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            problems.append("файл пропал после первой записи")
            break
        if not raw:
            problems.append("пустой файл")
            break
        try:
            doc = json.loads(raw)
        except ValueError as e:
            problems.append(f"частичный JSON: {e}")
            break
        main = doc["streams"]["main"]
        if main["frames"] < last:
            problems.append(f"frames убыл: {last} -> {main['frames']}")
            break
        if main["source"]["i"] != main["frames"]:
            problems.append("source не согласован с frames в одном документе")
            break
        last = main["frames"]
        reads += 1
    t.join()

    assert writer_errors == []
    assert problems == []
    assert reads > 0
    final = _read(path)
    assert final["streams"]["main"]["frames"] == 1000
    assert ST.validate_state(final) == []


# --- 8. ошибка записи --------------------------------------------------------------------------------------------

def test_write_to_missing_directory_raises_oserror(tmp_path):
    w = _writer(tmp_path / "no_such_dir" / "state.json")
    with pytest.raises(OSError):
        w.write()
    assert list(tmp_path.glob("*.tmp")) == []


def test_update_logs_write_error_and_does_not_raise(tmp_path, caplog):
    w = _writer(tmp_path / "no_such_dir" / "state.json")
    with caplog.at_level(logging.WARNING, logger="sheregesh.videosvc.state"):
        w.update("main", frames=1)             # не бросает
    assert "запись не удалась" in caplog.text
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_replace_leaves_no_tmp_file(tmp_path):
    target = tmp_path / "statedir"
    target.mkdir()                             # rename(файл → каталог) невозможен
    w = _writer(target)
    with pytest.raises(OSError):
        w.write()
    w.update("main", frames=1)                 # update глотает ошибку
    assert list(tmp_path.glob("*.tmp")) == []
    assert target.is_dir()


# --- 9. validate_state -------------------------------------------------------------------------------------------

def test_validate_accepts_correct_document(tmp_path):
    doc = _good_doc(tmp_path)
    assert ST.validate_state(doc) == []


@pytest.mark.parametrize("state", ["idle", "starting", "streaming", "degraded", "recovering", "error", "stuck"])
def test_validate_accepts_every_contract_state(tmp_path, state):
    assert ST.STATES == ("idle", "starting", "streaming", "degraded", "recovering", "error", "stuck")
    doc = _good_doc(tmp_path)
    doc["streams"]["main"]["state"] = state
    assert ST.validate_state(doc) == []


def test_validate_accepts_empty_session_and_null_optionals(tmp_path):
    doc = _good_doc(tmp_path)
    doc["streams"]["main"]["session"] = ""
    doc["streams"]["main"]["latency_ms"]["p50"] = None
    doc["streams"]["main"]["last_frame_age_s"] = None
    assert ST.validate_state(doc) == []


def _mut_del(path_keys):
    def f(doc):
        d = doc
        for k in path_keys[:-1]:
            d = d[k]
        del d[path_keys[-1]]
    return f


def _mut_set(path_keys, value):
    def f(doc):
        d = doc
        for k in path_keys[:-1]:
            d = d[k]
        d[path_keys[-1]] = value
    return f


def _mut_top_extra(doc):
    doc["extra"] = 1


def _mut_rename_stream(doc):
    doc["streams"]["MAIN"] = doc["streams"].pop("main")


def _mut_stream_extra(doc):
    doc["streams"]["main"]["zzz"] = 1


def _mut_transitions_21(doc):
    doc["streams"]["main"]["transitions"] = [[1.0, "streaming", "r"]] * 21


def _mut_rtp_extra(doc):
    doc["streams"]["main"]["rtp"]["extra"] = 1


# (id, мутация, подстрока ожидаемой ошибки)
VIOLATIONS = [
    ("no_v", _mut_del(["v"]), "нет поля v"),
    ("v_is_2", _mut_set(["v"], 2), "v должно быть 1"),
    ("role_bad", _mut_set(["role"], "xx"), "role должна быть tx или rx"),
    ("top_extra", _mut_top_extra, "лишние поля"),
    ("streams_not_object", _mut_set(["streams"], ["main"]), "streams не объект"),
    ("stream_name_bad", _mut_rename_stream, "неверное имя потока"),
    ("stream_missing_field", _mut_del(["streams", "main", "fps"]), "нет поля fps"),
    ("stream_extra_field", _mut_stream_extra, "лишнее поле zzz"),
    ("state_unknown", _mut_set(["streams", "main", "state"], "running"), "streams.main.state: неверное значение"),
    ("session_not_hex32", _mut_set(["streams", "main", "session"], "XYZ"), "streams.main.session: неверное значение"),
    ("ssrc_negative", _mut_set(["streams", "main", "ssrc"], -1), "streams.main.ssrc: неверное значение"),
    ("bool_instead_of_int", _mut_set(["streams", "main", "frames"], True), "streams.main.frames: неверное значение"),
    ("latency_p50_string", _mut_set(["streams", "main", "latency_ms", "p50"], "12"),
     "streams.main.latency_ms.p50: неверное значение"),
    ("rtp_extra_subfield", _mut_rtp_extra, "streams.main.rtp: лишнее поле extra"),
    ("transitions_over_ring", _mut_transitions_21, "больше 20 записей"),
    ("transition_bad_shape", _mut_set(["streams", "main", "transitions"], [[1.0, "streaming"]]),
     "streams.main.transitions[0]: неверная запись"),
    ("last_frame_age_string", _mut_set(["streams", "main", "last_frame_age_s"], "soon"),
     "streams.main.last_frame_age_s: неверное значение"),
]


@pytest.mark.parametrize("name, mutate, expected", VIOLATIONS, ids=[v[0] for v in VIOLATIONS])
def test_validate_detects_violation(tmp_path, name, mutate, expected):
    doc = copy.deepcopy(_good_doc(tmp_path))
    mutate(doc)
    errs = ST.validate_state(doc)
    assert any(expected in e for e in errs), errs


def test_validate_rejects_non_object_root():
    assert ST.validate_state([]) == ["корень: не объект"]
    assert ST.validate_state(None) == ["корень: не объект"]


# --- 10. snapshot возвращает глубокую копию ----------------------------------------------------------------------

def test_snapshot_is_deep_copy(tmp_path):
    w = _writer(tmp_path / "state.json")
    w.transition("main", "streaming", "first frame")
    w.update("main", frames=7, source={"k": {"n": 1}}, rtp={"packets": 3})
    before = w.snapshot()

    snap = w.snapshot()
    s = snap["streams"]["main"]
    s["frames"] = 999
    s["rtp"]["packets"] = 999
    s["source"]["k"]["n"] = 999
    s["transitions"].append([0.0, "idle", "tampered"])
    snap["streams"]["ghost"] = {}
    snap["role"] = "rx"

    assert w.snapshot() == before
    assert w.snapshot()["streams"]["main"]["frames"] == 7


@pytest.mark.parametrize("fields", [
    {"frames": -1}, {"transport": "udp"}, {"ssrc": 2**40}, {"rtp": {"packets": "x"}},
    {"rtp": {"unknown": 1}}, {"fps": float("nan")}, {"source": {"o": object()}},
])
def test_update_rejects_bad_types_and_values(tmp_path, fields):
    """update не пускает в состояние значения, нарушающие схему (и не ломает последующую запись)."""
    w = ST.StateWriter(tmp_path / "state.json", "tx", "1")
    with pytest.raises(ValueError):
        w.update("main", **fields)
    w.write()
    assert ST.validate_state(json.loads((tmp_path / "state.json").read_text())) == []


def test_unmeasured_counters_are_null_and_measured_zero_stays_zero(tmp_path):
    """Контракт v9, §4: неизмеряемые счётчики — null, не 0; null принимается схемой v1 у этих полей и только у них;
    измеренный ноль остаётся нулём, а новое значение поверх null — числом."""
    from sheregesh.videosvc.state import validate_state
    w = _writer(tmp_path / "state.json", "rx")
    w.update("main", frames=0)
    s = w.snapshot()["streams"]["main"]
    assert s["rtp"] == {"packets": None, "lost": None, "jitter_ms": None, "reordered": None}
    assert s["srtp"] == {"auth_fail": None, "replay_fail": None, "unknown_ssrc": 0}
    assert validate_state(w.snapshot()) == []
    w.update("main", rtp={"packets": 0, "lost": 0, "jitter_ms": 0.0})
    s = w.snapshot()["streams"]["main"]
    assert s["rtp"] == {"packets": 0, "lost": 0, "jitter_ms": 0.0, "reordered": None}
    w.update("main", srtp={"auth_fail": 0})
    assert w.snapshot()["streams"]["main"]["srtp"]["auth_fail"] == 0
    with pytest.raises(ValueError):
        w.update("main", srtp={"unknown_ssrc": None})                # счётчик службы: всегда число
    doc = w.snapshot()
    doc["streams"]["main"]["rtcp"]["pli_sent"] = None
    assert validate_state(doc)
    old = w.snapshot()                                               # состояние прежней службы: нули
    old["streams"]["main"]["rtp"]["reordered"] = 0
    old["streams"]["main"]["srtp"].update(auth_fail=0, replay_fail=0)
    assert validate_state(old) == []


def test_idle_tx_stream_frames_unmeasured(tmp_path):
    """Контракт v11: поток tx и в idle — frames/fps null; rx — измеренный ноль."""
    from sheregesh.videosvc.state import validate_state
    tx, rx = _writer(tmp_path / "tx.json", "tx"), _writer(tmp_path / "rx.json", "rx")
    tx.update("main", error="")
    rx.update("main", error="")
    t, r = tx.snapshot(), rx.snapshot()
    assert (t["streams"]["main"]["frames"], t["streams"]["main"]["fps"]) == (None, None)
    assert (r["streams"]["main"]["frames"], r["streams"]["main"]["fps"]) == (0, 0.0)
    assert validate_state(t) == [] and validate_state(r) == []
