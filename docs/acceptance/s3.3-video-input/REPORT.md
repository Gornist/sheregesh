# S3.3 — Universal Video Input: результаты испытаний

Архитектура и исследование — [S3.3-universal-video-input.md](../../architecture/S3.3-universal-video-input.md).
Это не приёмка требований: испытания Must не отмечаются PASS, гейт REQ-REL-001 — **NO-GO**. Настоящего устройства UVC
и HDMI-генератора нет — аппаратные сценарии с UVC **NOT TESTED / BLOCKED** (§3).

Ветка `s3.3-video-input` (база `origin/v2` d44798a) слита в `v2` merge-коммитом **`a0ba631`** (2026-10-24). Выпуск
`0.1.0~dev0+ga0ba6311bee4` развёрнут штатным DEV Deploy на pi1 и pi2; синтетический режим, защищённый видеоканал и
телеметрия на платах не деградировали по сравнению с S3.1-VIDEO (§5). Разделы §1–§4 — испытания субагента до слияния.

## 1. Автоматические испытания (L2/L3, без устройств)

Устройства подставные, но структуры настоящие.
- `tests/system/fake_v4l2.py` строит дерево sysfs/`/dev`, как у Pi 4 с HDMI→USB-адаптером, и отвечает на ioctl структурами `videodev2.h`. В дереве:
  - узел захвата `uvcvideo` и узел метаданных UVC;
  - `bcm2835-codec` `/dev/video10–12`, `bcm2835-isp` `/dev/video13–16`, `unicam` `/dev/video19`;
  - ссылки `/dev/v4l/by-id`.
- Процесс захвата — `tests/system/fake_capture.py`: кадры RGBx, как у `gst-launch-1.0 v4l2src … ! fdsink`. Есть режимы «завершиться, как при отключении USB» и «зависнуть».

| Сценарий задания | Испытание | Итог |
|---|---|---|
| Обнаружение устройства | `test_capture_device_distinguished_from_service_nodes`, `test_modes_parsed_from_real_structures`, `test_stepwise_sizes_and_continuous_intervals`, `test_local_input_plays_and_reports_source` | PASS |
| Служебные узлы не открываются | там же: `bcm2835-codec`, ISP, `unicam` не открывались; метаданные UVC отсеяны по `V4L2_CAP_META_CAPTURE` | PASS |
| Не захватывать без разрешённой конфигурации | `test_scan_without_probe_opens_nothing`; `test_configuration_without_video_fields_keeps_previous_behaviour`; `test_unsupported_mode_is_reported_without_capture` | PASS |
| Отсутствие устройства | `test_device_absent_or_other_device`, `test_absent_device_does_not_block_and_hotplug_is_picked_up`, `test_missing_device_is_shown_with_reason_and_event` (экран «Нет видеоустройства» + причина, VIDEO-LOST, GUI работает), `test_agent_without_uvc_device_keeps_running_and_reports_reason` | PASS |
| Нет прав / нет sysfs / исчезновение во время опроса | `test_no_sysfs_and_no_permission`, `test_vanishing_device_during_probe_is_not_fatal` | PASS |
| Неподдерживаемый формат | `test_unsupported_format_reports_what_device_offers`, `test_unsupported_mode_is_reported_without_capture` (состояние `unsupported`, перечень режимов, процесс не запускался) | PASS |
| Несколько устройств | `test_several_devices_require_explicit_choice` | PASS |
| Переключение источников | `test_source_switching_leaves_no_processes`, `test_disabled_and_synthetic_sources` | PASS |
| Потеря кадров | `test_local_input_plays_and_reports_source` (> 20 к/с при 30 к/с источника за 2 с) | PASS |
| Отключение устройства | `test_disconnect_then_recovery` (`restarting` → `no-device`), `test_unplug_and_replug` | PASS |
| Восстановление | там же: новый `/dev/videoN` с тем же `vvvv:pppp` → `playing` | PASS |
| Зависание устройства | `test_stalled_device_restarts_capture` (процесс убит, перезапуск) | PASS |
| Сбой процесса обработки | `test_capture_crash_is_restarted_without_freezing` | PASS |
| Нет лишних процессов | `test_source_switching_leaves_no_processes` (3 запуска — 0 живых), `test_disconnect_then_recovery`, `test_stalled_device_restarts_capture`, `test_uvc_source_selected_in_settings_survives_application_update` | PASS |
| Нет регрессий синтетического режима | `test_synthetic_pipeline_unchanged` (конвейер побайтно прежний) и все прежние видеотесты: `tests/system/test_video_link.py`, `tests/gui/test_video_remote.py`, `tests/gui/test_video_and_pages.py`, `tests/unit/test_video_codec.py`, `tests/unit/test_check_video_link.py` | PASS |
| Сохранение конфигурации после обновления приложения | `test_uvc_source_selected_in_settings_survives_application_update`: новый процесс на том же каталоге данных (на плате `/var/lib/sheregesh`, его сохраняют DEV deploy и OTA) — та же ревизия и режим, снова `playing`; ревизии без `video_*` корректны | PASS |
| Схема конфигурации | `tests/unit/test_config_schema.py`: +3 корректных, +6 некорректных набора (`dji`, `off`, `1280x720@25`, `nv12`, `/dev/video0`, `;reboot`) | PASS |
| Конвейеры | `test_local_capture_decodes_device_format_without_encoding`, `test_yuyv_needs_no_decoder`, `test_h264_device_uses_selected_hardware_decoder`, `test_passthrough_when_device_format_matches_channel_codec`, `test_h264_passthrough` | PASS |
| Настоящий GStreamer | `test_capture_pipeline_runs_in_gstreamer[YUYV/MJPG]`, `test_mjpeg_passthrough_pipeline_runs_in_gstreamer` (Mac: Homebrew GStreamer 1.28.8; CI: Ubuntu 24.04) | PASS |

