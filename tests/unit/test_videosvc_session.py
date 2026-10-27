# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты сеанса видеослужбы (videosvc/session.py, ADR-0016, контракт §5–§6): без транспорта и плат."""
import base64
import copy
import json
import logging
import re
import secrets

import pytest

from sheregesh.videosvc import session as S

GCM = "aes-128-gcm"
ICM = "aes-128-icm"
_HEX32 = re.compile(r"[0-9a-f]{32}")


def _other_session(session: str) -> str:
    """Валидный 32-hex session, отличный от данного."""
    return "0" * 32 if session != "0" * 32 else "f" * 32


def _begun(stream: str = "main", cipher: str = GCM):
    tx = S.TxSessions()
    off = tx.begin(stream, cipher)
    return tx, off


def _tx_video_key(stream: str = "main", cipher: str = GCM, tx=None):
    """Блок начинает поколение; возвращает (tx, offer, body video-key)."""
    if tx is None:
        tx = S.TxSessions()
    off = tx.begin(stream, cipher)
    body = S.video_key_body(off.stream, off.session, off.gen, off.ssrc, off.cipher, off.key)
    return tx, off, body


# --- 1. генерация ----------------------------------------------------------------------------------------------

def test_new_session_is_32_hex():
    """new_session: 32 строчных hex-символа (128 бит), значения не повторяются."""
    values = [S.new_session() for _ in range(100)]
    assert all(_HEX32.fullmatch(v) for v in values)
    assert len(set(values)) == len(values)


@pytest.mark.parametrize("cipher, length", [(GCM, 28), (ICM, 30)])
def test_new_key_length_by_cipher(cipher, length):
    """new_key: длина с солью 28 байт для GCM и 30 для ICM."""
    key = S.new_key(cipher)
    assert isinstance(key, bytes)
    assert len(key) == length


def test_new_key_unknown_cipher_raises():
    """new_key: неизвестный шифр — ValueError."""
    with pytest.raises(ValueError):
        S.new_key("aes-256-xyz")


def test_new_ssrc_in_range_real_random():
    """new_ssrc без подмены генератора: значения в 1..2^32-1."""
    values = [S.new_ssrc() for _ in range(2000)]
    assert all(1 <= v <= 2**32 - 1 for v in values)


def test_new_ssrc_skips_prev_and_zero(monkeypatch):
    """new_ssrc: сперва prev, потом 0 отбрасываются; берётся следующее значение."""
    prev = 0xDEADBEEF
    seq = iter([prev, 0, 0x12345678])
    calls = []

    def fake_randbits(n):
        calls.append(n)
        return next(seq)

    monkeypatch.setattr(S.secrets, "randbits", fake_randbits)
    assert S.new_ssrc(prev) == 0x12345678
    assert calls == [32, 32, 32]


# --- 2. TxSessions.begin ---------------------------------------------------------------------------------------

def test_begin_thousand_generations_unique_pairs_keys_ssrc():
    """1000 begin на одном потоке: gen строго 1..1000, без повторов (key, ssrc), key и ssrc."""
    tx = S.TxSessions()
    offers = [tx.begin("main", GCM) for _ in range(1000)]
    assert [o.gen for o in offers] == list(range(1, 1001))
    assert len({(o.key, o.ssrc) for o in offers}) == 1000
    assert len({o.key for o in offers}) == 1000
    assert len({o.ssrc for o in offers}) == 1000
    assert all(len(o.key) == 28 for o in offers)
    assert all(o.session == tx.session for o in offers)


def test_begin_gen_counter_is_per_stream():
    """Каждый поток (stream) ведёт свой счётчик gen."""
    tx = S.TxSessions()
    assert tx.begin("main", GCM).gen == 1
    assert tx.begin("main", GCM).gen == 2
    assert tx.begin("aux", GCM).gen == 1
    assert tx.begin("main", GCM).gen == 3


