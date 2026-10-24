# DUAL-DEVICE E2E — протокол испытаний (ШАБЛОН, результатов нет)

> Результат пункта: PASS / FAIL / ERROR / NOT TESTED / BLOCKED. ERROR и NOT TESTED ≠ PASS. Модельный отказ (pause,
> stop-start, restart, sender-kill) не выдаётся за физическое отключение кабеля. Протокол стенда, не приёмка: Must —
> NOT TESTED, REQ-REL-001 — NO-GO. Матрица, пороги, допуск — [MATRIX.md](MATRIX.md).

## 0. Шапка

| Поле | Значение |
|---|---|
| Дата/время | |
| Исполнитель | |
| Интегрированный SHA | |
| SHA инструментов | |
| Внешнее оборудование (REQ-SYS-003) | только эмуляторы и videotestsrc |
| Стенд | сеть, какой кабель, отличия от STAND-1 |

| | pi1 (console) | pi2 (remote) |
|---|---|---|
| Серийный номер | | |
| Адрес | | |
| Образ / слот / OTA (`ota-client status`) | | |
| Пакет (`dpkg-query`) | | |
| Выпуск (`readlink …/sheregesh-deploy/current`) | | |
| build.json | | |
| PROTOCOL_VERSION и сессия (`link.versions`) | | |
| health до | | |

## 1. Допуск (MATRIX §4.2)

| № | Условие | Выполнено | Доказательство |
|---|---|---|---|
| 1 | OTA pi2 COMMITTED/B, разрешение снято; pi1 IDLE/A | | |
| 2 | Снимки slot_guard «до» (SHA коммита) | | |
| 3 | Базовая линия check_link / check_video_link PASS, health без SKIP | | |
| 4 | Решение по коду pi2 (смешанная пара или DEV deploy) | | |
| 5 | Человек для `manual` (нет → manual NOT TESTED) | | |

## 2. Результаты E2E-01…15

| E2E | Требование, порог | Метод / модель отказа | Измерено | Результат | Доказательство (файл) |
|---|---|---|---|---|---|
| 01 … 15 | пороги — MATRIX §1 | | | | |

## 3. Циклы (`tools/probe/e2e_run.py`, блок на модель)

Модель, эквивалентность (`summary.json` → `equivalence`), число циклов, длительность обрыва, пауза.

| Цикл | detect_s (≤5) | link_back_s (≤10) | video_off_s (≤3) | video_back_s (≤10) | gui_stale_max_s | Инварианты | Вердикт |
|---|---|---|---|---|---|---|---|

Итог PASS/FAIL/ERROR, медиана/максимум (timing), aborted.

## 4. Ресурсы (E2E-11, без порога)

| Роль | Процесс | RSS до→после | Потоки | Дескрипторы | Тот же PID | GStreamer/сироты | MemAvailable |
|---|---|---|---|---|---|---|---|

## 5. Журналы (E2E-12)

Блок — новые `session from` / `video: session from` на цикл; пульт — строки journald, экспорт журнала приложения;
канарейки REQ-SW-005.

## 6. После E2E — регрессии OTA

ota-status pi2 COMMITTED/B/V; pi1 IDLE/A; `slot_guard --mode unchanged` pi1 PASS; pi2 autoboot / dt.tryboot /
persistent / роли / сертификаты как до; failed пусто; health PASS без SKIP; check_video_link session, stream,
leftovers PASS.

## 7. Отклонения и дефекты

| № | Наблюдение | Тип | Владелец зоны | Ссылка |
|---|---|---|---|---|

## 8. Итог

Доказано на платах (L4, стенд ≠ STAND-1); только моделью (не кабель); NOT TESTED / BLOCKED и причины;
Must — NOT TESTED; REQ-REL-001 — NO-GO.
