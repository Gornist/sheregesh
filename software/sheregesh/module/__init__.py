# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Внешний модуль удалённого блока (ADR-0012): менеджер, профили, бэкенды, настройки, формат SHRG-LAB.

S6 — только инфраструктура: опознание, инициализация, состояние, диагностика, восстановление и настройки.
Единственный бэкенд — лабораторный эмулятор; радиоуправляющих сигналов и выхода управления нет (D6, раздел 12.3).
Реэкспорт ленивый (PEP 562): sheregesh.module.wire остаётся без зависимостей для пульта и приёмки.
"""
_EXPORTS = {"ModuleManager": "manager", "ModuleService": "service", "State": "states", "ModuleSettings": "settings",
            "REGISTRY": "profiles"}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        import importlib
        return getattr(importlib.import_module(f".{_EXPORTS[name]}", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
