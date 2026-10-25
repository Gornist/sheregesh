# S5 «UNIVERSAL VIDEO INPUT» — отчёт (промежуточный: программная часть закрыта, ждём адаптеры)

Дата: 2026-10-25, ревизия: `v2 b3af093` (код на платах — `g980c45`, `software/` идентичен). Порядок и пороги —
[`ACCEPT.md`](ACCEPT.md).

## 1. Итог в одной строке

**PASS с BLOCKED.** RTSP с RTP/UDP и RTP/TCP (авто, принудительно, переход при недоступном транспорте), H.264 и
MJPEG, потеря/возврат и переключения проверены на двух Pi 4; GUI показывает «Воспроизведение» только по кадрам нового
источника; CLI выбирает источник через работающий пульт без второго сеанса. CVBS/HDMI — **BLOCKED: hardware
unavailable** (адаптеры 27–28.10), реальная Ethernet-камера — BLOCKED (нет камеры).

## 2. Коммиты (v2, S5)

| SHA | Агент | Кратко |
|---|---|---|
| `51d0fbb` `b577cd1` | NETWORK | `transport: auto\|tcp\|udp` у RTSP-источника (rtspsrc `udp+tcp`/`tcp`/`udp`), тестовый сервер `--transports`, `--codec h264`, `--udp-drop`; тесты транспорта (реальный rtspsrc); S3.3 §13, [`S5-network-camera.md`](../../architecture/S5-network-camera.md) |
| `a4479ec` | главный | `transport` в SPEC_KEYS блока; `transport`/`transport_mode` доходят до пульта |
| `711e657` `3416bc5` `07cab62` | VIDEO | аналоговые захваты: `deinterlace` перед v4l2h264enc, выбор входа `composite\|s-video\|N` (S_INPUT), `standard: auto` только по однозначному QUERYSTD (иначе PAL) и перепроверка перед запуском; таблица чип→драйвер→риск |
| `aa62941` | GUI | GUI-01: `videosource.Handover` — «Подключение…» до кадров нового источника |
| `980c45a` | GUI | GUI-02: локальный канал управления пульта (unix-сокет 0600), `videoselect --via auto\|console\|direct` |
| `cd3e251` `a18e072` `d6a0142` `a171aa3` `af4c6b8` | QA | `s5_capture_diag`, `s5_accept`, `s5_rtsp_matrix`, ACCEPT, шаблон отчёта |
| `b3af093` | главный | материалы стенда, доки, правка теста |

## 3. CI и тесты

- Локально (Mac): 1008 passed, 2 skipped; после правки `test_video_ingest` — файл 17/17. QA: 80 passed.
- CI v2 `b3af093` — run 40208746036: **success** (весь код S5 на платах).
- S4: CI 40203379085 (`4d562f7`) — success → **S4 закрыт**.

## 4. Версии на платах

| | pi1 (пульт, `10000000568152de`) | pi2 (блок, `100000009d497e91`) |
|---|---|---|
| Образ / слот / OTA | b1d95a2 / B / COMMITTED — без изменений | 57e8fe4 / A / ROLLED_BACK — без изменений |
| DEV-выпуск | `0.1.0~dev0+g980c45ab1c5f` | `0.1.0~dev0+g980c45ab1c5f` |
| Откат | `shdeploy rollback pi1` → gf404014 | `shdeploy rollback pi2` → ga4479ec |
| `video-input.json` | — | `{"source":"synthetic","rtsp_urls":[PI1 RTSP test]}` (исходный восстановлен) |
| Службы | active, NRestarts 0, failed 0 | active, NRestarts 0, failed 0 |

## 5. Матрица поддерживаемых источников

