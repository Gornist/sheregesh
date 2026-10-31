<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Сухой прогон подготовки выпуска

**Дата:** 2026-10-31 · **Инструмент:** `tools/release/release.py prepare` (коммит `39faa66`) · **Артефакты:** задание
shbuild `20261031-174520-44f5c21` · **Паспорта и GUI:** `sheregesh-info --json` с pi1 и pi2, `gui_system_panel.py
--details AboutPage` на pi1 (обе платы на образе `44f5c21`, без DEV-выпуска) · Процедура — [RELEASE](../../development/RELEASE.md).

Итог сухого прогона — **PASS** (тег из исходников `v0.1.0-dev.0`, не ставился). Гейт REQ-REL-001 записан: NO-GO,
30 незакрытых проверок Must — в сухом прогоне не останавливает, в выпуске с тегом остановит.

| Проверка | Итог |
|---|---|
| задание shbuild PASSED, все этапы PASS | PASS |
| реестр валиден (64 REQ, 42 Must) | PASS |
| гейт REQ-REL-001 | NO-GO (30 Must) — ожидаемо |
| секреты в отслеживаемых файлах | PASS, 0 находок в 591 файле |
| секреты во всей истории 12 веток origin | PASS: в каждой 1 находка с записанным решением, неразобранных 0 |
| reuse lint по дереву коммита `44f5c21` | PASS, 582/582 |
| лицензии зависимостей описаны | PASS |
| SBOM образа (syft, SPDX 2.3, 2532 пакета) | PASS |
| SBOM пакета `sheregesh` (SPDX 2.3, 33 зависимости Depends) | PASS |
| SemVer: исходники, тег, пакет `.deb`, паспорта пульта и блока, GUI = `0.1.0-dev.0`; HW `HW-1.0 Rev A` отдельно в паспортах, GUI и примечаниях | PASS |

Комплект: `sheregesh-0.1.0-dev.0-lab-dev-pi4.img.zst`, пакет `.deb`, SBOM образа и пакета, `RELEASE-NOTES.md`,
`release-check.md` и `.json`, `SHA256SUMS`.

## Найдено и исправлено в ходе прогона

- `reuse lint` по рабочей копии падал на чужих неотслеживаемых файлах (`.codex/`, `analysis/`) — проверка теперь
  по `git archive` коммита выпуска.
- SHA-256 из `build-report.json` — несжатого образа; в примечаниях подписано, архив — по `SHA256SUMS`.

## Что остаётся до PASS требований

TEST-REL-010, TEST-REL-011, TEST-DOC-006 закрываются на первом выпуске с тегом (отчёт и SBOM приложены к релизу).
Образы с SBOM пакета в артефактах shbuild — с ревизии `39faa66` (новая сборка); для `44f5c21` SBOM пакета создан
при подготовке.

## Доказательства

Архив devbox `~/archive/acceptance/release-dry-run/release-dry-run-evidence.tar.gz`, SHA-256
`ef9d621f513f87a94014fe6f74e57befd3635a19ce618f5c1b9a34b51fa49980`: `out/` (примечания, отчёт проверки, SBOM
пакета, `SHA256SUMS`), паспорта `console.json`, `remote.json`, GUI `system.json`.
