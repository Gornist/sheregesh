# Видео пульта мимо Python (ADR-0015): образ, OTA pi1 — состояние на 2026-10-26

**Итог: PASS на pi1.** Образ `db36670` установлен по OTA и подтверждён (COMMITTED, слот A) по решению владельца;
путь видео GL (ADR-0015) на плате: камера 1280×720 при 30 к/с, задержка в пульте 4,7 / 6,9 мс (p50/p95), пульт 34 %
ядра, gst 8 %. pi2 не менялся (слот B, unchanged 44/0/0). Дисплей к pi1 не подключён — замеры на виртуальном
выходе cage (рендер на GPU платы); рабочий пульт без дисплея — на прежнем пути, 30 к/с.

## Образ `db36670` (devbox, `os/docker-build.sh`, SHEREGESH_BUILD_AS_ROOT=1)

| Проверка | Итог |
|---|---|
| build-report | commit `db3667031e71…`, `dirty: false`, `verify_exit: 0`; image_sha256 `9920503f…1f85` |
| Пакеты GL | `gstreamer1.0-qt6`, `gstreamer1.0-gl`, `python3-gi`, GIR — в `packages.tsv` (1.26.2 = gst платы) |
| owners_check | = эталон (`owners-bd74c8c`), diff пуст |
| order_check (8 прогонов) | PASS, циклов нет |
| OTA-пакет | `sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1793024692.shota`, sha256 `ee3ec8a0d02849f37990e4becf811365f704a326e8895a697927c61770a10988`, две сборки — REPRODUCIBLE; подпись — Good (allowed_signers pi1) |

## OTA pi1 (`ota-pi1/`)

| Шаг | Итог |
|---|---|
| 0 | verify_payload PASS; ota-status COMMITTED B, consistent; ota-disable; снимки pi1/pi2; check_link и check_video_link PASS |
| 1 | `ota-install` OK → INSTALLED, target A, V 0.1.1793024692 (3 мин 16 с) |
| 1a | `slot_guard compare --mode install` 44/0/0 (после исправления направления B → A в slot_guard); pi2 unchanged 44/0/0 |
| 2–3 | `ota-enable` (решение владельца); **первый tryboot** — прошивка загрузила прежний B без флага tryboot (как в цикле 2 b1d95a2, `../e2e/ota-pi1/cycle2/TRYBOOT-FAIL.md`), агент: ROLLED_BACK; сверка отката 36/0/0 |
| retry 1 | повторная установка → INSTALLED, 43/1 (разрешение уже выдано — ожидаемо) |
| retry 3 | tryboot → A (p2, `db36670`, флаг tryboot 1), SSH через 20 с, адрес .92 |
| retry 4 | uptime 216 с: HEALTH_PENDING; /var = `slots/system_a/var`, var-seed success, failed нет, циклов 0, console active без перезапусков; health 15/15; trial 26/0/0; check_link 4/4, check_video_link 5/5; pi2 unchanged 44/0/0; GL-плагины и python3-gi на месте |
| retry 5 | `ota-confirm` OK → COMMITTED A, 19/19 критериев |
| retry 6–7 | 3 ожидаемых FAIL в той же загрузке; контрольная перезагрузка — A (p2, tryboot 0) за ~15 с, failed нет, циклов 0, видео session/stream PASS; `ota-disable` |

Код агента обновлений, health и deploy с образа pi1 (`b1d95a2`, прошлый успешный OTA) не менялся.

## Путь GL на плате (`matrix/`, `screens-pi1-gl/`)

| Путь | Кадр | Пульт CPU | gst CPU | к/с | Задержка p50 / p95 |
|---|---|---|---|---|---|
| Python (прежний) | 640×360 | 33 % | 28 % | 30 | 5,4 / 8,8 мс |
| **GL** | 1280×720 | 33,9 % | 7,7 % | 30 | 4,7 / 6,9 мс |
| GL, разделы каждые 2 с | 1280×720 | 44,3 % | 6,4 % | 30 | 7,7 / 12,6 мс; открытие 13–37 мс |

Найдено и исправлено: конвейер стартует раньше, чем окно выходит на экран, и оставался на прежнем пути —
`VideoReceiver._gl_reconcile` раз в секунду перезапускает конвейер на нужный путь (и обратно при потере дисплея).

Не проверено: физический дисплей, задержка «стекло-стекло», 1080p (камера отдаёт 1280×720).
