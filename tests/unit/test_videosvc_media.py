# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты фрагментов GStreamer и проверок аргументов видеослужбы (videosvc/media.py, ADR-0016, трек D).

Без gi и без запуска GStreamer: источники — заглушки, кодеры и декодеры выбираются через переменные окружения
или подмену video.h264_elements / video.has_element.
"""
import math

import pytest

from sheregesh import video
from sheregesh import videoinput as vi
from sheregesh.videosvc import ctl as C
from sheregesh.videosvc import media as M
from sheregesh.videosvc.ctl import CtlError

CANARY = "SHRG-CANARY-VIDEOKEY-1234"
PROFILE = {"width": 640, "height": 480, "fps": 30, "bitrate": 4000}
V4L2_PAIR = (video.V4L2_ENC, "v4l2h264dec")
OPENH264_PAIR = (video.OPENH264_ENC, "avdec_h264")
ENV_VARS = (M.ENV_ENCODER, M.ENV_DECODER, M.ENV_RX_SINK)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Переменные переопределения не должны просачиваться из окружения машины в тесты."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class FakeSource:
    """Заглушка VideoSource: описание, захват и декодирование без устройства."""

    def __init__(self, pixfmt="YUYV", *, width=640, height=480, fps=30, capture=None, kind="v4l2",
                 sid="dev-1", name="Cam", available=True, missing=False, error="", present=None):
        self.mode = vi.Mode(width, height, fps, pixfmt)
        self.kind, self.id, self.name = kind, sid, name
        self.available, self.missing, self.error = available, missing, error
        self._capture = list(capture) if capture is not None else ["v4l2src", "device=/dev/video0", "!"]
        self._present = present if present is not None else (available, error)

    def capture(self):
        return list(self._capture)

    def decode(self, decoder=None):
        code = self.mode.pixfmt if self.mode else ""
        return {"MJPG": ["jpegdec", "!"],
                "H264": ["h264parse", "!", decoder or "avdec_h264", "!"]}.get(code, [])

    def present(self):
        return self._present


class Opener:
    """Подставная функция открытия источника: запоминает вызовы и возвращает заготовленный источник."""

    def __init__(self, src):
        self.src = src
        self.calls = []

    def __call__(self, spec):
        self.calls.append(spec)
        return self.src


# --- 1. экранирование токенов ---

@pytest.mark.parametrize("tok, expected", [
    ("font-desc=Sans 28", 'font-desc="Sans 28"'),
    ("is-live=true", "is-live=true"),
    ("video/x-raw,format=YUY2,width=640", "video/x-raw,format=YUY2,width=640"),
    ("!", "!"),
    ("nospace", "nospace"),
    ("location=rtsp://u:p@h:554/a?x=1&y=2", 'location="rtsp://u:p@h:554/a?x=1&y=2"'),
])
def test_quote_token_values(tok, expected):
    """Значения с пробелом и спецсимволами — в кавычках; безопасные токены и «!» не меняются."""
    assert M.quote_token(tok) == expected


def test_quote_token_escapes_quote_in_value():
    """Двойная кавычка в значении экранируется обратной косой."""
    assert M.quote_token('title=a"b') == r'title="a\"b"'


def test_quote_token_escapes_backslash_in_value():
    """Обратная косая в значении удваивается."""
    assert M.quote_token("p=C:\\x") == r'p="C:\\x"'


@pytest.mark.parametrize("tok", ["a\nb=1", "b=1\n", "x=\r", "x=\x00", "\n", 5, None])
def test_quote_token_rejects_control_chars_and_non_str(tok):
    """Перевод строки, CR, NUL и не строки — bad-request, не попадают в конвейер."""
    with pytest.raises(M.MediaError) as ei:
        M.quote_token(tok)
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("tok", ["foo bar", "a b=c"])
def test_quote_token_rejects_space_outside_property(tok):
    """Пробел в токене без вида name=value — ошибка, а не тихая склейка элементов."""
    with pytest.raises(M.MediaError) as ei:
        M.quote_token(tok)
    assert ei.value.code == "bad-request"


def test_join_tokens_joins_with_quoting():
    """join_tokens — пробелы между токенами и кавычки там, где они нужны."""
    assert M.join_tokens(["a", "font-desc=Sans 28", "!"]) == 'a font-desc="Sans 28" !'


# --- 2. фрагмент кодера ---

def test_encoder_env_substitutes_kbps_and_bps(monkeypatch):
    """Готовый фрагмент из переменной: {bitrate_kbps} и {bitrate_bps} подставляются, признак hw — False."""
    monkeypatch.setenv(M.ENV_ENCODER, "x264enc tune=zerolatency bitrate={bitrate_kbps} x={bitrate_bps}")
    frag, hw = M.encoder_fragment(4000, pair=None)
    assert frag == "x264enc tune=zerolatency bitrate=4000 x=4000000"
    assert hw is False


def test_encoder_env_newline_rejected(monkeypatch):
    """Перевод строки внутри переменной окружения — bad-request."""
    monkeypatch.setenv(M.ENV_ENCODER, "x264enc\nfoo")
    with pytest.raises(M.MediaError) as ei:
        M.encoder_fragment(4000, V4L2_PAIR)
    assert ei.value.code == "bad-request"


def test_encoder_v4l2_pair_sets_video_bitrate_and_hw():
    """V4L2: video_bitrate из профиля (кбит/с -> бит/с), аппаратный признак True, фрагмент кодера без пробелов внутри."""
    frag, hw = M.encoder_fragment(4000, V4L2_PAIR)
    assert "video_bitrate=4000000" in frag
    assert frag.startswith("v4l2h264enc ")
    assert frag.endswith("h264parse")
    assert hw is True


def test_encoder_openh264_pair_sets_bitrate_and_not_hw():
    """OpenH264: bitrate из профиля, признак hw — False."""
    frag, hw = M.encoder_fragment(4000, OPENH264_PAIR)
    assert "bitrate=4000000" in frag.split()
    assert frag.startswith("openh264enc ")
    assert hw is False


def test_encoder_without_pair_and_without_elements_is_unsupported(monkeypatch):
    """Нет переменной, нет пары, h264_elements() вернул None — MediaError unsupported."""
    monkeypatch.setattr(video, "h264_elements", lambda: None)
    with pytest.raises(M.MediaError) as ei:
        M.encoder_fragment(4000, None)
    assert ei.value.code == "unsupported"


def test_encoder_without_pair_takes_from_video_module(monkeypatch):
    """pair=None — пара берётся из video.h264_elements()."""
    monkeypatch.setattr(video, "h264_elements", lambda: OPENH264_PAIR)
    frag, hw = M.encoder_fragment(2000, None)
    assert frag.startswith("openh264enc ")
    assert hw is False


# --- 3. декодер RX ---

def test_decoder_env_has_priority(monkeypatch):
    """SHEREGESH_VIDEO_DECODER важнее пары."""
    monkeypatch.setenv(M.ENV_DECODER, "avdec_h264")
    assert M.decoder_name(V4L2_PAIR) == "avdec_h264"


@pytest.mark.parametrize("bad", ["Bad Name", "avdec-h264", "AVDEC", "avdec;rm", "x y"])
def test_decoder_env_must_be_element_name(monkeypatch, bad):
    """Имя декодера — только [a-z0-9_]+; иначе bad-request."""
    monkeypatch.setenv(M.ENV_DECODER, bad)
    with pytest.raises(M.MediaError) as ei:
        M.decoder_name(V4L2_PAIR)
    assert ei.value.code == "bad-request"


def test_decoder_blank_env_falls_back_to_pair(monkeypatch):
    """Пустая (из пробелов) переменная — как не заданная."""
    monkeypatch.setenv(M.ENV_DECODER, "   ")
    assert M.decoder_name(V4L2_PAIR) == "v4l2h264dec"


@pytest.mark.parametrize("pair, expected", [(V4L2_PAIR, "v4l2h264dec"), (OPENH264_PAIR, "avdec_h264")])
def test_decoder_from_pair(pair, expected):
    """Без переменной декодер — второй элемент пары."""
    assert M.decoder_name(pair) == expected


def test_decoder_without_pair_and_without_elements_is_unsupported(monkeypatch):
    """Нет пары и нет рабочей связки — unsupported."""
    monkeypatch.setattr(video, "h264_elements", lambda: None)
    with pytest.raises(M.MediaError) as ei:
        M.decoder_name(None)
    assert ei.value.code == "unsupported"


# --- 4. приёмник кадров RX ---

def test_rx_sink_default_unixfdsink():
    """По умолчанию unixfdsink на frames-<stream>.sock в каталоге выполнения, sync=false."""
    assert M.rx_sink_fragment("/run/sheregesh-video", "rx1") == \
        "unixfdsink socket-path=/run/sheregesh-video/frames-rx1.sock sync=false"


def test_rx_sink_path_with_space_is_quoted():
    """Путь с пробелом в каталоге — значение в кавычках."""
    assert M.rx_sink_fragment("/run/with space", "rx1") == \
        'unixfdsink socket-path="/run/with space/frames-rx1.sock" sync=false'


def test_rx_sink_env_replaces_everything(monkeypatch):
    """SHEREGESH_VIDEO_RX_SINK заменяет весь фрагмент приёмника (например fakesink)."""
    monkeypatch.setenv(M.ENV_RX_SINK, "fakesink sync=false")
    assert M.rx_sink_fragment("/run/sheregesh-video", "rx1") == "fakesink sync=false"


# --- 5. сборка фрагмента TX ---

def test_build_passthrough_h264():
    """Сквозной H.264: без кодера, заканчивается h264parse config-interval=-1 и выходными caps."""
    m = M.build_media(FakeSource("H264"), PROFILE, None)
    assert m.passthrough is True
    assert m.fragment.endswith("h264parse config-interval=-1 ! " + M.H264_OUT_CAPS)
    for enc in ("x264enc", "v4l2h264enc", "openh264enc"):
        assert enc not in m.fragment
    assert m.info["encoder"] == "passthrough"


def test_build_direct_yuyv_to_hardware_encoder():
    """YUYV на аппаратный кодер идёт без videoconvert; h264parse не удвоен перед выходными caps."""
    m = M.build_media(FakeSource("YUYV"), PROFILE, V4L2_PAIR)
    assert m.passthrough is False
    assert "videoconvert" not in m.fragment
    assert "v4l2h264enc" in m.fragment
    assert "video_bitrate=4000000" in m.fragment
    assert m.fragment.endswith("h264parse ! " + M.H264_OUT_CAPS)
    assert m.fragment.count("h264parse") == 1
    assert m.info["encoder"] == "v4l2h264enc"


def test_build_yuyv_openh264_uses_videoconvert_i420():
    """YUYV на программный кодер — videoconvert в I420 перед кодером, h264parse добавлен в конце."""
    m = M.build_media(FakeSource("YUYV"), PROFILE, OPENH264_PAIR)
    assert "videoconvert ! video/x-raw,format=I420 !" in m.fragment
    assert "openh264enc" in m.fragment
    assert "bitrate=4000000" in m.fragment
    assert m.fragment.endswith("h264parse ! " + M.H264_OUT_CAPS)
    assert m.info["encoder"] == "openh264enc"


def test_build_mjpg_on_hardware_encoder_decodes_then_converts():
    """MJPG даже на аппаратный кодер: декодирование jpegdec, затем videoconvert (MJPEG не в списке прямых)."""
    m = M.build_media(FakeSource("MJPG"), PROFILE, V4L2_PAIR)
    assert "jpegdec" in m.fragment
    assert "videoconvert ! video/x-raw,format=I420 !" in m.fragment
    assert m.fragment.index("jpegdec") < m.fragment.index("videoconvert")


def test_build_quotes_source_property_with_space():
    """Свойство источника с пробелом в значении (font-desc=Sans 28) попадает в фрагмент в кавычках."""
    src = FakeSource("YUYV", capture=["timeoverlay", "halignment=right", "font-desc=Sans 28", "!"])
    m = M.build_media(src, PROFILE, OPENH264_PAIR)
    assert 'font-desc="Sans 28"' in m.fragment
    assert "font-desc=Sans 28" not in m.fragment


@pytest.mark.parametrize("profile", [
    {"width": 640, "height": 480, "fps": 30},                                  # нет bitrate
    {"width": 640, "height": 480, "fps": 30, "bitrate": 4000, "extra": 1},     # лишний ключ
    {"width": 640, "height": 480, "fps": 30, "bitrate": 10},                   # битрейт вне диапазона
    {"width": 640, "height": 480, "fps": 0, "bitrate": 4000},                  # fps вне диапазона
    {"width": True, "height": 480, "fps": 30, "bitrate": 4000},                # bool вместо int
    {"width": 640.0, "height": 480, "fps": 30, "bitrate": 4000},               # float вместо int
    "не объект",
])
def test_build_rejects_bad_profile(profile):
    """Неверный профиль — bad-request, и для сквозного режима тоже."""
    with pytest.raises(M.MediaError) as ei:
        M.build_media(FakeSource("H264"), profile, None)
    assert ei.value.code == "bad-request"
    with pytest.raises(M.MediaError) as ei:
        M.build_media(FakeSource("YUYV"), profile, V4L2_PAIR)
    assert ei.value.code == "bad-request"


def test_profile_args_bad_is_ctl_error():
    """profile_args для неверного профиля — CtlError bad-request."""
    with pytest.raises(CtlError) as ei:
        M.profile_args({"width": 640, "height": 480, "fps": 30, "bitrate": 10})
    assert ei.value.code == "bad-request"


def test_profile_args_good_returns_profile():
    """profile_args для верного профиля возвращает его же."""
    assert M.profile_args(PROFILE) is PROFILE


# --- 6. открытие источника ---

def test_open_media_no_device_when_missing():
    """Источник отсутствует (missing=True) — no-device с причиной."""
    op = Opener(FakeSource(available=False, missing=True, error="nope"))
    with pytest.raises(M.MediaError) as ei:
        M.open_media({"source": "v4l2"}, PROFILE, opener=op, pair=V4L2_PAIR)
    assert ei.value.code == "no-device"
    assert ei.value.detail == "nope"


def test_open_media_unsupported_when_not_available_not_missing():
    """Источник есть, но недоступен по другой причине — unsupported."""
    op = Opener(FakeSource(available=False, missing=False, error="mode not supported"))
    with pytest.raises(M.MediaError) as ei:
        M.open_media({"source": "v4l2"}, PROFILE, opener=op, pair=V4L2_PAIR)
    assert ei.value.code == "unsupported"


def test_open_media_no_device_when_present_fails():
    """available=True, но present() сообщает об отсутствии — no-device."""
    op = Opener(FakeSource(available=True, present=(False, "gone")))
    with pytest.raises(M.MediaError) as ei:
        M.open_media({"source": "rtsp"}, PROFILE, opener=op, pair=V4L2_PAIR)
    assert ei.value.code == "no-device"
    assert ei.value.detail == "gone"


@pytest.mark.parametrize("spec", ["v4l2", None, ["synthetic"], 7])
def test_open_media_spec_not_dict_is_bad_request(spec):
    """Описание не объект — bad-request; источник не открывается."""
    op = Opener(FakeSource())
    with pytest.raises(M.MediaError) as ei:
        M.open_media(spec, PROFILE, opener=op, pair=V4L2_PAIR)
    assert ei.value.code == "bad-request"
    assert op.calls == []


def test_open_media_unknown_kind_is_bad_request():
    """Неизвестный вид источника — bad-request; источник не открывается."""
    op = Opener(FakeSource())
    with pytest.raises(M.MediaError) as ei:
        M.open_media({"source": "bogus"}, PROFILE, opener=op, pair=V4L2_PAIR)
    assert ei.value.code == "bad-request"
    assert op.calls == []


def test_open_media_bad_profile_is_bad_request_before_open():
    """Неверный профиль отвергается до открытия источника."""
    op = Opener(FakeSource())
    with pytest.raises(M.MediaError) as ei:
        M.open_media({"source": "v4l2"}, {"width": 640}, opener=op, pair=V4L2_PAIR)
    assert ei.value.code == "bad-request"
    assert op.calls == []


def test_open_media_success_info_keys_v4l2():
    """Успешное открытие: info содержит kind, id, name, encoder (и формат), фрагмент собран."""
    src = FakeSource("YUYV", kind="v4l2", sid="cam0", name="Cam")
    m = M.open_media({"source": "v4l2"}, PROFILE, opener=Opener(src), pair=V4L2_PAIR)
    assert m.passthrough is False
    assert m.info["kind"] == "v4l2"
    assert m.info["id"] == "cam0"
    assert m.info["name"] == "Cam"
    assert m.info["encoder"] == "v4l2h264enc"
    assert m.info["format"] == "YUYV"
    assert "v4l2h264enc" in m.fragment


def test_open_media_success_passthrough_info():
    """Сквозной источник: info["encoder"] == "passthrough"."""
    m = M.open_media({"source": "rtsp"}, PROFILE, opener=Opener(FakeSource("H264", kind="rtsp")), pair=None)
    assert m.passthrough is True
    assert m.info["encoder"] == "passthrough"
    assert m.info["kind"] == "rtsp"


def test_open_media_synthetic_default_opener(monkeypatch):
    """Синтетический источник без подставной функции: фрагмент от videotestsrc, битрейт и ширина из профиля."""
    monkeypatch.setenv(M.ENV_ENCODER, "x264enc tune=zerolatency bitrate={bitrate_kbps}")
    monkeypatch.setattr(video, "has_element", lambda name: False)      # без надписей pango, без gst-inspect
    profile = {"width": 640, "height": 480, "fps": 30, "bitrate": 2000}
    m = M.open_media({"source": "synthetic"}, profile)
    assert m.fragment.startswith("videotestsrc")
    assert "bitrate=2000" in m.fragment
    assert "width=640" in m.fragment
    assert m.info["kind"] == "synthetic"
    assert m.info["encoder"] == "x264enc"


# --- 7. проверки аргументов команд ---

def test_check_obj_accepts_exact_set():
    """Объект с точным набором полей проходит и возвращается."""
    obj = {"a": 1}
    assert M.check_obj(obj, {"a"}) is obj


def test_check_obj_accepts_optional_field():
    """Необязательное поле допускается."""
    assert M.check_obj({"a": 1, "c": 2}, {"a"}, {"c"}) == {"a": 1, "c": 2}


@pytest.mark.parametrize("obj", [{"a": 1, "b": 2}, {}, {"b": 1}])
def test_check_obj_rejects_extra_or_missing(obj):
    """Лишнее или недостающее поле — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.check_obj(obj, {"a"})
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("obj", [[("a", 1)], "a", None, 5])
def test_check_obj_rejects_non_dict(obj):
    """Не объект — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.check_obj(obj, {"a"})
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("value, ok", [(1, True), (10, True), (5, True), (0, False), (11, False),
                                       (True, False), (5.0, False), ("5", False), (None, False)])
def test_req_int_bounds_and_types(value, ok):
    """Целое в границах 1..10 включительно; bool, float, строка отвергаются."""
    obj = {"n": value}
    if ok:
        assert M.req_int(obj, "n", 1, 10, "args") == value
    else:
        with pytest.raises(CtlError) as ei:
            M.req_int(obj, "n", 1, 10, "args")
        assert ei.value.code == "bad-request"


@pytest.mark.parametrize("value", ["rx-1", "a" * 16, "0", "7-b"])
def test_req_stream_accepts(value):
    """Имя потока: [a-z0-9-], 1..16 символов."""
    assert M.req_stream({"stream": value}) == value


@pytest.mark.parametrize("value", ["a" * 17, "RX", "rx_1", "rx 1", "rx\n", "", 5, None])
def test_req_stream_rejects(value):
    """Недопустимые имена потоков и не строки — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.req_stream({"stream": value})
    assert ei.value.code == "bad-request"