@pytest.mark.parametrize("stream", ["A", "", "x" * 17, "bad_name", 5, None])
def test_begin_bad_stream_name_raises(stream):
    """Неверное имя потока — ValueError, и счётчик не сдвигается."""
    tx = S.TxSessions()
    with pytest.raises(ValueError):
        tx.begin(stream, GCM)
    assert tx.begin("main", GCM).gen == 1


@pytest.mark.parametrize("cipher", ["aes-256-xyz", None, ""])
def test_begin_bad_cipher_raises(cipher):
    """Неизвестный шифр — ValueError, и счётчик не сдвигается."""
    tx = S.TxSessions()
    with pytest.raises(ValueError):
        tx.begin("main", cipher)
    assert tx.begin("main", GCM).gen == 1


# --- 3. ack ----------------------------------------------------------------------------------------------------

def test_ack_current_generation_ok():
    """ack текущего поколения с ok=True: True и acked(stream) становится True."""
    tx, off = _begun()
    assert tx.acked("main") is False
    assert tx.ack(S.ack_body("main", off.session, off.gen, True)) is True
    assert tx.acked("main") is True


@pytest.mark.parametrize("case", ["другой gen", "другой session", "другой stream", "ok=False", "лишнее поле"])
def test_ack_rejected_variants(case):
    """ack с другим gen / session / stream, ok=False или лишним полем — False, acked остаётся False."""
    tx, off = _begun()
    bodies = {
        "другой gen": S.ack_body("main", off.session, off.gen + 1, True),
        "другой session": S.ack_body("main", _other_session(off.session), off.gen, True),
        "другой stream": S.ack_body("aux", off.session, off.gen, True),
        "ok=False": S.ack_body("main", off.session, off.gen, False),
        "лишнее поле": {**S.ack_body("main", off.session, off.gen, True), "extra": 1},
    }
    assert tx.ack(bodies[case]) is False
    assert tx.acked("main") is False


def test_ack_old_generation_does_not_activate_new():
    """Старое ack (gen 1) после begin gen 2 не активирует gen 2."""
    tx = S.TxSessions()
    off1 = tx.begin("main", GCM)
    off2 = tx.begin("main", GCM)
    assert tx.ack(S.ack_body("main", off1.session, off1.gen, True)) is False
    assert tx.acked("main") is False
    assert tx.ack(S.ack_body("main", off2.session, off2.gen, True)) is True
    assert tx.acked("main") is True


def test_ack_before_begin_is_false():
    """ack до begin — False."""
    tx = S.TxSessions()
    assert tx.ack(S.ack_body("main", tx.session, 1, True)) is False
    assert tx.acked("main") is False


# --- 4. stop ---------------------------------------------------------------------------------------------------

def test_stop_returns_current_body_and_validates():
    """stop возвращает body video-stop с текущими (stream, session, gen) и проходит validate_stop."""
    tx, off = _begun()
    body = tx.stop("main", "user")
    assert body == {"stream": "main", "session": tx.session, "gen": off.gen, "reason": "user"}
    S.validate_stop(body)


def test_stop_twice_or_without_begin_raises():
    """Повторный stop и stop без begin — ValueError."""
    tx, _ = _begun()
    tx.stop("main", "user")
    with pytest.raises(ValueError):
        tx.stop("main", "user")
    with pytest.raises(ValueError):
        S.TxSessions().stop("main", "user")


def test_ack_after_stop_is_false():
    """После stop ack текущего поколения — False."""
    tx, off = _begun()
    tx.stop("main", "user")
    assert tx.ack(S.ack_body("main", off.session, off.gen, True)) is False
    assert tx.acked("main") is False


def test_begin_after_stop_continues_gen_counter():
    """После stop следующий begin получает gen + 1 (счётчик не сбрасывается)."""
    tx, off = _begun()
    tx.stop("main", "user")
    assert tx.begin("main", GCM).gen == off.gen + 1


# --- 5. new_control_session ------------------------------------------------------------------------------------

