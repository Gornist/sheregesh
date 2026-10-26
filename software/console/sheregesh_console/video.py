# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Приём синтетического видеопотока в GUI (REQ-SW-002, ADR-0003).

Декодирует отдельный процесс gst-launch-1.0; кадры читает фоновый поток и хранит только последний.
Основной поток GUI лишь забирает кадр по таймеру, поэтому сбой, зависание или отсутствие видео
не блокируют интерфейс. Источник — синтетический генератор (sheregesh.video) или локальный видеовход пульта
(S3.3, sheregesh.videoinput: стандартное устройство V4L2/UVC) — тот же процесс, тот же разбор кадров и перезапуск.
Удалённый блок (S3.1-VIDEO): RTP приходит по защищённому видеоканалу (sheregesh.videolink, TLS 1.3 с взаимной
аутентификацией) и подаётся декодеру через stdin; разрыв канала завершает декодер, и он перезапускается с новым
подключением. Незащищённого удалённого источника нет.
Режим «служба» (ADR-0016, транспорт SRTP): приём, расшифровку и декодирование ведёт служба sheregesh-video-rx, а GUI
берёт готовые кадры из её сокета unixfdsink (dmabuf): GL — unixfdsrc в процессе GUI (glvideo), без дисплея — дочерний
gst-launch unixfdsrc → RGBx → stdout и прежний _reader. Метрики канала — из state.json службы (videorx).
"""
from collections import deque
import os
import select
import signal
import subprocess
import threading
import time

from PySide6.QtCore import Property, QObject, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtQuick import QQuickPaintedItem

from sheregesh import video as gst

FRAME_W, FRAME_H = 640, 360
FRAME_FORMAT = QImage.Format_RGBX8888    # раскладка байтов кадра из gst (RGBx); BGRx — Format_RGB32
FRAME_WAKE = True                        # кадр — на экран сразу по приходу (сигнал потока чтения), а не по таймеру 33 мс:
#                                          задержка в пульте p50 5 мс вместо 15, p95 9 вместо 32 (ui-minimal, video-matrix)
NO_VIDEO_AFTER_S = 1.5
RESTART_BACKOFF_S = (1.0, 2.0, 4.0)
SERVICE_RETRY_S = 0.5        # режим «служба»: повторная проверка сокета кадров, пока его нет
RESCAN_S = 2.0               # локальный вход: повторный поиск устройства (нет устройства / режим не поддерживается)
REAP_WAIT_S = 0.05           # остановка: сколько поток GUI ждёт завершения убитого декодера, остальное — в фоне
STALL_RESTART_S = 5.0       # локальный вход: нет кадров дольше — перезапуск захвата с новым поиском устройства



def service_rgbx_args(path, width, height):
    """Кадры службы rx без дисплея → RGBx в stdout. Служба отдаёт dmabuf v4l2h264dec (caps memory:DMABuf, DMA_DRM),
    их не принимают программные videoscale/videoconvert (not-negotiated). На Pi 4 — ISP: v4l2convert импортирует
    dmabuf и сам уменьшает кадр (pi1: 30 к/с, CPU 11 %); иначе — GPU без окна (EGL surfaceless, 28 %)."""
    src = [gst.GST_LAUNCH, "-q", "unixfdsrc", f"socket-path={path}", "!"]
    tail = ["videoconvert", "!", f"video/x-raw,format=RGBx,width={width},height={height}", "!", "fdsink", "fd=1",
            "sync=false"]
    if gst.has_element("v4l2convert"):
        return [*src, "v4l2convert", "output-io-mode=dmabuf-import", "!",
                f"video/x-raw,format=BGRx,width={width},height={height}", "!", *tail]
    return ["env", "GST_GL_WINDOW=surfaceless", "GST_GL_PLATFORM=egl", *src, "glupload", "!", "glcolorconvert", "!",
            "glcolorscale", "!", f"video/x-raw(memory:GLMemory),format=RGBA,width={width},height={height}", "!",
            "gldownload", "!", *tail]


class VideoReceiver(QObject):
    frameChanged = Signal()
    stateChanged = Signal()
    _arrived = Signal()                  # из потока чтения: пришёл кадр (FRAME_WAKE)

    def __init__(self, parent=None, command=None):
        super().__init__(parent)
        self._command = command            # для испытаний: подмена конвейера
        self._codec = None
        self._port = None
        self._remote = None                # (ключ, фабрика VideoClient) — удалённый блок по защищённому каналу
        self._service = None               # ((кодек, путь сокета кадров), stats()) — кадры службы rx (SRTP)
        self._svc_pending = False          # сокета кадров службы ещё нет: ждём и повторяем
        self._svc_dead = False             # приёмник GL службы упал (нет процесса, который можно убить)
        self._local = None                 # (ключ, opener() → VideoSource) — локальный видеовход пульта (S3.3)
        self._source = None                # текущий VideoSource локального входа
        self._client = None
        self._feeder = None
        self._sessions = 0
        self._decoded = deque(maxlen=300)
        self._proc = None
        self._thread = None
        self._reaper = None
        self._gen = 0                      # поколение конвейера: потоки остановленного не трогают новый
        self._lock = threading.Lock()
        self._latest = None                # буфер последнего полного кадра (после передачи не меняется)
        self._image_buf = None
        self._seq = 0
        self._shown_seq = 0
        self._last_frame_t = None
        self._fps = 0.0
        self._fps_window = []
        # off | waiting | playing | no-video | restarting | unavailable | no-device | unsupported
        self._state = "off"
        self._error = ""
        self._restarts = 0                 # статистика: перезапусков всего
        self._backoff = 0                  # перезапусков подряд без восстановления — для паузы перед повтором
        self._next_start = 0.0
        self._started_at = 0.0
        self._last_notify = 0.0
        self.image = None
        self.gl = None                     # glvideo.GlVideo — кадр мимо Python (ADR-0015); None — прежний путь RGBx
        self._gl_on = False                # текущий конвейер — через GL
        self._gl_codec = {}                # кодек → годится ли для GL (аппаратный декодер, unixfdsink); проверка — раз
        self._gl_sock = None
        self._gl_check_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._poll)
        self._arrived.connect(self._poll, Qt.QueuedConnection)
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

    @Property(bool, notify=stateChanged)
    def glAvailable(self):
        """Путь GL в этом процессе возможен (плагины есть, Minimal): QML держит наготове элемент GstGLQt6VideoItem."""
        return self.gl is not None

    @Property(bool, notify=stateChanged)
    def glActive(self):
        """Кадры сейчас рисует qml6glsink (GlVideoItem), а не VideoView."""
        return self._gl_on

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

    @Property(dict, notify=stateChanged)
    def source(self):
        """Локальный видеовход: имя устройства, формат, разрешение и частота режима (VideoSource.info); иначе {}."""
        return self._source.info() if self._local is not None and self._source is not None else {}

    def shown_seq(self):
        """Номер последнего показанного кадра (VideoView: кадры до потери сигнала не выдаются за новые)."""
        return self._shown_seq

    def frames_since(self, t):
        """Сколько кадров декодировано позже момента t (time.monotonic) — по меткам последних 300 кадров.
        Переход источника (videosource.Handover): кадры после перехода — кадры нового источника."""
        with self._lock:
            n = 0
            for ts in reversed(self._decoded):
                if ts <= t:
                    break
                n += 1
            return n

    # ---------- управление ----------
    def configure(self, codec, port):
        """Источник сообщил кодек и порт. Перезапуск только при изменении."""
        if (codec, port) == (self._codec, self._port) and self._remote is None and self._local is None and \
                self._service is None and self._proc is not None:
            return
        self.stop()
        self._backoff = 0
        self._remote = self._local = self._source = self._service = None
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
        self._backoff = 0
        self._local = self._source = self._service = None
        self._remote, self._codec, self._port = ((codec, key), factory), codec, key[1]
        if self._command is None and not gst.available():
            self._error = "gst-launch-1.0 not found"
            self._set_state("unavailable")
            return
        self._start()

    def configure_service(self, codec, sock_path, stats):
        """Видеосеанс SRTP (ADR-0016): кадры отдаёт служба rx в unixfdsink по пути sock_path; stats() → метрики канала
        (videorx.VideoRx.stats). Сокета ещё нет — приёмник ждёт его и повторяет."""
        if self._service is not None and self._service[0] == (codec, sock_path) and self._state != "off":
            return
        self.stop()
        self._backoff = 0
        self._remote = self._local = self._source = None
        self._service, self._codec, self._port = ((codec, sock_path), stats), codec, None
        if self._command is None and not gst.available():
            self._error = "gst-launch-1.0 not found"
            self._set_state("unavailable")
            return
        self._start()

    def configure_local(self, key, opener):
        """Локальный видеовход пульта (S3.3). opener() → VideoSource; вызывается при каждом (пере)запуске, поэтому
        отключённое и вновь подключённое устройство находится заново (номер /dev/videoN может измениться).
        key — конфигурация источника: перезапуск только при её изменении."""
        if self._local is not None and self._local[0] == key and self._state != "off":
            return
        self.stop()
        self._backoff = 0
        self._remote, self._codec, self._port, self._source, self._service = None, None, None, None, None
        self._local = (key, opener)
        if self._command is None and not gst.available():
            self._error = "gst-launch-1.0 not found"
            self._set_state("unavailable")
            return
        self._start()

    def session_info(self):
        """INFO текущего видеосеанса удалённого блока (кодек, режим и — с блоков после 1.1 — источник); иначе {}."""
        client = self._client
        info = client.info if client is not None else None
        return dict(info) if isinstance(info, dict) else {}

    def metrics(self):
        """Для диагностики без дисплея: фактически декодированные кадры и состояние видеоканала."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._decoded if now - t <= 5.0]
            frames = self._seq
        out = {"state": self._state, "codec": self._codec, "frames_decoded": frames, "restarts": self._restarts,
               "decoded_fps_5s": round(len(recent) / 5.0, 1) if recent else 0.0, "output": f"{FRAME_W}x{FRAME_H}",
               "error": self._error, "mode": "remote-tls" if self._remote else "remote-srtp" if self._service else
               "local-input" if self._local else ("local" if self._codec else "off"), "sessions": self._sessions,
               "transport": "srtp" if self._service else "tls" if self._remote else None}
        if self._local is not None and self._source is not None:
            out["source"] = self._source.info()
        if self._client is not None:
            out["link"] = self._client.stats()
        elif self._service is not None:
            out["link"] = self._service[1]()
        return out

    def _start_service(self):
        """Кадры службы rx: GL (unixfdsrc в процессе GUI) или RGBx (дочерний gst-launch), как у остальных источников."""
        path = self._service[0][1]
        self._svc_pending = False
        if not os.path.exists(path):                # служба ещё не создала сокет — тихо ждём и повторяем
            self._svc_pending = True
            self._next_start = time.monotonic() + SERVICE_RETRY_S
            if self._state == "off" or self._started_at == 0.0:
                self._started_at = time.monotonic()
            self._error = ""
            if self._state not in ("waiting", "no-video"):
                self._set_state("waiting")
            return
        gl_on = self._use_gl()
        if not gl_on:
            args = self._command or service_rgbx_args(path, FRAME_W, FRAME_H)
            try:
                self._proc = gst.spawn(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
            except OSError as e:
                self._error = str(e)
                self._schedule_restart()
                return
        self._error = ""
        self._started_at = time.monotonic()
        with self._lock:
            self._last_frame_t = None
        self._gl_on = gl_on
        self._set_state("waiting")
        if gl_on:
            gen = self._gen
            self.gl.start(path, lambda t: self._gl_frame(gen, t), lambda why: self._gl_error(gen, why))
            return
        self._thread = threading.Thread(target=self._reader, args=(self._proc, self._gen), name="video-reader",
                                        daemon=True)
        self._thread.start()

    def _start(self):
        if self._service is not None:
            self._start_service()
            return
        remote = self._remote is not None
        if self._local is not None:
            try:
                src = self._source = self._local[1]()
            except OSError as e:            # sysfs/ioctl: сбой опроса не должен останавливать интерфейс
                src = None
                self._error = f"device scan failed: {e}"
            if src is None or not src.available:
                self._error = src.error if src is not None else self._error
                self._next_start = time.monotonic() + RESCAN_S
                self._set_state("no-device" if src is None or getattr(src, "missing", False) else "unsupported")
                return
            args = self._command or gst.capture_args(src, FRAME_W, FRAME_H)
        elif self._use_gl():
            self._gl_sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
                                         f"sheregesh-video-{os.getpid()}-{self._gen}.sock")
            try:
                os.unlink(self._gl_sock)
            except OSError:
                pass
            args = gst.stream_receiver_gl_args(self._codec, self._gl_sock) if remote else \
                gst.receiver_gl_args(self._codec, self._port, self._gl_sock)
        elif remote:
            args = self._command or gst.stream_receiver_args(self._codec, FRAME_W, FRAME_H)
        else:
            args = self._command or gst.receiver_args(self._codec, self._port, FRAME_W, FRAME_H)
        gl_on = self._gl_sock is not None
        try:
            self._proc = gst.spawn(args, stdin=subprocess.PIPE if remote else subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL if gl_on else subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._error = str(e)
            self._schedule_restart()
            return
        self._error = ""                    # до запуска потока видеоканала: его причина разрыва не затирается
        if remote:
            self._client = self._remote[1]()
            self._sessions += 1
            self._feeder = threading.Thread(target=self._feed, args=(self._client, self._proc, self._gen),
                                            name="video-link", daemon=True)
            self._feeder.start()
        self._started_at = time.monotonic()
        with self._lock:
            self._last_frame_t = None
        self._gl_on = gl_on
        self._set_state("waiting")
        if gl_on:
            gen = self._gen
            self.gl.start(self._gl_sock, lambda t: self._gl_frame(gen, t), lambda why: self._gl_error(gen, why))
            return
        self._thread = threading.Thread(target=self._reader, args=(self._proc, self._gen), name="video-reader",
                                        daemon=True)
        self._thread.start()

    def _use_gl(self):
        """GL — для кадров с блока или локального UDP, H.264 с аппаратным декодером, при готовом элементе QML."""
        if self.gl is None or self._command or not self._codec:
            return False
        if self._codec not in self._gl_codec:
            self._gl_codec[self._codec] = gst.gl_capable(self._codec)
        return self._gl_codec[self._codec] and self.gl.ready()

    def _gl_frame(self, gen, now):
        """Проба приёмника GL (поток GStreamer): кадр дошёл до qml6glsink. Пикселей здесь нет — только учёт."""
        with self._lock:
            if gen != self._gen:
                return
            self._seq += 1
            self._last_frame_t = now
            self._decoded.append(now)
        client = self._client
        if client is not None:
            client.frame_decoded(now)
        if FRAME_WAKE:
            self._arrived.emit()

    def _gl_error(self, gen, why):
        """Сбой приёмника GL: декодер завершается — _poll перезапустит конвейер, как при любом выходе декодера."""
        if gen != self._gen:
            return
        if self._service is not None and why.startswith("decoder socket"):
            why = ""                                # службы ещё нет — не ошибка, а ожидание
        self._error = why
        if self._service is not None and self._proc is None:
            self._svc_dead = True                   # приёмник GL службы: перезапуск выполнит _poll
            return
        proc = self._proc
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def _feed(self, client, proc, gen):
        """Видеоканал → stdin декодера. Конец канала (разрыв, отказ аутентификации) закрывает stdin — декодер
        завершается, и _poll перезапускает его с новым подключением."""
        why = client.run(proc.stdin.fileno())
        if gen == self._gen:                # причина остановленного сеанса не затирает состояние нового
            self._error = f"video link: {why}"
        try:
            proc.stdin.close()
        except OSError:
            pass

    def stop(self):
        """Остановка без ожидания в потоке GUI (E2E-08). Вызывается из таймера бэкенда при потере связи: прежде
        здесь были join(2 с) потока видеоканала и wait() декодера, и подключение видеоканала, ещё не завершённое
        (TCP/TLS к пропавшему блоку), или декодер, переставший читать stdin, останавливали интерфейс на секунды.
        Теперь: декодер — SIGKILL (запись в его stdin сразу получает EPIPE), канал — close(), а ожидание потоков и
        процесса — в фоновом потоке. Поколение _gen отсекает запоздалые кадры и ошибки остановленного конвейера."""
        with self._lock:
            self._gen += 1
            self._latest = None
        self._svc_pending = self._svc_dead = False
        client, self._client = self._client, None
        proc, self._proc = self._proc, None
        if self._gl_on:
            self.gl.stop()
            self._gl_on = False
        if self._gl_sock is not None:
            try:
                os.unlink(self._gl_sock)
            except OSError:
                pass
            self._gl_sock = None
        threads = [t for t in (self._feeder, self._thread) if t is not None]
        self._feeder = self._thread = None
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):   # macOS: группа уже распалась — PermissionError
                pass
        if client is not None:
            client.close()
        if proc is not None:
            try:                            # после SIGKILL процесс обычно завершается за миллисекунды; зависший
                proc.wait(timeout=REAP_WAIT_S)   # в ядре (драйвер V4L2) дождётся фоновый поток, а не интерфейс
                proc = None
            except subprocess.TimeoutExpired:
                pass
        if proc is not None or threads:
            self._reaper = threading.Thread(target=self._reap, args=(proc, threads, self._reaper),
                                            name="video-reaper", daemon=True)
            self._reaper.start()

    @staticmethod
    def _reap(proc, threads, previous):
        if proc is not None:
            proc.wait()
        for t in threads:
            t.join()
        if previous is not None:
            previous.join()

    @Slot()
    def shutdown(self):
        self._timer.stop()
        self.stop()
        if self._reaper is not None:          # при выходе можно подождать: интерфейса уже нет
            self._reaper.join(timeout=3)

    # ---------- фоновое чтение ----------
    def _reader(self, proc, gen):
        """Кадры читаются в отдельный буфер на кадр (readinto, без склейки и срезов): кадр, отданный интерфейсу,
        больше не меняется, и QImage строится прямо на нём без копии."""
        size = FRAME_W * FRAME_H * 4
        fd = proc.stdout.fileno()
        try:                                       # Linux: труба на целый кадр — меньше пробуждений и чтений
            import fcntl
            fcntl.fcntl(fd, 1031, 1 << 20)         # F_SETPIPE_SZ
        except (ImportError, OSError):
            pass
        raw = os.fdopen(os.dup(fd), "rb", buffering=0)
        frame, got = bytearray(size), 0
        try:
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
                    n = raw.readinto(memoryview(frame)[got:])
                except OSError:
                    return
                if not n:
                    return
                got += n
                if got < size:
                    continue
                now = time.monotonic()
                with self._lock:
                    if gen != self._gen:            # конвейер уже остановлен
                        return
                    self._latest = frame
                    self._seq += 1
                    self._last_frame_t = now
                    self._decoded.append(now)
                frame, got = bytearray(size), 0
                if FRAME_WAKE:
                    self._arrived.emit()
                client = self._client
                if client is not None:
                    client.frame_decoded(now)
        finally:
            raw.close()

    # ---------- основной поток: таймер ----------
    def _set_state(self, state):
        self._state = state
        self._last_notify = time.monotonic()
        self.stateChanged.emit()

    def _schedule_restart(self):
        self._restarts += 1
        self._backoff += 1                 # 1 → 2 → 4 с подряд; после восстановления потока снова с 1 с
        self._next_start = time.monotonic() + RESTART_BACKOFF_S[min(self._backoff - 1, len(RESTART_BACKOFF_S) - 1)]
        self._set_state("restarting")

    def _gl_reconcile(self, now):
        """Раз в секунду: путь конвейера соответствует готовности GL? Окно появляется на экране позже старта
        приёмника (cage, подключение дисплея), а дисплей можно отключить — тогда конвейер перезапускается на нужный
        путь (ADR-0015). Только для работающего конвейера H.264 с блока или UDP."""
        running = self._proc is not None or (self._service is not None and self._gl_on)
        if self.gl is None or not running or self._local is not None or now < self._gl_check_at:
            return False
        self._gl_check_at = now + 1.0
        if self._use_gl() == self._gl_on:
            return False
        self.stop()
        self._start()
        return True

    def _poll(self):
        now = time.monotonic()
        if self._gl_reconcile(now):
            return
        if self._service is not None and (self._svc_dead or (self._svc_pending and now >= self._next_start)):
            if self._svc_dead:
                self.stop()
                self._schedule_restart()
            else:
                self._start()                       # сокета ещё не было: процесса и приёмника нет, останавливать нечего
            return
        if self._state in ("restarting", "no-device", "unsupported") and now >= self._next_start and \
                (self._codec or self._local):
            self._start()
            return
        if self._proc is not None and self._proc.poll() is not None:
            if not (self._remote is not None and self._error.startswith("video link:")):
                # причина разрыва видеоканала важнее кода выхода декодера, который она и вызвала
                self._error = f"{'capture' if self._local else 'decoder'} exited with code {self._proc.returncode}"
            self.stop()
            self._schedule_restart()
            return
        if self._local is not None and self._proc is not None and self._state == "no-video" and \
                now - max(self._last_frame_t or 0.0, self._started_at) > STALL_RESTART_S:
            self._error = f"no frames for {STALL_RESTART_S:.0f} s; capture restarted"   # устройство зависло/отключено
            self.stop()
            self._schedule_restart()
            return
        with self._lock:
            frame, seq, last = self._latest, self._seq, self._last_frame_t
        if (frame is not None or self._gl_on) and seq != self._shown_seq:
            if not self._gl_on:                                 # путь GL: кадр уже у qml6glsink, изображения нет
                self.image = QImage(frame, FRAME_W, FRAME_H, FRAME_FORMAT)          # на буфере кадра, без копии
                self._image_buf = frame                                             # буфер живёт вместе с QImage
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
            if new == "playing":
                self._backoff = 0
            if new != self._state:
                self._set_state(new)
            elif now - self._last_notify >= 0.5:      # обновить fps и счётчики в QML не чаще 2 раз в секунду
                self._last_notify = now
                self.stateChanged.emit()


