# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Фрагменты описания GStreamer для служб видео и общая часть служб tx/rx (ADR-0016, трек D).

Первая половина — чистые функции без gi: фрагмент источника и кодера TX (`open_media`), декодер и приёмник кадров
RX. Вторая — `ServiceBase`: каталог выполнения, сокет управления, state.json, sd_notify, SIGTERM, проверки аргументов
команд. Всё, что требует gi, живёт в `pipelines` и подгружается лениво.

Ключ SRTP сюда не попадает: ни в описание конвейера, ни в журнал, ни в исключения.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import re
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import sdnotify
from .ctl import CtlError, CtlServer
from .state import StateWriter

log = logging.getLogger("sheregesh.videosvc")

# --- окружение (переопределения для Mac и тестов) ---
ENV_RUNTIME = "SHEREGESH_VIDEO_RUNTIME"
ENV_ENCODER = "SHEREGESH_VIDEO_ENCODER_FRAGMENT"
ENV_DECODER = "SHEREGESH_VIDEO_DECODER"
ENV_RX_SINK = "SHEREGESH_VIDEO_RX_SINK"
DEFAULT_RUNTIME = "/run/sheregesh-video"

# Выход TX всегда такой: byte-stream, access unit (то, что ждёт rtph264pay).
H264_OUT_CAPS = "video/x-h264,stream-format=byte-stream,alignment=au"


