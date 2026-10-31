# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Перебор режимов «mode: auto» между поколениями (videoinput.auto_modes, ModeMemory, служба tx; контракт v13 §6).

Дешёвые захваты объявляют режимы, которых не отдают: режим, не давший кадров при подключённом устройстве, следующий
запуск пропускает, удачный пробуется первым. Отключение устройства режим не порочит.
"""
import base64
import secrets

import pytest

from sheregesh import videoinput as V
from sheregesh.videosvc import media as M
from sheregesh.videosvc import tx as T

Y720_60 = V.Mode(1280, 720, 60, "YUYV")
Y720_30 = V.Mode(1280, 720, 30, "YUYV")
J1080_30 = V.Mode(1920, 1080, 30, "MJPG")
J720_60 = V.Mode(1280, 720, 60, "MJPG")
Y480_30 = V.Mode(640, 480, 30, "YUYV")
Y480_15 = V.Mode(640, 480, 15, "YUYV")
H1080_60 = V.Mode(1920, 1080, 60, "H264")
MS2130 = [Y720_60, Y720_30, J1080_30, J720_60, Y480_30, H1080_60]


# --- auto_modes ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("modes", [MS2130, [Y480_15, Y480_30], [Y480_15], [J720_60, H1080_60], [Y720_30, J1080_30],
                                   []], ids=["ms2130", "480", "только-15", "без-сырых", "720-1080", "пусто"])
def test_first_candidate_is_the_former_auto_choice(modes):
    found = V.auto_modes(modes)
    assert (found[0] if found else None) == V.auto_mode(modes)
    assert len(found) == len(set(found))
    assert set(found) == {m for m in modes if m.pixfmt in V.FORMATS}


def test_candidates_follow_the_documented_order():
    found = V.auto_modes(MS2130)
    assert found[0] == Y720_60                       # 720p60 без декодирования
    assert found.index(Y720_30) < found.index(J1080_30)   # затем типичные режимы
    assert V.auto_modes([Y480_15, Y480_30])[-1] == Y480_15     # < 25 к/с — в конце


def test_format_filter_applies_to_every_candidate():
    assert all(m.pixfmt == "MJPG" for m in V.auto_modes(MS2130, "mjpeg"))


# --- ModeMemory ------------------------------------------------------------------------------------------------

def test_memory_skips_failed_prefers_good_and_resets_when_all_failed():
    mem, dev, cands = V.ModeMemory(), "uvc:345f:2130:x", V.auto_modes(MS2130)
    assert mem.pick(dev, cands) == cands[0]
    mem.failed(dev, cands[0])
    assert mem.pick(dev, cands) == cands[1]
    assert mem.pick("uvc:other", cands) == cands[0]          # память — по устройству
    mem.worked(dev, cands[3])
    assert mem.pick(dev, cands) == cands[3]                  # удачный — первым
    mem.failed(dev, cands[3])
    assert mem.pick(dev, cands) == cands[1]
    for m in cands:
        mem.failed(dev, m)
    assert mem.pick(dev, cands) == cands[0]                  # все неудачны — перебор сначала
    assert mem.pick(dev, cands) == cands[0]
    assert mem.pick(dev, []) is None


# --- служба tx -------------------------------------------------------------------------------------------------

class Src:
    """Источник V4L2 для Media: id, текущий режим, подключён ли."""

    def __init__(self, mode, here=True):
        self.id, self.mode, self.here = "uvc:345f:2130:x", mode, here

    def present(self):
        return self.here, "" if self.here else "gone"


class Pipe:
    def __init__(self, cfg, on_event):
        self.on_event = on_event

    def start(self):
        pass

    def stop(self):
        pass

    def stats(self):
        return {}

    def request_keyframe(self):
        return True

    def emit(self, kind, **detail):
        self.on_event(kind, detail)


class Rig:
    def __init__(self, tmp_path, *, auto=True):
        self.now = 1000.0
        self.pipes, self.opened = [], []
        self.here = True
        self.auto = auto
        self.svc = T.TxService(str(tmp_path), factory=self.factory, media_opener=self.opener,
                               mono=lambda: self.now, clock=lambda: 1_793_000_000.0 + self.now, version="t")
        self.gen = 0

    def factory(self, cfg, on_event):
        p = Pipe(cfg, on_event)
        self.pipes.append(p)
        return p

    def opener(self, spec, profile):
        cands = V.auto_modes(MS2130)
        mode = self.svc.modes.pick("uvc:345f:2130:x", cands) if self.auto else Y720_60
        src = Src(mode, self.here)
        self.opened.append(mode)
        return M.Media(fragment="videotestsrc ! fakeout", passthrough=False, info={"kind": "uvc"}, src=src,
                       auto=self.auto)

    def start(self):
        self.gen += 1
        key = base64.b64encode(secrets.token_bytes(28)).decode("ascii")
        self.svc.handle("start", {"stream": "main", "session": "a" * 32, "gen": self.gen, "ssrc": 1000 + self.gen,
                                  "peer": {"ip": "127.0.0.1", "port": 7444, "rtcp_port": 7445},
                                  "srtp": {"cipher": "aes-128-gcm", "key_b64": key},
                                  "source": {"source": "uvc", "mode": "auto"},
                                  "profile": {"width": 1280, "height": 720, "fps": 30, "bitrate": 6000}})

    def no_frames(self):
        self.now += T.START_TIMEOUT_S + 0.1
        self.svc.tick()

    def state(self):
        return self.svc.state.snapshot()["streams"]["main"]


def test_mode_without_frames_is_skipped_by_the_next_generation(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.no_frames()
    assert rig.state()["error"] == "need-new-gen"
    rig.start()
    assert rig.opened == [Y720_60, V.auto_modes(MS2130)[1]]


def test_first_frame_makes_the_mode_preferred(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.no_frames()                                          # 720p60 YUYV не дал кадров
    rig.start()
    good = rig.opened[-1]
    rig.pipes[-1].emit("first-frame")
    rig.svc.tick()
    assert rig.state()["state"] == "streaming"
    rig.pipes[-1].emit("error", message="usb reset")         # сбой после кадров режим не порочит
    rig.svc.tick()
    rig.start()
    assert rig.opened[-1] == good


def test_unplugged_device_does_not_mark_the_mode(tmp_path):
    rig = Rig(tmp_path)
    rig.start()
    rig.here = False
    rig.svc._act["main"].media.src.here = False
    rig.no_frames()
    rig.here = True
    rig.start()
    assert rig.opened == [Y720_60, Y720_60]


def test_explicit_mode_is_never_remembered(tmp_path):
    rig = Rig(tmp_path, auto=False)
    rig.start()
    rig.no_frames()
    assert rig.svc.modes.pick("uvc:345f:2130:x", V.auto_modes(MS2130)) == Y720_60
