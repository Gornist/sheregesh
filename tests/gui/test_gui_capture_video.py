# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/probe/gui_capture.py --by video: снимки по фактическим переходам видео и источника (offscreen, L3).

Поток → нет сигнала → восстановлено → нет устройства → восстановлено задаются подменой сведений блока
(Backend._video_report) по таймеру, кадры — подменный декодер. Проверяется порядок и состав снимков, метки фаз и
доказательство смены кадров: при потоке область кадра меняется, при «нет сигнала» / «нет устройства» — нет.
На стенде тот же класс Capture работает с настоящей связью (main).
"""
import json
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtQuickControls2 import QQuickStyle

from sheregesh_console.app import PAGE_NAMES, Ui, compile_translations
from sheregesh_console.backend import Backend
from test_gui_video_source import CONNECTED, FAKE_DECODER, STATES

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/probe"))
import gui_capture  # noqa: E402

QQuickStyle.setStyle("Fusion")


def test_args():
    a = gui_capture.parse_args(["--out", "x", "--by", "video", "--video-states", "no-signal,connect-error"])
    assert a.by == "video" and a.video_states == ["no-signal", "connect-error"]
    assert gui_capture.parse_args(["--out", "x"]).by == "link"
    with pytest.raises(SystemExit):
        gui_capture.parse_args(["--out", "x", "--by", "video", "--video-states", "melted"])


@pytest.mark.test_id("TEST-UI-002")
def test_capture_by_video_follows_actual_transitions(qapp, tmp_path):
    compile_translations()
    b = Backend(tmp_path / "data", autostart=False, video_command=FAKE_DECODER)
    report = {"mode": "remote", **STATES["synthetic-stream"]}
    b._video_report = lambda: report
    u = Ui(b)
    b._status.update(CONNECTED)
    b.statusChanged.emit()
    b.video.configure("h264", 5600)
    out = tmp_path / "shots"
    out.mkdir()
    a = gui_capture.parse_args(["--out", str(out), "--by", "video", "--timeout", "60"])
    cap = gui_capture.Capture(a, b, u, PAGE_NAMES)

    def set_state(name):
        report.clear()
        report.update({"mode": "remote", **STATES[name]})
        b._update_video_source()

    # внешнее «внесение отказа» по меткам фаз, как оператор на стенде
    script = [("phase-01-02-stream", "cvbs-no-signal"), ("phase-03-04-no-signal", "synthetic-stream"),
              ("phase-05-06-recovered-no-signal", "hdmi-no-device"), ("phase-07-08-no-device", "synthetic-stream")]

    def driver():
        while script and (out / script[0][0]).exists():
            set_state(script.pop(0)[1])
    t = QTimer()
    t.timeout.connect(driver)
    t.start(100)
    try:
        assert cap.run() == 0, sorted(p.name for p in out.iterdir())
    finally:
        t.stop()
        u.close()
    names = {p.name for p in out.iterdir()}
    for prefix in ("01-02-stream", "03-04-no-signal", "05-06-recovered-no-signal", "07-08-no-device",
                   "09-10-recovered-no-device"):
        assert {f"{prefix}-dashboard.png", f"{prefix}-video.png", f"{prefix}-video-t+1s.png",
                f"phase-{prefix}"} <= names, prefix
    states = json.loads((out / "states.json").read_text())
    pairs = {s["shot"].removesuffix("-video-t+1s.png"): s for s in states if s["shot"].endswith("-video-t+1s.png")}
    for prefix in ("01-02-stream", "05-06-recovered-no-signal", "09-10-recovered-no-device"):
        assert pairs[prefix]["frame_changed"] and pairs[prefix]["videoSource"]["live"], prefix
    for prefix, fault in (("03-04-no-signal", "no-signal"), ("07-08-no-device", "no-device")):
        s = pairs[prefix]
        assert not s["frame_changed"], f"{prefix}: в кадре меняется изображение при отказе источника"
        assert s["videoSource"]["display"] == fault and not s["videoSource"]["live"]
        assert s["video"]["state"] == "playing"        # кадры от блока шли, но не выдавались за видео
