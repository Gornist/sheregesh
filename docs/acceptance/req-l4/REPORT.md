<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Приёмка L4 по реестру требований: TEST-REL-001, TEST-UPD-001

**Дата:** 2026-10-31 · **Полномочия:** владелец в чате — «Принять 2 и почистить» · **Реестр:** после D8 (CHG-016,
CHG-017).

## TEST-REL-001

**REQ-REL-001** — гейт выпуска: все Must с подтверждённым PASS. Уровень L4 — на эталонных релизных материалах (реестр
`docs/requirements/requirements.json`, инструмент `tools/reqs/reqgen.py` на `fc222c0`).

| Критерий | Доказательство | Итог |
|---|---|---|
| NO-GO, если у Must есть тест без PASS на требуемом уровне или без ссылки на доказательство | `reqgen.py gate` на реестре: NO-GO, перечень блокирующих проверок | PASS |
| Матрицы с FAIL, NOT TESTED и без ссылки дают NO-GO (3/3); полностью пройденная — GO | `tests/tools/test_reqgen.py`: `test_gate_is_no_go_while_not_tested`, `test_gate_rejects_fail_missing_evidence_and_low_level` (FAIL, нет ссылки, низкий уровень), `test_gate_go_only_when_all_must_pass`; 16 passed | PASS |

## TEST-UPD-001

**REQ-UPD-001** — два загрузочных слота A/B и отдельный раздел данных. Эталон: Raspberry Pi 4 Model B 4 ГБ, pi1
`10000000568152de`, pi2 `100000009d497e91`; образ `c0c509c` (OTA `0.1.1793394197`, утренняя раскатка —
[remove-tls-video](../remove-tls-video/REPORT.md)). Сверка — `tools/deploy/ota-check/slot_guard.py` (независимо от
агента OTA: счётчики секторов ядра при том же boot_id, «диск = сумма разделов»), повторена по сохранённым снимкам.

| Критерий | Доказательство | Итог |
|---|---|---|
| Таблица разделов: boot, rootfs A, rootfs B, data | `lsblk` обеих плат: p1 `BOOTCONFIG` (autoboot), p2/p3 `BOOT` A/B, p4/p5 `SYSTEM` A/B, p6 `PERSISTENT` 1 ГБ (`/persistent`, `/var` слотов внутри). Загрузочных разделов два — по одному на слот | PASS |
| Установка пишет только в неактивный слот; активный остаётся загрузочным до подтверждения | `compare --mode install`, обе платы 44/0: `written.p2.boot_a` и `written.p4.system_a` — ровно размер образа; `untouched.p1.bootconfig`, `p3.boot_b`, `p5.system_b` — 0 записанных и 0 отброшенных секторов; `slot.active_unchanged` B → B; `other_slot_not_mounted` | PASS |
| После переключения хеши контрольных файлов раздела данных не изменились | `compare --mode trial`, обе платы 26/0: `same.fp.*` — ключи и сертификаты связи, `link.json`, роль, `authorized_keys`, ключи хоста SSH, `machine-id` | PASS |

## Доказательства

Архив devbox `~/archive/acceptance/req-l4/req-l4-evidence.tar.gz`, SHA-256
`0bebc2a46c47408afda6473f02b32cd32b81595eb476a07e4a3413200b058774`: снимки `slot_guard` (`snaps/*-before|install|trial.json`),
итоги сверок (`pi1|pi2-install|trial.txt`), `lsblk` обеих плат, вывод тестов гейта.

## Попутно (CHG-017)

Критерии REQ-SYS-004, REQ-SW-004, REQ-UPD-002, REQ-UPD-004 очищены от ссылок на требования, удалённые или пониженные
в D8 (справка, резервирование через USB, безопасный режим, корень подписи и отозванный ключ, снимок конфигурации ОС).