def test_req_stream_missing_is_bad_request():
    """Поле stream отсутствует — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.req_stream({})
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("value", ["0" * 32, "abcdef0123456789abcdef0123456789"])
def test_req_session_accepts_32_hex(value):
    """Сеанс — 32 строчных hex-символа."""
    assert M.req_session({"session": value}) == value


@pytest.mark.parametrize("value", ["A" * 32, "a" * 31, "a" * 33, "g" * 32, 0, None])
def test_req_session_rejects(value):
    """Неверная длина, регистр, не hex, не строка — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.req_session({"session": value})
    assert ei.value.code == "bad-request"


def test_req_reason_default_and_ascii():
    """Причины нет — пустая строка; печатный ASCII до 64 символов — принимается."""
    assert M.req_reason({}) == ""
    assert M.req_reason({"reason": "ok reason"}) == "ok reason"
    assert M.req_reason({"reason": "a" * 64}) == "a" * 64


@pytest.mark.parametrize("value", ["a" * 65, "причина", "x\x7f", "tab\there", "line\n", 5, None])
def test_req_reason_rejects(value):
    """Длиннее 64, не ASCII, управляющие символы, не строка — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.req_reason({"reason": value})
    assert ei.value.code == "bad-request"


@pytest.mark.parametrize("value", ["192.168.1.10", "::1", "2001:db8::1"])
def test_req_ip_accepts(value):
    """IPv4 и IPv6 адреса проходят."""
    assert M.req_ip(value, "peer") == value


@pytest.mark.parametrize("value", ["", "not-an-ip", "999.1.1.1", "1.2.3", 16843009, None])
def test_req_ip_rejects(value):
    """Мусор, неполный адрес и не строки — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.req_ip(value, "peer")
    assert ei.value.code == "bad-request"


