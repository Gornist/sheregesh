# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S4: список видеоисточников блока (status.video.sources), выбор по id и сохранение выбора (sheregesh.videosources);
проверка телеметрии и запрос выбора на стороне пульта (sheregesh.link.client). sysfs и /dev — поддельные."""
import json
import os
import stat

import pytest

from fake_v4l2 import FakeV4L2
from sheregesh import videosources as vs
from sheregesh.link import client as lc

SPEC_KEYS = {"source", "device", "mode", "format", "standard", "url", "codec", "latency_ms", "timeout_s"}


@pytest.fixture
def v4l2(tmp_path):
    return FakeV4L2(tmp_path)


def nodes(v):
    return vs.Catalog(lambda: ({}, []), v.scan_kw()).nodes()


def test_list_synthetic_v4l2_rtsp_without_opening_devices(v4l2):
    v4l2.pi4()                                                   # 14 служебных узлов Pi 4 + HDMI→USB MS2109
    raw = {"rtsp_urls": ["rtsp://op:secret@192.168.77.50:554/stream1?token=abc",
                         {"url": "rtsp://10.0.0.7/cam", "codec": "mjpeg", "name": "Мачта"}, "http://bad", 5]}
    rtsp = vs.rtsp_items(raw)
    got = vs.build({"source": "synthetic"}, rtsp, nodes(v4l2))
    assert got == [
        {"id": "synthetic", "input": "synthetic", "name": "SHEREGESH synthetic test pattern", "available": True,
         "active": True},
        {"id": "v4l2:534d:2109", "input": "hdmi", "name": "USB Video: USB Video", "available": True, "active": False},
        {"id": "rtsp:0", "input": "rtsp", "name": "RTSP 192.168.77.50:554/stream1", "available": True, "active": False},
        {"id": "rtsp:1", "input": "rtsp", "name": "Мачта", "available": True, "active": False}]
    assert v4l2.opened == [] and v4l2.aux_opened == []          # ни устройство, ни служебные узлы не открывались
    assert "secret" not in json.dumps(got) and "token" not in json.dumps(got)


def test_identical_adapters_without_serial_differ_by_port(v4l2):
    for n, path in (("video0", "platform-usb-0:1.2:1.0-video-index0"), ("video2", "platform-usb-0:1.3:1.0-video-index0")):
        v4l2.add(n, "uvcvideo", "USB Video", 0, ("534d", "2109", "") if n == "video0" else ("534d", "2109", "x"),
                 by_path=path)
    # второй адаптер — тот же VID:PID; серийный номер различает
    ids = [e["id"] for e in vs.build({}, [], nodes(v4l2))]
    assert ids == ["synthetic", "v4l2:534d:2109", "v4l2:534d:2109:x"]


def test_identical_adapters_same_id_use_by_path(tmp_path):
    v = FakeV4L2(tmp_path)
    v.add("video0", "usbtv", "usbtv", 0, ("1b71", "3002", ""), by_path="platform-usb-0:1.2:1.0-video-index0")
    v.add("video2", "usbtv", "usbtv", 0, ("1b71", "3002", ""), by_path="platform-usb-0:1.3:1.0-video-index0")
    n = nodes(v)
    got = vs.build({}, [], n)
    assert [e["id"] for e in got[1:]] == ["v4l2:platform-usb-0:1.2:1.0-video-index0",
                                          "v4l2:platform-usb-0:1.3:1.0-video-index0"]
    assert all(e["input"] == "cvbs" for e in got[1:])
    spec = vs.resolve("v4l2:platform-usb-0:1.3:1.0-video-index0", {}, [], n)
    assert spec == {"source": "v4l2", "device": "platform-usb-0:1.3:1.0-video-index0", "mode": "auto",
                    "format": "auto", "standard": "auto"}
    assert spec["device"] in n[1].selectors()                     # videoinput.open_v4l2 найдёт именно этот узел


def test_active_flag_and_missing_selected_device(v4l2):
    v4l2.pi4()
    n = nodes(v4l2)
    rtsp = vs.rtsp_items({"rtsp_urls": ["rtsp://10.0.0.7/cam"]})
    for dev in (str(v4l2.dev / "v4l/by-id/usb-MACROSILICON_USB_Video-video-index0"), "video0", "534d:2109", ""):
        act = [e["id"] for e in vs.build({"source": "v4l2", "device": dev}, rtsp, n) if e["active"]]
        assert act == ["v4l2:534d:2109"], dev                     # любой селектор описания → id из списка
    assert [e["id"] for e in vs.build({"source": "rtsp", "url": "rtsp://10.0.0.7/cam"}, rtsp, n) if e["active"]] == \
        ["rtsp:0"]
    missing = vs.build({"source": "v4l2", "device": "1b71:3002"}, rtsp, n)
    assert missing[1] == {"id": "v4l2:1b71:3002", "input": "v4l2", "name": "1b71:3002", "available": False,
                          "active": True}
    assert sum(e["active"] for e in missing) == 1


def test_legacy_url_without_rtsp_urls_is_listed_next(v4l2):
    spec = {"source": "rtsp", "url": "rtsp://u:p@10.0.0.9:8554/x", "codec": "mjpeg"}
    rtsp = vs.rtsp_items({"rtsp_urls": ["rtsp://10.0.0.7/cam"]}, spec)
    got = vs.build(spec, rtsp, [])
    assert got[-1] == {"id": "rtsp:1", "input": "rtsp", "name": "RTSP 10.0.0.9:8554/x", "available": True,
                       "active": True}
    assert vs.resolve("rtsp:1", spec, rtsp, []) == spec            # повторный выбор активного — без изменений


def test_at_most_16_and_active_kept():
    urls = [f"rtsp://10.0.0.{i}/cam" for i in range(30)]
    rtsp = vs.rtsp_items({"rtsp_urls": urls})
    got = vs.build({"source": "rtsp", "url": urls[29]}, rtsp, [])
    assert len(got) == vs.MAX_SOURCES and got[-1]["id"] == "rtsp:29" and got[-1]["active"]


def test_resolve_known_and_unknown_ids(v4l2):
    v4l2.pi4()
    n = nodes(v4l2)
    rtsp = vs.rtsp_items({"rtsp_urls": [{"url": "rtsp://10.0.0.7/cam", "codec": "mjpeg", "latency_ms": 50}]})
    cur = {"source": "synthetic"}
    assert vs.resolve("synthetic", cur, rtsp, n) == {"source": "synthetic"}
    assert vs.resolve("rtsp:0", cur, rtsp, n) == {"source": "rtsp", "url": "rtsp://10.0.0.7/cam", "codec": "mjpeg",
                                                   "latency_ms": 50}
    assert vs.resolve("v4l2:534d:2109", cur, rtsp, n)["device"] == "534d:2109"
    for bad in ("rtsp:1", "v4l2:dead:beef", "v4l2:video10", "camera", "", "x" * 97, "a b", None, 5, "../etc"):
        assert vs.resolve(bad, cur, rtsp, n) is None, bad
    assert v4l2.opened == []
    keep = {"source": "v4l2", "device": "534d:2109", "mode": "1920x1080@60", "format": "mjpeg"}
    assert vs.resolve("v4l2:534d:2109", keep, rtsp, n) == keep    # активный: режим и формат сохраняются


def test_save_spec_atomic_keeps_other_keys_and_mode(tmp_path):
    p = tmp_path / "video-input.json"
    p.write_text(json.dumps({"source": "v4l2", "device": "534d:2109", "mode": "1280x720@30",
                             "rtsp_urls": ["rtsp://a:b@10.0.0.7/cam"], "note": "оператор"}))
    os.chmod(p, 0o640)
    vs.save_spec(p, {"source": "rtsp", "url": "rtsp://a:b@10.0.0.7/cam", "codec": "h264"}, SPEC_KEYS)
    assert json.loads(p.read_text()) == {"rtsp_urls": ["rtsp://a:b@10.0.0.7/cam"], "note": "оператор",
                                         "source": "rtsp", "url": "rtsp://a:b@10.0.0.7/cam", "codec": "h264"}
    assert stat.S_IMODE(p.stat().st_mode) == 0o640 and sorted(os.listdir(tmp_path)) == ["video-input.json"]
    new = tmp_path / "new.json"
    vs.save_spec(new, {"source": "synthetic"}, SPEC_KEYS)
    assert json.loads(new.read_text()) == {"source": "synthetic"} and stat.S_IMODE(new.stat().st_mode) == 0o600


def test_save_spec_refuses_to_overwrite_unreadable_file(tmp_path):
    p = tmp_path / "video-input.json"
    p.write_text('{"rtsp_urls": ["rtsp://10.0.0.7/cam"],')
    with pytest.raises(ValueError, match="not overwritten"):
        vs.save_spec(p, {"source": "synthetic"}, SPEC_KEYS)
    assert p.read_text() == '{"rtsp_urls": ["rtsp://10.0.0.7/cam"],'
    assert sorted(os.listdir(tmp_path)) == ["video-input.json"]


def test_catalog_refresh_rate_limited_and_rtsp_checked_by_tcp(v4l2):
    v4l2.pi4()
    t = [100.0]
    calls = []

    def connect(addr, timeout):
        calls.append(addr)
        if addr[0] == "10.0.0.8":
            raise OSError("refused")
        return type("S", (), {"close": lambda self: None})()
    spec = {"source": "synthetic"}
    rtsp = vs.rtsp_items({"rtsp_urls": ["rtsp://10.0.0.7:8554/cam", "rtsp://10.0.0.8/cam"]})
    cat = vs.Catalog(lambda: (spec, rtsp), v4l2.scan_kw(), connect=connect, clock=lambda: t[0])
    assert cat.refresh() and not cat.refresh()                    # второй раз раньше 2 с — без опроса
    t[0] += 2.0
    assert cat.refresh() and cat.refreshes == 2 and calls == [("10.0.0.7", 8554), ("10.0.0.8", 554)] * 2
    snap = {e["id"]: e for e in cat.snapshot()}
    assert snap["rtsp:0"]["available"] and not snap["rtsp:1"]["available"] and snap["v4l2:534d:2109"]["available"]
    spec = {"source": "rtsp", "url": "rtsp://10.0.0.8/cam"}       # смена выбора видна сразу, без нового опроса
    assert [e["id"] for e in cat.snapshot() if e["active"]] == ["rtsp:1"] and cat.refreshes == 2
    assert v4l2.opened == []


# ---------------- пульт: проверка телеметрии и запрос выбора ----------------
def test_console_sanitizes_sources_and_select():
    body = {"video": {"enabled": True, "sources": [
        {"id": "synthetic", "input": "synthetic", "name": "S", "available": True, "active": True, "junk": 1},
        {"id": "synthetic", "input": "synthetic", "name": "dup", "available": True, "active": False},
        {"id": "v4l2:534d:2109", "input": "hdmi", "name": "N" * 200, "available": 1, "active": "yes"},
        {"id": "bad id", "input": "rtsp"}, {"id": "rtsp:0", "input": "ftp"}, "x"] + [
        {"id": f"rtsp:{i}", "input": "rtsp", "name": "", "available": True, "active": False} for i in range(20)],
        "select": {"req": 3, "id": "rtsp:9", "result": "unknown-id", "error": "unknown video source id 'rtsp:9'",
                   "extra": True}}}
    v = lc._sanitize_status(body)["video"]
    assert v["sources"][0] == {"id": "synthetic", "input": "synthetic", "name": "S", "available": True, "active": True}
    assert v["sources"][1] == {"id": "v4l2:534d:2109", "input": "hdmi", "name": "N" * 64, "available": False,
                               "active": False}
    assert len(v["sources"]) <= 16 and len({e["id"] for e in v["sources"]}) == len(v["sources"])
    assert v["select"] == {"req": 3, "id": "rtsp:9", "result": "unknown-id",
                           "error": "unknown video source id 'rtsp:9'"}
    old = lc._sanitize_status({"video": {"enabled": True, "sources": "x", "select": {"result": "maybe"}}})["video"]
    assert "sources" not in old and "select" not in old            # старый блок / мусор: полей нет


def test_select_video_source_without_link_or_on_old_session():
    c = lc.PeerClient("127.0.0.1", 1)                             # поток не запущен: соединения нет
    assert c.snapshot()["video_select"] is None
    c.select_video_source("rtsp:0")
    assert c.snapshot()["video_select"] == {"req": 1, "id": "rtsp:0", "state": "offline"} and c._outbox == []
    c._set(phase="connected", session="1.1")                       # старый блок: сессия 1.1
    c.select_video_source("synthetic")
    assert c.snapshot()["video_select"] == {"req": 2, "id": "synthetic", "state": "unsupported"} and c._outbox == []
    c._set(session="1.2")
    c.select_video_source("v4l2:534d:2109")
    assert c.snapshot()["video_select"]["state"] == "queued"
    assert c._outbox == [("video-select", {"id": "v4l2:534d:2109", "req": 3})]
    for bad in ("", "a b", "x" * 97, None, 3):
        with pytest.raises(ValueError):
            c.select_video_source(bad)
