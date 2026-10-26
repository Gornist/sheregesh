# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты чистой (без gi) части конвейеров видеослужбы: sheregesh.videosvc.pipelines (ADR-0016, трек A).

Проверяется: параметры SRTP и их секретность (repr, str, base64), вычисление RTCP SSRC, валидация TxConfig и RxConfig,
строки описания gst-launch для TX и RX (порты, адреса привязки, шифр, отсутствие ключа в любом виде).
GStreamer не запускается и конвейеры не собираются: тесты выполняются без python3-gi.
"""
import base64
import dataclasses
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sheregesh.videosvc import pipelines as P

SOFTWARE_DIR = Path(__file__).resolve().parents[2] / "software"

# Тестовые ключи с заметными байтами: их появление в строке было бы легко заметить.
KEY_GCM = bytes(range(0xE0, 0xE0 + 28))  # 28 байт, aes-128-gcm
KEY_ICM = bytes(range(0xD0, 0xD0 + 30))  # 30 байт, aes-128-icm
SSRC = 0x1A2B3C4D
MEDIA = ("videotestsrc is-live=true ! video/x-raw,width=640,height=360,framerate=30/1 ! "
         "x264enc tune=zerolatency ! video/x-h264,stream-format=byte-stream,alignment=au")
SINK = "fakesink sync=false"


def srtp_gcm():
    return P.SrtpParams("aes-128-gcm", KEY_GCM)


def srtp_icm():
    return P.SrtpParams("aes-128-icm", KEY_ICM)


def tx_cfg(**over):
    kw = dict(stream="main", ssrc=SSRC, peer_ip="127.0.0.1", peer_port=5000,
              rtcp_local_port=5003, rtcp_peer_port=5002, media=MEDIA, srtp=srtp_gcm())
    kw.update(over)
    return P.TxConfig(**kw)


def rx_cfg(**over):
    kw = dict(stream="main", ssrc=SSRC, peer_ip="127.0.0.1", local_port=5000,
              rtcp_local_port=5002, rtcp_peer_port=5003, decoder="avdec_h264", sink=SINK,
              srtp=srtp_gcm())
    kw.update(over)
    return P.RxConfig(**kw)


def _find(desc, pattern):
    """Возвращает группы первого совпадения шаблона в описании; при отсутствии падает с описанием."""
    m = re.search(pattern, desc)
    assert m is not None, f"в описании нет {pattern!r}:\n{desc}"
    return m.groups()


def _key_forms(key):
    """Видимые формы ключа, которых не должно быть ни в описании, ни в repr/str конфигов."""
    return [
        key.hex(),
        base64.b64encode(key).decode("ascii"),
        repr(key),
        key[:8].hex(),
        base64.b64encode(key[:12]).decode("ascii"),
    ]


# ---------------------------------------------------------------------------------------------------------------
# Контрактные константы
# ---------------------------------------------------------------------------------------------------------------

def test_KEY_LENGTHS_соответствует_контракту():
    assert P.KEY_LENGTHS == {"aes-128-gcm": 28, "aes-128-icm": 30}
    assert "null" not in P.KEY_LENGTHS


# ---------------------------------------------------------------------------------------------------------------
# SrtpParams
# ---------------------------------------------------------------------------------------------------------------

def test_gcm_ключ_28_байт_принимается():
    p = P.SrtpParams("aes-128-gcm", KEY_GCM)
    assert p.cipher == "aes-128-gcm"
    assert p.key == KEY_GCM


def test_icm_ключ_30_байт_принимается():
    p = P.SrtpParams("aes-128-icm", KEY_ICM)
    assert p.cipher == "aes-128-icm"
    assert p.key == KEY_ICM


@pytest.mark.parametrize("cipher, length", [
    ("aes-128-gcm", 27), ("aes-128-gcm", 29), ("aes-128-gcm", 16), ("aes-128-gcm", 0),
    ("aes-128-icm", 28), ("aes-128-icm", 31), ("aes-128-icm", 16), ("aes-128-icm", 0),
])
def test_неверная_длина_ключа_отклоняется(cipher, length):
    with pytest.raises(ValueError, match="длина ключа"):
        P.SrtpParams(cipher, bytes(length))


@pytest.mark.parametrize("cipher", ["null", "aes-256-gcm", "AES-128-GCM", "", "aes-128-gcm ", None])
def test_шифр_null_и_неизвестный_отклоняется(cipher):
    with pytest.raises(ValueError, match="шифр"):
        P.SrtpParams(cipher, KEY_GCM)


def test_ключ_строкой_отклоняется():
    with pytest.raises(ValueError, match="bytes"):
        P.SrtpParams("aes-128-gcm", "e" * 28)


def test_ключ_bytearray_копируется_в_bytes():
    src = bytearray(KEY_GCM)
    p = P.SrtpParams("aes-128-gcm", src)
    src[0] ^= 0xFF  # изменение исходного буфера не должно затронуть параметры
    assert isinstance(p.key, bytes)
    assert p.key == KEY_GCM


def test_параметры_SRTP_неизменяемы():
    p = srtp_gcm()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.cipher = "aes-128-icm"
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.key = KEY_ICM


@pytest.mark.parametrize("cipher, key", [
    ("aes-128-gcm", KEY_GCM),
    ("aes-128-icm", KEY_ICM),
    ("aes-128-gcm", bytes(range(28))),
], ids=["gcm", "icm", "gcm-0..27"])
def test_repr_и_str_SRTP_не_содержат_ключа(cipher, key):
    p = P.SrtpParams(cipher, key)
    for text in (repr(p), str(p)):
        assert cipher in text  # шифр виден, ключ — нет
        for form in _key_forms(key):
            assert form not in text


def test_from_b64_принимает_корректный_base64():
    p = P.SrtpParams.from_b64("aes-128-gcm", base64.b64encode(KEY_GCM).decode("ascii"))
    assert p.cipher == "aes-128-gcm"
    assert p.key == KEY_GCM
    p2 = P.SrtpParams.from_b64("aes-128-icm", base64.b64encode(KEY_ICM).decode("ascii"))
    assert p2.key == KEY_ICM


@pytest.mark.parametrize("text", ["не base64!", "%%%%", "AAEC", "", "AAEC\n", None])
def test_from_b64_на_мусор_бросает_ValueError(text):
    with pytest.raises(ValueError):
        P.SrtpParams.from_b64("aes-128-gcm", text)


def test_from_b64_ключ_чужой_длины_отклоняется():
    with pytest.raises(ValueError):
        P.SrtpParams.from_b64("aes-128-gcm", base64.b64encode(KEY_ICM).decode("ascii"))


# ---------------------------------------------------------------------------------------------------------------
# rtcp_ssrc_for
# ---------------------------------------------------------------------------------------------------------------

_RNG = random.Random(20261027)
_SAMPLES = [_RNG.randint(1, 0xFFFFFFFE) for _ in range(300)]


@pytest.mark.parametrize("ssrc", [1, 2, SSRC, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFE])
def test_rtcp_ssrc_отличается_от_входа(ssrc):
    assert P.rtcp_ssrc_for(ssrc) != ssrc


def test_rtcp_ssrc_лежит_в_диапазоне_1_до_2_32_минус_1():
    for x in _SAMPLES + [1, 0xFFFFFFFE]:
        r = P.rtcp_ssrc_for(x)
        assert 1 <= r <= 0xFFFFFFFF, f"вход {x:#x} дал {r:#x}"


def test_rtcp_ssrc_нулевой_результат_заменяется_на_1():
    # инверсия 0xFFFFFFFE даёт 0, функция должна вернуть 1
    assert P.rtcp_ssrc_for(0xFFFFFFFE) == 1


@pytest.mark.parametrize("ssrc", _SAMPLES[:50] + [1, SSRC, 0xFFFFFFFE])
def test_rtcp_ssrc_дважды_применённая_даёт_исходный(ssrc):
    assert P.rtcp_ssrc_for(P.rtcp_ssrc_for(ssrc)) == ssrc


def test_rtcp_ssrc_детерминирован_и_равен_инверсии():
    assert P.rtcp_ssrc_for(SSRC) == P.rtcp_ssrc_for(SSRC) == 0xE5D4C3B2


# ---------------------------------------------------------------------------------------------------------------
# Валидация TxConfig и RxConfig
# ---------------------------------------------------------------------------------------------------------------

MAKERS = [pytest.param(tx_cfg, id="TX"), pytest.param(rx_cfg, id="RX")]


def test_корректный_TxConfig_создаётся_с_умолчаниями():
    cfg = tx_cfg()
    assert cfg.stream == "main"
    assert cfg.ssrc == SSRC
    assert cfg.peer_port == 5000
    assert cfg.mtu == 1200


def test_корректный_RxConfig_создаётся_с_умолчаниями():
    cfg = rx_cfg()
    assert cfg.local_port == 5000
    assert cfg.decoder == "avdec_h264"
    assert cfg.jitter_latency_ms == 20


@pytest.mark.parametrize("make", MAKERS)
@pytest.mark.parametrize("ssrc", [1, 0xFFFFFFFE])
def test_граничные_ssrc_принимаются(make, ssrc):
    make(ssrc=ssrc)


@pytest.mark.parametrize("make", MAKERS)
@pytest.mark.parametrize("ssrc", [0, -1, 2 ** 32, "1", 1.0, None, True, False])
def test_ssrc_вне_диапазона_и_не_int_отклоняется(make, ssrc):
    with pytest.raises(ValueError):
        make(ssrc=ssrc)


@pytest.mark.parametrize("make", MAKERS)
def test_ssrc_FFFFFFFF_зарезервирован(make):
    with pytest.raises(ValueError, match="зарезервирован"):
        make(ssrc=0xFFFFFFFF)


@pytest.mark.parametrize("make, field", [
    (tx_cfg, "peer_port"), (tx_cfg, "rtcp_local_port"), (tx_cfg, "rtcp_peer_port"),
    (rx_cfg, "local_port"), (rx_cfg, "rtcp_local_port"), (rx_cfg, "rtcp_peer_port"),
])
@pytest.mark.parametrize("port", [0, 65536, -1, True, "5000"])
def test_порт_вне_1_65535_отклоняется(make, field, port):
    with pytest.raises(ValueError, match="порт"):
        make(**{field: port})


@pytest.mark.parametrize("make, field", [
    (tx_cfg, "peer_port"), (rx_cfg, "local_port"),
])
@pytest.mark.parametrize("port", [1, 65535])
def test_граничные_порты_принимаются(make, field, port):
    make(**{field: port})


@pytest.mark.parametrize("make", MAKERS)
@pytest.mark.parametrize("ip", ["не-адрес", "127.0.0.256", "", "localhost", "127.0.0.1 ! fakesink", None])
def test_peer_ip_не_адрес_отклоняется(make, ip):
    with pytest.raises(ValueError, match="peer_ip"):
        make(peer_ip=ip)


@pytest.mark.parametrize("make", MAKERS)
@pytest.mark.parametrize("stream", ["BAD_NAME", "", "a" * 17, "Main", "with space", "x;y", None])
def test_stream_не_по_шаблону_отклоняется(make, stream):
    with pytest.raises(ValueError, match="stream"):
        make(stream=stream)


@pytest.mark.parametrize("make", MAKERS)
def test_stream_граница_16_символов_принимается(make):
    make(stream="a" * 16)


@pytest.mark.parametrize("bad", ["", "   ", "videotestsrc\nfakesink", "a\x00b", None])
def test_TX_media_пустой_или_с_запрещёнными_символами_отклоняется(bad):
    with pytest.raises(ValueError, match="media"):
        tx_cfg(media=bad)


@pytest.mark.parametrize("bad", ["", "   ", "fakesink\nfakesink", "fakesink\x00", None])
def test_RX_sink_пустой_или_с_запрещёнными_символами_отклоняется(bad):
    with pytest.raises(ValueError, match="sink"):
        rx_cfg(sink=bad)


@pytest.mark.parametrize("mtu", [100, 199, 65001, 70000, "1200", 1200.0, True])
def test_TX_mtu_вне_200_65000_отклоняется(mtu):
    with pytest.raises(ValueError, match="mtu"):
        tx_cfg(mtu=mtu)


@pytest.mark.parametrize("mtu", [200, 65000])
def test_TX_граничный_mtu_принимается(mtu):
    assert tx_cfg(mtu=mtu).mtu == mtu


@pytest.mark.parametrize("decoder", ["avdec h264; rm", "", "AvDec", "avdec-h264", "avdec_h264 ! fakesink", None])
def test_RX_decoder_не_имя_элемента_отклоняется(decoder):
    with pytest.raises(ValueError, match="decoder"):
        rx_cfg(decoder=decoder)


@pytest.mark.parametrize("jit", [-1, 2001, "20", 20.0, None])
def test_RX_jitter_latency_вне_0_2000_отклоняется(jit):
    with pytest.raises(ValueError, match="jitter_latency_ms"):
        rx_cfg(jitter_latency_ms=jit)


@pytest.mark.parametrize("jit", [0, 2000])
def test_RX_граничный_jitter_принимается(jit):
    assert rx_cfg(jitter_latency_ms=jit).jitter_latency_ms == jit


@pytest.mark.parametrize("make", MAKERS)
@pytest.mark.parametrize("srtp", [KEY_GCM, ("aes-128-gcm", KEY_GCM), {"cipher": "aes-128-gcm"}, None])
def test_srtp_должен_быть_SrtpParams(make, srtp):
    with pytest.raises(ValueError, match="srtp"):
        make(srtp=srtp)


# ---------------------------------------------------------------------------------------------------------------
# build_tx_description
# ---------------------------------------------------------------------------------------------------------------

def test_описание_TX_содержит_ssrc_mtu_и_порты():
    desc = P.build_tx_description(tx_cfg())
    toks = desc.split()
    assert f"ssrc={SSRC}" in toks
    assert "mtu=1200" in toks
    assert _find(desc, r"udpsink name=rtpout host=(\S+) port=(\d+)") == ("127.0.0.1", "5000")
    assert _find(desc, r"udpsink name=rtcpout host=(\S+) port=(\d+)") == ("127.0.0.1", "5002")
    assert _find(desc, r"udpsrc name=rtcpin address=(\S+) port=(\d+)") == ("127.0.0.1", "5003")


def test_описание_TX_mtu_подставляется():
    desc = P.build_tx_description(tx_cfg(mtu=1400))
    assert "mtu=1400" in desc.split()
    assert "mtu=1200" not in desc.split()


def test_описание_TX_содержит_payloader_шифрование_и_AVPF():
    desc = P.build_tx_description(tx_cfg())
    assert "rtph264pay" in desc
    assert "srtpenc" in desc
    assert "srtpdec" in desc
    assert "rtp-profile=avpf" in desc.split()


def test_описание_TX_GCM_аутентификация_null():
    toks = P.build_tx_description(tx_cfg()).split()
    assert "rtp-cipher=aes-128-gcm" in toks
    assert "rtcp-cipher=aes-128-gcm" in toks
    assert "rtp-auth=null" in toks
    assert "rtcp-auth=null" in toks
    assert "hmac-sha1-80" not in toks


def test_описание_TX_ICM_аутентификация_hmac_sha1_80():
    desc = P.build_tx_description(tx_cfg(srtp=srtp_icm()))
    toks = desc.split()
    assert "rtp-cipher=aes-128-icm" in toks
    assert "rtp-auth=hmac-sha1-80" in toks
    assert "rtcp-auth=hmac-sha1-80" in toks
    assert "rtp-auth=null" not in toks


@pytest.mark.parametrize("peer, bind", [
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    ("192.168.1.92", "0.0.0.0"),
    ("fd00::2", "::"),
], ids=["loopback4", "loopback6", "lan4", "lan6"])
def test_описание_TX_приём_RTCP_привязан_по_адресу_пира(peer, bind):
    desc = P.build_tx_description(tx_cfg(peer_ip=peer))
    assert _find(desc, r"udpsrc name=rtcpin address=(\S+) port=(\d+)")[0] == bind
    assert _find(desc, r"udpsink name=rtpout host=(\S+) port=(\d+)")[0] == peer


def test_описание_TX_детерминировано():
    cfg = tx_cfg()
    assert P.build_tx_description(cfg) == P.build_tx_description(cfg)


# ---------------------------------------------------------------------------------------------------------------
# build_rx_description
# ---------------------------------------------------------------------------------------------------------------

def test_описание_RX_содержит_порты_и_адрес_пира():
    desc = P.build_rx_description(rx_cfg())
    assert _find(desc, r"udpsrc name=rtpin address=(\S+) port=(\d+)") == ("127.0.0.1", "5000")
    assert _find(desc, r"udpsrc name=rtcpin address=(\S+) port=(\d+)") == ("127.0.0.1", "5002")
    assert _find(desc, r"udpsink name=rtcpout host=(\S+) port=(\d+)") == ("127.0.0.1", "5003")


def test_описание_RX_содержит_декодер_приёмник_и_request_keyframe():
    desc = P.build_rx_description(rx_cfg(decoder="v4l2h264dec",
                                         sink="unixfdsink socket-path=/run/sheregesh/video.sock"))
    toks = desc.split()
    assert "request-keyframe=true" in toks
    assert "rtph264depay" in desc
    assert "h264parse" in desc
    assert "v4l2h264dec name=dec ! unixfdsink socket-path=/run/sheregesh/video.sock" in desc
    assert "rtp-profile=avpf" in toks
    assert "srtpenc" in desc
    assert "srtpdec" in desc


@pytest.mark.parametrize("jit", [0, 20, 2000])
def test_описание_RX_latency_равен_jitter_latency_ms(jit):
    desc = P.build_rx_description(rx_cfg(jitter_latency_ms=jit))
    assert f"latency={jit}" in desc.split()


@pytest.mark.parametrize("peer, bind", [
    ("127.0.0.1", "127.0.0.1"),
    ("::1", "::1"),
    ("192.168.1.92", "0.0.0.0"),
    ("fd00::2", "::"),
], ids=["loopback4", "loopback6", "lan4", "lan6"])
def test_описание_RX_приём_привязан_по_адресу_пира(peer, bind):
    desc = P.build_rx_description(rx_cfg(peer_ip=peer))
    assert _find(desc, r"udpsrc name=rtpin address=(\S+) port=(\d+)")[0] == bind
    assert _find(desc, r"udpsrc name=rtcpin address=(\S+) port=(\d+)")[0] == bind
    assert _find(desc, r"udpsink name=rtcpout host=(\S+) port=(\d+)")[0] == peer


def test_описание_RX_GCM_аутентификация_null_и_caps():
    desc = P.build_rx_description(rx_cfg())
    toks = desc.split()
    assert "rtp-cipher=aes-128-gcm" in toks
    assert "rtp-auth=null" in toks
    assert "rtcp-auth=null" in toks
    assert "srtp-cipher=aes-128-gcm,srtp-auth=null,srtcp-cipher=aes-128-gcm,srtcp-auth=null" in desc
    assert "hmac-sha1-80" not in desc


def test_описание_RX_ICM_аутентификация_hmac_sha1_80_и_caps():
    desc = P.build_rx_description(rx_cfg(srtp=srtp_icm()))
    toks = desc.split()
    assert "rtp-cipher=aes-128-icm" in toks
    assert "rtp-auth=hmac-sha1-80" in toks
    assert "rtcp-auth=hmac-sha1-80" in toks
    assert "rtp-auth=null" not in toks
    assert "srtp-cipher=aes-128-icm,srtp-auth=hmac-sha1-80,srtcp-cipher=aes-128-icm,srtcp-auth=hmac-sha1-80" in desc


def test_описание_RX_детерминировано():
    cfg = rx_cfg()
    assert P.build_rx_description(cfg) == P.build_rx_description(cfg)


# ---------------------------------------------------------------------------------------------------------------
# Ключ не попадает ни в описания, ни в repr конфигов
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["TX", "RX"])
@pytest.mark.parametrize("cipher", ["aes-128-gcm", "aes-128-icm"])
def test_описание_не_содержит_ключа_ни_в_каком_виде(role, cipher):
    key = KEY_GCM if cipher == "aes-128-gcm" else KEY_ICM
    srtp = P.SrtpParams(cipher, key)
    desc = P.build_tx_description(tx_cfg(srtp=srtp)) if role == "TX" else P.build_rx_description(rx_cfg(srtp=srtp))
    for form in _key_forms(key):
        assert form not in desc
    assert "srtp-key" not in desc


@pytest.mark.parametrize("role", ["TX", "RX"])
@pytest.mark.parametrize("cipher", ["aes-128-gcm", "aes-128-icm"])
def test_repr_и_str_конфигов_не_раскрывают_ключ(role, cipher):
    key = KEY_GCM if cipher == "aes-128-gcm" else KEY_ICM
    srtp = P.SrtpParams(cipher, key)
    cfg = tx_cfg(srtp=srtp) if role == "TX" else rx_cfg(srtp=srtp)
    for text in (repr(cfg), str(cfg)):
        assert cipher in text
        for form in _key_forms(key):
            assert form not in text


# ---------------------------------------------------------------------------------------------------------------
# Импорт модуля без gi
# ---------------------------------------------------------------------------------------------------------------

def test_модуль_импортируется_без_gi():
    code = (
        "import sys\n"
        "from sheregesh.videosvc import pipelines\n"
        "sys.exit(1 if 'gi' in sys.modules else 0)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SOFTWARE_DIR), env.get("PYTHONPATH", "")) if p)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
