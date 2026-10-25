# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""RTSP backend 1.0 (S5): транспорт auto | tcp | udp — конвейер rtspsrc, разбор описания и rtsp_urls, поле
transport в status.video.source; разбор SETUP тестового сервера стенда."""
import sys
from pathlib import Path

import pytest

from sheregesh import videoinput as vi
from sheregesh import videosources as vs
from sheregesh.videoingest import Ingest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import s4_rtsp_test_server as rt  # noqa: E402

URL = "rtsp://admin:secret@192.168.1.50:554/stream1"


@pytest.mark.parametrize("transport, protocols, known", [("auto", "udp+tcp", ""), ("tcp", "tcp", "tcp"),
                                                         ("udp", "udp", "udp")])
def test_capture_protocols_per_transport(transport, protocols, known):
    s = vi.RTSPSource(URL, "mjpeg", latency_ms=80, timeout_s=2, transport=transport)
    assert s.capture() == ["rtspsrc", f"location={URL}", f"protocols={protocols}", "latency=80",
                           "tcp-timeout=2000000", "timeout=2000000", "!", "rtpjpegdepay", "!"]
    assert (s.transport_mode, s.transport) == (transport, known)
    info = s.info()
    assert (info["transport"], info["transport_mode"]) == (known, transport) and "secret" not in str(info)
    assert "secret" not in s.id and "secret" not in s.name and "secret" not in s.device


def test_default_is_auto_and_bad_transport_is_value_error():
    assert vi.RTSPSource(URL).transport_mode == "auto"
    for bad in ("TCP", "http", "", None, 1, "udp+tcp"):
        with pytest.raises(ValueError, match="RTSP transport"):
            vi.RTSPSource(URL, transport=bad)
    with pytest.raises(ValueError) as e:                         # учётные данные не попадают и в сообщение
        vi.RTSPSource("rtsp://admin:secret@cam/x", "vp8", transport="tcp")
    assert "secret" not in str(e.value)


def test_rtspt_scheme_is_tcp_only():
    s = vi.RTSPSource("rtspt://cam/x")
    assert s.transport_mode == "tcp" and "protocols=tcp" in s.capture()
    with pytest.raises(ValueError, match="TCP only"):
        vi.RTSPSource("rtspt://cam/x", transport="udp")


def test_open_source_reads_transport_and_reports_bad_value():
    src = vi.open_source({"source": "rtsp", "url": URL, "codec": "h264", "transport": "udp"})
    assert isinstance(src, vi.RTSPSource) and src.transport == "udp" and "protocols=udp" in src.capture()
    assert vi.open_source({"source": "rtsp", "url": URL}).transport_mode == "auto"
    bad = vi.open_source({"source": "rtsp", "url": URL, "transport": "quic"})
    assert isinstance(bad, vi.Unavailable) and "RTSP transport 'quic'" in bad.error and "secret" not in bad.error


def test_rtsp_urls_objects_carry_transport_to_selection():
    raw = {"rtsp_urls": [{"url": "rtsp://10.0.0.7/cam", "codec": "mjpeg", "transport": "udp", "name": "Мачта"},
                         "rtsp://10.0.0.8/cam", {"url": "rtsp://10.0.0.9/cam", "transport": "bogus"}]}
    rtsp = vs.rtsp_items(raw)
    assert [it.get("transport") for _n, it in rtsp] == ["udp", None, "bogus"]
    cur = {"source": "synthetic"}
    assert vs.resolve("rtsp:0", cur, rtsp, []) == {"source": "rtsp", "url": "rtsp://10.0.0.7/cam", "codec": "mjpeg",
                                                   "transport": "udp"}
    assert "transport" not in vs.resolve("rtsp:1", cur, rtsp, [])          # не задан — auto при открытии
    assert vi.open_source(vs.resolve("rtsp:1", cur, rtsp, [])).transport_mode == "auto"
    bad = vi.open_source(vs.resolve("rtsp:2", cur, rtsp, []))                # неверное — ошибка источника, не списка
    assert isinstance(bad, vi.Unavailable) and "bogus" in bad.error
    spec = {"source": "rtsp", "url": "rtsp://10.0.0.5/x", "transport": "tcp"}     # текущий url вне списка
    assert vs.rtsp_items(raw, spec)[-1][1] == {"url": "rtsp://10.0.0.5/x", "transport": "tcp"}


@pytest.mark.parametrize("transport, known", [("auto", ""), ("tcp", "tcp"), ("udp", "udp")])
def test_status_reports_transport_only_while_streaming(transport, known):
    src = vi.RTSPSource(URL, "mjpeg", transport=transport)
    ing = Ingest(lambda: src, lambda s: ["true"])
    try:
        ing._shown = src
        for state, want in (("starting", ""), ("streaming", known), ("connect-error", ""), ("error", "")):
            ing.state = state
            st = ing.status()
            assert (st["transport"], st["transport_mode"]) == (want, transport), state
            assert "secret" not in str(st)
        ing._shown = vi.SyntheticSource()
        ing.state = "streaming"
        assert (ing.status()["transport"], ing.status()["transport_mode"]) == ("", "")
    finally:
        ing.close()


# ---------------- тестовый сервер стенда: разбор SETUP ----------------
def test_server_transport_negotiation():
    assert rt.parse_transports("tcp,udp") == ("tcp", "udp") and rt.parse_transports(" UDP ") == ("udp",)
    for bad in ("", "sctp", "tcp,http"):
        with pytest.raises(ValueError):
            rt.parse_transports(bad)
    udp = "RTP/AVP;unicast;client_port=50000-50001"
    tcp = "RTP/AVP/TCP;unicast;interleaved=0-1"
    assert rt.choose_transport(udp) == ("udp", (50000, 50001))
    assert rt.choose_transport("RTP/AVP/UDP;unicast;client_port=6000") == ("udp", (6000, 6001))
    assert rt.choose_transport(tcp) == ("tcp", None)
    assert rt.choose_transport(udp, ("tcp",)) is None                       # → 461 Unsupported Transport
    assert rt.choose_transport(tcp, ("udp",)) is None
    assert rt.choose_transport(f"{udp},{tcp}", ("tcp",)) == ("tcp", None)    # список вариантов: первый допустимый
    assert rt.choose_transport("RTP/AVP;multicast;port=5000-5001") is None
    assert rt.choose_transport("RTP/AVP;unicast") is None                   # нет client_port


def test_server_codec_names_and_encoder_choice():
    assert rt.producer("mjpeg", have=lambda e: False) == rt.producer("jpeg", have=lambda e: False)
    assert "JPEG/90000" in rt.sdp("mjpeg")
    mac = " ".join(rt.producer("h264", have=lambda e: e == "vtenc_h264"))
    assert "vtenc_h264" in mac and "rtph264pay" in mac
    assert rt.h264_encoder(lambda e: e in ("openh264enc", "v4l2h264enc")) == "v4l2h264enc"
    assert rt.h264_encoder(lambda e: False) is None


def test_transport_reaches_spec_file_and_console():
    """Ключ transport верхнего уровня сохраняется в video-input.json (SPEC_KEYS блока); поля транспорта источника
    доходят до пульта только из перечня значений."""
    from sheregesh.link import client
    from sheregesh_remote import agent
    assert "transport" in agent.SPEC_KEYS
    ok = client._sanitize_video_source({"state": "streaming", "input": "rtsp", "transport_mode": "auto",
                                        "transport": "udp"})
    assert (ok["transport_mode"], ok["transport"]) == ("auto", "udp")
    bad = client._sanitize_video_source({"state": "streaming", "input": "rtsp", "transport_mode": "x" * 9,
                                         "transport": "sctp"})
    assert (bad["transport_mode"], bad["transport"]) == ("", "")
