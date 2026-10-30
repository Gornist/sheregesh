# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Координация видео пульта: какой поток показывает приёмник, что сказать о источнике и выбор источника на блоке.

VideoOps — единственный владелец состояния видео в пульте: видеовход из конфигурации (S3.3) и локальный
синтетический генератор, маршрут приёмника (служба SRTP, локальный UDP), описание источника для
экранов (videosource), переход источника (GUI-01), запрос выбора на блоке (S4), событие VIDEO-LOST и переходы видео
в журналах. Используется из потока интерфейса; сетевой вызов выбора — в отдельном потоке, итог — сигналом.

Backend остаётся Qt/QML-адаптером: его свойства videoSource/videoSources/videoSelection и слот selectVideoSource
отдают и вызывают это состояние. Точки подмены для испытаний — report() (сведения блока, как из
телеметрии) и peer() (клиент канала с select_video_source)."""
import os
import re
import signal
import socket
import subprocess
import threading

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from sheregesh import video as gst
from sheregesh import videoinput

from . import journal as jr
from . import videosource as vsrc

# Видеовход пульта (S3.3): необязательные поля system.yaml. Без них — прежнее поведение (синтетический поток).
VIDEO_DEFAULTS = {"video_source": "synthetic", "video_device": "", "video_mode": "1280x720@30", "video_format": "auto"}
SELECT_TIMEOUT_S = 15.0      # выбор источника видео: блок должен отметить новый источник активным за это время
# Ответ блока/клиента на выбор источника → код ошибки для экрана (VideoPage.selectErrorText)
_SELECT_ERRORS = (("unknown", ("unknown", "not-found", "no-such", "invalid-id", "bad-id")),
                  ("unavailable", ("unavailable", "not-available", "no-device", "offline")),
                  ("unsupported", ("unsupported", "not-supported", "not-implemented")),
                  ("no-link", ("not-connected", "no-link", "disconnected", "no-session")),
                  ("busy", ("busy", "in-progress")),
                  ("timeout", ("timeout", "timed-out")))


def video_config(system_yaml):
    return {k: system_yaml.get(k, v) for k, v in VIDEO_DEFAULTS.items()}


def select_error(result):
    """Итог вызова select_video_source → (код, подробность). Код "" — блок принял запрос. Окончательный вид ответа
    задаёт клиент канала (агент B), поэтому принимаются: None/True/"ok" — принято; False — отказ; dict {ok, error,
    detail|message}; строка-код; исключение (код из .code или текста)."""
    if isinstance(result, BaseException):
        code, detail = str(getattr(result, "code", "") or ""), str(result) or type(result).__name__
    elif result is None or result is True:
        return "", ""
    elif result is False:
        return "failed", ""
    elif isinstance(result, dict):
        if result.get("ok", not result.get("error")) is True:
            return "", ""
        code = str(result.get("error") or result.get("code") or "")
        detail = str(result.get("detail") or result.get("message") or "")
    elif isinstance(result, str):
        if result.strip().lower() in ("ok", "accepted", "switching", "selected"):
            return "", ""
        code, detail = result, ""
    else:
        return "", ""
    probe = re.sub(r"[\s_]+", "-", f"{code} {detail}".lower())
    for norm, keys in _SELECT_ERRORS:
        if any(k in probe for k in keys):
            return norm, vsrc.scrub(detail)[:200]
    return "failed", vsrc.scrub(detail or code)[:200]


class VideoOps(QObject):
    sourceChanged = Signal()            # display — источник видео для экранов (Backend.videoSourceChanged)
    sourcesChanged = Signal()           # список источников блока и ход выбора (Backend.videoSourcesChanged)
    _selectDone = Signal(int, object)   # из потока вызова select_video_source → в поток интерфейса

    def __init__(self, receiver, *, client, link_cfg, videorx, events, status, log, journal, local_video=False,
                 scan=None, parent=None):
        """receiver — VideoReceiver; client() — клиент канала или None; videorx — видеосеанс SRTP или None;
        events — события монитора (inject/clear); status() — status Backend (None до первого обновления);
        log(level, text) — журнал приложения; journal — системный журнал (E2E-12)."""
        super().__init__(parent)
        self.video = receiver
        self._client = client
        self._link_cfg = link_cfg
        self._videorx = videorx
        self._events = events
        self._status = status
        self._log = log
        self._journal = journal
        self._local_video = local_video
        self._scan = dict(scan or {})
        self.source = None                          # видеовход из конфигурации: synthetic | uvc | disabled
        self.local_sender = None                    # процесс локального синтетического генератора
        self._applied = None
        self.display = {**vsrc.empty(), **vsrc.display(vsrc.empty(), "off")}
        self.remote_telemetry = None                # video из телеметрии блока, пока сеанс управления есть
        self._peer_link = ""                        # peer_link канала управления (для «Переподключение» на «Видео»)
        self.sources = []                           # status.video.sources блока (videosource.sources)
        self._sel = {"state": "idle", "id": "", "name": "", "error": "", "detail": ""}
        self._sel_seq = 0
        self._sel_blocker = None
        self._handover = vsrc.Handover()            # переход источника блока: подтверждён ли он кадрами (GUI-01)
        self._last_state = None
        self._journal_class = None
        self._selectDone.connect(self._on_select_done)
        receiver.stateChanged.connect(self.journal_state)
        receiver.stateChanged.connect(self.update)
        receiver.frameChanged.connect(self._on_frame)

    # ---------- видеовход из конфигурации (S3.3) ----------
    def apply(self, cfg):
        """Видеоисточник из конфигурации: synthetic — прежний режим (поток удалённого блока или локальный
        генератор), uvc — локальное устройство V4L2/UVC, disabled — видео выключено. Устройство UVC опрашивается и
        захватывается только при явном выборе uvc."""
        if cfg == self._applied:
            return
        self._applied = cfg
        self.source = cfg["video_source"]
        if self.source == "uvc":
            self.stop_local_sender()
            dev, mode, fmt, scan = cfg["video_device"], cfg["video_mode"], cfg["video_format"], self._scan
            self.video.configure_local((dev, mode, fmt), lambda: videoinput.open_uvc(dev, mode, fmt, **scan))
            self._log("info", f"source uvc device={dev or 'auto'} mode={mode} format={fmt}")
        elif self.source == "disabled":
            self.stop_local_sender()
            self.video.configure(None, None)
            self._log("info", "source disabled")
        else:
            self.video.configure(None, None)      # поток удалённого блока подключится в on_link
            if self._local_video:
                self._start_local_video()

    def _start_local_video(self):
        self.stop_local_sender()
        codec = gst.choose_codec()
        if codec is None:
            self._log("error", "no working synthetic video codec")
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        self.video.configure(codec, port)
        self.local_sender = gst.spawn(gst.sender_args(codec, port), stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self._log("info", f"local synthetic generator {codec} on 127.0.0.1:{port}")

    def stop_local_sender(self):
        if self.local_sender is not None:
            try:
                os.killpg(self.local_sender.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.local_sender.wait()
            self.local_sender = None

    # ---------- маршрут приёмника по состоянию канала ----------
    def on_link(self, peer_state, snap, vid, peer_link):
        """Такт Backend: телеметрия video блока, снимок клиента канала и peer_link → поток приёмника, источник для
        экранов, событие VIDEO-LOST."""
        client = self._client()
        if client is not None:
            vrx = self._videorx
            if self.source != "synthetic":
                pass                                # локальный видеовход или видео выключено (S3.3)
            elif vrx is not None and vrx.active and peer_state == "connected":
                # Видеосеанс SRTP (ADR-0016): кадры отдаёт служба rx — единственный удалённый видеотранспорт
                self.video.configure_service(vrx.codec, vrx.frames_path, vrx.stats)
            elif self._link_cfg is not None:
                if self.video.state != "off":       # сетевой профиль без сеанса SRTP: видео нет
                    self.video.configure(None, None)
            elif vid.get("codec") and vid.get("port") and peer_state == "connected":
                self.video.configure(vid["codec"], vid["port"])
        self.remote_telemetry = vid if client is not None and peer_state == "connected" else None
        self._peer_link = peer_link if client is not None else ""
        self.update()
        # Пропадание видео — диагностическое событие, видно на главной панели и в диагностике
        expected = (client is not None and bool(vid.get("enabled")) and peer_state == "connected") or \
            self.local_sender is not None
        expected = {"uvc": True, "disabled": False}.get(self.source, expected)
        if (self.video.state in ("no-video", "restarting", "no-device", "unsupported")
                or self.display["display"] in vsrc.BLOCKING) and expected:   # на блоке нет сигнала/устройства
            self._events.inject("VIDEO-LOST", state=self.display["display"])
        elif self.display["live"] or not expected:
            self._events.clear("VIDEO-LOST")
        if self.video.state != self._last_state:
            self._log("info" if self.video.state in ("playing", "waiting", "off") else "warning",
                      f"state {self.video.state} {self.video.error}".strip())
            self._last_state = self.video.state

    @Slot()
    def journal_state(self):
        """Наличие видеопотока — по сигналу приёмника, а не по такту бэкенда: обрыв короче такта (0,125–0,5 с)
        при опросе по такту в журнал не попадал (CI ubuntu, E2E-12)."""
        cls = jr.video_class(self.video.state)
        if cls is None or cls == self._journal_class:
            return
        src = self.source
        if cls == "playing":
            how = "restored" if self._journal_class == "no-stream" else "started"
            self._journal.emit(jr.INFO, f"video: stream {how} (codec={self.video.codec or '-'} source={src})")
        elif cls == "no-stream":
            self._journal.emit(jr.WARNING, f"video: no stream (state={self.video.state} "
                                           f"error={self.video.error or '-'} source={src})")
        elif self._journal_class is not None:
            self._journal.emit(jr.NOTICE, f"video: off (source={src})")
        self._journal_class = cls

    # ---------- источник для экранов ----------
    def report(self):
        """Откуда сведения об источнике: режим и сырые данные для videosource.describe."""
        if self.source == "uvc":
            return {"mode": "local-input", "local_info": self.video.source}
        if self.source == "disabled":
            return {"mode": "off"}
        if self.local_sender is not None:
            return {"mode": "local-synthetic"}
        if self._client() is not None:
            return {"mode": "remote", "telemetry_video": self.remote_telemetry, "link": self._peer_link}
        return {"mode": "off"}

    @Slot()
    def update(self):
        report = self.report()
        src = vsrc.describe(rx_state=self.video.state, rx_error=self.video.error, **report)
        self._update_sources(vsrc.sources(report.get("telemetry_video"))
                             if report["mode"] == "remote" and src["state"] != vsrc.RECONNECTING else [])
        # кадры прежнего источника (буфер, NO_VIDEO_AFTER_S приёмника) не выдаются за кадры нового (GUI-01)
        fresh = self._handover.update(src, self.video.frames_since) if report["mode"] == "remote" else True
        src.update(vsrc.display(src, self.video.state, fresh))
        if self._sel["state"] == "switching" and src["display"] in ("playing", vsrc.CONNECTING):
            # до подтверждения блоком (активный id в телеметрии) кадры — прежнего источника: не выдаются за новый
            src.update(display="switching", live=False)
        if src != self.display:
            # переходы состояния источника (у приёмника — свой журнал «state …» в on_link)
            source_level = vsrc.BLOCKING + ("starting", vsrc.CONNECTING)
            if src["display"] != self.display["display"] and (src["display"] in source_level or
                                                               self.display["display"] in source_level):
                self._log("warning" if src["display"] in vsrc.BLOCKING else "info",
                          f"source {src['input'] or '-'} {src['display']} {src['error']}".strip())
            self.display = src
            self.sourceChanged.emit()

    @Slot()
    def _on_frame(self):
        """Переход источника ждёт кадров нового (GUI-01): решение — с первым же подходящим кадром, а не с тактом
        бэкенда или очередным уведомлением приёмника (до 0,5 с). Вне перехода кадры экран не пересчитывают."""
        if self._handover.pending and not self._handover.waiting:
            self.update()

    # ---------- выбор источника видео на блоке (S4) ----------
    def peer(self):
        """Клиент канала с блоком, у которого вызывается select_video_source (для испытаний подменяется)."""
        return self._client()

    def _selector(self):
        return getattr(self.peer(), "select_video_source", None)

    def blocker(self):
        """Почему выбор сейчас невозможен: "" | local | no-link | no-list | unsupported."""
        report = self.report()
        if report["mode"] != "remote":
            return "local"
        status = self._status() or {}
        if report.get("link") == "RECONNECTING" or (
                self._client() is not None and status.get("peerState", "connected") != "connected"):
            return "no-link"
        if not self.sources:
            return "no-list"
        if not callable(self._selector()):
            return "unsupported"
        return ""

    @property
    def selection(self):
        """Ход выбора: state idle|switching|error, id/name выбираемого, error — код, detail — текст блока;
        blocker — почему выбор недоступен ("" — доступен)."""
        return {**self._sel, "blocker": self.blocker()}

    def _set_selection(self, **kw):
        new = {**self._sel, **kw}
        if new != self._sel:
            self._sel = new
            self.sourcesChanged.emit()

    def _update_sources(self, sources):
        changed = sources != self.sources
        self.sources = sources
        blocker = self.blocker() if self._status() is not None else "local"
        changed = changed or blocker != self._sel_blocker
        self._sel_blocker = blocker
        switching = self._sel["state"] == "switching"
        if switching and any(x["id"] == self._sel["id"] and x["active"] for x in sources):
            self._log("info", f"source selected {self._sel['id']}")
            self._sel_seq += 1                      # ответ/таймаут прежнего запроса больше не нужен
            self._handover.confirmed()
            self._set_selection(state="idle", error="", detail="")
        elif switching and blocker == "no-link":   # связь потеряна во время переключения: итог неизвестен
            self._sel_seq += 1
            self._handover.cancel()
            self._set_selection(state="error", error="no-link", detail="")
        elif changed:
            self.sourcesChanged.emit()

    def select(self, source_id, confirmed):
        """Выбор источника на блоке — только после подтверждения на экране (без случайных переключений). True —
        запрос отправлен (или источник уже активен); итог — в selection."""
        if not confirmed or self._sel["state"] == "switching":
            return False
        src = next((x for x in self.sources if x["id"] == source_id), None)
        name = src["name"] if src else vsrc.scrub(source_id)[:64]
        blocker = self.blocker()
        err = {"no-link": "no-link", "unsupported": "unsupported", "local": "unsupported",
               "no-list": "unsupported"}.get(blocker, "")
        if not err and src is None:
            err = "unknown"
        elif not err and not src["available"]:
            err = "unavailable"
        if err:
            self._log("warning", f"source select {source_id}: {err}")
            self._set_selection(state="error", id=source_id, name=name, error=err, detail="")
            return False
        if src["active"]:
            self._set_selection(state="idle", id=source_id, name=name, error="", detail="")
            return True
        self._sel_seq += 1
        seq, call = self._sel_seq, self._selector()
        self._log("info", f"source select {source_id} ({src['input'] or '-'})")
        self._set_selection(state="switching", id=source_id, name=name, error="", detail="")
        self._handover.select()
        self.update()

        def run():                                  # вызов идёт по сети — не в потоке интерфейса
            try:
                result = call(source_id)
            except Exception as e:                  # noqa: BLE001 — любая ошибка клиента показывается текстом
                result = e
            self._selectDone.emit(seq, result)
        threading.Thread(target=run, name="video-select", daemon=True).start()
        QTimer.singleShot(int(SELECT_TIMEOUT_S * 1000), lambda: self._on_select_done(seq, {"ok": False, "error": "timeout"}))
        return True

    @Slot(int, object)
    def _on_select_done(self, seq, result):
        if seq != self._sel_seq or self._sel["state"] != "switching":
            return                                  # устаревший ответ или уже подтверждено телеметрией
        code, detail = select_error(result)
        if code:
            self._sel_seq += 1
            self._log("warning", f"source select {self._sel['id']}: {code} {detail}".strip())
            self._handover.cancel()
            self._set_selection(state="error", error=code, detail=detail)
        self.update()                               # блок мог уже отметить источник активным; снять «Переключение»
