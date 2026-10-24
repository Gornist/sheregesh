# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приём синтетического видеопотока в GUI (REQ-SW-002, ADR-0003).

Декодирует отдельный процесс gst-launch-1.0; кадры читает фоновый поток и хранит только последний.
Основной поток GUI лишь забирает кадр по таймеру, поэтому сбой, зависание или отсутствие видео
не блокируют интерфейс. Источник — только синтетический генератор (sheregesh.video).
Удалённый блок (S3.1-VIDEO): RTP приходит по защищённому видеоканалу (sheregesh.videolink, TLS 1.3 с взаимной
аутентификацией) и подаётся декодеру через stdin; разрыв канала завершает декодер, и он перезапускается с новым
подключением. Незащищённого удалённого источника нет.
"""
from collections import deque
import os
import select
import signal
import subprocess
import threading
import time

from PySide6.QtCore import Property, QObject, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtQuick import QQuickPaintedItem

from sheregesh import video as gst

FRAME_W, FRAME_H = 640, 360
NO_VIDEO_AFTER_S = 1.5
RESTART_BACKOFF_S = (1.0, 2.0, 4.0)


class VideoReceiver(QObject):
    frameChanged = Signal()
    stateChanged = Signal()

    def __init__(self, parent=None, command=None):
        super().__init__(parent)
        self._command = command            # для испытаний: подмена конвейера
        self._codec = None
        self._port = None
        self._remote = None                # (ключ, фабрика VideoClient) — удалённый блок по защищённому каналу
        self._client = None
        self._feeder = None
        self._sessions = 0
        self._decoded = deque(maxlen=300)
        self._proc = None
        self._thread = None
        self._lock = threading.Lock()
        self._latest = None                # bytes последнего полного кадра
        self._seq = 0
        self._shown_seq = 0
        self._last_frame_t = None
        self._fps = 0.0
        self._fps_window = []
        self._state = "off"                # off | waiting | playing | no-video | restarting | unavailable
        self._error = ""
        self._restarts = 0
        self._next_start = 0.0
        self._started_at = 0.0
        self._last_notify = 0.0
        self.image = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    # ---------- свойства для QML ----------
    @Property(str, notify=stateChanged)
    def state(self):
        return self._state

    @Property(str, notify=stateChanged)
    def codec(self):
        return self._codec or ""

    @Property(int, notify=stateChanged)
    def port(self):
        return self._port or 0

    @Property(float, notify=stateChanged)
    def fps(self):
        return round(self._fps, 1)

    @Property(int, notify=stateChanged)
    def frames(self):
        return self._seq

    @Property(int, notify=stateChanged)
    def restarts(self):
        return self._restarts

    @Property(str, notify=stateChanged)
    def error(self):
        return self._error

    @Property(str, constant=True)
    def resolution(self):
        return f"{FRAME_W}×{FRAME_H}"

    # ---------- управление ----------
    def configure(self, codec, port):
        """Источник сообщил кодек и порт. Перезапуск только при изменении."""
        if (codec, port) == (self._codec, self._port) and self._remote is None and self._proc is not None:
            return
        self.stop()
        self._remote = None
        self._codec, self._port = codec, port
        if codec is None or port is None:
            self._set_state("off")
            return
        if self._command is None and not gst.available():
            self._error = "gst-launch-1.0 not found"
            self._set_state("unavailable")
            return
        self._start()

    def configure_remote(self, codec, key, factory):
        """Удалённый блок: видео только по защищённому каналу. factory() → новый sheregesh.videolink.VideoClient."""
        if self._remote is not None and self._remote[0] == (codec, key) and self._state != "off":
            return
        self.stop()
        self._remote, self._codec, self._port = ((codec, key), factory), codec, key[1]
        if self._command is None and not gst.available():
            self._error = "gst-launch-1.0 not found"
            self._set_state("unavailable")
            return
        self._start()

    def metrics(self):
        """Для диагностики без дисплея: фактически декодированные кадры и состояние видеоканала."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._decoded if now - t <= 5.0]
            frames = self._seq
        out = {"state": self._state, "codec": self._codec, "frames_decoded": frames, "restarts": self._restarts,
               "decoded_fps_5s": round(len(recent) / 5.0, 1) if recent else 0.0, "output": f"{FRAME_W}x{FRAME_H}",
               "error": self._error, "mode": "remote-tls" if self._remote else ("local" if self._codec else "off"),
               "sessions": self._sessions}
        if self._client is not None:
            out["link"] = self._client.stats()
        return out

    def _start(self):
        remote = self._remote is not None
        if remote:
            args = self._command or gst.stream_receiver_args(self._codec, FRAME_W, FRAME_H)
        else:
            args = self._command or gst.receiver_args(self._codec, self._port, FRAME_W, FRAME_H)
        try:
            self._proc = gst.spawn(args, stdin=subprocess.PIPE if remote else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._error = str(e)
            self._schedule_restart()
            return
        if remote:
            self._client = self._remote[1]()
            self._sessions += 1
            self._feeder = threading.Thread(target=self._feed, args=(self._client, self._proc), name="video-link",
                                            daemon=True)
            self._feeder.start()
        self._error = ""
        self._started_at = time.monotonic()
        with self._lock:
            self._last_frame_t = None
        self._set_state("waiting")
        self._thread = threading.Thread(target=self._reader, args=(self._proc,), name="video-reader", daemon=True)
        self._thread.start()

    def _feed(self, client, proc):
        """Видеоканал → stdin декодера. Конец канала (разрыв, отказ аутентификации) закрывает stdin — декодер
        завершается, и _poll перезапускает его с новым подключением."""
        why = client.run(proc.stdin.fileno())
        self._error = f"video link: {why}"
        try:
            proc.stdin.close()
        except OSError:
            pass

    def stop(self):
        client, self._client = self._client, None
        if client is not None:
            client.close()
        if self._feeder is not None:
            self._feeder.join(timeout=2)
            self._feeder = None
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            self._latest = None

    @Slot()
    def shutdown(self):
        self._timer.stop()
        self.stop()

    # ---------- фоновое чтение ----------
    def _reader(self, proc):
        size = FRAME_W * FRAME_H * 4
        buf = bytearray()
        fd = proc.stdout.fileno()
        while True:
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                return
            if proc.poll() is not None and not ready:
                return
            if not ready:
                continue
            try:
                chunk = os.read(fd, 1 << 20)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            if len(buf) >= size:
                full = len(buf) // size
                frame = bytes(buf[(full - 1) * size: full * size])   # только последний полный кадр
                del buf[: full * size]
                now = time.monotonic()
                with self._lock:
                    self._latest = frame
                    self._seq += full
                    self._last_frame_t = now
                    self._decoded.extend([now] * full)
                client = self._client
                if client is not None:
                    for _ in range(full):
                        client.frame_decoded(now)

    # ---------- основной поток: таймер ----------
    def _set_state(self, state):
        self._state = state
        self._last_notify = time.monotonic()
        self.stateChanged.emit()

    def _schedule_restart(self):
        self._restarts += 1
        self._next_start = time.monotonic() + RESTART_BACKOFF_S[min(self._restarts - 1, len(RESTART_BACKOFF_S) - 1)]
        self._set_state("restarting")

    def _poll(self):
        now = time.monotonic()
        if self._state == "restarting" and now >= self._next_start and self._codec:
            self._start()
            return
        if self._proc is not None and self._proc.poll() is not None:
            self._error = f"decoder exited with code {self._proc.returncode}"
            self.stop()
            self._schedule_restart()
            return
        with self._lock:
            frame, seq, last = self._latest, self._seq, self._last_frame_t
        if frame is not None and seq != self._shown_seq:
            self.image = QImage(frame, FRAME_W, FRAME_H, QImage.Format_RGBX8888).copy()
            self._fps_window = [t for t in self._fps_window if now - t < 1.0] + [now]
            self._fps = float(len(self._fps_window))         # показанных кадров за последнюю секунду
            self._shown_seq = seq
            self.frameChanged.emit()
        if self._state in ("waiting", "playing", "no-video"):
            if last is not None and now - last <= NO_VIDEO_AFTER_S:
                new = "playing"
            elif last is None and now - self._started_at <= 2 * NO_VIDEO_AFTER_S:
                new = "waiting"
            else:
                new, self._fps = "no-video", 0.0
            if new != self._state:
                self._set_state(new)
            elif now - self._last_notify >= 0.5:      # обновить fps и счётчики в QML не чаще 2 раз в секунду
                self._last_notify = now
                self.stateChanged.emit()


class VideoView(QQuickPaintedItem):
    """Отображение последнего кадра с сохранением пропорций."""
    receiverChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._receiver = None
        self.setAntialiasing(False)

    def _get_receiver(self):
        return self._receiver

    def _set_receiver(self, r):
        if r is self._receiver:
            return
        if self._receiver is not None:
            self._receiver.frameChanged.disconnect(self.update)
            self._receiver.stateChanged.disconnect(self.update)
        self._receiver = r
        if r is not None:
            r.frameChanged.connect(self.update)
            r.stateChanged.connect(self.update)
        self.receiverChanged.emit()
        self.update()

    receiver = Property(QObject, _get_receiver, _set_receiver, notify=receiverChanged)

    def paint(self, painter: QPainter):
        painter.fillRect(QRectF(0, 0, self.width(), self.height()), QColor("#000000"))
        r = self._receiver
        img = r.image if (r is not None and r.state == "playing") else None
        if img is None or img.isNull():
            return
        sw, sh = self.width() / img.width(), self.height() / img.height()
        k = min(sw, sh)
        w, h = img.width() * k, img.height() * k
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), img)