## 2. Проверки на настоящем GStreamer (devbox, Docker)

Среда:
- хост `devbox-lan`, контейнер `ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest`;
- GStreamer 1.26.2, как в образе плат;
- `--cpus=2`, `nice`; пакеты ставились только внутри контейнера; каталог `/srv/build/s33-vin` удалён после прогона;
- **x86_64, не Pi 4.**

| Формат → вывод RGBx 640×360 | к/с | Интервал p50/p95/max, мс | До 1-го кадра, мс | ЦП обработки, % ядра | мс/кадр |
|---|---|---|---|---|---|
| YUYV 640×480@30 | 30,0 | 33,3 / 35,9 / 44,8 | 35 | 3,7 | 1,23 |
| YUYV 640×480@60 | 60,0 | 16,7 / 18,9 / 27,9 | 42 | 7,2 | 1,20 |
| MJPEG 1280×720@30 | 30,0 | 33,4 / 36,5 / 39,6 | 50 | 3,3 | 1,09 |
| MJPEG 1280×720@60 | 60,0 | 16,7 / 20,0 / 21,7 | 72 | 6,2 | 1,05 |
| MJPEG 1920×1080@30 | 30,0 | 33,3 / 37,1 / 38,7 | 61 | 4,0 | 1,33 |
| MJPEG 1920×1080@60 | не измерено — эмулятор сам упирается в ЦП | | | | |
| H.264 1280×720@30 (avdec_h264) | 30,0 | 33,4 / 36,7 / 41,2 | 148 | 1,9 | 0,64 |
| H.264 1920×1080@30 (avdec_h264) | 30,0 | 33,2 / 38,5 / 45,2 | 180 | 2,5 | 0,85 |

Сквозная передача удалённого блока (`stream_sender_args` → `stream_receiver_args`, 6 с, 1280×720@30): MJPEG→jpeg и H.264→h264 без перекодирования — по 181 кадру; YUYV→jpeg и MJPEG→h264 с кодером — тоже по 181 кадру.

## 3. Аппаратные испытания — NOT TESTED / BLOCKED

Оборудования (USB UVC-камеры, HDMI→UVC-адаптера, HDMI-генератора) на стенде нет; действующие FPV-системы не подключались. После слияния на платах выполнены только проверки без устройства (§5.4: обнаружение рядом с системными узлами V4L2, поведение без устройства — это часть H1 и H2 без устройства). Аппаратная поддержка UVC **не подтверждена**. Процедура:

| № | Сценарий | Статус | Процедура и критерий |
|---|---|---|---|
| H1 | Обнаружение на Pi 4 | NOT TESTED с устройством; без устройства — PASS (§5.4) | pi1 от `sheregesh-ui`: `PYTHONPATH=<выпуск>/software python3 -m sheregesh.videoinput` без устройства и с адаптером: `CAPTURE` — только `uvcvideo` index 0; `video10–12`, ISP, unicam — `other` с причиной; режимы перечислены; отдельно `--no-probe` |
| H2 | Права доступа | NOT TESTED (устройства нет; `sheregesh-ui` в группе `video`, узлы `root:video 660` + ACL) | `id sheregesh-ui`, `ls -l /dev/video*`, `getfacl /dev/video0`: открытие из сеанса cage без `Permission denied` |
| H3 | Изображение HDMI-генератора | NOT TESTED | генератор 1280×720@30 → адаптер → pi1. В настройках выбрать «Стандартное UVC-устройство» и «1280x720@30». Ожидается: имя устройства, MJPEG 1280×720, 30 к/с, `playing`; `ui-state.json` → `video.mode == "local-input"` |
| H4 | Частота и нагрузка | NOT TESTED | `python3 tools/dev/video_input_bench.py --device <vvvv:pppp> --mode 1280x720@30 --seconds 60` (+ `1920x1080@30`, `640x480@60`); ≥ 29 к/с; ЦП, `vcgencmd measure_temp`, `get_throttled` |
| H5 | Задержка «стекло-стекло» | NOT TESTED | счётчик мс генератора на втором мониторе рядом с дисплеем пульта; 20 фото с выдержкой ≤ 1/250 с; задержка — разница показаний |
| H6 | Отключение и восстановление USB | NOT TESTED | выдернуть адаптер: «Нет видеоустройства» ≤ 3 с, GUI отзывчив; вставить: `playing` ≤ 10 с; `pgrep -af gst-launch` — один процесс |
| H7 | Неподдерживаемый режим | NOT TESTED | режим, которого нет у адаптера: «Режим не поддерживается», перечень режимов, процесс не запущен |
| H8 | Переключение источников | NOT TESTED | UVC → Выключено → Синтетический → UVC: после каждого шага 0 или 1 процесс; синтетический поток pi2 снова `remote-tls playing` |
| H9 | Сохранение после DEV deploy | NOT TESTED | UVC → `shdeploy` нового выпуска на pi1 → после перезапуска `video_source: uvc`, `playing` |
| H10 | Удалённый блок с UVC | BLOCKED | нужны аргументы `--video-source uvc` в `sheregesh-remote-net.service` (юниты менять нельзя) — решение основной сессии |
| H11 | Walksnail Ascent VRX Pro по UVC | NOT TESTED, вне 1.0 | действующий приёмник не подключать; только по решению владельца: H1 покажет, стандартный ли это UVC |

**Важно для OTA:** если на pi1 выбрать `video_source: uvc`, критерий подтверждения слота `video` (`update/confirm.py`) не пройдёт: он ждёт `remote-tls`. Во время испытаний OTA держать `synthetic`.

## 4. Итоги прогонов

| Набор | Где | Итог |
|---|---|---|
| `pytest -q -p no:cacheprovider` | Mac, ветка | 472 passed, 2 skipped, 1 failed. Падение исправлено, после этого видео- и GUI-файлы — 43 passed; полный набор на ветке после исправления не перезапускался |
| `pytest -q -p no:cacheprovider` | Mac, пробное слияние с origin/v2 4b90bb0 (без коммита документа) | 530 passed, 2 skipped |
| `python3 -m unittest discover -s tests/tools -p 'test_*.py'` | Mac, ветка | 46 OK |
| `tools/security/scan.py scope` / `secrets` | Mac | 0 / 0 |
| CI `ci.yml` (workflow_dispatch, 42778f5) | GitHub | success — https://github.com/Gornist/sheregesh/actions/runs/40124199991 |
| `git merge-tree origin/v2 s3.3-video-input` | — | без конфликтов |

## 5. Интеграция в v2 и испытания на платах (основная сессия, 2026-10-24)

### 5.1. Слияние

- Состав ветки: 7 коммитов субагента и отчёт; 19 файлов, +1762/−49. OTA, юниты, PKI, `os/*`, `tools/image/*`, `tools/deploy/*`, `videolink.py`, требования не затронуты.
- Новых демонов, системных пакетов и Python-зависимостей нет: `videoinput.py` использует только stdlib (`fcntl`, `struct`, `os`), захват — `v4l2src` из уже установленного gst-plugins-good. Конвейеры по-прежнему собирает только `video.py`; удалённый источник идёт тем же `VideoServer` через TLS 1.3 на 7444 — второго видеотракта нет.
- `git merge --no-ff origin/s3.3-video-input` → **`a0ba631`**, конфликтов нет.
- После слияния, на Mac: `pytest` — 501 passed, 2 skipped, 53 подтеста; `unittest discover -s tests/tools` (как в CI) — 71 OK; `scan.py scope/secrets` — 0/0. CI — см. итоговую строку в §5.7.

### 5.2. Развёртывание и откат

