# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Кадрирование и проверка сообщений SHRG-LAB (ADR-0007): JSON UTF-8, одно сообщение на строку.

Набор типов закрыт по версиям: сообщение исполняется, только если его тип есть в версии сессии (CHG-012).
1.2: выбор видеоисточника video-select; 1.3: команда подсистеме внешнего модуля module-cmd; 1.4: кадры органов
управления rc и их состояние на блоке rc-state — см. TYPES.
"""
import json
from dataclasses import dataclass

MAX_LINE = 64 * 1024
# Типы сообщений по версии протокола. 1.0–1.1 — без команд:
# hello — рукопожатие; heartbeat/status/event — данные; error — диагностика; bye — завершение.
# 1.1 (S3.1): ack — подтверждение получения heartbeat/event пультом (измерение задержки и потерь); ничего не исполняет.
TYPES = {"1.0": frozenset({"hello", "heartbeat", "status", "event", "error", "bye"})}
TYPES["1.1"] = TYPES["1.0"] | {"ack"}
# 1.2 (S4): video-select — выбор видеоисточника блока пультом, body {"id": str, "req": int}. Единственное сообщение
# пульт → блок, меняющее состояние блока: только выбор из списка status.video.sources (без команд ОС, устройств и
# радиоинтерфейсов); результат — в телеметрии status.video.select. Сессия 1.1 (старый пульт или старый блок) его не
# отправляет и не исполняет (CHG-012).
TYPES["1.2"] = TYPES["1.1"] | {"video-select"}
# 1.3 (S6, ADR-0012): module-cmd — команда подсистеме внешнего модуля блока, body {"req": int, "op": str, "args": {}}:
# status | reinit | reset | configure (enabled, auto_init, auto_recover, profile) | lab (лабораторный сценарий
# эмулятора, только при лабораторном бэкенде). Каналов и значений органов управления в module-cmd нет (они —
# в rc, 1.4); выхода на модуль в S6 нет (status.module.output = "none"). Состояние — в status.module, итог — в status.module.cmd. Сессия 1.2
# (старый пульт или старый блок) его не отправляет и не исполняет, status.module не передаётся (CHG-012).
TYPES["1.3"] = TYPES["1.2"] | {"module-cmd"}
# 1.4 (S7, ADR-0013): rc — кадр органов управления пульта, до 50 Гц, body {"n", "en", "ch"[8], "btn", "dev"}
# (sheregesh.control.wire.check_rc); rc-state — состояние команд на блоке IDLE | ACTIVE | FAILSAFE, 10 Гц (IDLE — 1 Гц)
# и сразу при переходе. Блок отдаёт значения сторожу свежести (sheregesh.control.receiver), выхода управления нет
# (rc-state.output = "none"). Сессия 1.3 (старый пульт или старый блок) rc не отправляет и не исполняет, rc-state не
# передаётся (CHG-012).
TYPES["1.4"] = TYPES["1.3"] | {"rc", "rc-state"}


@dataclass(frozen=True)
class BadMessage:
    reason: str        # not-utf8 | not-json | not-object | fields | too-long
    sample: str


def encode(proto: str, mtype: str, seq: int, ts: float, body: dict) -> bytes:
    line = json.dumps({"proto": proto, "type": mtype, "seq": seq, "ts": round(ts, 3), "body": body},
                      ensure_ascii=False, separators=(",", ":"))
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("сообщение превышает MAX_LINE")
    return data


def _check(obj):
    if not isinstance(obj, dict):
        return "not-object"
    if not (isinstance(obj.get("proto"), str) and isinstance(obj.get("type"), str)
            and isinstance(obj.get("seq"), int) and not isinstance(obj.get("seq"), bool) and obj["seq"] >= 0
            and isinstance(obj.get("ts"), (int, float)) and isinstance(obj.get("body"), dict)):
        return "fields"
    return None


class Decoder:
    """Накопитель потока байт → сообщения (dict) и BadMessage. Слишком длинная строка отбрасывается целиком."""

    def __init__(self):
        self._buf = bytearray()
        self._skipping = False

    def feed(self, data: bytes):
        out = []
        self._buf += data
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                if len(self._buf) > MAX_LINE:
                    out.append(BadMessage("too-long", bytes(self._buf[:40]).decode("utf-8", "replace")))
                    self._buf.clear()
                    self._skipping = True          # остаток этой строки до \n тоже отбросить
                return out
            raw = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if self._skipping:
                self._skipping = False
                continue
            if not raw.strip():
                continue
            if len(raw) > MAX_LINE:
                out.append(BadMessage("too-long", raw[:40].decode("utf-8", "replace")))
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                out.append(BadMessage("not-utf8", raw[:40].decode("utf-8", "replace")))
                continue
            try:
                obj = json.loads(text)
            except ValueError:
                out.append(BadMessage("not-json", text[:40]))
                continue
            reason = _check(obj)
            out.append(BadMessage(reason, text[:40]) if reason else obj)
