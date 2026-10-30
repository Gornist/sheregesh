# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Учёт запросов пульта (link/requests.py, этап 7.3): queued, sent, completed, timeout, offline, unknown."""
from sheregesh.link import discovery
from sheregesh.link.requests import Requests


def test_states_and_loss_before_and_after_send():
    r = Requests()
    a = r.new("module-cmd")
    r.open("module-cmd", a, "queued", op="reset")
    assert r.snapshot("module-cmd") == {"req": 1, "op": "reset", "state": "queued"}
    r.lost()                                            # обрыв до отправки — блок не получал
    assert r.state("module-cmd", a) == "offline"
    b = r.new("module-cmd")
    r.open("module-cmd", b, "queued", op="ping")
    r.sent("module-cmd", b)
    r.lost()                                            # после отправки — итог неизвестен
    assert r.state("module-cmd", b) == "unknown" and r.state("module-cmd", a) is None   # старый req — не текущий
    c = r.new("module-cmd")
    r.open("module-cmd", c, "queued", op="ping")
    r.sent("module-cmd", c)
    r.timed_out("module-cmd", c)
    r.completed("module-cmd", c)                        # итог пришёл позже ожидания вызывающего
    assert r.state("module-cmd", c) == "completed"
    r.lost()
    assert r.state("module-cmd", c) == "completed"      # завершённый не становится неизвестным


def test_kinds_and_numbers_are_separate():
    r = Requests()
    assert [r.new("video-select"), r.new("video-select"), r.new("module-cmd")] == [1, 2, 1]
    r.open("video-select", 2, "unsupported", id="rtsp:0")
    r.sent("video-select", 1)                           # не текущий req — без изменений
    assert r.snapshot("video-select") == {"req": 2, "id": "rtsp:0", "state": "unsupported"}
    assert r.snapshot("module-cmd") is None


def test_discovery_candidates():
    assert discovery.candidates("192.168.77.0/30") == ["192.168.77.1", "192.168.77.2"]
    assert len(discovery.candidates("10.0.0.0/16")) == discovery.MAX_HOSTS