def test_new_control_session_resets_generation_and_acks():
    """Новый сеанс управления: новый session, gen снова с 1, ack со старым session — False."""
    tx = S.TxSessions()
    old_session = tx.session
    tx.begin("main", GCM)
    tx.begin("main", GCM)
    new_session = tx.new_control_session()
    assert new_session != old_session
    assert tx.session == new_session
    off = tx.begin("main", GCM)
    assert off.gen == 1
    assert off.session == new_session
    assert tx.ack(S.ack_body("main", old_session, 1, True)) is False
    assert tx.acked("main") is False
    assert tx.ack(S.ack_body("main", new_session, 1, True)) is True


# --- 6. RxSessions ---------------------------------------------------------------------------------------------

def test_rx_first_accept_sets_session():
    """Первый ok video-key задаёт session на пульте."""
    _, off, body = _tx_video_key()
    rx = S.RxSessions()
    assert rx.accept(body) == (True, None)
    assert rx.session == off.session


def test_rx_other_session_is_stale():
    """video-key с другим session после принятого — stale."""
    _, off, body = _tx_video_key()
    rx = S.RxSessions()
    assert rx.accept(body) == (True, None)
    foreign = S.video_key_body("main", _other_session(off.session), 2, off.ssrc, GCM,
                               secrets.token_bytes(28))
    assert rx.accept(foreign) == (False, "stale")
    assert rx.session == off.session


def test_rx_gen_not_greater_is_stale():
    """gen меньше последнего принятого — stale."""
    tx = S.TxSessions()
    off1 = tx.begin("main", GCM)
    off2 = tx.begin("main", GCM)
    body1 = S.video_key_body(off1.stream, off1.session, off1.gen, off1.ssrc, GCM, off1.key)
    body2 = S.video_key_body(off2.stream, off2.session, off2.gen, off2.ssrc, GCM, off2.key)
    rx = S.RxSessions()
    assert rx.accept(body2) == (True, None)
    assert rx.accept(body1) == (False, "stale")


def test_rx_repeat_same_video_key_is_idempotent():
    """Повтор того же video-key: (True, None) и состояние пульта не меняется."""
    _, _, body = _tx_video_key()
    rx = S.RxSessions()
    assert rx.accept(body) == (True, None)
    snapshot = copy.deepcopy(vars(rx))
    assert rx.accept(body) == (True, None)
    assert vars(rx) == snapshot


def test_rx_same_gen_other_key_is_stale():
    """Тот же gen, но другой ключ — stale."""
    _, off, body = _tx_video_key()
    other = S.video_key_body(off.stream, off.session, off.gen, off.ssrc, GCM, secrets.token_bytes(28))
    rx = S.RxSessions()
    assert rx.accept(body) == (True, None)
    assert rx.accept(other) == (False, "stale")


def test_rx_gen_plus_one_accepted():
    """gen + 1 того же session принимается."""
    tx, _, body1 = _tx_video_key()
    off2 = tx.begin("main", GCM)
    body2 = S.video_key_body(off2.stream, off2.session, off2.gen, off2.ssrc, GCM, off2.key)
    rx = S.RxSessions()
    assert rx.accept(body1) == (True, None)
    assert rx.accept(body2) == (True, None)


def test_rx_reset_allows_new_session():
    """reset() снимает session: новый сеанс управления принимается."""
    _, off1, body1 = _tx_video_key()
    rx = S.RxSessions()
    assert rx.accept(body1) == (True, None)
    rx.reset()
    assert rx.session is None
    tx2, off2, body2 = _tx_video_key()
    assert off2.session != off1.session
    assert rx.accept(body2) == (True, None)
    assert rx.session == tx2.session


@pytest.mark.parametrize("cipher", [None, "aes-256-xyz"])
def test_rx_unsupported_cipher(cipher):
    """Шифр None или неизвестный — (False, 'unsupported')."""
    body = S.video_key_body("main", "a" * 32, 1, 12345, cipher, secrets.token_bytes(28))
    rx = S.RxSessions()
    assert rx.accept(body) == (False, "unsupported")
    assert rx.session is None


