<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Этап A, часть 1: приёмка на стенде — SW-004, UPD-002, SYS-008, SW-003, UPD-004

**Дата:** 2026-10-31 · **План:** [ROADMAP](../../development/ROADMAP.md), этап A · **Аренда:** `stage-a` ·
**Полномочия:** владелец в чате — «Да, начинай»; DEV deploy исправлений, запись неисправного образа в слот B pi2 —
отдельные согласия. **Эталон:** Raspberry Pi 4 Model B 4 ГБ, pi1 `10000000568152de` (пульт), pi2 `100000009d497e91`
(блок); образ `c0c509c`, DEV `g678453b`, затем `g3b4ffc7`.

## TEST-SW-004 — нет второго устройства: PASS

Проба `tools/probe/check_absent_peer.py` (DEV `g678453b`).

| Сценарий | Итог |
|---|---|
| Пульт без блока: служба связи pi2 остановлена, GUI пульта перезапущен | «НЕТ СВЯЗИ С БЛОКОМ» через 0,05 с после старта (≤ 10 с); 30 с GUI обновляет состояние, перезапусков 0; строка состояния «нет связи / подключение»; проверка здоровья — `link.state` FAIL «connecting (refused)», новых FAIL нет |
| Блок без пульта: служба пульта остановлена | статус канала блока `connected: false` через 0,25 с; 60 с `sheregesh-remote-net` и `sheregesh-video-tx` active, перезапусков 0; проверка здоровья — «нет сессии»; в журнале — `video-stop … control session closed` |

## TEST-UPD-002 — подпись образа: PASS

На pi2 поданы три заголовка потока SHRGOTA1 с настоящим манифестом `0.1.1793394197` (без образов):

| Вариант | Ответ платы |
|---|---|
| без подписи | `REJECTED` — неверная длина блока подписи |
| изменённый манифест (версия), подпись ключом выпуска владельца от исходного | `REJECTED` — incorrect signature |
| подписан посторонним ключом ed25519 | `REJECTED` — Could not verify signature |

`slot_guard compare --mode unchanged` до/после: 44/0 (0 записанных секторов ни в один раздел), состояние OTA
`COMMITTED` не изменилось. Принятие подписанного пакета — утренняя OTA обеих плат
([remove-tls-video](../remove-tls-video/REPORT.md)). Проверка — `ssh-keygen -Y verify` по `allowed_signers` образа,
локально (без сети). Подмена образа при верном манифесте ловится хешем при записи неактивного слота
(`tests/unit/test_update_model.py::test_rejected_bundles_never_write`, L3 ota-check).

## TEST-SYS-008, TEST-SW-003 — совместимость: PASS

- Матрица `software/sheregesh/compat-matrix.yaml` установлена с ПО (`/usr/lib/sheregesh/…`, выпуск DEV).
- Правило CHG-012 — 16 пар (`tests/unit/test_monitor_emulators.py::test_protocol_negotiation_rule`): MAJOR, MINOR ниже
  минимума, отклонённое ПО, недопустимая HW, неразборчивые версии — 100 %. Сообщения новее сессии не исполняются, а
  учитываются: `tests/system/test_module_compat.py`, `test_rc_link.py`, `test_console_video_srtp.py`,
  `test_two_processes.py::test_newer_minor_peer_negotiates_lower_session`. 142 passed.
- На платах: совместимая пара 1.5/1.5 — штатная работа; несовместимая — блок с `--protocol 2.0` (drop-in в `/run`):
  пульт `INCOMPATIBLE` (major), версия блока 2.0; блок «hello from console proto=1.5 → major»; подтверждений и событий 0,
  телеметрии нет, видео off.
- **Найдено и исправлено:** Minimal при несовместимости показывал «НЕТ СВЯЗИ С БЛОКОМ» без версий (`3b4ffc7`). На
  `g3b4ffc7`: «НЕСОВМЕСТИМАЯ ВЕРСИЯ БЛОКА», «Протокол: пульт 1.5, блок 2.0 (major)…».

## TEST-UPD-004 — автоматический откат: PASS (pi2 и pi1)

pi2: `c0c509c` записан в слот B (запись только `boot_b`/`system_b`, активные разделы 0 секторов), две пробные загрузки
с отключённым MS2130 (unbind USB) перед подтверждением:

| Шаг | Состояние |
|---|---|
| попытка 1 | `ota-confirm` FAILED (не пройдены video, uptime) → `TRYBOOT` «health FAIL, повтор» |
| попытка 2 | FAILED → `ROLLED_BACK` «health FAIL» (MAX_BOOT_ATTEMPTS = 2) |
| перезагрузка | прошивка загрузила A; `slot_guard --mode rollback` 36/0; 0 неуспешных юнитов; видео session/stream и связь baseline/health — PASS |

Журнал агента OTA содержит обе попытки и итог. У блока GUI нет (D3) — событие для него видно в журнале.

**pi1 (пульт, «видно в GUI»):** тот же сценарий — `c0c509c` в слот B (install 44/0), MS2130 блока отвязан на время
пробных загрузок (у пульта нет видео): попытка 1 → FAILED «health FAIL, повтор», попытка 2 → `ROLLED_BACK`;
перезагрузка → A, `slot_guard --mode rollback` 36/0, 0 неуспешных юнитов. Раздел «Система» реального GUI выпуска
(`tools/probe/gui_system_panel.py`, программный рендерер на pi1): строка **OTA — `ROLLED_BACK`** (цвет предупреждения),
«Слоты: активный A · второй B», «Образ системы: 0.1.1793394197 · c0c509c0b716» (`system.png`, `system.json`). Журнал
агента OTA — обе попытки и итог. После возврата MS2130: видео session/stream и связь baseline/health — PASS.

Слоты B обеих плат теперь `c0c509c` (прежний `40d1e09` заменён по согласованию); состояние OTA обеих — `ROLLED_BACK`,
переключение запрещено.

## Попутно

- **Ложный FAIL `storage.persistent`** на обеих платах: `sheregesh-health` (ProtectSystem=strict) видел `/persistent`
  только для чтения в своём пространстве монтирования. Исправлено (`c40f35d`) — по опциям суперблока в mountinfo;
  проверено на плате в той же песочнице (PASS, прежний способ — FAIL). Служба берёт заводской код образа — в силу с
  новым образом.

## Не сделано на этой части

TEST-SYS-002 (три физических отключения кабеля) и TEST-SYS-004 (первая загрузка свежей карты pi3 без сети) — нужны руки
владельца.

## Доказательства

Архив devbox `~/archive/acceptance/stage-a-1/stage-a-evidence.tar.gz`, SHA-256
`38dda806962af179a027f0b60dece590ce97060d222d23230428257748d077a7`: `sw004/`, `upd002/` (заголовки, ответы, снимки),
`sw003/` (пульт и блок при несовместимости, проверка здоровья), `upd004/` (снимки, подтверждения, журнал, видео и связь
после отката), `compat-tests.txt`.
Прогон pi1: `~/archive/acceptance/stage-a-1/upd004-pi1-evidence.tar.gz`, SHA-256
`dbf2ed059c5df82865841378d3d4bf3405cbc01e498fea8bd92af2e46a4c2c83` (снимки, подтверждения, журнал, раздел «Система»).
