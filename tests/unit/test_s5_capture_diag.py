# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S5: диагностика USB-адаптера (tools/probe/s5_capture_diag.py) без плат — разбор, выбор режимов, вердикт, --fake."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "probe"))
import s5_capture_diag as d  # noqa: E402


def test_speed_and_lsusb():
    assert d.speed_label("480").startswith("USB 2.0") and d.speed_label("5000").startswith("USB 3.x")
    assert d.speed_label("12").startswith("USB 1.x") and d.speed_label("") == "?"
    assert d.lsusb_ids("Bus 001 Device 003: ID 534D:2109 MacroSilicon\nBus 002: ID 1d6b:0003 root") == \
        ["1d6b:0003", "534d:2109"]


def test_new_device_and_target():
    hub = {"port": "1-1", "usb_id": "2109:3431"}
    cap = {"port": "1-1.2", "usb_id": "534d:2109", "interfaces": {"1-1.2:1.0": {"class": "0e", "driver": "uvcvideo"},
                                                                  "1-1.2:1.1": {"class": "0e", "driver": "uvcvideo"}}}
    assert d.new_devices([hub], [hub, cap]) == [cap]
    assert d.pick_target([hub, cap], [cap])[0] is cap
    assert d.pick_target([hub, cap], [], usb_id="534D:2109")[0] is cap
    assert d.pick_target([hub, cap], [], nodes=[{"usb_id": "534d:2109", "video_capture": True}])[0] is cap
    t, cand = d.pick_target([hub, cap, dict(cap, port="1-1.3")], [cap, dict(cap, port="1-1.3")])
    assert t is None and len(cand) == 2                                  # два одинаковых — нужен --usb-id
    assert d.unbound(cap) == [] and d.unbound({"interfaces": {"x:1.0": {"class": "ff", "driver": ""}}}) == ["x:1.0"]


def test_kernel_log_sources():
    k = d.kernel_log("@SRC journalctl\nusb 1-1.2: reset high-speed USB device\nuvcvideo: Found UVC 1.00 device")
    assert k["source"] == "journalctl" and k["available"] and len(k["usb_errors"]) == 1
    assert d.kernel_log("@SRC none") == {"source": "none", "available": False, "tail": [], "usb_errors": []}
    assert d.kernel_log("usb 1-1: device descriptor read/64, error -71")["usb_errors"]


def test_v4l_links():
    lines = ["crw-rw---- 1 root video 81, 0 /dev/video0", "/dev/v4l/by-id:", "usb-MACROSILICON_USB_Video-video-index0",
             "usb-MACROSILICON_USB_Video-video-index1", "/dev/v4l/by-path:",
             "ls: cannot access '/dev/v4l/by-path': No such file or directory"]
    assert d.v4l_links(lines) == {"by-id": ["usb-MACROSILICON_USB_Video-video-index0",
                                            "usb-MACROSILICON_USB_Video-video-index1"], "by-path": []}


HDMI = {"node": "video0", "driver": "uvcvideo", "usb_id": "534d:2109", "video_capture": True, "standards": [],
        "inputs": [{"index": 0, "name": "Camera 1", "type": "camera"}],
        "formats": [{"fourcc": "MJPG", "sizes": [{"size": "1920x1080", "rates": ["30/1", "25/1"]},
                                                 {"size": "1280x720", "rates": ["60/1", "30/1"]},
                                                 {"size": "640x480", "rates": ["30/1"]}]},
                    {"fourcc": "YUYV", "sizes": [{"size": "1920x1080", "rates": ["5/1"]},
                                                 {"size": "1280x720", "rates": ["10/1"]}]},
                    {"fourcc": "GREY", "sizes": [{"size": "640x480", "rates": ["30/1"]}]}]}


def test_plan_modes_hdmi_top_and_every_fourcc():
    modes = d.plan_modes(HDMI, top=3)
    keys = [m["key"] for m in modes]
    assert keys == ["mjpg-1920x1080@30", "mjpg-1280x720@60", "yuyv-1920x1080@5"]   # YUYV — лучший свой режим
    assert modes[0]["caps"] == "image/jpeg,width=1920,height=1080,framerate=30/1" and not modes[0]["norm"]
    assert all("GREY" not in m["key"].upper() for m in modes)                        # формат без caps пропущен
    assert len(d.plan_modes(HDMI, top=1)) == 2                                       # каждый FourCC хотя бы раз