def test_rx_key_wrong_length_bad_request():
    """Ключ неверной длины — (False, 'bad-request')."""
    body = S.video_key_body("main", "a" * 32, 1, 12345, GCM, secrets.token_bytes(10))
    rx = S.RxSessions()
    assert rx.accept(body) == (False, "bad-request")
    assert rx.session is None


def test_rx_key_not_base64_bad_request():
    """key_b64 не в base64 — (False, 'bad-request')."""
    _, _, body = _tx_video_key()
    body["srtp"]["key_b64"] = "@@@ не base64 @@@"
    rx = S.RxSessions()
    assert rx.accept(body) == (False, "bad-request")


def test_rx_stop_accepts_only_current_triplet():
    """video-stop принимается только для текущих (stream, session, gen); после stop повтор video-key — stale."""
    _, off, body = _tx_video_key()
    rx = S.RxSessions()
    assert rx.accept(body) == (True, None)
    s, sess, g = off.stream, off.session, off.gen
    assert rx.stop(S.stop_body(s, sess, g + 1, "user")) is False
    assert rx.stop(S.stop_body(s, _other_session(sess), g, "user")) is False
    assert rx.stop(S.stop_body("aux", sess, g, "user")) is False
    assert rx.stop(S.stop_body(s, sess, g, "")) is False
    assert rx.stop(S.stop_body(s, sess, g, "user")) is True
    assert rx.stop(S.stop_body(s, sess, g, "user")) is False
    assert rx.accept(body) == (False, "stale")


def test_rx_ack_body_passes_validate_ack():
    """ack_body строит dict, который проходит validate_ack (и для ok=True, и для ошибки)."""
    rx = S.RxSessions()
    ok = rx.ack_body("main", "a" * 32, 1, True)
    S.validate_ack(ok)
    assert ok == {"stream": "main", "session": "a" * 32, "gen": 1, "ok": True}
    err = rx.ack_body("main", "a" * 32, 1, False, "stale")
    S.validate_ack(err)
    assert err["error"] == "stale"


# --- 7. строгая схема ------------------------------------------------------------------------------------------

def _vk(cipher: str = GCM, key: bytes | None = None) -> dict:
    """Валидный body video-key (если ключ не задан — случайный нужной длины)."""
    if key is None:
        key = secrets.token_bytes(S.KEY_LEN.get(cipher, 28))
    return S.video_key_body("main", "a" * 32, 1, 12345, cipher, key)


def _rejects_vk(body, code: str = "bad-request") -> None:
    with pytest.raises(S.SchemaError) as ei:
        S.validate_video_key(body)
    assert ei.value.code == code


def test_vk_valid_baseline():
    """Базовый валидный body (GCM и ICM) проходит validate_video_key."""
    S.validate_video_key(_vk(GCM))
    S.validate_video_key(_vk(ICM))


def test_vk_extra_top_level_field():
    """Лишнее поле на верхнем уровне."""
    b = _vk()
    b["extra"] = 1
    _rejects_vk(b)


def test_vk_extra_field_in_profile():
    """Лишнее поле в profile."""
    b = _vk()
    b["profile"]["extra"] = 1
    _rejects_vk(b)


def test_vk_extra_field_in_srtp():
    """Лишнее поле в srtp."""
    b = _vk()
    b["srtp"]["extra"] = 1
    _rejects_vk(b)


@pytest.mark.parametrize("field", ["stream", "session", "gen", "transport", "ssrc", "codec",
                                   "profile", "srtp", "rtcp_port"])
def test_vk_missing_top_level_field(field):
    """Отсутствующее поле верхнего уровня."""
    b = _vk()
    del b[field]
    _rejects_vk(b)


