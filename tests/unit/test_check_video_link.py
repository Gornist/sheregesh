# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Инструмент испытаний видеоканала (tools/probe/check_video_link.py): без плат."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "probe"))
import check_video_link as cv  # noqa: E402


def test_busy_counts_idle_and_iowait():
    assert cv._busy([100, 0, 0, 900, 0], [150, 0, 0, 1850, 0]) == 5.0       # 50 занятых из 1000
    assert cv._busy([0] * 5, [0] * 5) is None


def test_errors_are_not_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "ssh", lambda h, c, timeout=120: "x")

    def boom(c, r):
        raise TimeoutError("ssh")
    monkeypatch.setattr(cv, "SCENARIOS", {"a": boom, "b": lambda c, r: {"verdict": "ERROR", "abort": True},
                                          "c": lambda c, r: {"verdict": "PASS"}})
    assert cv.main(["--console", "c", "--remote", "r", "--out", str(tmp_path), "--only", "a,b,c"]) == 1
    s = json.loads((tmp_path / "summary.json").read_text())
    assert s["results"] == {"a": "ERROR", "b": "ERROR", "c": "SKIP"}         # после abort дальше не идём


def test_no_plain_flags_tcp_listener_and_plain_udp_ports():
    """TLS-видеосервера нет: TCP 7444 никто не слушает; UDP 5600/5004 (прежнее незашифрованное видео) — нарушение."""
    srtp_rx = [["udp", "00000000:1D14", "00000000:0000", "07"], ["udp", "00000000:1D15", "00000000:0000", "07"]]
    assert cv.judge_no_plain({"console": srtp_rx, "remote": []})["verdict"] == "PASS"
    listen = [["tcp", "00000000:1D14", "00000000:0000", "0A"]]
    r = cv.judge_no_plain({"console": srtp_rx, "remote": listen})
    assert r["verdict"] == "FAIL" and r["violations"] == ["remote: listen tcp 0.0.0.0:7444"], r
    r = cv.judge_no_plain({"console": [["udp", "0100007F:15E0", "00000000:0000", "07"]], "remote": []})
    assert r["verdict"] == "FAIL" and "udp video port" in r["violations"][0], r


def test_delta_measured_zero_differs_from_unmeasured():
    """Контракт видео v9: «ноль потерь» — только измеренный ноль; неизмеренный счётчик (None) не превращается в 0."""
    assert cv.delta({"x": None}, {"x": 5}, "x") is None and cv.delta({}, {"x": 5}, "x") is None
    assert cv.delta({"x": 0}, {"x": 0}, "x") == 0


def test_latency_judged_by_metric_with_same_bounds():
    srtp = {"transport": "srtp", "capture_to_decoded_ms": {"p50": 80.0, "p95": 95.0, "max": 120.0, "samples": 600},
            "transport_latency_ms": {"p50": None, "p95": None, "max": None, "samples": 0},
            "decode_latency_ms": {"p50": None, "p95": None, "samples": 0, "desync_resets": 0}}
    r = cv.judge_latency(srtp)
    assert r["verdict"] == "PASS" and r["capture_to_decoded_ms"]["p50"] == 80.0
    assert cv.judge_latency({**srtp, "capture_to_decoded_ms": {"p50": None, "samples": 0}})["verdict"] == "FAIL"
    # стенд 2026-10-29: p50 −49 мс получил PASS — задержка «захват → декодирован» не бывает ≤ 0 (кодер, сеть и
    # декодер); такое значение — ошибка смещения часов, а не измерение
    for bad in ({"p50": -49.2, "p95": -44.6, "max": -42.0, "samples": 109},
                {"p50": 0.0, "p95": 2.0, "max": 3.0, "samples": 600}):
        r = cv.judge_latency({**srtp, "capture_to_decoded_ms": bad})
        assert r["verdict"] == "FAIL" and "смещени" in r["reason"], (bad, r)
    tls = {"transport": "tls", "transport_latency_ms": {"p50": 4.0, "samples": 600},
           "decode_latency_ms": {"p50": 30.0, "samples": 600, "desync_resets": 0}}
    assert cv.judge_latency(tls)["verdict"] == "FAIL"                  # TLS-видео удалено (ADR-0011)


