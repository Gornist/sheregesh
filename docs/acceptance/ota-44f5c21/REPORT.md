<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Образ `44f5c21` на обеих платах через OTA

**Дата:** 2026-10-31 · **Ветка:** `v2` · **Образ:** `44f5c21` · **Аренда:** `ota-44f5c21` · **Полномочия:** владелец
в чате — «Да, оба» (после PASS сборки раскатить OTA на обе платы). **Источник видео:** HDMI devbox → MS2130 → USB 3.0
pi2, 1280×720@60.

Итог: обе платы на `44f5c21`, слот **B COMMITTED**, `ota-disable`, имена pi1/pi2 после контрольной перезагрузки;
слот A обеих — прежний `c0c509c`. В образе (прежде только DEV-выпусками) — видеоконтракт v13 (пауза поколений без
источника), паспорт `/usr/bin/sheregesh-info`, SemVer, монитор служб, исправление health `storage.persistent`
(суперблок по mountinfo), диалоги подтверждения Minimal.

## Путь образа

| Шаг | Итог |
|---|---|
| shbuild `20261031-174520-44f5c21` | PASSED: tests 17 с, build 1888 с, gates 32 с, boot 288 с |
| Пакет OTA ×2 | `cmp` совпадает, SHA-256 `0663c352611d4a6a8733fd4aaa43b8b816aaaabacd8fe8dc84abb5a70cfeb2d9`, версия `0.1.1793457876` |
| L3 (база — образ плат `c0c509c` на devbox; код OTA с `c0c509c` не менялся) | 39/0 |
| pi1: install → trial → confirm | install 44/0, trial 26/0; NTP синхронизирован; связь 10/10; видео 9/9; COMMITTED |
| pi2: install → trial → confirm | install 44/0 (pi1 unchanged 44/0), trial 26/0; NTP синхронизирован; связь 10/10; видео 9/9; COMMITTED |
| После перезагрузки обеих плат | связь 10/10, видео 9/9; `systemctl --failed` пусто на обеих |

## Проверки образа

- **health от `sheregesh-ui`:** пульт 15/15, блок 14/14 PASS; `storage.persistent` — PASS (`/dev/mmcblk0p6 ext4 rw`).
  Прежний ложный FAIL в пространстве имён `ProtectSystem=strict` устранён в заводском коде.
- **`sheregesh-info`:** роль console/remote, ПО `0.1.0-dev.0`, сборка `0.1.1793457876 · 44f5c21b220c`, DEV-выпуск «—»,
  `HW-1.0 Rev A (Raspberry Pi 4 Model B Rev 1.5)`, ID `console-10000000568152de` / `remote-100000009d497e91`.
  От root без профиля роль «—» и ID `device-…` — ожидаемо (роль из конфигурации пользователя).

## Видео после раскатки

| Сценарий | Результат |
|---|---|
| stream | 21 с: tx 6003 / rx 5843 пакетов, потерь и разрывов 0, перезапусков декодера 0, 56,7 кадров/с (получено 60), джиттер 0,5 мс |
| latency | «захват → декодирован» p50 42,9 мс, p95 45,6 мс, max 51,2 мс (150 выборок) |
| resources | пульт 22,3 % CPU (4 ядра), 405 МБ, 60 °C; блок 14,8 %, 213 МБ, 66 °C; троттлинга нет |
| ctl-loss | «нет видео» 2,81 с, видео снова 2,15 с в новом сеансе SRTP |
| restart | управление 1,81 с, видео 0,6 с |
| session, no-plain, telemetry, leftovers | PASS |

## Доказательства

Архив devbox `~/archive/acceptance/ota-44f5c21/ota-44f5c21-evidence.tar.gz`, SHA-256
`c05ae1305ac136ce8effbd98bc6f5e313d0df241841c0aea8001a1beced849d8`: `raw/l3.*`, `raw/pi1/` и `raw/pi2/` (снимки
slot_guard, ответы install/confirm, `link/`, `video/`), `raw/final/` (связь, видео, health), манифест пакета,
`shbuild-report.json`. Артефакты образа на Mac удалены.

## Состояние стенда

pi1 `10000000568152de` 192.168.1.94 — слот B, `44f5c21`, COMMITTED, `ota-disable`, имя pi1. pi2 `100000009d497e91`
192.168.1.173 — слот B, `44f5c21`, COMMITTED, `ota-disable`, имя pi2; DEV-выпуска нет (заводской код образа).
Слот A обеих — `c0c509c`.
