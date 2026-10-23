# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Чтение и проверка набора YAML-файлов по схеме (REQ-CFG-001, ADR-0004)."""
import json
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import yaml

SCHEMA_DIR = Path(__file__).with_name("schemas")


@dataclass(frozen=True)
class ValidationIssue:
    file: str
    path: str          # путь к полю: "temp_warning_c", "" — весь файл
    kind: str          # класс ошибки: yaml, type, enum, required, extra, range, pattern, missing-file, unknown-file, semantic
    message: str

    def __str__(self):
        loc = f"{self.file}:{self.path}" if self.path else self.file
        return f"{loc}: [{self.kind}] {self.message}"


_KINDS = {"type": "type", "enum": "enum", "required": "required", "additionalProperties": "extra",
          "minimum": "range", "maximum": "range", "minLength": "range", "maxLength": "range", "pattern": "pattern"}


class Schema:
    """Схема набора конфигурации одного типа устройства."""

    def __init__(self, data: dict):
        self.data = data
        self.version = data["schema_version"]
        self.device = data["device"]
        self.files = data["files"]
        self._validators = {name: jsonschema.Draft202012Validator(spec["schema"]) for name, spec in self.files.items()}

    @classmethod
    def load(cls, name="console-1"):
        return cls(json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8")))

    def services_for(self, filenames):
        """Службы, которые нужно перезапустить и проверить при изменении этих файлов."""
        out = []
        for f in filenames:
            for s in self.files[f].get("x-services", []):
                if s not in out:
                    out.append(s)
        return out

    def parse(self, files: dict):
        """Разбирает {имя: текст YAML}. Возвращает ({имя: данные}, [ValidationIssue])."""
        parsed, issues = {}, []
        for name in self.files:
            if name not in files:
                issues.append(ValidationIssue(name, "", "missing-file", "файл отсутствует в наборе"))
        for name, text in files.items():
            if name not in self.files:
                issues.append(ValidationIssue(name, "", "unknown-file", "файл не описан схемой"))
                continue
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as e:
                issues.append(ValidationIssue(name, "", "yaml", f"ошибка разбора YAML: {e}".splitlines()[0]))
                continue
            for err in sorted(self._validators[name].iter_errors(data), key=lambda e: list(e.absolute_path)):
                path = ".".join(str(p) for p in err.absolute_path)
                kind = _KINDS.get(err.validator, err.validator)
                if err.validator == "required":
                    missing = err.message.split("'")[1] if "'" in err.message else ""
                    path = ".".join(filter(None, [path, missing]))
                if err.validator == "additionalProperties" and isinstance(err.instance, dict):
                    allowed = set(err.schema.get("properties", {}))
                    for extra in sorted(set(err.instance) - allowed):
                        issues.append(ValidationIssue(name, ".".join(filter(None, [path, str(extra)])), kind,
                                                      f"поле «{extra}» не описано схемой"))
                    continue
                issues.append(ValidationIssue(name, path, kind, err.message))
            parsed[name] = data
        # Межполевые правила — для каждого файла без собственных ошибок схемы, чтобы все ошибки были видны сразу.
        clean = {n: d for n, d in parsed.items() if not any(i.file == n for i in issues)}
        issues += self._semantic(clean)
        return parsed, issues

    def validate(self, files: dict):
        return self.parse(files)[1]

    def _semantic(self, parsed):
        """Межполевые правила, которые не выражаются JSON Schema."""
        issues = []
        d = parsed.get("diagnostics.yaml")
        if d:
            if d["temp_warning_c"] >= d["temp_critical_c"]:
                issues.append(ValidationIssue("diagnostics.yaml", "temp_warning_c", "semantic",
                                              "порог предупреждения должен быть ниже критического"))
            if d["battery_critical_pct"] >= d["battery_warning_pct"]:
                issues.append(ValidationIssue("diagnostics.yaml", "battery_critical_pct", "semantic",
                                              "критический порог заряда должен быть ниже порога предупреждения"))
        return issues