- Перед развёртыванием: OTA pi2 — `IDLE`, активный A, разрешения переключения нет. Значит, запись слота не начиналась и DEV Deploy pi2 разрешён.
- Снимки «до»: [`hw/pi1-before.json`](hw/pi1-before.json), [`hw/pi2-before.json`](hw/pi2-before.json) (`slot_guard`); конфигурация приложения — [`hw/*-config-before.txt`](hw).
- Порядок: pi1 → проверка смешанной пары (pi1 новый, pi2 прежний: session, stream, health — PASS) → pi2. Обе службы READY, без перезапусков 40 с.
- Откат и повторное развёртывание на каждой плате ([`hw/rollback-pi1.txt`](hw/rollback-pi1.txt), [`hw/rollback-pi2.txt`](hw/rollback-pi2.txt)): `rollback` → `g37adc72c2bb1` READY, `deploy` → `a0ba631` READY. Хеш дерева ревизий `/var/lib/sheregesh/config` (7 файлов), состояние (`current = confirmed = r000001`) и `~/.config/sheregesh` совпадают на всех этапах; полей `video_*` в ревизиях нет — источник по умолчанию **synthetic**.

### 5.3. Физические испытания (синтетический режим) — сравнение с S3.1-VIDEO

| № | Критерий | Результат | Доказательство |
|---|---|---|---|
| 1 | Приложение запускается без ошибок | PASS — службы READY; записей журнала уровня err за 90 мин нет на обеих платах | `deploy-*.log`, журнал |
| 2 | Роли сохранены | PASS — `console.role`/`remote.role`, отпечатки идентичности совпадают со снимком «до» | `hw/*-unchanged-compare.txt` |
| 3 | SyntheticSource работает | PASS | `video/stream.json` |
| 4 | Защищённый видеоканал поднимается | PASS — TLSv1.3, `urn:sheregesh:remote:…`, `tls1.3-mutual` | `video/session.json` |
| 5 | Приём и декодирование ≈ 30 к/с | PASS — 29,79 и 29,91 к/с (S3.1: 29,89) | `video/`, `final-video/stream.json` |
| 6 | Телеметрия вместе с видео | PASS — 2,16 сообщ./с, потерь событий 0, `seq_gaps` 0 (S3.1: то же) | `video/telemetry.json` |
| 7 | Health обеих ролей | PASS — 15/15 и 14/14 | `link/health.json`, `final-link/` |
| 8 | Нет ошибок, перезапусков, остатков GStreamer | PASS — NRestarts 0, по одному `gst-launch-1.0` на плате, `leftovers` PASS (в т. ч. после сценариев восстановления) | `video-recovery/`, `final-video/leftovers.json` |
| 9 | Нет новых портов | PASS — `ports`: pi2 — 22, 7443, 7444; pi1 — 22 (как в S3.1-VIDEO); `no-plain`: UDP-видео нет | `link/ports.json`, `video/no-plain.json` |
| 10 | Deploy и rollback не повреждают конфигурацию | PASS | §5.2 |
| 11 | Нет UVC-устройства | PASS (без устройства) | §5.4 |
| 12 | Системные узлы video не мешают обнаружению | PASS | §5.4 |

Сценарии восстановления и безопасности (как в S3.1-VIDEO), [`hw/video-recovery/`](hw/video-recovery), [`hw/link-outsider/`](hw/link-outsider): `unauth`, `ctl-loss`, `sender-kill`, `restart`, `leftovers`, `outsider` — PASS; `identity` PASS; `version` — PASS после исправления инструмента (§5.6).

**Фактические показатели** (60 с, `resources`, `latency`):

| Показатель | S3.3 (`a0ba631`) | S3.1-VIDEO |
|---|---|---|
| Отправлено / принято / декодировано, к/с | 31,36 / 29,79 / 29,79 | 31,45 / 29,89 / 29,89 |
| Битрейт, кбит/с | 2008 | 2015 |
| Разрывы, потери, ошибки RTP, перезапуски декодера | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| Транспорт pi2→pi1, мс p50 / p95 / max | 1,77 / 33,2 / 39,2 | 1,71 / 34,5 / 43,2 |
| Приёмная часть pi1, мс p50 / p95 | 22,8 / 30,1 | 22,6 / 30,5 |
| pi1: ЦП (4 ядра), ОЗУ, температура, троттлинг | 15,4 %, 352 МБ, 49,1→48,7 °C, 0x0 | 15,4 %, 339 МБ, 51,1→50,6 °C, 0x0 |
| pi1: приложение / декодер, % ядра, RSS | 30,5 % 215 МБ / 27,9 % 16 МБ | 29,5 % 204 МБ / 27,9 % 16 МБ |
| pi2: ЦП (4 ядра), ОЗУ, температура, троттлинг | 20,3 %, 170 МБ, 54,0→55,0 °C, 0x0 | 20,3 %, 164 МБ, 52,5→52,1 °C, 0x0 |
| pi2: агент / генератор+кодер, % ядра, RSS | 2,8 % 31 МБ / 74,6 % 22 МБ | 2,9 % 29 МБ / 75,6 % 22 МБ |