def test_req_srtp_returns_cipher_and_key():
    """Корректная форма: возвращается (cipher, key_b64)."""
    assert M.req_srtp({"cipher": "aes-128-gcm", "key_b64": "QUJD"}) == ("aes-128-gcm", "QUJD")
    assert M.req_srtp({"cipher": "aes-128-icm", "key_b64": "QUJD"}) == ("aes-128-icm", "QUJD")


def test_req_srtp_unknown_cipher_is_unsupported():
    """Неизвестный шифр — unsupported."""
    with pytest.raises(CtlError) as ei:
        M.req_srtp({"cipher": "aes-256-gcm", "key_b64": "QUJD"})
    assert ei.value.code == "unsupported"


@pytest.mark.parametrize("obj", [
    {"cipher": 1, "key_b64": "QUJD"},
    {"cipher": "aes-128-gcm", "key_b64": None},
    {"cipher": "aes-128-gcm"},
    {"cipher": "aes-128-gcm", "key_b64": "QUJD", "extra": 1},
    "aes-128-gcm",
    None,
])
def test_req_srtp_bad_shape_is_bad_request(obj):
    """Не строки, лишние и недостающие поля, не объект — bad-request."""
    with pytest.raises(CtlError) as ei:
        M.req_srtp(obj)
    assert ei.value.code == "bad-request"


