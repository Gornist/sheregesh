# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Настройки внешнего модуля (ADR-0012, раздел 8): ConfigStore (ADR-0004) со схемой remote-1, файл module.yaml.

Атомарные ревизии, подтверждение и восстановление при повреждении — те же, что у конфигурации пульта. Ошибки
хранилища наружу не выходят: при любом сбое модуль запускается с заводскими значениями (в памяти, если сохранить
нельзя), причина — в журнале и в settings.source / settings.notice.
"""
import json
import os
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from ..config import ConfigError, ConfigStore, NoValidConfiguration, Schema
from ..config.defaults import remote_defaults

FILE = "module.yaml"
SCHEMA = "remote-1"


UART_DEFAULTS = {"device": "auto", "baud": 400000, "half_duplex": True}
NEUTRAL = (0,) * 8


@dataclass(frozen=True)
class ModuleSettings:
    enabled: bool = True
    profile: str = "radiomaster-nomad"
    backend: str = "lab-emulator"          # lab-emulator | crsf-pty-lab | crsf-uart (ADR-0014)
    auto_init: bool = True
    auto_recover: bool = True
    uart: tuple = tuple(UART_DEFAULTS.items())   # линия crsf-uart: (("device", …), ("baud", …), ("half_duplex", …))
    failsafe_ch: tuple = NEUTRAL           # каналы 1–8 режима выхода neutral, −1000…1000

    def __post_init__(self):
        uart = dict(self.uart) if isinstance(self.uart, (dict, tuple, list)) else {}
        object.__setattr__(self, "uart", tuple({**UART_DEFAULTS, **uart}.items()))
        object.__setattr__(self, "failsafe_ch", tuple(self.failsafe_ch))

    @classmethod
    def from_data(cls, d: dict) -> "ModuleSettings":
        """Данные module.yaml (уже прошедшие схему); незнакомые ключи будущих версий игнорируются."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def uart_dict(self) -> dict:
        return dict(self.uart)

    def to_yaml(self) -> str:
        return "".join(f"{k}: {_yaml(v)}\n" for k, v in self.as_dict().items())

    def replace(self, **kw) -> "ModuleSettings":
        return replace(self, **kw)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(uart=self.uart_dict(), failsafe_ch=list(self.failsafe_ch))
        return d


def _yaml(v):
    """Скаляр — как в S6 (true/false, строка без кавычек); вложенное — JSON (подмножество YAML)."""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


class SettingsStore:
    def __init__(self, root: Path | None, *, log, clock=time.time):
        self.root = Path(root) if root is not None else None
        self.log, self.clock = log, clock
        self.store = None                 # ConfigStore или None (настройки только в памяти)

    def _info(self, source, revision="", notice=""):
        return {"source": source, "revision": revision, "notice": notice}

    def load(self) -> tuple:
        """Блокирующая, при старте. → (ModuleSettings, {"source","revision","notice"}); исключений не выпускает."""
        if self.root is None:
            self.log("settings: no storage directory — factory defaults in memory")
            return ModuleSettings(), self._info("memory", notice="CFG-MEMORY")
        try:
            schema = Schema.load(SCHEMA)
            store = ConfigStore(self.root, schema, clock=self.clock)
            source, notice = "store", ""
            try:
                if not store.is_initialized():
                    store.initialize(remote_defaults())
                    source = "factory"
                    self.log(f"settings: {self.root} created with factory defaults")
                report = store.recover()
                if report.actions:
                    source = "recovered"
                    notice = ",".join(dict.fromkeys(code for code, _ in report.actions))
                    self.log(f"settings recovered: {notice}")
            except NoValidConfiguration as e:
                bad = self.root.with_name(f"{self.root.name}.corrupt-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(self.clock()))}")
                os.replace(self.root, bad)
                self.log(f"settings: no valid revision ({e}); {self.root} moved to {bad.name}, factory defaults")
                store = ConfigStore(self.root, schema, clock=self.clock)
                store.initialize(remote_defaults())
                store.recover()
                source, notice = "reset", "CFG-RESET"
            rev = store.active()
            settings = ModuleSettings.from_data(rev.data[FILE])
            self.store = store
            return settings, self._info(source, rev.id, notice)
        except (OSError, ConfigError, ValueError, KeyError, TypeError) as e:
            self.store = None
            self.log(f"settings: storage unavailable ({e}) — factory defaults in memory")
            return ModuleSettings(), self._info("memory", notice="CFG-MEMORY")

    def save(self, s: ModuleSettings, validate) -> tuple:
        """Блокирующая (fsync) — только в исполнителе. → ("ok"|"invalid"|"not-saved", info)."""
        if self.store is None:
            return "not-saved", {"text": "settings storage is not available"}
        why = validate(s)
        if why:
            return "invalid", {"text": why}

        def health_check(rev, _services):
            return validate(ModuleSettings.from_data(rev.data[FILE])) is None
        try:
            res = self.store.apply({FILE: s.to_yaml()}, health_check, reason="console module-cmd")
        except (OSError, ConfigError) as e:
            return "not-saved", {"text": f"settings not saved: {e}"[:200]}
        if not res.ok:
            text = "; ".join(map(str, res.issues)) or res.message
            return "invalid", {"text": text[:200]}
        self.log(f"settings revision {res.revision} saved")
        return "ok", self._info("store", res.revision)