class VideoView(QQuickPaintedItem):
    """Отображение последнего кадра с сохранением пропорций.

    live=false (источник на блоке без сигнала / без устройства / с ошибкой подключения, sheregesh_console.videosource)
    — чёрное поле вместо кадров; кадры, полученные до возврата live=true, тоже не показываются: прежний кадр не
    выдаётся за актуальный."""
    receiverChanged = Signal()
    liveChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._receiver = None
        self._live = True
        self._stale_seq = -1             # кадры с номером не больше этого не показываются (сняты при live=false)
        self.setAntialiasing(False)

    def geometryChange(self, new, old):
        super().geometryChange(new, old)
        self._fit_texture()

    def _fit_texture(self):
        """Текстура не больше, чем нужно кадру: при растяжении кадра (поле видео больше 640×360) paint() рисует в
        текстуру с разрешением самого кадра, а до размера поля её увеличивает сцена (GPU, линейная фильтрация).
        Иначе процессор масштабирует кадр до размера поля 30 раз в секунду (на 1920×1200 — около 2 Мп на кадр).
        Система координат paint() от этого не меняется (QQuickPaintedItem.textureSize)."""
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        k = max(1.0, min(w / FRAME_W, h / FRAME_H))
        self.setTextureSize(QSize(max(1, round(w / k)), max(1, round(h / k))))

    def _get_live(self):
        return self._live

    def _set_live(self, live):
        live = bool(live)
        if live == self._live:
            return
        self._live = live
        if not live and self._receiver is not None:
            self._stale_seq = self._receiver.shown_seq()
        self.liveChanged.emit()
        self.update()

    live = Property(bool, _get_live, _set_live, notify=liveChanged)

    def showing_frame(self):
        """Для испытаний: рисуется ли сейчас кадр (а не чёрное поле)."""
        r = self._receiver
        return bool(self._live and r is not None and r.state == "playing" and r.image is not None
                    and not r.image.isNull() and r.shown_seq() > self._stale_seq)

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
        w, h = self.width(), self.height()
        if not self.showing_frame():
            painter.fillRect(QRectF(0, 0, w, h), QColor("#000000"))
            return
        img = self._receiver.image
        k = min(w / img.width(), h / img.height())
        dw, dh = img.width() * k, img.height() * k
        x, y = (w - dw) / 2, (h - dh) / 2
        black = QColor("#000000")
        if x > 0.5:                                # поля по бокам / сверху-снизу, а не заливка всего кадра
            painter.fillRect(QRectF(0, 0, x, h), black)
            painter.fillRect(QRectF(x + dw, 0, w - x - dw, h), black)
        if y > 0.5:
            painter.fillRect(QRectF(0, 0, w, y), black)
            painter.fillRect(QRectF(0, y + dh, w, h - y - dh), black)
        # В текстуре размером с кадр (_fit_texture) масштаб ≈ 1: сглаживание не нужно, копия идёт быстрым путём
        t = self.textureSize()
        scale = min(t.width() / w, t.height() / h) * k if w > 0 and h > 0 else 1.0
        painter.setRenderHint(QPainter.SmoothPixmapTransform, abs(scale - 1.0) > 0.02)
        painter.drawImage(QRectF(x, y, dw, dh), img)
