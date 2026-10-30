# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Источник видео для интерфейса пульта: что за вход, в каком он состоянии и что показывать в кадре.

Данные приходят из трёх мест, интерфейс видит одно описание (describe) и одно решение для кадра (display):
  * удалённый блок — телеметрия (status_body["video"]);
  * видеовход самого пульта (S3.3, sheregesh.videoinput.VideoSource.info());
  * локальный синтетический генератор (--local-video).

ТОЧКА ПОДКЛЮЧЕНИЯ КОНТРАКТА БЛОКА — remote_fields(). Ожидаемые поля (в телеметрии video.source; объект или
строка-тип входа):
  input   synthetic | hdmi | cvbs | usb | v4l2 | rtsp        тип входа (иной — показывается как есть)
  state   no-device | no-signal | starting | streaming | connect-error | error    состояние источника НА БЛОКЕ
  device  имя устройства / адрес потока (учётные данные из URL не показываются)
  error   текст причины (для no-device / no-signal / connect-error / error)
  width, height, fps   режим источника (номинальный)
Принимаются и близкие имена (kind/type, status, name/url, reason/detail, resolution "WxH", framerate "30/1"),
чтобы окончательные имена у блока не требовали правки здесь. Блок 1.1 этих полей не передаёт: тогда вход —
синтетический (иного у 1.1 нет), legacy=True, состояние — по приёмнику пульта.

«Переподключение» (reconnecting) — состояние для экрана, не из контракта SOURCE_STATES: канал управления с блоком
потерян и восстанавливается (peer_link RECONNECTING; сведения блока устарели, кадр не актуален) либо блок сам сообщает
reconnecting/retrying.

СПИСОК ИСТОЧНИКОВ — sources(): телеметрия status.video.sources, ≤ 16 объектов {id, input, name, available, active};
id — стабильная строка ("synthetic", "v4l2:…", "rtsp:<n>"), выбор — PeerClient.select_video_source(id). Блок без
sources (1.1 и ранние 1.x) — список пуст: только синтетический источник, выбор с пульта недоступен.