def test_req_srtp_error_messages_do_not_contain_key():
    """Ключ SRTP не попадает в текст исключения, даже при неверном шифре или форме."""
    cases = [
        {"cipher": "aes-256-gcm", "key_b64": CANARY},
        {"cipher": ["x"], "key_b64": CANARY},
        {"cipher": "aes-128-gcm", "key_b64": CANARY, "extra": CANARY},
    ]
    for obj in cases:
        with pytest.raises(CtlError) as ei:
            M.req_srtp(obj)
        assert CANARY not in str(ei.value)
        assert CANARY not in repr(ei.value)
        assert CANARY not in ei.value.detail


def test_req_srtp_valid_canary_key_not_checked_here():
    """Длина ключа здесь не проверяется, а значение возвращается как есть (не в сообщении)."""
    assert M.req_srtp({"cipher": "aes-128-gcm", "key_b64": CANARY}) == ("aes-128-gcm", CANARY)


@pytest.mark.parametrize("value, ok", [(1, True), (0xFFFFFFFE, True), (0, False), (0xFFFFFFFF, False),
                                       (True, False), (-1, False), ("1", False)])
def test_req_ssrc_bounds(value, ok):
    """SSRC: 1..2**32-2; 0 и 2**32-1 отвергаются."""
    if ok:
        assert M.req_ssrc({"ssrc": value}) == value
    else:
        with pytest.raises(CtlError) as ei:
            M.req_ssrc({"ssrc": value})
        assert ei.value.code == "bad-request"