| Источник | Транспорт / формат | Статус | Доказательство |
|---|---|---|---|
| Synthetic | videotestsrc | PASS (S4, S5-05) | — |
| RTSP + RTP/UDP | MJPEG, H.264 | **PASS на платах** | [`run-rtsp-20261025-1833/`](run-rtsp-20261025-1833/) S5-02 |
| RTSP + RTP/TCP (interleaved) | MJPEG, H.264 | **PASS на платах** | S5-02 |
| RTSP auto (UDP → TCP) | сервер только TCP (461) → TCP; только UDP → UDP; UDP молчит → TCP через `timeout_s` (5 с) | **PASS на платах** | S5-03, [`hw/rtsp-matrix/matrix.txt`](hw/rtsp-matrix/matrix.txt) |
| RTSP, недоступный принудительный транспорт | udp@только-TCP, tcp@только-UDP → блок `error`, пульт «ошибка», без зависания | PASS | S5-03 |
| `rtspt://` | только TCP | модульно | `tests/unit/test_rtsp_transport_unit.py` |
| Сырой RTP/UDP без RTSP | — | не реализован (по ТЗ), точка расширения — S3.3 §13 | — |
| USB HDMI (UVC, MS2109/MS2130) | MJPEG / YUYV | **BLOCKED: hardware unavailable** | — |
| USB CVBS EasyCAP (usbtv / stk1160 / em28xx) | YUYV/UYVY, PAL/NTSC | **BLOCKED: hardware unavailable** | — |
| EasyCAP на SMI2021 | — | драйвера в ядре нет → BLOCKED даже после доставки | [`VIDEO_CAPTURE_COMPAT.md`](../../hardware/VIDEO_CAPTURE_COMPAT.md) §0 |
| Лабораторная RTSP-камера «shcam» (Pi 5 + IMX477, MediaMTX, H.264 Baseline 1280×720@30, 3 Мбит/с) | RTP/UDP, RTP/TCP, auto | **PASS на платах** (реальный сенсор; промышленная IP-камера — не проверялась) | [`hw/shcam/`](hw/shcam/) |

## 6. Физические испытания (две Pi 4, сервер на pi1, источник на pi2, ≈ 4,3 мин)

| ID | Итог | Измерено |
|---|---|---|
| S5-01 службы, связь | PASS | active, CONNECTED |
| S5-02 {udp,tcp,auto}×{mjpeg,h264} | PASS 6/6 | фактический транспорт по статусу (udp/tcp) или сокетам rtspsrc (auto → udp); 25 к/с; пульт live |
| S5-03 один транспорт недоступен | PASS 5/5 | auto@tcp → tcp, auto@udp → udp, auto@UDP-молчит → tcp; принудительный недоступный → error |
| S5-04 потеря сервера | PASS | «нет видео» 1,5 с (≤ 3), блок connect-error, возврат 3,5 с (≤ 10) |
| S5-05 переключения synthetic↔rtsp | PASS | 0,2–2,1 с (≤ 15), ресурсы без роста, 1 конвейер |
| S5-06 выбор кнопкой GUI (10 источников) | PASS | «Переключение…» (live=false) → «Воспроизведение» с кадрами нового источника; снимки `gui-rtsp/` |
| S5-07 журналы, OTA | PASS | Traceback 0, failed 0, NRestarts 0; slot_guard обеих плат без неожиданных отличий |
| GUI-02 на pi1 | PASS | `videoselect --list / rtsp:0 / synthetic`: `via: console`, `result ok`, переподключений пульта нет ([`hw/gui02-cli/`](hw/gui02-cli/)) |

## 7. Реальные параметры адаптеров

Не измерены — адаптеры не получены. Ожидания по чипам (гипотезы до `lsusb`) — `VIDEO_CAPTURE_COMPAT.md` §0.

### 6a. Реальная камера «shcam» (`rtsp://192.168.1.193:8554/cam`, подключена параллельной сессией)

| Проверка | Итог | Измерено |
|---|---|---|
| transport=udp | PASS | streaming за 1,8 с, `transport=udp`, сокеты rtspsrc UDP 2 + TCP 1 (RTSP), 1280×720, 30 к/с, пульт live |
| transport=tcp | PASS | 2,9 с, `transport=tcp`, только TCP, 30 к/с |
| transport=auto | PASS | 2,9 с, по сокетам — UDP, 30 к/с |
| обрыв камеры (SIGKILL mediamtx на 6 с) | PASS | «нет видео» 1,8 с (≤ 3), блок connect-error → starting, возврат 2,1 с после запуска камеры (≤ 10) |
| выбор кнопкой GUI | PASS | «Переключение…» → «Воспроизведение», карточка «shcam Pi5 IMX477 · активный», живая картинка ([`hw/shcam/gui/`](hw/shcam/gui/)) |
| нагрузка pi2 | — | H.264 камеры идёт без перекодирования: gst-launch ≈ 9 % ядра |