@pytest.mark.parametrize("section, field", [("profile", "fps"), ("srtp", "key_b64"), ("srtp", "cipher")])
def test_vk_missing_nested_field(section, field):
    """Отсутствующее поле в profile или srtp."""
    b = _vk()
    del b[section][field]
    _rejects_vk(b)


def test_vk_transport_not_srtp():
    """transport отличен от srtp."""
    b = _vk()
    b["transport"] = "rtp"
    _rejects_vk(b)


@pytest.mark.parametrize("ssrc", [0, 2**32, True, "123", None])
def test_vk_ssrc_bounds_and_type(ssrc):
    """ssrc: 0, 2^32, bool, строка, None — отклоняются."""
    b = _vk()
    b["ssrc"] = ssrc
    _rejects_vk(b)


@pytest.mark.parametrize("gen", [0, True, 2**32, "1", None])
def test_vk_gen_bounds_and_type(gen):
    """gen: 0, bool, выше 2^32-1, строка — отклоняются."""
    b = _vk()
    b["gen"] = gen
    _rejects_vk(b)


@pytest.mark.parametrize("stream", ["Bad_Name", "", "x" * 17, "Main"])
def test_vk_stream_bad_name(stream):
    """Неверное имя потока (заглавные, подчёркивание, пусто, длиннее 16)."""
    b = _vk()
    b["stream"] = stream
    _rejects_vk(b)


@pytest.mark.parametrize("session", ["A" * 32, "a" * 31, "g" * 32, "a" * 33, 12])
def test_vk_session_not_32_hex(session):
    """session не 32 строчных hex-символа."""
    b = _vk()
    b["session"] = session
    _rejects_vk(b)


@pytest.mark.parametrize("codec", ["", "H264", "h-264", "x" * 17, 264])
def test_vk_codec_format(codec):
    """codec: пусто, верхний регистр, дефис, длиннее 16, не строка."""
    b = _vk()
    b["codec"] = codec
    _rejects_vk(b)


@pytest.mark.parametrize("port", [0, 70000, True, "7445"])
def test_vk_rtcp_port_bounds(port):
    """rtcp_port: 0, 70000, bool, строка — отклоняются."""
    b = _vk()
    b["rtcp_port"] = port
    _rejects_vk(b)


@pytest.mark.parametrize("field, value", [
    ("width", 0), ("width", 16385), ("width", True),
    ("height", 0), ("height", 16385),
    ("fps", 0), ("fps", 241),
    ("bitrate", 0), ("bitrate", 10_000_001),
])
def test_vk_profile_values_out_of_range(field, value):
    """Значения profile вне диапазона."""
    b = _vk()
    b["profile"][field] = value
    _rejects_vk(b)


@pytest.mark.parametrize("body", [None, [], "x", 1])
def test_vk_body_not_dict(body):
    """body, который не dict, — bad-request."""
    _rejects_vk(body)


def test_vk_profile_not_dict():
    """profile не объект — bad-request."""
    b = _vk()
    b["profile"] = "x"
    _rejects_vk(b)


@pytest.mark.parametrize("cipher", ["aes-256-xyz", None])
def test_vk_cipher_unsupported_code(cipher):
    """Неподдерживаемый шифр или null — код unsupported."""
    b = _vk()
    b["srtp"]["cipher"] = cipher
    _rejects_vk(b, "unsupported")


@pytest.mark.parametrize("key_b64", [123, None, "!!!!", "", base64.b64encode(b"x" * 27).decode()])
def test_vk_key_b64_invalid(key_b64):
    """key_b64: не строка, не base64, пусто, неверная длина ключа."""
    b = _vk()
    b["srtp"]["key_b64"] = key_b64
    _rejects_vk(b)


def test_vk_key_length_mismatch_for_icm():
    """Ключ GCM-длины (28) при шифре ICM (30) — bad-request."""
    b = _vk(ICM)
    b["srtp"]["key_b64"] = base64.b64encode(secrets.token_bytes(28)).decode("ascii")
    _rejects_vk(b)


