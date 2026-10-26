# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Кадр видео мимо Python (ADR-0015): декодер — отдельный процесс gst-launch, его кадры (dmabuf v4l2h264dec) приходят
дескрипторами по unix-сокету; в процессе пульта — unixfdsrc → glupload → glcolorconvert → qml6glsink, который рисует
в элемент QML GstGLQt6VideoItem (GlVideoItem.qml) текстурой GPU. Python видит только отметку «кадр пришёл» (проба на
входе приёмника) — для состояния, FPS и счётчиков VideoReceiver; пиксели через интерпретатор не идут.

Включается, только если есть всё: GObject Introspection (python3-gi), элементы unixfdsrc, glupload, glcolorconvert,
qml6glsink (gstreamer1.0-gl, gstreamer1.0-qt6), окно на экране (без дисплея сцена не инициализируется и qml6glsink
не получит контекст GL). Иначе — прежний путь: RGBx через stdout и VideoView (video.py).
"""
import ctypes
import os
import threading
import time

ELEMENTS = ("unixfdsrc", "glupload", "glcolorconvert", "qml6glsink")
QML_TYPE = "org.freedesktop.gstreamer.Qt6GLVideoItem"
CONNECT_WAIT_S = 3.0         # сокет декодера появляется, когда его конвейер дошёл до PAUSED


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    if not Gst.is_initialized():
        Gst.init(None)
    return Gst


def probe():
    """None — путь доступен; иначе строка причины (для журнала)."""
    try:
        gst = _gst()
    except (ImportError, ValueError) as e:
        return f"no GStreamer introspection: {e}"
    missing = [e for e in ELEMENTS if gst.ElementFactory.find(e) is None]
    if missing:
        return "missing elements: " + ", ".join(missing)
    return None


def register_qml_type():
    """Тип QML GstGLQt6VideoItem регистрирует плагин qml6 при загрузке — до загрузки QML (иначе import не найдётся)."""
    gst = _gst()
    sink = gst.ElementFactory.make("qml6glsink", None)
    if sink is None:
        raise RuntimeError("qml6glsink not available")
    sink.set_state(gst.State.NULL)


def _set_pointer(obj, prop, address):
    """Свойство gpointer (widget у qml6glsink): PyGObject принимает капсулу Python с адресом объекта C++."""
    try:
        obj.set_property(prop, address)
        return
    except TypeError:
        pass
    new = ctypes.pythonapi.PyCapsule_New
    new.restype, new.argtypes = ctypes.py_object, (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p)
    obj.set_property(prop, new(address, None, None))


class GlVideo:
    """Приёмник кадров в процессе пульта. ready() — можно ли сейчас включать путь (окно на экране, элемент привязан)."""

    def __init__(self, ready=lambda: True):
        self._ready = ready
        self._item = None            # адрес C++ элемента GstGLQt6VideoItem
        self._pipe = None
        self._lock = threading.Lock()
        self.error = ""

    def bind(self, item):
        import shiboken6
        self._item = shiboken6.getCppPointer(item)[0] if item is not None else None

    def ready(self):
        return self._item is not None and bool(self._ready())

    def start(self, socket_path, on_frame, on_error):
        """Подключиться к сокету декодера в фоне (он появляется не сразу) и запустить приёмник."""
        threading.Thread(target=self._start, args=(socket_path, on_frame, on_error), name="gl-video",
                         daemon=True).start()

    def _start(self, socket_path, on_frame, on_error):
        gst = _gst()
        end = time.monotonic() + CONNECT_WAIT_S
        while not os.path.exists(socket_path):
            if time.monotonic() > end:
                on_error(f"decoder socket {socket_path} did not appear")
                return
            time.sleep(0.05)
        pipe = gst.parse_launch(f"unixfdsrc socket-path={socket_path} ! queue max-size-buffers=1 leaky=downstream ! "
                                "glupload ! glcolorconvert ! qml6glsink name=sink sync=false")
        sink = pipe.get_by_name("sink")
        _set_pointer(sink, "widget", self._item)
        sink.get_static_pad("sink").add_probe(gst.PadProbeType.BUFFER,
                                              lambda pad, info: (on_frame(time.monotonic()), gst.PadProbeReturn.OK)[1])
        # Обработчик шины — в потоке, где сообщение возникло (без главного цикла GLib: у Qt он есть не везде)
        def message(_bus, msg):
            if msg.type == gst.MessageType.ERROR:
                on_error(f"gl video: {msg.parse_error()[0].message}")
            elif msg.type == gst.MessageType.EOS:
                on_error("gl video: end of stream")
            return gst.BusSyncReply.PASS
        pipe.get_bus().set_sync_handler(message)
        with self._lock:
            old, self._pipe = self._pipe, pipe
        if old is not None:
            old.set_state(gst.State.NULL)
        if pipe.set_state(gst.State.PLAYING) == gst.StateChangeReturn.FAILURE:
            on_error("gl video: pipeline did not start")

    def stop(self):
        with self._lock:
            pipe, self._pipe = self._pipe, None
        if pipe is not None:
            pipe.set_state(_gst().State.NULL)
