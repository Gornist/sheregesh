<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Этап A, часть 2: SW-005 на платах, паспорт устройства (SYS-007)

**Дата:** 2026-10-31 · **План:** [ROADMAP](../../development/ROADMAP.md), этап A · **Эталон:** Raspberry Pi 4 Model B
4 ГБ, pi1 `10000000568152de` (пульт), pi2 `100000009d497e91` (блок); образ `c0c509c`, DEV `g3b4ffc7`.

## TEST-SW-005 — секреты не попадают в журналы: PASS

Проба `tools/probe/check_secrets_in_logs.py` (root по ключу, DEV-образ). На каждой плате из мест хранения
([secrets.md](../../security/secrets.md)) читаются настоящие закрытые ключи — ключ устройства канала управления
(`device.key`) и ключи хоста SSH (ecdsa, ed25519, rsa): 15 фрагментов по 24 символа; фрагменты плату не покидают.

| Где искали | pi1 (пульт) | pi2 (блок) |
|---|---|---|
| journald, все загрузки | 3,6 МБ — 0 совпадений | 2,4 МБ — 0 |
| ключи SRTP без маски `***` в journald | 0 | 0 |
| `/var/log` | 9 файлов — 0 | 9 файлов — 0 |
| `/run/sheregesh*` (состояние служб, ui-state, link-status) | 7 файлов — 0 | 6 файлов — 0 |
| `/tmp` | 6 файлов — 0 | — |
| экспорт журналов в домашнем каталоге | файлов нет | файлов нет |
| **контроль:** канарейка `SHRG-CANARY-SW005-<случайная>` в journald и `/tmp` | найдена 1 + 1 | найдена 1 + 1 |

Журналы охватывают сценарии дня: потеря и восстановление связи, несовместимая версия, OTA с двумя откатами,
отключение источника видео, перезапуски служб. Экспорт журналов с маскированием канарейки и `PRIVATE KEY`, контрольная
сумма архива — автотест `tests/gui/test_video_and_pages.py::test_logs_page_and_export_without_secrets`.
`secrets.md` обновлён после D8 (ключ владельца, ключ устройства, ключи SRTP только в памяти, ключи хоста SSH).

## TEST-SYS-007 — паспорт устройства: PASS

Найдено на платах: «О системе» показывал «HW-1.0 Rev A (эмулятор, x86)» и сборку «—» (git на плате нет); блок
представлялся `remote-emu-01` с `HW-1.0 Rev A (emulator)`, команды `sheregesh-info` не было. Исправление (`0c8c88e`,
`732e299`): `sheregesh.info` — один источник для `sheregesh-info`, экрана «О системе» (новая строка «Серийный номер») и
hello канала управления: `HW-1.0 Rev A (<модель из device-tree>)`, `<роль>-<серийный номер>`, сборка образа из
`build.json`, DEV-выпуск. Команда `/usr/bin/sheregesh-info` — с новым образом (до него `python3 -m sheregesh.info`).
На платах (DEV `gde3e4fb`):

| Источник | pi1 (пульт) | pi2 (блок) |
|---|---|---|
| `sheregesh-info` (`python3 -m sheregesh.info`) | ПО 0.1.0.dev0, протокол 1.5, образ `c0c509c0b716` / 0.1.1793394197, выпуск `gde3e4fb`, HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5), серийный `10000000568152de`, `console-10000000568152de` | то же, серийный `100000009d497e91`, `remote-100000009d497e91` |
| манифест образа `build.json` и агент OTA | `c0c509c0b716`, 0.1.1793394197, серийный совпадает | `c0c509c0b716`, 0.1.1793394197, серийный совпадает |
| «О системе» реального GUI (`gui_system_panel.py --details AboutPage`, `about.png`) | Сборка «0.1.1793394197 c0c509c0b716 · 0.1.0~dev0+gde3e4fbcea49», Аппаратура «HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5)», Серийный номер «10000000568152de», Протокол 1.5 | — |
| данные блока на пульте (`link-status.json` → плитка «Удалённый блок») | `remote-100000009d497e91`, HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5), ПО 0.1.0.dev0, протокол 1.5 — совпадает с `sheregesh-info` блока | — |