@pytest.mark.parametrize("ok", [1, 0, "true", None])
def test_ack_ok_not_bool(ok):
    """validate_ack: ok не bool — bad-request."""
    body = S.ack_body("main", "a" * 32, 1, True)
    body["ok"] = ok
    with pytest.raises(S.SchemaError) as ei:
        S.validate_ack(body)
    assert ei.value.code == "bad-request"


def test_ack_error_with_ok_true():
    """validate_ack: error при ok=true — bad-request."""
    body = S.ack_body("main", "a" * 32, 1, True)
    body["error"] = "stale"
    with pytest.raises(S.SchemaError) as ei:
        S.validate_ack(body)
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("error", ["boom", "", None, 7])
def test_ack_unknown_error_code(error):
    """validate_ack: неизвестный код error — bad-request."""
    body = S.ack_body("main", "a" * 32, 1, False)
    body["error"] = error
    with pytest.raises(S.SchemaError) as ei:
        S.validate_ack(body)
    assert ei.value.code == "bad-request"


def test_ack_extra_field():
    """validate_ack: лишнее поле — bad-request."""
    body = S.ack_body("main", "a" * 32, 1, True)
    body["extra"] = 1
    with pytest.raises(S.SchemaError) as ei:
        S.validate_ack(body)
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("reason", ["", "причина", "x" * 65, "two\nlines", 5])
def test_stop_reason_rejected(reason):
    """validate_stop: пустой, не ASCII, длиннее 64, с управляющим символом, не строка — отклоняются."""
    body = S.stop_body("main", "a" * 32, 1, "ok")
    body["reason"] = reason
    with pytest.raises(S.SchemaError) as ei:
        S.validate_stop(body)
    assert ei.value.code == "bad-request"


def test_stop_reason_64_ascii_accepted():
    """validate_stop: 64 печатных ASCII символа — допустимо."""
    S.validate_stop(S.stop_body("main", "a" * 32, 1, "x" * 64))


# --- 8. mask ---------------------------------------------------------------------------------------------------

def test_mask_nested_structures_keep_other_fields_and_copy():
    """mask маскирует key_b64 на любом уровне (dict/list/tuple), прочие поля сохраняет, оригинал не меняет."""
    body = {
        "outer": [{"inner": {"key_b64": "SECRET1", "keep": 1}}],
        "tup": ({"key_b64": "SECRET2"}, "x", 3),
        "deep": [[{"key_b64": "SECRET3"}]],
        "key_b64": "TOP",
        "n": 5,
    }
    original = copy.deepcopy(body)
    masked = S.mask(body)
    assert masked["key_b64"] == "***"
    assert masked["outer"][0]["inner"]["key_b64"] == "***"
    assert masked["outer"][0]["inner"]["keep"] == 1
    assert masked["tup"][0]["key_b64"] == "***"
    assert list(masked["tup"][1:]) == ["x", 3]
    assert masked["deep"][0][0]["key_b64"] == "***"
    assert masked["n"] == 5
    assert body == original
    masked["outer"][0]["inner"]["keep"] = 99
    masked["outer"].append("z")
    assert body == original


# --- 9. секретность --------------------------------------------------------------------------------------------

def _canary_body():
    """Канарейка в ключе. Для ключа ljust не обрезает: канарейка длиннее 28 байт, поэтому ключ 37 байт.

    Длина намеренно неверная: так же проверяется путь «неверная длина ключа» без утечки.
    """
    canary = "SHRG-CANARY-VIDEOKEY-" + secrets.token_hex(8)
    key = canary.encode().ljust(28, b"x")
    key_b64 = base64.b64encode(key).decode("ascii")
    body = S.video_key_body("main", "a" * 32, 1, 12345, GCM, key)
    return canary, key_b64, key, body


def test_canary_not_in_mask_repr_or_json():
    """Канарейка и key_b64 не попадают в repr и json маскированного body."""
    canary, key_b64, _, body = _canary_body()
    masked = S.mask(body)
    for text in (repr(masked), json.dumps(masked)):
        assert canary not in text
        assert key_b64 not in text
    assert masked["srtp"]["key_b64"] == "***"


