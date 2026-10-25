# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Источник видео для интерфейса пульта: что за вход, в каком он состоянии и что показывать в кадре.

Данные приходят из трёх мест, интерфейс видит одно описание (describe) и одно решение для кадра (display):
  * удалённый блок — телеметрия (status_body["video"]) и INFO видеосеанса (sheregesh.videolink, VideoClient.info);
  * видеовход самого пульта (S3.3, sheregesh.videoinput.VideoSource.info());
  * локальный синтетический генератор (--local-video).

ТОЧКА ПОДКЛЮЧЕНИЯ КОНТРАКТА БЛОКА — remote_fields(). Ожидаемые поля (в телеметрии video.source и/или в INFO
видеосеанса под ключом "source"; объект или строка-тип входа):
  input   synthetic | hdmi | cvbs | usb | v4l2 | rtsp        тип входа (иной — показывается как есть)
  state   no-device | no-signal | starting | streaming | connect-error | error    состояние источника НА БЛОКЕ
  device  имя устройства / адрес потока (учётные данные из URL не показываются)
  error   текст причины (для no-device / no-signal / connect-error / error)
  width, height, fps   режим источника (номинальный), как в INFO 1.1
Принимаются и близкие имена (kind/type, status, name/url, reason/detail, resolution "WxH", framerate "30/1"),
чтобы окончательные имена у блока не требовали правки здесь. Блок 1.1 этих полей не передаёт: тогда вход —
синтетический (иного у 1.1 нет), legacy=True, состояние — по приёмнику пульта.
"""
import re

INPUTS = ("synthetic", "hdmi", "cvbs", "usb", "v4l2", "rtsp")
SOURCE_STATES = ("no-device", "no-signal", "starting", "streaming", "connect-error", "error")
BLOCKING = ("no-device", "no-signal", "connect-error", "error")      # кадра нет: чёрное поле и причина

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
                  "refused": "connect-error", "timeout": "connect-error", "failed": "error", "fault": "error"}
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
    state = _norm(d.get("state", d.get("status", d.get("source_state"))), _STATE_ALIASES, SOURCE_STATES)
    if not state and available is False:
        state = "no-device" if d.get("missing") else "error"
    return {"input": _norm(d.get("input", d.get("kind", d.get("type", d.get("source_type")))), _INPUT_ALIASES, INPUTS,
                           keep_unknown=True),
            "state": state,
            "device": scrub(d.get("device") or d.get("name") or d.get("url") or d.get("uri") or d.get("id") or ""),
            "error": scrub(d.get("error") or d.get("reason") or d.get("detail") or d.get("message") or ""),
            "width": int(_num(w)), "height": int(_num(h)), "fps": round(_num(d.get("fps", d.get("framerate"))), 2)}


def remote_fields(telemetry_video, session_info):
    """Блок → поля источника. Режим (width/height/fps) — сначала из INFO видеосеанса (как в 1.1), состояние и
    причина — из телеметрии (она периодическая, поэтому свежее), недостающее — из другого источника."""
    tel = _fields(_source_part(telemetry_video))
    inf = _fields(_source_part(session_info))
    out = {}
    for k in ("input", "state", "device", "error"):
        out[k] = tel[k] or inf[k]
    for k in ("width", "height", "fps"):
        out[k] = inf[k] or tel[k]
    return out


def empty(origin="none"):
    return {"origin": origin, "input": "", "reported": False, "legacy": False, "state": "", "device": "",
            "error": "", "width": 0, "height": 0, "fps": 0.0}


def describe(mode, telemetry_video=None, session_info=None, local_info=None, rx_state="off", rx_error=""):
    """mode: remote | local-input | local-synthetic | off. Возвращает словарь для QML (все ключи всегда есть)."""
    if mode == "remote":
        f = remote_fields(telemetry_video or {}, session_info or {})
        out = {**empty("remote"), **f}
        out["reported"] = bool(f["input"])
        if not f["input"] and (telemetry_video or session_info):
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


def display(source, rx_state):
    """Что в кадре. state — код для подписи; live — показывать ли кадры. Состояние источника на блоке (нет
    сигнала, нет устройства, ошибка подключения) важнее приёмника: кадры, если они и идут (заставка блока или
    последний кадр), не выдаются за актуальное видео."""
    st = source.get("state", "")
    if st in BLOCKING and rx_state != "unavailable":
        return {"display": st, "live": False}
    if rx_state == "playing":
        return {"display": "playing", "live": True}
    if st == "starting" and rx_state in ("waiting", "restarting", "no-video", "off"):
        return {"display": "starting", "live": False}
    return {"display": rx_state or "off", "live": False}