def test_plan_modes_cvbs_pal_then_ntsc():
    node = {"formats": [{"fourcc": "UYVY", "sizes": []}], "standards": ["PAL 25/1", "NTSC 30000/1001"]}
    modes = d.plan_modes(node)
    assert [(m["norm"], m["size"], m["fourcc"]) for m in modes] == [("PAL", "720x576", "UYVY"),
                                                                     ("NTSC", "720x480", "UYVY")]
    assert modes[1]["caps"] == "video/x-raw,format=UYVY,width=720,height=480" and modes[1]["nominal_fps"] == 29.97
    assert d.plan_modes(HDMI, kind="cvbs")[0]["fourcc"] == "YUYV"                    # --kind cvbs принудительно


def test_cpu_delta_and_raw_parse():
    c = d.cpu_delta("cpu 100 0 100 800 0 0 0 0 0 0", "cpu 200 0 200 1400 0 0 0 0 0 0", 4)
    assert c == {"busy_pct": 25.0, "cores": 1.0, "ncpu": 4}
    assert d.cpu_delta("", "cpu 1") is None
    pts = "\n".join(f"@P pts: 0:00:{0.5 + i / 30:09.6f}" for i in range(60))
    r = d.parse_raw(f"@A cpu 1 0 1 10 0 0 0 0\n@T0 10.0\n@RC 0\n@T1 12.5\n@B cpu 2 0 2 12 0 0 0 0\n@N 4\n{pts}\n"
                    "@E WARNING: from element /GstPipeline:pipeline0/GstV4l2Src: lost frames")
    assert r["fps_actual"] == 30.0 and r["buffers"] == 60 and r["wall_s"] == 2.5 and not r["timeout"]
    assert r["errors"] and r["cpu"]["busy_pct"] == 50.0
    t = d.parse_raw("@RC 124\n@T0 1\n@T1 13")
    assert t["timeout"] and t["buffers"] == 0 and t["fps_actual"] is None


def test_signal_and_notes():
    live = {"frames": 150, "fps_avg": 25, "luma_mean": 90, "segments": [{"state": "live", "from_s": 0, "to_s": 5},
                                                                         {"state": "blank", "from_s": 5, "to_s": 6}]}
    s = d.signal_of(live)
    assert s["label"] == "signal" and s["shares"] == {"live": 0.83, "blank": 0.17}
    assert d.signal_of({"frames": 0})["label"] == "no-frames" and d.signal_of(None)["label"] == "no-frames"
    m = {"nominal_fps": 30.0, "raw": {"fps_actual": 10.0, "buffers": 60, "errors": []},
         "signal": {"label": "blank"}}
    notes = d.mode_note(m)
    assert any("80 %" in n for n in notes) and any("синее" in n for n in notes)
    assert any("буферов нет" in n for n in d.mode_note({"raw": {"timeout": True, "buffers": 0}, "signal": {}}))


def test_overall_verdicts():
    assert d.overall({"error": "ssh"}) == "ERROR" and d.overall({"busy": True}) == "BUSY"
    assert d.overall({}) == "NO-DEVICE" and d.overall({"target": {"x": 1}, "unbound": ["i"]}) == "NO-DRIVER"
    assert d.overall({"target": {"x": 1}, "unbound": []}) == "NO-NODE"
    base = {"target": {"x": 1}, "unbound": [], "node": {"node": "video0"}}
    assert d.overall({**base, "modes": [{"signal": {"label": "blank"}}]}) == "NO-SIGNAL"
    assert d.overall({**base, "modes": [{"signal": {"label": "blank"}}, {"signal": {"label": "signal"}}]}) == "OK"