def test_canary_not_in_offer_repr():
    """Offer.key (bytes) не попадает в repr и str объекта Offer."""
    canary, key_b64, key, _ = _canary_body()
    offer = S.Offer("main", "a" * 32, 1, 12345, GCM, key)
    for text in (repr(offer), str(offer)):
        assert canary not in text
        assert key_b64 not in text


def test_canary_not_in_schema_error_text():
    """Текст и repr SchemaError при неверной длине ключа и неизвестном шифре не содержат ключа."""
    canary, key_b64, _, body = _canary_body()
    with pytest.raises(S.SchemaError) as ei:
        S.validate_video_key(body)
    assert ei.value.code == "bad-request"
    for text in (str(ei.value), repr(ei.value)):
        assert canary not in text
        assert key_b64 not in text

    body["srtp"]["cipher"] = "aes-256-xyz"
    with pytest.raises(S.SchemaError) as ei2:
        S.validate_video_key(body)
    assert ei2.value.code == "unsupported"
    for text in (str(ei2.value), repr(ei2.value)):
        assert canary not in text
        assert key_b64 not in text


def test_canary_not_in_accept_result():
    """RxSessions.accept возвращает только код; канарейка и ключ в результате не появляются."""
    canary, key_b64, _, body = _canary_body()
    result = S.RxSessions().accept(body)
    assert canary not in repr(result)
    assert key_b64 not in repr(result)


def test_canary_not_in_log(caplog):
    """При логировании через mask() канарейка и key_b64 не попадают в журнал."""
    canary, key_b64, _, body = _canary_body()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("t").info("%s", S.mask(body))
    assert "***" in caplog.text
    assert canary not in caplog.text
    assert key_b64 not in caplog.text


# --- stop с недопустимым reason не теряет поколение ----------------------------------------------------------------

def test_stop_invalid_reason_keeps_active_generation():
    """Ожидаемое поведение: неверный reason не должен сбрасывать активное поколение."""
    tx, _ = _begun()
    with pytest.raises(S.SchemaError):
        tx.stop("main", "")
    assert tx.current_gen("main") == 1


# --- video-fallback (контракт v5) ----------------------------------------------------------------------------

def test_fallback_matches_only_current_generation():
    tx = S.TxSessions()
    tx.new_control_session()
    first = tx.begin("main", "aes-128-gcm")
    second = tx.begin("main", "aes-128-gcm")
    assert tx.fallback_matches(S.fallback_body("main", second.session, second.gen, "no first frame"))
    assert not tx.fallback_matches(S.fallback_body("main", first.session, first.gen, "no first frame"))
    assert not tx.fallback_matches(S.fallback_body("main", "0" * 32, second.gen, "no first frame"))
    assert not tx.fallback_matches(S.fallback_body("aux", second.session, second.gen, "no first frame"))


def test_validate_fallback_schema():
    body = S.fallback_body("main", "a" * 32, 1, "rx start failed")
    S.validate_fallback(body)
    for bad in ({**body, "extra": 1}, {**body, "reason": ""}, {**body, "gen": 0}, {k: v for k, v in body.items() if k != "reason"}):
        with pytest.raises(S.SchemaError):
            S.validate_fallback(bad)


def test_renew_matches_only_current_generation():
    tx = S.TxSessions()
    tx.new_control_session()
    first = tx.begin("main", "aes-128-gcm")
    second = tx.begin("main", "aes-128-gcm")
    assert tx.renew_matches(S.renew_body("main", second.session, second.gen, "rx lost generation"))
    assert not tx.renew_matches(S.renew_body("main", first.session, first.gen, "rx lost generation"))
    with pytest.raises(S.SchemaError):
        S.validate_renew({**S.renew_body("main", second.session, second.gen, "x"), "extra": 1})

