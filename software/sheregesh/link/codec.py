# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Кадрирование и проверка сообщений SHRG-LAB (ADR-0007): JSON UTF-8, одно сообщение на строку.

S1.5: только синтетические диагностические данные. Набор типов закрыт по версиям; команд управления нет
(1.2: выбор видеоисточника video-select — см. TYPES).
"""
import json
from dataclasses import dataclass

MAX_LINE = 64 * 1024
# Типы сообщений по версии протокола. Получатель ничего не исполняет по сообщениям:
# hello — рукопожатие; heartbeat/status/event — данные; error — диагностика; bye — завершение.
# 1.1 (S3.1): ack — подтверждение получения heartbeat/event пультом (измерение задержки и потерь); ничего не исполняет.
TYPES = {"1.0": frozenset({"hello", "heartbeat", "status", "event", "error", "bye"})}
TYPES["1.1"] = TYPES["1.0"] | {"ack"}
# 1.2 (S4): video-select — выбор видеоисточника блока пультом, body {"id": str, "req": int}. Единственное сообщение
# пульт → блок, меняющее состояние блока: только выбор из списка status.video.sources (без команд ОС, устройств и
# радиоинтерфейсов); результат — в телеметрии status.video.select. Сессия 1.1 (старый пульт или старый блок) его не
# отправляет и не исполняет (CHG-012).
TYPES["1.2"] = TYPES["1.1"] | {"video-select"}


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