Данные — из локальных файлов (device-tree, `build.json`, ссылка выпуска): без сети. **Не выполнено:** «версия ПО
(SemVer)» — `0.1.0.dev0` в формате PEP 440, не SemVer; решается вместе с TEST-REL-010 (схема версий).
Доказательства: `~/archive/acceptance/stage-a-2/sys007-evidence.tar.gz`, SHA-256 `d6bd66ad4eb1fc6b4d367fddac57795b86dc3972f9a284ccc1713a2784530341`.

## TEST-CFG-001 — конфигурация YAML со схемой: PASS

- Схемы `software/sheregesh/config/schemas/console-1.json`, `remote-1.json` опубликованы и входят в образ
  (`/usr/lib/sheregesh/software/sheregesh/config/schemas/` на pi1, образ `c0c509c`).
- Валидатор: ≥ 5 корректных наборов принимаются, ≥ 20 некорректных отклоняются с путём к полю и классом ошибки
  (`tests/unit/test_config_schema.py`: `test_valid_sets_accepted`, `test_invalid_sets_rejected_with_field_path`,
  `test_sample_counts_meet_criterion`, `test_all_errors_reported_at_once`).
- Критерий профилей уточнён решением владельца (CHG-018): профиль устройства — отдельная схема на роль; настройки
  модуля блока (`module.yaml`, включая профиль) меняются только новой ревизией, прежняя сохраняется —
  `tests/unit/test_config_store.py::test_module_settings_change_only_through_new_revision`. 75 passed.

## SemVer (SYS-007, REL-010)

Решение владельца: версия пакета Python остаётся PEP 440 (`0.1.0.dev0`), снаружи — SemVer 2.0 через одну функцию
`sheregesh.semver`: `0.1.0.dev0` → `0.1.0-dev.0`, локальная часть DEV-выпуска — метаданные сборки
(`0.1.0-dev.0+g43ea9a9.dirty`). GUI («О системе», «ПО пульта», версия блока) и `sheregesh-info` показывают SemVer;
hello канала управления — прежняя строка (по ней работает матрица совместимости). Тег и имя архива — при выпуске
(TEST-REL-010).

## TEST-DOC-001 — лицензии: PASS

- `reuse lint` (reuse 6.3.0): 572/572 файлов со сведениями об авторских правах и лицензии, REUSE 3.3 — соответствует;
  шаг добавлен в CI (задание «Реестр требований»).
- `LICENSES/`: GPL-3.0-or-later (ПО), CC-BY-SA-4.0 (документация), CERN-OHL-S-2.0 (CAD и аппаратная часть, `hardware/`).
  Сторонних материалов в репозитории нет.
- Зависимости распространяемого ПО: [dependency-licenses.md](../../security/dependency-licenses.md) — основная лицензия
  33 прямых зависимостей пакета с платы (DEP-5 `Files: *`): несовместимых с GPLv3 среди связываемых библиотек 0;
  nftables и chrony (GPL-2 only) — отдельные программы (агрегация). Поле `licenseDeclared` SBOM syft для этого
  непригодно: объединяет все лицензии файла `copyright`.
- Попутно: reuse 6 считает двоичными часть файлов с кириллицей (кодировка — по фрагменту фиксированной длины, граница
  режет двухбайтовую букву); `REUSE.toml` покрывает их.

Доказательства: `~/archive/acceptance/stage-a-2/doc001-evidence.tar.gz`, SHA-256
`0ba40f2a543b1601e3d732741621575d863f7c53161c867b06bc50ae5576a6db`.

**SemVer на платах (DEV `gdc74aa8`):** «О системе» pi1 — ПО `0.1.0-dev.0`; `sheregesh-info` обеих плат — `software`
`0.1.0-dev.0`, `package` `0.1.0.dev0`, выпуск `0.1.0~dev0+gdc74aa86ff1c`. Доказательства:
`~/archive/acceptance/stage-a-2/sys007-semver-evidence.tar.gz`, SHA-256 `be78a2b864db413c1474281c2452048eafc1c489100022f5292dfc05280d5f06`.

## Доказательства

Архив devbox `~/archive/acceptance/stage-a-2/stage-a2-evidence.tar.gz`, SHA-256
`62a8e04ee0e96733e3c3eb2ebcbccce5b6e00bb403c014d3500b21f7ed762b1c`: `sw005/console.json`, `sw005/remote.json`.