# SRTP (ADR-0016): по службам видео (стенд 2026-10-29: исправный поток 30 к/с по прежнему TLS-критерию давал FAIL)
CH, RH = "192.168.1.92", "192.168.1.171"
SESS = {"state": "streaming", "transport": "srtp", "session": "00cf96", "gen": 2, "ssrc": 125430704}
CONTROL = {"phase": "connected", "tls": "TLSv1.3", "peer_uri": "urn:sheregesh:remote:100000009d497e91", "host": RH}


def test_srtp_session_by_video_services():
    rx, tx = {**SESS, "peer": RH}, {**SESS, "peer": CH}
    assert cv.judge_session_srtp(CONTROL, rx, tx, CH, RH)["verdict"] == "PASS"
    for bad in ({"tx": {**tx, "gen": 3}}, {"tx": {**tx, "ssrc": 1}}, {"rx": {**rx, "state": "recovering"}},
                {"tx": {**tx, "peer": "192.168.1.50"}}, {"control": {**CONTROL, "tls": None}},
                {"rx": {**rx, "transport": "tls"}}):
        r = cv.judge_session_srtp(bad.get("control", CONTROL), bad.get("rx", rx), bad.get("tx", tx), CH, RH)
        assert r["verdict"] == "FAIL", bad


def _srtp_win(c_extra=None, l_extra=None, x_extra=None):
    link0 = {"gen": 2, "packets": 1000, "seq_gaps": 0, "lost_packets": 0, "reordered": None}
    c0 = {"mode": "remote-srtp", "frames_decoded": 100, "restarts": 6, "codec": "h264", "output": "640x360",
          "state": "playing", "link": link0}
    c1 = {**c0, "frames_decoded": 700, "link": {**link0, "packets": 14000, "received_fps_5s": 30.0,
                                                 "jitter_ms": 0.3, **(l_extra or {})}, **(c_extra or {})}
    x0 = {"session": "00cf96", "gen": 2, "rtp": {"packets": 1100},
          "source": {"kind": "synthetic", "encoder": "v4l2h264enc", "resolution": "1280x720", "fps": 30}}
    x1 = {**x0, "rtp": {"packets": 14100}, **(x_extra or {})}
    return c0, c1, x0, x1


def test_srtp_stream_window():
    r = cv.judge_stream_srtp(*_srtp_win(), 20.0)
    assert r["verdict"] == "PASS" and r["metrics"]["decoded_fps"] == 30.0 and "unmeasured" not in r, r
    assert r["metrics"]["reordered"] is None                                  # не измеряется — не требуется
    for kw in ({"l_extra": {"lost_packets": 2}}, {"l_extra": {"seq_gaps": 1}}, {"c_extra": {"restarts": 7}},
               {"x_extra": {"rtp": {"packets": 1100}}}, {"l_extra": {"gen": 3}},
               {"x_extra": {"source": {"resolution": "640x480"}}}, {"c_extra": {"output": "1280x720"}}):
        assert cv.judge_stream_srtp(*_srtp_win(**kw), 20.0)["verdict"] == "FAIL", kw
    r = cv.judge_stream_srtp(*_srtp_win(l_extra={"lost_packets": None}), 20.0)
    assert r["verdict"] == "FAIL" and r["unmeasured"] == ["lost_packets"]


def test_srtp_leftovers():
    rx, tx = {**SESS, "peer": RH}, {**SESS, "peer": CH}
    gui = ["39100 39062 39100 S gst-launch-1.0 -q unixfdsrc socket-path=/run/sheregesh-video/frames-main.sock ! ..."]
    assert cv.judge_leftovers_srtp(gui, [], rx, tx)["verdict"] == "PASS"
    assert cv.judge_leftovers_srtp([], [], rx, tx)["verdict"] == "PASS"                       # путь GL: в процессе GUI
    assert cv.judge_leftovers_srtp(gui * 2, [], rx, tx)["verdict"] == "FAIL"
    assert cv.judge_leftovers_srtp(["4 1 4 S gst-launch-1.0 unixfdsrc"], [], rx, tx)["verdict"] == "FAIL"   # сирота
    assert cv.judge_leftovers_srtp(gui, ["5 9 5 S gst-launch-1.0 rtpstreampay"], rx, tx)["verdict"] == "FAIL"
    assert cv.judge_leftovers_srtp(gui, [], rx, {**tx, "gen": 3})["verdict"] == "FAIL"
