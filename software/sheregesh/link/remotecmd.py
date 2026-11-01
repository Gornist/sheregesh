# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Обслуживание удалённого блока с пульта (протокол 1.6): выгрузка журналов, штатное выключение и разовый режим
разработчика блока.

remote-cmd   пульт → блок   {"req": int, "op": "log-export" | "shutdown" | "dev-arm", "confirm": true}
remote-log   блок → пульт   {"req", "part", "parts", "data"}  — часть журнала, base64, по порядку с 0
remote-cmd-result  блок → пульт  {"req", "op", "result": RESULTS, "error", для log-export ещё "name", "size",
             "sha256", "parts"} — после всех частей remote-log этого req

Журнал на блоке — journalctl служб «Шерегеша» за текущую загрузку, секреты заменены на [REDACTED]
(sheregesh.redact), не больше MAX_LOG байт (обрезается с начала, остаются последние записи). Пульт собирает части,
сверяет SHA-256 и пишет файл на «USB» с контрольной суммой рядом (REQ-SW-001/005). Выключение исполняется только с
confirm: true (подтверждение оператора на пульте, REQ-UI-006); блок отвечает итогом и через SHUTDOWN_DELAY_S
выключается: юнит sheregesh-remote-poweroff.service (правило polkit — только «start» этого юнита), в эмуляторе —
завершение процесса агента. dev-arm (REQ-UPD-005: «для удалённого блока — подтверждение на пульте») — тоже только с
confirm: true; блок вызывает своего агента OTA (sheregesh-ota-client dev-arm) из службы sheregesh-remote-net, а агент
сам проверяет, что вызывающий — служба устройства. Других команд ОС в протоколе нет."""
import base64
import hashlib

OPS = ("log-export", "shutdown", "dev-arm")
CONFIRMED = ("shutdown", "dev-arm")          # изменяющие: только с подтверждением оператора
RESULTS = ("ok", "error", "busy", "bad-request")
CHUNK = 32 * 1024                    # байт журнала в одной части: base64 ≈ 43 КБ < MAX_LINE 64 КБ
MAX_LOG = 2 * 1024 * 1024            # не больше 64 частей
MAX_PARTS = MAX_LOG // CHUNK
SHUTDOWN_DELAY_S = 1.0               # итог успевает уйти пульту до остановки служб


class BadCommand(ValueError):
    @property
    def text(self):
        return str(self)


def check_command(body) -> dict:
    """Тело remote-cmd → {"req", "op"}; BadCommand — не исполнять."""
    if not isinstance(body, dict):
        raise BadCommand("body is not an object")
    req, op = body.get("req"), body.get("op")
    if not isinstance(req, int) or isinstance(req, bool) or req < 1:
        raise BadCommand("req must be a positive integer")
    if op not in OPS:
        raise BadCommand(f"unknown op {op!r}")
    if op in CONFIRMED and body.get("confirm") is not True:
        raise BadCommand(f"{op} requires confirm: true")
    return {"req": req, "op": op}


def split(data: bytes) -> list[str]:
    """Журнал → части remote-log (base64); пустой журнал — одна пустая часть."""
    return [base64.b64encode(data[i:i + CHUNK]).decode("ascii") for i in range(0, len(data), CHUNK)] or [""]


def limit(data: bytes, cap: int = MAX_LOG) -> bytes:
    """Последние cap байт с начала целой строки (старые записи отбрасываются)."""
    if len(data) <= cap:
        return data
    tail = data[-cap:]
    nl = tail.find(b"\n")
    return tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail


class Assembly:
    """Сборка частей remote-log одного req на пульте. Части только по порядку; иное — ошибка сборки."""

    def __init__(self, req: int):
        self.req = req
        self.parts = []
        self.error = ""

    def add(self, body) -> None:
        if self.error:
            return
        part, parts, data = body.get("part"), body.get("parts"), body.get("data")
        if not (isinstance(part, int) and isinstance(parts, int) and isinstance(data, str)
                and 0 < parts <= MAX_PARTS and part == len(self.parts) and part < parts):
            self.error = "bad part"
            return
        try:
            self.parts.append(base64.b64decode(data, validate=True))
        except ValueError:
            self.error = "bad base64"

    def finish(self, result: dict) -> bytes:
        """Итог блока → данные журнала; ValueError — части не сходятся с итогом."""
        if self.error:
            raise ValueError(self.error)
        data = b"".join(self.parts)
        if result.get("parts") != len(self.parts) or result.get("size") != len(data):
            raise ValueError(f"incomplete: {len(self.parts)} of {result.get('parts')} parts")
        if hashlib.sha256(data).hexdigest() != result.get("sha256"):
            raise ValueError("checksum mismatch")
        return data