Отличия в пределах разброса прогонов. RSS приложения пульта +10 МБ — модуль `videoinput` и новые элементы QML.

### 5.4. V4L2 без устройства на Pi 4 (H1/H2 без устройства)

[`hw/v4l2-discovery.txt`](hw/v4l2-discovery.txt): от `sheregesh-ui` на обеих платах `python3 -m sheregesh.videoinput` с опросом и без. 14 системных узлов (`bcm2835-codec` ×5, `bcm2835-isp` ×8, `rpi-hevc-dec`) классифицированы как вспомогательные, ошибок нет, код 0; устройств захвата нет; `open_uvc()` → «UVC device not found».

- pi1 ([`hw/pi1-console-no-uvc.txt`](hw/pi1-console-no-uvc.txt)): `VideoReceiver` пульта с источником UVC (без дисплея, offscreen). Состояние `no-device`, режим `local-input`, ошибка «UVC device not found»; процессов `v4l2src` 0 во время и после. Настройки pi1 при этом не менялись.
- pi2 ([`hw/pi2-remote-no-uvc.txt`](hw/pi2-remote-no-uvc.txt)): временный экземпляр удалённого блока `--video-source uvc` только на 127.0.0.1 (порты 7643/7644, свой файл состояния). Работает 12 с, «VIDEO unavailable: UVC device not found», дочерних GStreamer нет; после остановки процессов и портов не осталось, рабочая служба не перезапускалась.

### 5.5. Совместимость с OTA

- Источник видео на обеих платах — **synthetic** (полей `video_*` в конфигурации нет). Сертификаты, роли, TLS и видеотранспорт не менялись (`slot_guard compare --mode unchanged`: идентичность, слоты, `autoboot.txt`, система A — PASS; ни одного записанного сектора на p1–p5; ожидаемые расхождения только `deploy.status` — новый выпуск — и InvocationID службы роли — перезапуск развёртыванием).
- OTA не включался: pi2 — `IDLE`, разрешения нет, tryboot/confirm не вызывались, watchdog не трогался. Пакет `0.1.1792865618` не изменился: SHA-256 `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb`.
- Влияние на будущий preflight OTA — в [`../s3.2-ota/preflight/PREFLIGHT.md`](../s3.2-ota/preflight/PREFLIGHT.md) §8.

### 5.6. Дефекты и исправления

| Дефект | Статус |
|---|---|
| `check_link version`: временный сервер протокола 2.0 занимал видеопорт 7444 рабочей службы (с S3.1-VIDEO, `4d3e9b6`; в паре с видео не запускался) | исправлено: временному серверу `--no-video`; PASS |
| **app-stop** (OPEN с S3.1): останов пульта → код 143 → юнит `failed`. Воспроизводится и с `a0ba631` (журнал pi1, 20:59 — останов выпуска `a0ba631`). Причина: юнит запускает `/usr/bin/sheregesh-notify-relay` — ретранслятор **образа** `57e8fe4` (`/usr/lib/sheregesh`, sha 72eefcb3…), исправленный ретранслятор `02a9b64` есть в выпуске DEV (sha c991991c…), но DEV Deploy его не доставляет | **OPEN**; исправление придёт только с образом (`a7f1b74`, OTA-пакет `f6e19234…` — проверено на образе диска). К S3.3 не относится |
| H10: удалённый блок с UVC требует `--video-source uvc` в юните | BLOCKED (юниты не менялись) |

### 5.7. Итог

S3.3 в `v2` (`a0ba631`). Автоматические тесты проходят: CI `ci.yml` — success на `a0ba631` (run 40126623158) и на коммите испытаний `4138e49` (run 40128669795). Функции S3.1 на платах не деградировали. Синтетический режим, канал, видео, телеметрия, health, порты, deploy/rollback — PASS. Работа без устройства и рядом с системными узлами V4L2 — PASS. Аппаратная поддержка UVC **не подтверждена**: H1–H9 с устройством NOT TESTED, H10 BLOCKED, H11 вне 1.0.
