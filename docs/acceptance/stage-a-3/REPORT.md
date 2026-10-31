<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Этап A, часть 3: обязательные службы (TEST-SW-001)

**Дата:** 2026-10-31 · **План:** [ROADMAP](../../development/ROADMAP.md), этап A · **Полномочия:** владелец в чате —
«Да, продолжай» (остальные пункты SW-001 на стенде) · **Эталон:** Raspberry Pi 4 Model B 4 ГБ, pi1 `10000000568152de`
(пульт), pi2 `100000009d497e91` (блок); образ `c0c509c`, DEV `g030a89d` / `gaba3d6a`.

## TEST-SW-001 — службы с диагностируемыми состояниями и журналами: PASS

| Критерий | Доказательство | Итог |
|---|---|---|
| Каждая обязательная служба — юнит systemd с watchdog; перечень — `docs/software/services.md` | [services.md](../../software/services.md): 21 юнит, 5 постоянных (`Type=notify`) — у всех `WatchdogSec` и `Restart=`; тест `tests/tools/test_services_doc.py` сверяет документ с юнитами | PASS |
| Состояние каждой службы видно в диагностике GUI (блок — через пульт) | Раздел «Диагностика» Minimal — строка на службу («Служба пульта: video-rx — active · перезапусков 0», «Служба блока: video-tx — …»); блок передаёт снимок в `status` (поле `services`). На pi1 — строки `ui-state.json`: console, video-rx (пульт), remote-net, video-tx (блок) | PASS |
| Принудительное завершение → ошибка в журнале, перезапуск, событие в GUI ≤ 10 с | `tools/probe/check_services.py`, `kill -9` основного процесса: см. таблицу ниже | PASS |
| Экспорт журналов — архив с контрольной суммой; канарейки — 0 | `tests/gui/test_video_and_pages.py::test_logs_page_and_export_without_secrets` (маска канарейки и `PRIVATE KEY`, SHA-256 архива); ключи в журналах плат — [stage-a-2](../stage-a-2/REPORT.md) (SW-005) | PASS |

| Служба (плата) | NRestarts | Журнал systemd | SVC-RESTART в GUI пульта после kill |
|---|---|---|---|
| sheregesh-video-rx (pi1) | 0 → 1 | Main process exited, code=killed, status=9/KILL; Failed with result 'signal'; Scheduled restart | 5,26 с |
| sheregesh-video-tx (pi2) | 0 → 1 | то же | 4,74 с |
| sheregesh-remote-net (pi2) | 0 → 1 | то же | 5,69 с |
| sheregesh-console (pi1, сам GUI) | 0 → 1 | то же | 6,76 с (событие поднял новый процесс GUI) |

Время — от `kill` по часам платы до записи `ui-state.json` с событием по часам пульта (часы синхронизированы chrony,
шаг записи 1 с).

## Что сделано для этого

- `sheregesh.services`: снимок `systemctl show` обязательных служб роли (незагруженные юниты — CI, другая роль — не
  отказ); монитор пульта — `SVC-RESTART` (перезапуск, включая сам GUI после сбоя) и `SVC-DOWN` (не active, до
  возврата); блок — поле `services` в `status` (необязательное, старый пульт его отбрасывает).
- Раздел «Диагностика» Minimal — строки служб; `ui-state.json` — последние события монитора и строки служб (пробы без
  дисплея).
- `docs/software/services.md` — черновик таблиц составлен локальной моделью Offrail, проверен построчно.

## Доказательства

Архив devbox `~/archive/acceptance/stage-a-3/sw001-evidence.tar.gz`, SHA-256 `0cea9c7c70c0291770709957ab38cfb4fb1e40fe3517b95113a52c774a44777a`: `kill/*.json` (по службе:
перезапуски, записи журнала, событие, время), `services-rows.json`.
