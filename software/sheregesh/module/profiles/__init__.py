# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Профили внешних модулей (ADR-0012, разделы 2 и 5): всё, что зависит от модели модуля.

Менеджер не знает ни модели, ни способа связи: идентичность, шаги и таймауты инициализации, период опроса — здесь.
Новый модуль — новый файл профиля и строка в реестре (register_profile); менеджер при этом не меняется.
"""
from dataclasses import dataclass

from .. import wire


@dataclass(frozen=True)
class ModuleProfile:
    id: str                    # "radiomaster-nomad"; ^[a-z0-9][a-z0-9-]{0,31}$
    vendor: str                # "RadioMaster"
    model: str                 # "Nomad"
    name: str                  # "RadioMaster Nomad"
    family: str                # "elrs" — справочно (D6 допускает упоминание)
    interface: str             # "uart-crsf" — справочно; в S6 не открывается
    init_steps: tuple = ("identify", "read-config", "check-version")
    init_timeout_s: float = 5.0
    ping_interval_s: float = 1.0
    response_timeout_s: float = 3.0

    def __post_init__(self):
        if not wire.PROFILE_ID.fullmatch(self.id or ""):
            raise ValueError(f"bad profile id {self.id!r}")

    def matches(self, identity: dict) -> bool:
        """Модуль соответствует профилю: производитель и модель совпадают."""
        return isinstance(identity, dict) and identity.get("vendor") == self.vendor \
            and identity.get("model") == self.model

    def describe(self) -> dict:
        """Для телеметрии (status.module.profile)."""
        return {"id": self.id, "name": self.name, "family": self.family, "interface": self.interface}


REGISTRY: dict = {}


def register_profile(p: ModuleProfile) -> None:
    """Добавить (или заменить) профиль в реестре — для будущих профилей и тестов расширяемости."""
    if not isinstance(p, ModuleProfile):
        raise TypeError("ModuleProfile expected")
    REGISTRY[p.id] = p


def get_profile(profile_id: str) -> ModuleProfile:
    """Профиль по id; KeyError — нет профиля."""
    return REGISTRY[profile_id]


from .radiomaster_nomad import NOMAD  # noqa: E402 — профиль регистрируется после объявления реестра

register_profile(NOMAD)
DEFAULT_PROFILE = NOMAD.id
