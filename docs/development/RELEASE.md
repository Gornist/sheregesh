<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Публичный выпуск

Процедура выпуска версии с тегом `vX.Y.Z` (REQ-REL-001, REQ-REL-010, REQ-REL-011, REQ-DOC-006). Регулярное обновление
плат без публичного выпуска — [RELEASE-STANDARD](../decisions/RELEASE-STANDARD.md). Тег, подпись и публикацию делает
владелец (ADR-0005); инструменты ниже собирают и проверяют комплект и ничего не публикуют.

## Что проверяется и где

| Требование | Проверка | Где |
|---|---|---|
| REL-001 | `reqgen.py gate`: все Must PASS на требуемом уровне | workflow `release` (блокирует), `release.py prepare` |
| REL-010 | SemVer одинаков в теге, имени архива образа, пакете, паспорте `sheregesh-info` и GUI «О системе»; HW-версия (`HW-1.0 Rev A`) отдельно в паспорте, GUI и примечаниях | `release.py versions` (тег — workflow `release`), `prepare` |
| REL-011 | секреты в отслеживаемых файлах и во всей истории всех веток `origin` (решения — [history-findings](../security/history-findings.md)); `reuse lint`; лицензии зависимостей — [dependency-licenses](../security/dependency-licenses.md); отчёт `release-check.md` в комплекте | workflow `release`, `prepare` |
| DOC-006 | SBOM SPDX 2.3: образа (rpi-image-gen, syft) и пакета `sheregesh` (`release.py sbom-package`, зависимости Depends) создаются при каждой сборке shbuild; гейт сборки проверяет оба; оба входят в комплект | shbuild (этап gates), `prepare` |

## Шаги

1. Версия в `pyproject.toml` и `software/sheregesh/__init__.py` — без `.dev` (например, `1.0.0`); коммит в `v2`, CI
   зелёный.
2. Образ — только shbuild: `python3 tools/build/shbuild.py submit <коммит>` → PASSED (гейт проверяет и SBOM).
3. Раскатка на стенд по [AGENT-WORKFLOW](AGENT-WORKFLOW.md#сборка-образа-и-раскатка) и приёмка; паспорта и GUI с плат:

   ```bash
   ssh sheregesh-ui@<пульт> sheregesh-info --json > console.json
   ssh sheregesh-ui@<блок> sheregesh-info --json > remote.json
   ```

   GUI — `tools/probe/gui_system_panel.py --details AboutPage` на пульте (файл `system.json`).
4. Комплект и отчёт (на Mac, артефакты — `shbuild.py fetch ID DIR`):

   ```bash
   python3 tools/release/release.py prepare --artifacts DIR --out OUT --tag vX.Y.Z \
     --passport console.json --passport remote.json --gui system.json --reuse .venv/bin/reuse
   ```

   Итог PASS — все проверки PASS; иначе код 1 и выпуск не публикуется. Без `--tag` — сухой прогон: гейт REL-001
   записывается, но не останавливает, `reuse` можно не указывать.
5. Владелец ставит подписанный тег `vX.Y.Z` на коммит образа и публикует выпуск с содержимым `OUT`: образ
   `sheregesh-X.Y.Z-<вариант>.img.zst`, пакет `.deb`, SBOM образа и пакета, `RELEASE-NOTES.md`, `release-check.md`
   и `.json`, `SHA256SUMS`. Workflow `release` на теге повторяет проверки исходников.

## Ограничения

- SBOM зависимостей пакета — имена Depends без версий: версии фиксирует SBOM образа (фактически установленные пакеты).
- Сухой прогон 2026-10-31 — [release-dry-run](../acceptance/release-dry-run/REPORT.md).
