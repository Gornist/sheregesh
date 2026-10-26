# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Состояния внешнего модуля, допустимые переходы, коды ошибок и таймауты (ADR-0012, раздел 4)."""
from dataclasses import dataclass
from enum import Enum

from . import wire


class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"      # канал к модулю есть или поднимается, модуль не обнаружен
    DETECTED = "DETECTED"              # модуль обнаружен, не инициализирован
    INITIALIZING = "INITIALIZING"      # идут шаги инициализации профиля
    READY = "READY"                    # опознан, соответствует профилю и отвечает на опрос (выхода управления нет)
    FAULT = "FAULT"                    # ошибка модуля или канала; код — в error
    RECOVERING = "RECOVERING"          # перезапуск канала и/или повторное обнаружение
    DISABLED = "DISABLED"              # выключен настройкой enabled: false; бэкенд остановлен

    def __str__(self):
        return self.value


assert tuple(s.value for s in State) == wire.STATES

S = State
# Переходы из начального состояния (до start()): таблица 4.2, строки «— (старт)».
INITIAL = frozenset({S.DISABLED, S.DISCONNECTED})
# Таблица 4.2. Любой другой переход — ошибка программы (ModuleManager._transition).
ALLOWED = {
    S.DISCONNECTED: frozenset({S.DETECTED, S.FAULT, S.RECOVERING, S.DISABLED}),
    S.DETECTED: frozenset({S.INITIALIZING, S.DISCONNECTED, S.FAULT, S.DISABLED}),
    S.INITIALIZING: frozenset({S.READY, S.FAULT, S.DISCONNECTED, S.DISABLED}),
    S.READY: frozenset({S.FAULT, S.DISCONNECTED, S.RECOVERING, S.DISABLED}),
    S.FAULT: frozenset({S.RECOVERING, S.DISABLED}),
    S.RECOVERING: frozenset({S.DETECTED, S.DISCONNECTED, S.FAULT, S.DISABLED}),
    S.DISABLED: frozenset({S.DISCONNECTED}),
}

# Причины переходов (reason): таблица 4.2; settings-profile / settings-backend — смена профиля или бэкенда
# (ADR-0014) перезапуском через DISABLED;
# internal-error — исключение внутри подсистемы (раздел 7.3).
REASONS = ("start", "settings-enabled", "settings-disabled", "settings-profile", "settings-backend", "present",
           "absent", "auto-init", "cmd-reinit", "cmd-reset", "init-ok", "init-error", "profile-mismatch", "init-timeout", "iface-fault",
           "no-response", "channel-down", "start-failed", "recover-attempt", "recover-timeout", "restart-limit",
           "internal-error")

# Коды ошибок (error.code), раздел 4.4
MOD_IFACE_ERROR = "MOD-IFACE-ERROR"
MOD_NO_RESPONSE = "MOD-NO-RESPONSE"
MOD_INIT_TIMEOUT = "MOD-INIT-TIMEOUT"
MOD_INIT_FAILED = "MOD-INIT-FAILED"
MOD_PROFILE_MISMATCH = "MOD-PROFILE-MISMATCH"
MOD_RECOVER_TIMEOUT = "MOD-RECOVER-TIMEOUT"
EMU_START_FAILED = "EMU-START-FAILED"
EMU_EXITED = "EMU-EXITED"
EMU_NO_KEEPALIVE = "EMU-NO-KEEPALIVE"
EMU_UNAVAILABLE = "EMU-UNAVAILABLE"
ERROR_CODES = wire.ERROR_CODES
assert {MOD_IFACE_ERROR, MOD_NO_RESPONSE, MOD_INIT_TIMEOUT, MOD_INIT_FAILED, MOD_PROFILE_MISMATCH, MOD_RECOVER_TIMEOUT,
        EMU_START_FAILED, EMU_EXITED, EMU_NO_KEEPALIVE, EMU_UNAVAILABLE} == set(ERROR_CODES)


@dataclass(frozen=True)
class Timings:
    """Таймауты канала и восстановления (раздел 4.3); таймауты модуля — в профиле (ModuleProfile)."""
    hello_timeout_s: float = 3.0              # дочерний процесс не прислал hello → EMU-START-FAILED
    keepalive_interval_s: float = 1.0
    keepalive_timeout_s: float = 3.0          # процесс жив, но молчит → убивается, EMU-NO-KEEPALIVE
    recover_timeout_s: float = 10.0           # RECOVERING без результата → FAULT MOD-RECOVER-TIMEOUT
    recover_backoff_s: tuple = (1, 2, 4, 8, 16, 30)   # задержка FAULT → RECOVERING; последняя повторяется
    restart_limit: int = 3                    # перезапуски процесса эмулятора в скользящем окне
    restart_window_s: float = 60.0
    stop_timeout_s: float = 1.0               # остановка: закрыть stdin → SIGTERM группе → SIGKILL