class MediaError(Exception):
    """Источник или кодер не подходят. code: no-device | unsupported | bad-request."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# --- экранирование и склейка ---

_PROP_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)=(.*)$", re.S)
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@%+,;=()\[\]<>{}-]*$")


def quote_token(tok: str) -> str:
    """Токен gst-launch: свойство name=value с пробелами и спецсимволами в значении — в двойных кавычках.

    Кавычки и обратная косая в значении экранируются. Строки с переводом строки и NUL отвергаются.
    """
    if not isinstance(tok, str) or "\n" in tok or "\r" in tok or "\x00" in tok:
        raise MediaError("bad-request", "недопустимый символ в элементе конвейера")
    if tok == "!":
        return tok
    m = _PROP_RE.match(tok)
    if m and not _SAFE_VALUE_RE.match(m.group(2)):
        val = m.group(2).replace("\\", "\\\\").replace('"', '\\"')
        return f'{m.group(1)}="{val}"'
    if not m and re.search(r"\s", tok):
        raise MediaError("bad-request", "пробел в токене, не являющемся свойством")
    return tok


def join_tokens(tokens) -> str:
    return " ".join(quote_token(t) for t in tokens)


# --- кодер и декодер ---

def _env_fragment(name: str) -> str | None:
    v = os.environ.get(name, "").strip()
    if not v:
        return None
    if "\n" in v or "\x00" in v:
        raise MediaError("bad-request", f"{name}: недопустимые символы")
    return v


def _with_bitrate(tokens, bitrate_kbps: int) -> list[str]:
    """Битрейт профиля (кбит/с) -> бит/с в свойстве кодера: video_bitrate=… (v4l2) или bitrate=… (openh264)."""
    bps = int(bitrate_kbps) * 1000
    out = []
    for t in tokens:
        t = re.sub(r"video_bitrate=\d+", f"video_bitrate={bps}", t)
        t = re.sub(r"^bitrate=\d+$", f"bitrate={bps}", t)
        out.append(t)
    return out


def encoder_fragment(bitrate_kbps: int, pair=None) -> tuple[str, bool]:
    """(фрагмент кодера, аппаратный v4l2) для профиля. Переменная SHEREGESH_VIDEO_ENCODER_FRAGMENT — готовый фрагмент
    (подстановки {bitrate_kbps} и {bitrate_bps}); иначе пара из video.h264_elements(); нет пары — MediaError."""
    env = _env_fragment(ENV_ENCODER)
    if env is not None:
        return env.replace("{bitrate_kbps}", str(int(bitrate_kbps))) \
                  .replace("{bitrate_bps}", str(int(bitrate_kbps) * 1000)), False
    if pair is None:
        from .. import video
        pair = video.h264_elements()
    if not pair:
        raise MediaError("unsupported", "нет рабочего кодера H.264")
    enc = _with_bitrate(pair[0], bitrate_kbps)
    return join_tokens(enc), enc[0] == "v4l2h264enc"


def decoder_name(pair=None) -> str:
    """Имя декодера RX: SHEREGESH_VIDEO_DECODER, иначе пара из video.h264_elements()."""
    env = _env_fragment(ENV_DECODER)
    if env is not None:
        if not re.fullmatch(r"[a-z0-9_]+", env):
            raise MediaError("bad-request", f"{ENV_DECODER}: нужно имя элемента")
        return env
    if pair is None:
        from .. import video
        pair = video.h264_elements()
    if not pair:
        raise MediaError("unsupported", "нет рабочего декодера H.264")
    return pair[1]


def rx_sink_fragment(runtime_dir: str, stream: str) -> str:
    """Приёмник кадров RX: unixfdsink на frames-<stream>.sock (ADR-0015); SHEREGESH_VIDEO_RX_SINK — замена (fakesink)."""
    env = _env_fragment(ENV_RX_SINK)
    if env is not None:
        return env
    path = os.path.join(runtime_dir, f"frames-{stream}.sock")
    return join_tokens(["unixfdsink", f"socket-path={path}", "sync=false"])


# --- источник TX ---

from ..video import V4L2_ENC_DIRECT as _RAW_DIRECT   # форматы, которые аппаратный кодер принимает сам


@dataclass
class Media:
    fragment: str                 # описание от источника до video/x-h264,byte-stream,au
    passthrough: bool             # сквозной H.264 без кодера (ключевой кадр по PLI не гарантирован)
    info: dict = field(default_factory=dict)   # краткое описание источника для state.json (без учётных данных)
    src: object = None            # источник (VideoSource) — для памяти режимов службы tx
    auto: bool = False            # режим выбран по «mode: auto» (перебор кандидатов, videoinput.ModeMemory)


def _check_profile(profile) -> tuple[int, int, int, int]:
    if not isinstance(profile, dict) or set(profile) != {"width", "height", "fps", "bitrate"}:
        raise MediaError("bad-request", "profile: нужны width, height, fps, bitrate")
    for k, lo, hi in (("width", 16, 8192), ("height", 16, 8192), ("fps", 1, 120), ("bitrate", 50, 100000)):
        v = profile[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise MediaError("bad-request", f"profile.{k}: целое {lo}..{hi}")
    return profile["width"], profile["height"], profile["fps"], profile["bitrate"]


def build_media(src, profile, pair=None) -> Media:
    """Фрагмент gst для готового источника `src` (VideoSource): сквозной H.264 либо сырые кадры -> кодер.

    Сквозной (как video._PASSTHROUGH): устройство или RTSP уже отдают H.264 — h264parse config-interval=-1.
    Иначе: захват и декодирование формата устройства, videoconvert в I420 только если кодер не v4l2 или формат
    не из списка прямых (video.V4L2_ENC_DIRECT), кодер с битрейтом профиля, h264parse. Выход всегда H264_OUT_CAPS.
    """
    _w, _h, _fps, bitrate = _check_profile(profile)
    mode = getattr(src, "mode", None)
    head = list(src.capture())
    if mode is not None and mode.pixfmt == "H264":
        body = [*head, "h264parse", "config-interval=-1", "!", H264_OUT_CAPS]
        return Media(join_tokens(body), True, _source_info(src, "passthrough"))
    enc, hw = encoder_fragment(bitrate, pair)
    raw = [*head, *src.decode(None), *getattr(src, "square_pixels", list)()]    # аналог — 4:3 (videoinput)
    direct = hw and mode is not None and mode.pixfmt in _RAW_DIRECT
    if not direct:
        raw += ["videoconvert", "!", "video/x-raw,format=I420", "!"]
    frag = join_tokens(raw) + " " + enc
    if not frag.rstrip().endswith("h264parse"):
        frag += " ! h264parse"
    frag += " ! " + H264_OUT_CAPS
    return Media(frag, False, _source_info(src, enc.split(" ", 1)[0]))


def _source_info(src, encoder: str) -> dict:
    m = getattr(src, "mode", None)
    info = {"kind": str(getattr(src, "kind", "")), "id": str(getattr(src, "id", "")),
            "name": str(getattr(src, "name", "")), "encoder": encoder}
    if m is not None:
        info["format"] = str(m.pixfmt)
        info["resolution"] = f"{m.width}x{m.height}" if m.width else ""
        info["fps"] = int(m.fps) if m.fps else 0
    return info


def open_media(spec, profile, *, opener=None, pair=None, memory=None) -> Media:
    """Открыть источник по `spec` агента (videoinput.open_source) и собрать фрагмент.

    Нет устройства (или RTSP-сервер недоступен) -> MediaError("no-device"); неверное описание -> bad-request;
    режим не поддержан -> unsupported. Синтетический источник строится под профиль (video.synthetic).
    memory — videoinput.ModeMemory службы: при «mode: auto» пропускает режимы, не давшие кадров.
    """
    from .. import videoinput
    if not isinstance(spec, dict):
        raise MediaError("bad-request", "source: ожидается объект")
    kind = str(spec.get("source") or "synthetic").lower()
    if kind not in videoinput.SOURCE_KINDS:
        raise MediaError("bad-request", "source: неизвестный вид источника")
    w, h, fps, _b = _check_profile(profile)
    if opener is None:
        from .. import video

        def opener(s):
            return videoinput.open_source(s, synthetic=lambda: video.synthetic(w, h, fps), memory=memory)
    src = opener(spec)
    if not getattr(src, "available", False):
        code = "no-device" if getattr(src, "missing", False) else "unsupported"
        raise MediaError(code, str(getattr(src, "error", "") or "источник недоступен")[:200])
    ok, why = src.present()
    if not ok:
        raise MediaError("no-device", str(why)[:200])
    med = build_media(src, profile, pair)
    med.src = src
    med.auto = kind in ("v4l2", "uvc") and str(spec.get("mode") or "auto") == "auto" and src.mode is not None
    return med


# --- проверка аргументов команд ---

_STREAM_RE = re.compile(r"[a-z0-9-]{1,16}")
_SESSION_RE = re.compile(r"[0-9a-f]{32}")
_REASON_RE = re.compile(r"[ -~]{0,64}")


def bad(detail: str) -> CtlError:
    return CtlError("bad-request", detail)


def check_obj(obj, required: set, optional: set = frozenset(), what: str = "args") -> dict:
    """Объект с точным набором полей: лишние и недостающие — bad-request (значения в сообщение не попадают)."""
    if not isinstance(obj, dict):
        raise bad(f"{what}: ожидается объект")
    got = set(obj)
    if not (required <= got <= required | set(optional)):
        raise bad(f"{what}: набор полей не соответствует схеме")
    return obj


def req_int(obj: dict, key: str, lo: int, hi: int, what: str) -> int:
    v = obj[key]
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise bad(f"{what}.{key}: целое {lo}..{hi}")
    return v


def req_stream(obj: dict) -> str:
    s = obj.get("stream")
    if not isinstance(s, str) or not _STREAM_RE.fullmatch(s):
        raise bad("stream: [a-z0-9-]{1,16}")
    return s


def req_session(obj: dict) -> str:
    s = obj.get("session")
    if not isinstance(s, str) or not _SESSION_RE.fullmatch(s):
        raise bad("session: 32 hex-символа")
    return s


def req_reason(obj: dict) -> str:
    r = obj.get("reason", "")
    if not isinstance(r, str) or not _REASON_RE.fullmatch(r):
        raise bad("reason: печатный ASCII до 64 символов")
    return r


def profile_args(profile) -> dict:
    """Проверка profile команды start: ошибка — bad-request."""
    try:
        _check_profile(profile)
    except MediaError as e:
        raise bad(e.detail) from None
    return profile


def req_ip(v, what: str) -> str:
    import ipaddress
    if not isinstance(v, str):
        raise bad(f"{what}: ожидается адрес IP")
    try:
        ipaddress.ip_address(v)
    except ValueError:
        raise bad(f"{what}: не адрес IP") from None
    return v


def req_srtp(obj) -> tuple[str, str]:
    """(cipher, key_b64) после проверки формы. Шифр вне KEY_LENGTHS — unsupported; значение ключа не проверяется
    здесь и никогда не попадает в сообщения об ошибках."""
    from .pipelines import KEY_LENGTHS
    check_obj(obj, {"cipher", "key_b64"}, what="srtp")
    if not isinstance(obj["cipher"], str) or not isinstance(obj["key_b64"], str):
        raise bad("srtp: cipher и key_b64 — строки")
    if obj["cipher"] not in KEY_LENGTHS:
        raise CtlError("unsupported", "srtp.cipher: допустимы aes-128-gcm, aes-128-icm")
    return obj["cipher"], obj["key_b64"]


def req_ssrc(obj: dict) -> int:
    return req_int(obj, "ssrc", 1, 0xFFFFFFFE, "args")


# --- общая часть служб ---

NOTIFY_PERIOD_S = 5.0        # WATCHDOG=1 раз в 5 с (WatchdogSec=15)
STALL_LIMIT_S = 10.0         # операция команды дольше — главный цикл считается зависшим, watchdog не пингуем


class ServiceBase:
    """Каталог выполнения, сокет управления, state.json, sd_notify, сигналы. Потомок задаёт `dispatch`, `tick`,
    `shutdown_streams`, `status_line`. Команды и tick сериализованы блокировкой `self.lock`."""

    role = ""

    def __init__(self, runtime_dir: str | None = None, *, version: str = "dev",
                 clock=time.time, mono=time.monotonic) -> None:
        self.runtime_dir = runtime_dir or os.environ.get(ENV_RUNTIME) or DEFAULT_RUNTIME
        self.clock, self.mono = clock, mono
        self.lock = threading.RLock()
        self._busy_since: float | None = None
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.state = StateWriter(os.path.join(self.runtime_dir, "state.json"), self.role, version,
                                 clock=clock, mono=mono)
        self.ctl: CtlServer | None = None

    # --- к переопределению ---
    def dispatch(self, cmd: str, args: dict) -> dict:
        raise NotImplementedError

    def tick(self) -> None:
        raise NotImplementedError

    def shutdown_streams(self) -> None:
        raise NotImplementedError

    def status_line(self) -> str:
        return self.role

    # --- команды ---
    def handle(self, cmd: str, args: dict) -> dict:
        """Обработчик CtlServer: вызывается из потока каждого соединения."""
        with self.lock:
            self._busy_since = self.mono()
            try:
                return self.dispatch(cmd, args)
            finally:
                self._busy_since = None

    def _tick_guarded(self) -> None:
        if not self.lock.acquire(blocking=False):
            return                                  # идёт команда: цикл не ждёт, а watchdog следит за её длительностью
        try:
            self._busy_since = self.mono()
            try:
                self.tick()
            except Exception as e:                  # noqa: BLE001 — один сбой не должен останавливать службу
                log.error("%s: сбой цикла: %s", self.role, type(e).__name__)
            finally:
                self._busy_since = None
        finally:
            self.lock.release()

    def _stalled(self) -> bool:
        b = self._busy_since
        return b is not None and self.mono() - b > STALL_LIMIT_S

    # --- запуск ---
    def serve(self, *, install_signals: bool = False, tick_s: float = 0.1) -> int:
        """Главный цикл. Возвращает код выхода (0 при штатном завершении)."""
        os.makedirs(self.runtime_dir, mode=0o700, exist_ok=True)
        if install_signals:
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop_event.set())
        self.state.write()
        self.ctl = CtlServer(os.path.join(self.runtime_dir, "ctl.sock"), self.handle)
        self.ctl.start()
        sdnotify.ready(self.status_line())              # сокет слушает
        last_notify = self.mono()
        last_write = self.mono()
        try:
            while not self.stop_event.is_set():
                self._tick_guarded()
                now = self.mono()
                if now - last_write >= 1.0:
                    try:
                        self.state.write()
                    except OSError as e:
                        log.warning("%s: state.json не записан: %s", self.role, e.strerror or type(e).__name__)
                    last_write = now
                if now - last_notify >= NOTIFY_PERIOD_S:
                    if not self._stalled():             # пинг только пока цикл жив
                        sdnotify.notify("WATCHDOG=1", f"STATUS={self.status_line()}")
                    last_notify = now
                self.stop_event.wait(tick_s)
        finally:
            sdnotify.stopping("stopping")
            with self.lock:
                self.shutdown_streams()
            if self.ctl is not None:
                self.ctl.close()
            try:
                self.state.write()
            except OSError:
                pass
        return 0


def finite_float(v, what: str) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise bad(f"{what}: конечное число")
    return float(v)


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def parse_runtime_arg(argv) -> str | None:
    """--runtime-dir DIR (или --runtime-dir=DIR); остальное — ошибка использования."""
    d = None
    it = iter(argv)
    for a in it:
        if a == "--runtime-dir":
            d = next(it, None)
            if not d:
                raise SystemExit(2)
        elif a.startswith("--runtime-dir="):
            d = a.split("=", 1)[1]
        else:
            raise SystemExit(2)
    return d


_ = Callable  # для аннотаций потомков
