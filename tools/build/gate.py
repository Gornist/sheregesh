# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Допуск образа «Шерегеша» к железу (ADR-0018): только артефакты задания shbuild с итогом PASSED.

На платы (OTA — `shdeploy ota-build`, карта — `tools/image/flash_sd.py`) идёт образ, рядом с которым лежит
`shbuild-report.json` сервиса сборки: состояние PASSED, все этапы (в том числе загрузочный тест) PASS и тот же коммит,
что в `build-report.json`. Ручные сборки, сборки CI и образы без отчёта — отказ: другого пути нет."""
import json
from pathlib import Path

REQUIRED = ("preflight", "checkout", "tests", "build", "gates", "boot")


class GateError(Exception):
    pass


def require_passed(art_dir, commit=None):
    """Отчёт shbuild в каталоге артефактов: PASSED, все обязательные этапы PASS, коммит совпадает. → отчёт."""
    p = Path(art_dir) / "shbuild-report.json"
    if not p.exists():
        raise GateError(f"нет {p.name} рядом с образом: образы для плат собирает только shbuild "
                        "(python3 tools/build/shbuild.py submit; ADR-0018)")
    try:
        rep = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise GateError(f"{p.name} не прочитан: {type(e).__name__}") from e
    if rep.get("state") != "PASSED":
        raise GateError(f"задание shbuild {rep.get('id')}: {rep.get('state')} — на железо только PASSED")
    stages = {s.get("name"): s.get("status") for s in rep.get("stages") or []}
    missing = [s for s in REQUIRED if stages.get(s) != "PASS"]
    if missing:
        raise GateError(f"задание shbuild {rep.get('id')}: этапы не PASS: {', '.join(missing)}")
    if commit and rep.get("commit") != commit:
        raise GateError(f"отчёт shbuild о коммите {rep.get('commit')}, а образ — {commit}")
    return rep


def is_sheregesh_image(image, build_report=None):
    """Образ «Шерегеша» (правило shbuild действует); посторонние карты стенда (pi3 — Raspberry Pi OS) — нет."""
    if build_report and (build_report.get("config") or "").startswith("sheregesh"):
        return True
    if build_report and build_report.get("image") and not str(build_report["image"]).startswith("sheregesh"):
        return False
    return Path(image).name.startswith("sheregesh")