ПЕРЕХОД ИСТОЧНИКА — Handover (S5 GUI-01). Приёмник пульта говорит «playing» ещё NO_VIDEO_AFTER_S после последнего
кадра, а кадры прежнего источника, уже бывшие в пути, приходят и после его остановки на блоке. Поэтому после смены
входа/устройства, перезапуска конвейера на блоке (restarts), запуска источника (state → starting) и выбора с пульта
«Воспроизведение» — только когда приёмник декодировал кадры, пришедшие ПОСЛЕ перехода (VideoReceiver.frames_since);
до того на экране «Подключение» (display "connecting"). Подтверждение блока (streaming) не ждётся: блок сообщает его
на 0,3–1,7 с позже фактических кадров.
"""
import re
import time

INPUTS = ("synthetic", "hdmi", "cvbs", "usb", "v4l2", "rtsp")
SOURCE_STATES = ("no-device", "no-signal", "starting", "streaming", "connect-error", "error")
BLOCKING = ("no-device", "no-signal", "connect-error", "error")      # кадра нет: чёрное поле и причина
RECONNECTING = "reconnecting"                                        # только для экрана (см. выше)
CONNECTING = "connecting"                    # только для экрана: переход источника, кадров нового ещё нет (Handover)
MAX_SOURCES = 16
FLUSH_S = 0.5            # кадры прежнего источника в пути (сеть, буфер джиттера 50 мс, декодер) приходят не позже
NEW_FRAMES = 2           # столько кадров после перехода — источник подтверждён кадрами (≈ 0,07 с при 30 кадр/с)
SELECT_GRACE_S = 2.5     # блок подтвердил выбор (active), а сведения источника ещё прежние — ждать смены не дольше

_INPUT_ALIASES = {"uvc": "usb", "usb-uvc": "usb", "webcam": "usb", "composite": "cvbs", "analog": "cvbs",
                  "pal": "cvbs", "ntsc": "cvbs", "av": "cvbs", "hdmi-csi": "hdmi", "csi-hdmi": "hdmi",
                  "tc358743": "hdmi", "test": "synthetic", "videotestsrc": "synthetic", "testsrc": "synthetic",
                  "pattern": "synthetic", "rtp": "rtsp", "network": "rtsp", "ip": "rtsp", "v4l": "v4l2"}
_STATE_ALIASES = {"playing": "streaming", "ok": "streaming", "active": "streaming", "running": "streaming",
                  "stream": "streaming", "live": "streaming", "signal": "streaming",
                  "nosignal": "no-signal", "no_signal": "no-signal", "signal-lost": "no-signal", "lost": "no-signal",
                  "missing": "no-device", "absent": "no-device", "no_device": "no-device", "nodevice": "no-device",
                  "not-found": "no-device", "connecting": "starting", "probing": "starting", "start": "starting",
                  "waiting": "starting", "connect_error": "connect-error", "unreachable": "connect-error",
                  "refused": "connect-error", "timeout": "connect-error", "failed": "error", "fault": "error",
                  "retrying": RECONNECTING, "reconnect": RECONNECTING, "re-connecting": RECONNECTING}
_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")


def scrub(text):
    """Без учётных данных в адресах (rtsp://user:pass@host → rtsp://host): экран видят посторонние."""
    return _CREDS.sub(r"\1", str(text or ""))[:300]


def _norm(value, aliases, allowed, keep_unknown=False):
    v = str(value or "").strip().lower().replace(" ", "-")
    v = aliases.get(v, v)
    if v in allowed:
        return v
    return re.sub(r"[^a-z0-9+._-]", "", v)[:24] if keep_unknown else ""   # новый тип входа — показать как есть


def _num(value):
    try:
        return float(str(value).split("/")[0]) / (float(str(value).split("/")[1]) if "/" in str(value) else 1.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _source_part(d):
    """Поля источника из словаря блока: вложенный "source" (объект или строка) поверх верхнего уровня."""
    if not isinstance(d, dict):
        return {}
    out = dict(d)
    src = d.get("source")
    if isinstance(src, dict):
        out.update(src)
    elif isinstance(src, str):
        out["input"] = src
    return out


def _fields(d):
    """Нормализованные поля одного словаря; пустые значения — «не сообщено»."""
    w, h = d.get("width"), d.get("height")
    res = d.get("resolution")
    if (not w or not h) and isinstance(res, str) and "x" in res.lower().replace("×", "x"):
        w, h = res.lower().replace("×", "x").split("x", 1)
    available = d.get("available")
    state = _norm(d.get("state", d.get("status", d.get("source_state"))), _STATE_ALIASES,
                  SOURCE_STATES + (RECONNECTING,))
    if not state and available is False:
        state = "no-device" if d.get("missing") else "error"
    return {"input": _norm(d.get("input", d.get("kind", d.get("type", d.get("source_type")))), _INPUT_ALIASES, INPUTS,
                           keep_unknown=True),
            "state": state,
            "device": scrub(d.get("device") or d.get("name") or d.get("url") or d.get("uri") or d.get("id") or ""),
            "error": scrub(d.get("error") or d.get("reason") or d.get("detail") or d.get("message") or ""),
            "width": int(_num(w)), "height": int(_num(h)), "fps": round(_num(d.get("fps", d.get("framerate"))), 2),
            "restarts": max(0, int(_num(d.get("restarts"))))}


def remote_fields(telemetry_video):
    """Блок → поля источника из телеметрии status.video.source."""
    tel = _fields(_source_part(telemetry_video))
    return {k: tel[k] for k in ("input", "state", "device", "error", "width", "height", "fps", "restarts")}


def sources(telemetry_video):
    """status.video.sources → список для экрана: не больше MAX_SOURCES, id — непустая строка без повторов, имя без
    учётных данных, активный — не больше одного. Данные блока недоверенные: лишнее отбрасывается."""
    raw = telemetry_video.get("sources") if isinstance(telemetry_video, dict) else None
    if not isinstance(raw, list):
        return []
    out, seen, active = [], set(), False
    for d in raw:
        if not isinstance(d, dict) or not isinstance(d.get("id"), str):
            continue
        sid = d["id"].strip()[:96]
        if not sid or sid in seen:
            continue
        seen.add(sid)
        is_active = d.get("active") is True and not active
        active = active or is_active
        out.append({"id": sid, "input": _norm(d.get("input", d.get("kind", d.get("type"))), _INPUT_ALIASES, INPUTS,
                                              keep_unknown=True),
                    "name": scrub(d.get("name") or sid)[:64], "available": d.get("available") is not False,
                    "active": is_active})
        if len(out) >= MAX_SOURCES:
            break
    return out


def empty(origin="none"):
    return {"origin": origin, "input": "", "reported": False, "legacy": False, "state": "", "device": "",
            "error": "", "width": 0, "height": 0, "fps": 0.0, "restarts": 0}


def describe(mode, telemetry_video=None, *, local_info=None, rx_state="off", rx_error="", link=""):
    """mode: remote | local-input | local-synthetic | off. link — peer_link канала управления (для remote).
    Возвращает словарь для QML (все ключи всегда есть)."""
    if mode == "remote":
        if link == "RECONNECTING":            # связь восстанавливается: прежние сведения блока не выдаются за текущие
            return {**empty("remote"), "state": RECONNECTING}
        f = remote_fields(telemetry_video or {})
        out = {**empty("remote"), **f}
        out["reported"] = bool(f["input"])
        if not f["input"] and telemetry_video:
            out["input"], out["legacy"] = "synthetic", True          # блок 1.1: только синтетический генератор
        return out
    if mode == "local-input":
        f = _fields(local_info or {})
        out = {**empty("local"), **f, "reported": bool(f["input"])}
        out["input"] = out["input"] or "usb"
        if rx_state == "no-device":
            out["state"] = "no-device"
        elif out["state"] in ("no-device", "error"):
            out["state"] = ""            # режим не поддерживается (rx unsupported) или устройство нашлось — решает приёмник
        out["error"] = scrub(out["error"] or (rx_error if out["state"] else ""))
        return out
    if mode == "local-synthetic":
        return {**empty("local"), "input": "synthetic", "reported": True, "width": 1280, "height": 720, "fps": 30.0}
    return empty()


def display(source, rx_state, fresh=True):
    """Что в кадре. state — код для подписи; live — показывать ли кадры. Состояние источника на блоке (нет
    сигнала, нет устройства, ошибка подключения) важнее приёмника: кадры, если они и идут (заставка блока или
    последний кадр), не выдаются за актуальное видео.

    fresh=False — переход источника не подтверждён кадрами (Handover): кадры, если приёмник их и показывает, —
    прежнего источника или из буфера, в кадре «Подключение». Если же кадров нет, а источник не запускается
    (приёмник «нет видео» / перезапуск декодера), показывается состояние приёмника (REQ-SW-002: «нет видео»).

    «Переподключение» канала управления — только когда кадров нет и по приёмнику (его «нет видео» — через
    NO_VIDEO_AFTER_S после последнего кадра): пока кадры идут (видео SRTP не зависит от канала управления),
    они актуальны. Иначе экран говорил «нет видео» раньше приёмника, и короткий разрыв (перезапуск блока быстрее
    NO_VIDEO_AFTER_S) не попадал в журнал (E2E-12; CI ubuntu, test_e2e_gui_link)."""
    st = source.get("state", "")
    if st == RECONNECTING and rx_state not in ("playing", "unavailable"):
        return {"display": st, "live": False}
    if st in BLOCKING and rx_state != "unavailable":
        return {"display": st, "live": False}
    if not fresh and rx_state != "unavailable":
        if rx_state in ("no-video", "restarting") and st != "starting":
            return {"display": rx_state, "live": False}
        return {"display": CONNECTING, "live": False}
    if rx_state == "playing":
        return {"display": "playing", "live": True}
    if st == "starting" and rx_state in ("waiting", "restarting", "no-video", "off"):
        return {"display": "starting", "live": False}
    return {"display": rx_state or "off", "live": False}


class Handover:
    """Переход источника удалённого блока: подтверждён ли он кадрами (см. «ПЕРЕХОД ИСТОЧНИКА» в начале модуля).

    Переход начинается, когда сведения блока сообщают смену входа/устройства или перезапуск конвейера (restarts),
    либо источник переходит в starting (запуск видеосеанса, смена источника), а также при выборе источника с пульта
    (select: до смены на блоке все кадры — прежнего источника). Кончается, когда приёмник декодировал NEW_FRAMES
    кадров позже guard. guard — момент замеченного перехода; если кадры шли в последние FLUSH_S (смена при идущем
    видео), — плюс FLUSH_S: кадры, уже бывшие в пути, не выдаются за новый источник. При обычном старте сеанса кадров
    до перехода нет, guard — сам момент перехода: кадры показываются, как только реально идут.

    Без Qt и без сети: часы подменяются (clock), кадры — функцией frames_since(t) (VideoReceiver.frames_since)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._key = self._state = None
        self.pending = False             # переход идёт: кадры нового источника ещё не подтверждены
        self.waiting = False             # выбор с пульта отправлен, блок ещё не сменил источник
        self.guard = 0.0
        self.reason = ""                 # start | change | select — для журнала и испытаний
        self._confirmed_at = None

    def select(self):
        """Выбор источника с пульта отправлен."""
        self.pending, self.waiting, self.reason, self._confirmed_at = True, True, "select", None

    def confirmed(self):
        """Блок отметил выбранный источник активным (status.video.sources); сведения источника могут ещё быть
        прежними (телеметрия собрана до остановки старого конвейера) — их смены ждать не дольше SELECT_GRACE_S."""
        if self.waiting and self._confirmed_at is None:
            self._confirmed_at = self._clock()

    def cancel(self):
        """Выбор не состоялся (отказ, таймаут, потеря связи): источник прежний, его кадры актуальны."""
        if self.waiting:
            self.pending = self.waiting = False

    def _begin(self, reason, now, frames_since):
        flowing = frames_since(now - FLUSH_S) > 0
        self.pending, self.waiting, self.reason, self._confirmed_at = True, False, reason, None
        self.guard = now + FLUSH_S if flowing else now

    def update(self, source, frames_since):
        """source — describe("remote", …); возвращает fresh для display()."""
        now = self._clock()
        if source.get("reported") and source.get("state") != RECONNECTING:
            key = (source.get("input", ""), source.get("device", ""), source.get("restarts", 0))
            st = source.get("state", "")
            if self._key is not None and key != self._key:
                self._begin("change", now, frames_since)
            elif st == "starting" and self._state != "starting":
                self._begin("start", now, frames_since)
            self._key, self._state = key, st
        if self.waiting and self._confirmed_at is not None and now - self._confirmed_at > SELECT_GRACE_S:
            self._begin("select", now, frames_since)       # блок принял выбор, а смены источника так и не сообщил
        if self.pending and not self.waiting and frames_since(self.guard) >= NEW_FRAMES:
            self.pending = False
        return not self.pending
