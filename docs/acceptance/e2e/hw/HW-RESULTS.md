# DUAL-DEVICE E2E — аппаратный прогон на двух Pi 4 (2026-10-25, слоты A + DEV deploy)

Решение владельца: «OTA отложим, переходим к аппаратному E2E на A с DEV deploy». Стенд: pi1 (пульт, 10000000568152de,
192.168.1.92) ↔ Ethernet ↔ pi2 (удалённый блок, 100000009d497e91, 192.168.1.171); только эмуляторы и videotestsrc.

## Конфигурация
- Обе платы: образ 57e8fe4, слот A; OTA pi1 IDLE, pi2 ROLLED_BACK; переключение слотов запрещено на обеих
  (`ota-disable` pi2 перед испытанием). Снимки slot_guard «до»: [`pi1-before.json`](pi1-before.json),
  [`pi2-before.json`](pi2-before.json).
- DEV deploy v2 на обе платы: выпуск `0.1.0~dev0+g1cd20fdcade2` (весь код E2E), после исправления монитора —
  `0.1.0~dev0+g7061b3c52ee8` ([`deploy-*.log`](.)).
- Инструменты: `tools/probe/check_link.py`, `check_video_link.py`, `e2e_run.py` (ревизия v2 на момент прогона).

## Результаты

| Проверка | Итог | Измерено (порог SRS) | Доказательство |
|---|---|---|---|
| Базовая линия канала | 10/10 PASS | connect, baseline, ports, hb-loss, watchdog, restart, outsider, identity, version, health | [`link-baseline/`](link-baseline/) |
| Базовая линия видео | 11/11 PASS | ctl-loss «нет видео» 1,95 с (≤ 3); возврат 2,13 с | [`video-baseline/`](video-baseline/) |
| `pause` (блок молчит, без FIN/RST) | 10/10 PASS | обнаружение пультом 3,2–4,0 с (≤ 5); канал 0,12–0,45 с (≤ 10); «нет видео» 2,5–2,7 с (≤ 3); видео 1,6–1,7 с (≤ 10) | [`e2e-pause/`](e2e-pause/) |
| `stop-start` | 5/5 PASS | 0,7–1,0 / 3,2–3,5 / 0,7–1,0 / 3,7–3,8 с | [`e2e-stop-start/`](e2e-stop-start/) |
| `restart` | 5/5 PASS | 0,3–0,6 / 3,6–3,8 / 0,3–0,6 / 4,5–4,8 с (с паузой 75 с) | [`e2e-restart/`](e2e-restart/) |
| `restart` без пауз | 3 PASS, 2 FAIL | `start-limit-hit`: юнит `StartLimitBurst=5` за 300 с — защита образа, превышена подряд идущими моделями | [`e2e-restart-startlimit/`](e2e-restart-startlimit/) |
| `sender-kill` (только видео) | 5/5 PASS | «нет видео» 0,8–0,9 с; видео 2,1–2,4 с; управление не прерывалось | [`e2e-sender-kill/`](e2e-sender-kill/) |
| **Физическое отключение кабеля pi2 (человек), 3 цикла** | **3/3 восстановлены** (e2e_run: 2 PASS, 1 ERROR — рассинхрон нажатий) | см. ниже | [`e2e-manual/`](e2e-manual/), журналы |
| Журналы (E2E-12) | PASS | пульт пишет переходы связи и видео в journald (`sheregesh-notify-relay`), блок — `console silent … dropped`, `session from` | журналы плат |
| Регрессии OTA | PASS | slot_guard `unchanged` обеих плат: 42 PASS; 2 ожидаемых FAIL — `same.deploy.status` (новый выпуск) и `same.unit.*` (перезапуск службы) | [`pi1-compare.txt`](pi1-compare.txt), [`pi2-compare.txt`](pi2-compare.txt) |
| Итог после исправления | PASS | check_link health/baseline/identity 3/3; check_video_link session/stream/leftovers 3/3 | [`link-after/`](link-after/), [`video-after/`](video-after/) |

### Физический кабель — по журналам ядра pi2 и journald обеих плат (независимо от e2e_run)

| Цикл | eth0 pi2 Down | Блок обнаружил | Пульт: «нет видео» / переподключение | eth0 Up | Пульт CONNECTED / видео |
|---|---|---|---|---|---|
| 1 | 06:41:03 | 06:41:06 (≈3 с) | 06:41:04 / 06:41:05 | 06:42:23 | 06:42:23 / 06:42:23 |
| 2 | 06:47:39 | 06:47:41 (≈2 с) | 06:47:39 / 06:47:40 | 06:48:04 | 06:48:08 / 06:48:09 |
| 3 | 06:50:36 | 06:50:38 (≈2 с) | 06:50:36 / 06:50:37 | 06:51:11 | 06:51:11 / 06:51:12 |

Обнаружение обеими сторонами ≤ 3 с (REQ-SYS-002 п.2 ≤ 5 с), восстановление канала ≤ 4 с и видео ≤ 5 с после
подключения кабеля (п.3 ≤ 10 с). Точность — секунда (метки журналов; часы плат — NTP). ERROR цикла 3 в e2e_run:
лишний Enter цикла 2 ответил на подсказку цикла 3 и сдвинул метки; исправлено (`fresh_input`, сброс буфера ввода).

## Дефект, найденный на платах и исправленный
`EventLog.raise_(t, code, **params)`: событие блока `REMOTE-EVENT` несёт параметр `code` → `TypeError` в такте монитора
пульта каждые ~5 с (563 исключения с 05:53, ещё в выпуске a0ba631), события блока не попадали в журнал пульта.
Исправлено (7061b3c, `code` только позиционно, тест `tests/unit/test_monitor_remote_event.py`), выпуск g7061b3c на
обеих платах: за 60 с после установки — 0 исключений.

## Не закрыто
- Визуальное подтверждение GUI на дисплее пульта и демонстрация (видео/фото) — состояние GUI проверено по
  `ui-state.json` (CONNECTED, «связь есть», видео playing 30 к/с), а не глазами.
- E2E-01/02 (время загрузки по REQ-P-012), E2E-13 (app-stop — нужен образ ≥ 6353c70 на пульте), E2E-15 (30 мин).
- Кабель — 3 цикла из 10 по REQ-SYS-002 п.4; остальные 7 — модели (pause/stop-start/restart).
