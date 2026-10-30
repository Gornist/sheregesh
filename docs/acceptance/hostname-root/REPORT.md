<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Имя по роли, root по ключу (DEV), время свежей платы — приёмка на платах через shbuild и OTA

**Дата:** 2026-10-30 · **Ветка:** `docs/build-service` · **Образ:** `40d1e09` (обе платы) · **Аренда:**
`ota-hostname-40d1e09` · **Полномочия:** владелец в чате — «да, делай» (ADR-0018), «Да, раскатывай» (OTA pi1, затем
pi2).

Итог: обе платы на `40d1e09`, слот **B COMMITTED**, `ota-disable`. Имена **pi1** (пульт) и **pi2** (блок) держатся
после загрузки, DHCP и контрольной перезагрузки; root входит по SSH ключом; блок синхронизирован с пультом.

## Путь образа

| Шаг | Итог | Доказательство |
|---|---|---|
| shbuild `20261030-164302-40d1e09` | PASSED: tests 10 с, build 2219 с (30,5 мин сборка + 6,5 мин разовый сборщик), gates 32 с, boot 280 с | `shbuild status` |
| Загрузка в QEMU (усиленная проба) | console → pi1, remote → pi2, fresh → `sheregesh-dev`, всё PASS; bc87d16 — FAIL (имя, DHCP, root), как на платах | раздел ADR-0018 §3.2 |
| Пакет OTA ×2 | `cmp` совпадает, SHA-256 `c5a356d41bd8e29fa6ca939aad3faa709388fe65d7cd342b904c1469af64b805` | — |
| L3 (агент = код bc87d16 = код 40d1e09) | 39/0 | `l3.log`, `l3.json` |
| pi1: install → trial → confirm | сверка install 44/0, trial 26/0; связь 10/10; видео session/stream PASS; confirm COMMITTED | `ota-pi1/` |
| pi2: install → trial → confirm | install 44/0, trial 26/0; chrony: источник 192.168.1.94, страта 4; связь 10/10; видео 4/4 | `ota-pi2/` |
| После перезагрузки pi2 | видео session/stream PASS, связь 10/10 | `final-video/`, `final-link/` |

## Найдено и открыто

- `check_video_link` **no-plain** и **sender-kill** не адаптированы к SRTP (UDP-порты SRTP считаются запрещёнными;
  ищется генератор TLS) — ложные FAIL/ERROR, не дефект образа (`ota-pi1/video/`).
- **ctl-loss** и **restart** (видео не вернулось после разрыва управления / перезапуска блока, `video_back` null) —
  тот же открытый дефект восстановления видео, что в `../time-sync/REPORT.md` §3; программная часть образа не
  менялась. Обычная перезагрузка pi2 после confirm — видео восстановилось.
- `tools/deploy/ota-check/run.py --agent-rev` включает ожидания агента 57e8fe4; для агентов новее — без него
  (здесь код агента bc87d16 = 40d1e09). Добавлен `--base-remote` (образ уже на devbox, не возить через Mac).

## Состояние стенда

pi1 `10000000568152de` 192.168.1.94 — слот B, `40d1e09`, COMMITTED, `ota-disable`, имя pi1. pi2 `100000009d497e91`
192.168.1.173 — слот B, `40d1e09`, COMMITTED, `ota-disable`, имя pi2. На обеих `chmod 711 ~sheregesh-ui` (обход
до исправления, больше не нужен, безвреден). Слот A обеих — прежний bc87d16.
