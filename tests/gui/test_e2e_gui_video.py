# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""DUAL-DEVICE E2E, приёмник видео пульта (offscreen, подставные декодеры без GStreamer): находки D1–D3 агента VIDEO.

D1 — пауза перед повтором сбрасывается после восстановления потока (прежде с третьего обрыва каждое восстановление
ждало 4 с); D2 — причина разрыва видеоканала не затирается кодом выхода декодера; D3 — PermissionError от killpg
(macOS, группа уже распалась) не роняет остановку.
"""
import os
import signal
import sys
import time

from sheregesh_console import video as vmod
from sheregesh_console.qtwait import pump, wait_until
from sheregesh_console.video import FRAME_H, FRAME_W, RESTART_BACKOFF_S, VideoReceiver

# Декодер: 5 кадров, ещё 0,4 с жив (приёмник успевает показать «playing»), затем выходит — обрыв потока.
FLAPPING = [sys.executable, "-c", (
    "import sys, time\nf = bytes(%d)\nfor _ in range(5):\n    sys.stdout.buffer.write(f); sys.stdout.buffer.flush()"
    "; time.sleep(0.04)\ntime.sleep(0.4)\n") % (FRAME_W * FRAME_H * 4)]


def _delays(rx):
    out = []

    def on_state():
        if rx.state == "restarting":
            out.append(round(rx._next_start - time.monotonic(), 2))
    rx.stateChanged.connect(on_state)
    return out


def test_d1_backoff_resets_after_stream_recovers(qapp):
    rx = VideoReceiver(command=FLAPPING)
    delays = _delays(rx)
    try:
        rx.configure("jpeg", 5999)
        assert wait_until(lambda: rx.restarts >= 4, 15), (rx.restarts, delays)
        assert all(d <= RESTART_BACKOFF_S[0] + 0.05 for d in delays), f"пауза растёт при восстановлениях: {delays}"
    finally:
        rx.shutdown()


def test_d1_backoff_still_grows_while_stream_never_recovers(qapp):
    rx = VideoReceiver(command=["false"])                 # декодер падает сразу, кадров нет
    delays = _delays(rx)
    try:
        rx.configure("jpeg", 5999)
        assert wait_until(lambda: rx.restarts >= 3, 10), delays
        assert [round(d) for d in delays[:3]] == [1, 2, 4], delays
    finally:
        rx.shutdown()


class _ClosedLink:
    """Подставной VideoClient: блок сразу закрыл видеоканал."""

    def run(self, fd):
        return "соединение закрыто блоком"

    def close(self):
        pass

    def frame_decoded(self, t):
        pass

    def stats(self):
        return {}


def test_d2_video_link_reason_kept_over_decoder_exit_code(qapp):
    decoder = [sys.executable, "-c", "import sys\nwhile sys.stdin.buffer.read(65536): pass\n"]   # выход по EOF, код 0
    rx = VideoReceiver(command=decoder)
    seen = []
    rx.stateChanged.connect(lambda: rx.state == "restarting" and seen.append(rx.error))
    try:
        rx.configure_remote("h264", ("127.0.0.1", 1), _ClosedLink)
        assert wait_until(lambda: len(seen) >= 2, 8), seen
        assert all(e == "video link: соединение закрыто блоком" for e in seen), seen
    finally:
        rx.shutdown()


def test_d3_permission_error_from_killpg_does_not_break_stop(qapp, monkeypatch):
    rx = VideoReceiver(command=[sys.executable, "-c", "import time; time.sleep(30)"])
    rx.configure("jpeg", 5999)
    pump(200)
    proc = rx._proc
    assert proc is not None

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(vmod.os, "killpg", denied)
    try:
        rx.configure(None, None)                         # не должно бросать исключение в таймер Qt
        assert rx.state == "off"
    finally:
        monkeypatch.undo()
        os.kill(proc.pid, signal.SIGKILL)
        rx.shutdown()


def test_video_journal_follows_receiver_signal_not_backend_tick(qapp, tmp_path):
    """E2E-12: переходы видео пишутся по сигналу приёмника. Бэкенд без таймера (такт не идёт) — обрывы
    и восстановления всё равно в журнале; прежде (опрос по такту) короткий обрыв терялся (CI ubuntu)."""
    from sheregesh_console.backend import Backend
    from sheregesh_console.journal import MemoryJournal
    jl = MemoryJournal()
    b = Backend(tmp_path / "data", autostart=False, video_command=FLAPPING, journal=jl)
    try:
        b.video.configure("jpeg", 5999)
        assert wait_until(lambda: b.video.restarts >= 2, 10)
        pump(1500)
        video = [t.split(" (")[0] for _, t in jl.records if t.startswith("video: ")]
        assert video[:3] == ["video: stream started", "video: no stream", "video: stream restored"], video
        assert all(a != b_ for a, b_ in zip(video, video[1:])), f"повторы подряд: {video}"
    finally:
        b.stop()
