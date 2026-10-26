# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Выход CRSF удалённого блока на JR-модуль (S8, ADR-0014): протокол, порты, поток отправки.

frames — кадры CRSF (CRC8 0xD5, каналы 0x16, ping 0x28, device info 0x29, link statistics 0x14, синхронизация
0x3A.0x10) и потоковый разборщик, чистый Python без зависимостей; port — UartPort (termios2, полудуплекс с эхом),
PtyPort (псевдотерминал эмулятора ELRS TX), find_uart3; output — CrsfOutput: поток с абсолютным расписанием по периоду
модуля. Внешних библиотек нет (pyserial на образе отсутствует). Реэкспорт ленивый (PEP 562), как в sheregesh.module.
"""
_EXPORTS = {"CrsfOutput": "output", "UartPort": "port", "PtyPort": "port", "find_uart3": "port", "Parser": "frames",
            "Frame": "frames", "pack_channels": "frames", "unpack_channels": "frames", "ping": "frames"}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        import importlib
        return getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
