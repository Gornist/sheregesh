<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# ADR-0017: время между платами без интернета — приёмка на платах

**Дата:** 2026-10-30 · **Ветка:** `v2` · **Образ:** `5ee5f57` (обе платы) · **Аренда:** `time-5ee5f57`
· **Полномочия:** владелец в чате — образы на devbox, OTA по одной плате; решение «пункт 1» (пульт — источник
времени блока).

Итог: обе платы на `5ee5f57`, OTA **COMMITTED** (pi1 — B, pi2 — A), confirm 19/19 у обеих, контрольные
перезагрузки чистые. Блок синхронизирован с пультом: смещение **0,33 мс** (SNTP с pi2 к pi1, задержка 0,2 мс),
пульт — страта 3 (пул Debian, интернет на стенде есть). После загрузки пульт поднял видео сразу по SRTP (исправление
`785b485` подтверждено), задержка `capture_to_decoded` положительная — p50 36–38 мс (знак `8410a3f`). Найден новый
дефект восстановления видео после перезагрузки блока (раздел 3). Без интернета на платах не проверялось.

## 1. Попытки

| Образ | Плата | Итог | Причина / доказательство |
|---|---|---|---|
| `f498f97` | pi2 первой | trial: health FAIL → `ota-abort` | часы pi2 после загрузки — 13 апреля (timesyncd больше не продвигает часы по своей отметке), старый пульт времени не отдаёт; сертификаты «ещё не действительны» (`ota-pi2/`) |
| `f1a215f` | pi1 | trial: health FAIL → `ota-abort` | `sheregesh-time-role` упал: `chronyc add pool` резолвит имя сам, песочница юнита это запрещает; часы в прошлом, mTLS `certificate is not yet valid` (`ota-pi1-attempt1-f1a215f/trial-health.txt`) |
| `5ee5f57` | pi1, затем pi2 | **COMMITTED** обе | источники — файлы `sourcedir`, chronyc только `local`/`allow`/`reload`; проверено до сборки под systemd в контейнере без сети (`ota-pi1/`, `ota-pi2-5ee5f57/`) |

Попытка 0 на `f1a215f` остановлена сценарием до tryboot: на pi2 оставался флаг `ota-enable` прерванного цикла
(`ota-abort` его не снимает) — `ota-pi1-attempt0/`.

## 2. Время

- pi1: `chrony`, `sheregesh-time-role`, `-save`, `-tick.timer` active; `role.sources` — пул; `local stratum 10`,
  `allow 192.168.1.171`.
- pi2: `role.sources` — только `server 192.168.1.92`; после загрузки «System time restored from driftfile» (часы
  отставали на 18 с — время перезагрузки), через 25 с «Selected source 192.168.1.92», шаг 18 с.
- Отметка времени (`/persistent/chrony/chrony.drift`) — раз в 10 мин (журнал pi1: 22:48:36, 22:58:36 UTC).
- Кратковременный отказ mTLS при пробной загрузке pi2 (`rejected by remote unit`, ~10 с) — до синхронизации:
  первая загрузка нового слота без drift-файла; дальше часы восстанавливаются по отметке.

**NOT TESTED:** работа без интернета на платах (пульт только с `local stratum 10`), долгий простой без питания,
отказ пульта как источника.

## 3. Найдено: видео не восстанавливается после перезагрузки блока (открыто)

Пульт работал, pi2 перезагрузилась (OTA). После нового сеанса управления служба rx — `streaming` 30 к/с (поколение
1 нового сеанса, «replaced by gen 1» в том же процессе), а приёмник GUI — `restarting`, `decoder exited with code 1`.
Вручную: `unixfdsrc socket-path=/run/sheregesh-video/frames-main.sock ! v4l2convert …` → «Internal data stream
error, reason error (-5)». Перезапуск только `sheregesh-console` не помогает; `systemctl restart sheregesh-video-rx`
и затем пульта — `playing` 30 к/с. Вероятно, кадры второго конвейера в том же процессе службы rx не принимаются
потребителем; нужен отдельный разбор с тестом (перезагрузка/перезапуск блока при работающем пульте).

## 4. Состояние стенда

pi1 `10000000568152de` — слот B, `5ee5f57`, COMMITTED, `ota-disable`; pi2 `100000009d497e91` — слот A, `5ee5f57`,
COMMITTED, `ota-disable`. Видео `remote-srtp playing` 30 к/с (после перезапуска rx и пульта, раздел 3), failed 0.
На devbox после опыта с systemd в контейнере восстановлена регистрация qemu-aarch64 в binfmt_misc (из штатного
`/usr/share/qemu/binfmt.d/qemu-aarch64.conf`, флаги `POF`).