Средство: `tools/probe/s5_shcam_check.py`. Штатный `systemctl stop mediamtx` с rpiCamera на камере висит до тайм-аута
systemd (90 с) — особенность стенда камеры, не нашего ПО; отказ моделируется SIGKILL.

## 7a. Дефекты S5

| № | Серьёзность | Описание | Статус |
|---|---|---|---|
| S5-D1 | P2 | **Выбор источника не сохраняется на плате**: `video select … ok: selection not saved: [Errno 30] Read-only file system` — юнит `sheregesh-remote-net` (`ProtectSystem=strict`, `ProtectHome=read-only`, без `ReadWritePaths`/`StateDirectory`) не даёт записать `video-input.json`; выбор действует до перезапуска службы/платы, затем — источник из файла (безопасно). L3-тесты S4 шли вне песочницы systemd. Найдено параллельной сессией «Камера» | исправление только образом (юнит в /usr): `StateDirectory=sheregesh-remote` + хранение выбора там (или `ReadWritePaths` каталога конфигурации); отдельная задача предложена владельцу |
| S5-D2 | наблюдение (стенд) | pi1 перезагрузилась несколько раз подряд (≈ 16:12–16:16 UTC; журналы обрываются без завершения работы) **одновременно с камерой** (uptime камеры 2 мин); pi2 — без перезагрузок (8 ч). `throttled=0x0` после загрузки. Вероятно, питание pi1 и камеры (общий источник) или работы у стенда. OTA pi1 после этого: B/COMMITTED — без изменений | нужен ответ владельца |
| S5-D3 | косметика | заголовок пульта «Эмуляция · только синтетические данные» и подпись «Подключений к реальным приёмникам и аппаратам нет» при живой камере не соответствуют источнику | открыт |

## 8. Известные ограничения

1. При `transport: auto` блок не знает согласованный транспорт (`transport=""`; `gst-launch -q` не сообщает) —
   приёмка определяет его по сокетам rtspsrc в `/proc/net/{udp,tcp}`.
2. Аналоговые драйверы (usbtv/stk1160/em28xx) не сообщают потерю сигнала (NO_H_LOCK) — «нет сигнала» только по
   прекращению кадров; адаптер, шлющий синее поле без сигнала, будет выглядеть как поток.
3. usbtv не поддерживает QUERYSTD → `standard: auto` даёт PAL; NTSC — явным `"standard": "ntsc"`.
4. MJPEG 1080p30 декодируется программно (jpegdec, одно ядро A72) — по оценке ½–1 ядра; auto выбирает 720p30.
5. Сокет управления пульта оказывается в `/run/user/<uid>` (pam_systemd заменяет `XDG_RUNTIME_DIR` юнита); CLI ищет
   оба пути. Закрепить путь в юните можно только образом (не DEV deploy).
6. Снимки GUI — Qt offscreen на pi1; cage/HDMI пульта — BLOCKED (нет дисплея).

## 9. Время приёмки

RTSP-матрица S5-01…07 — 4,3 мин; ручная матрица транспортов — ≈ 3 мин; GUI-02 — < 1 мин. На каждый адаптер
план — ≤ 5 мин диагностики + 7–10 мин приёмки.

## 10. Дальше

1. 27–28.10: `python3 tools/probe/s5_capture_diag.py --remote 192.168.1.171 --plug` для каждого адаптера, затем
   `python3 tools/probe/s5_accept.py --source v4l2:<id> --gui --manual-fault cable [--standard pal|ntsc]` (CVBS —
   PAL и NTSC; HDMI — фактические режимы из диагностики).
2. S5-D1 — исправление юнита в ближайшем образе (без нового OTA — только по решению владельца).
3. Следующий спринт (рекомендация): S6 — закрепление станции одного потока: образ с изменениями S3.3–S5 (юниты:
   путь сокета, без DEV deploy), выбор лучшего дешёвого адаптера по итогам S5, HDMI-дисплей пульта.
