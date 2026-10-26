# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Команды органов управления пульта на удалённом блоке (S7, ADR-0013): формат SHRG-LAB 1.4 и сторож свежести.

wire — схемы rc (пульт → блок) и rc-state (блок → пульт), чистый Python без зависимостей: импортируют клиент пульта
(sheregesh.link.client), агент блока и приёмка. receiver — ControlReceiver: состояние IDLE/ACTIVE/FAILSAFE по кадрам
rc, часы внедряются, потоков нет. Выхода управления нет: rc-state.output — только "none" (радиовыход — в ADR
аппаратного backend). Реэкспорт ленивый (PEP 562), как в sheregesh.module.
"""
_EXPORTS = {"ControlReceiver": "receiver", "RcFrame": "wire", "BadRc": "wire", "check_rc": "wire",
            "sanitize_rc_state": "wire"}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        import importlib
        return getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