def test_fake_run_usbtv_cvbs(tmp_path):
    """Встроенная подделка: подключение по подсказке, usbtv, PAL — сигнал, NTSC — поле; итог в JSON и тексте."""
    assert d.main(["--fake", "--out", str(tmp_path)]) == 0
    rep = json.loads((tmp_path / "diag.json").read_text())
    assert rep["verdict"] == "OK" and rep["name"] == "1b71-3002-usbtv" and rep["new_usb_ids"] == ["1b71:3002"]
    assert rep["kind"] == "cvbs" and [m["norm"] for m in rep["modes"]] == ["PAL", "NTSC"]
    assert rep["modes"][0]["raw"]["fps_actual"] == 25.0 and rep["modes"][1]["signal"]["label"] == "blank"
    assert rep["kernel_log"]["source"] == "dmesg" and rep["by_id"] == ["usb-fushicai_usbtv007-video-index0"]
    text = (tmp_path / "diag.txt").read_text()
    assert "QUERYSTD 0xff PAL" in text and "norm=NTSC" in text and "USB 2.0" in text


def test_fake_busy_and_free_restores_config(tmp_path):
    fx = d._usbtv_fixture()
    fx["status"] = json.dumps({"video": {"source": {"type": "v4l2", "device": "1b71:3002", "state": "streaming"}}})
    assert d.main(["--fake", str(_dump(tmp_path, fx)), "--out", str(tmp_path / "busy")]) == 2
    assert json.loads((tmp_path / "busy" / "diag.json").read_text())["verdict"] == "BUSY"
    fx["vconf"] = json.dumps({"source": "v4l2", "device": "1b71:3002", "rtsp_urls": ["rtsp://h/x"]})
    import argparse
    args = argparse.Namespace(remote="r", name="x", out=str(tmp_path / "free"), plug=True, usb_id=None, kind="auto",
                              top=4, seconds=6.0, settle=0, budget=300, free=True)
    fake = d.Fake(fx)
    assert d.Diag(args, fake, prompt=lambda t: None).run() == 0
    tags = [t for t, _c in fake.calls]
    assert tags.index("vconf-set") < tags.index("raw:pal-yuyv-720x576") < tags.index("vconf-restore")
    set_cmd = dict(fake.calls)["vconf-set"]
    assert '"rtsp_urls": ["rtsp://h/x"]' in set_cmd and '"source": "synthetic"' in set_cmd
    assert "kill -HUP $(systemctl show -p MainPID --value sheregesh-remote-net)" in dict(fake.calls)["vconf-restore"]


def test_fake_no_driver_stops_before_capture(tmp_path):
    fx = d._usbtv_fixture()
    after = json.loads(fx["usb@after"])
    after[-1]["interfaces"] = {"1-1.3:1.0": {"class": "ff", "driver": ""}}
    after[-1]["usb_id"] = "1c88:003c"
    fx["usb@after"] = json.dumps(after)
    fake = d.Fake(fx)
    import argparse
    args = argparse.Namespace(remote="r", name=None, out=str(tmp_path), plug=True, usb_id=None, kind="auto", top=4,
                              seconds=6.0, settle=0, budget=300, free=False)
    assert d.Diag(args, fake, prompt=lambda t: None).run() == 1
    rep = json.loads((tmp_path / "diag.json").read_text())
    assert rep["verdict"] == "NO-DRIVER" and rep["name"] == "1c88-003c-nodriver" and not rep["modes"]
    assert not any(t.startswith("raw:") for t, _c in fake.calls)


def test_remote_commands_shape():
    m = {"key": "pal", "norm": "PAL", "caps": "video/x-raw,format=YUY2,width=720,height=576", "nominal_fps": 25}
    c = d.raw_cmd("/dev/video0", m, 6)
    assert "v4l2src device=/dev/video0 norm=PAL do-timestamp=true num-buffers=150" in c
    assert "fakesink sync=false silent=false" in c and "timeout -s INT 12" in c and "/proc/stat" in c
    f = d.frames_cmd("/dev/video0", m, 6)
    assert "--norm PAL" in f and "d.pop(\"timeline\",None)" in f and "sudo" not in c + f
    assert "dmesg" in d.KLOG_CMD and "journalctl -k" in d.KLOG_CMD


def test_plan_no_ssh(capsys):
    assert d.main(["--plan", "--plug"]) == 0
    out = capsys.readouterr().out
    assert "D5" in out and "NTSC" in out and "подключите" in out


def _dump(tmp_path, fx):
    p = tmp_path / "fx.json"
    p.write_text(json.dumps(fx))
    return p