@pytest.mark.parametrize("value, ok", [(1.5, True), (0, True), (-2, True), (math.nan, False),
                                       (math.inf, False), (-math.inf, False), (True, False),
                                       ("1.0", False), (None, False)])
def test_finite_float(value, ok):
    """Конечное число (не bool, не nan и не inf); результат — float."""
    if ok:
        r = M.finite_float(value, "x")
        assert isinstance(r, float)
        assert r == float(value)
    else:
        with pytest.raises(CtlError) as ei:
            M.finite_float(value, "x")
        assert ei.value.code == "bad-request"


@pytest.mark.parametrize("argv, expected", [
    (["--runtime-dir", "x"], "x"),
    (["--runtime-dir=x"], "x"),
    ([], None),
])
def test_parse_runtime_arg_ok(argv, expected):
    """Два вида записи аргумента и пустой список аргументов."""
    assert M.parse_runtime_arg(argv) == expected


@pytest.mark.parametrize("argv", [
    ["--runtime-dir"],
    ["--runtime-dir", ""],
    ["--runtime-dir", "x", "extra"],
    ["extra"],
    ["--unknown"],
])
def test_parse_runtime_arg_usage_error(argv):
    """Лишний или неизвестный аргумент, пустое значение — выход с кодом использования."""
    with pytest.raises(SystemExit):
        M.parse_runtime_arg(argv)
