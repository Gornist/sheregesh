# S3.3 — Universal Video Input: результаты испытаний

Архитектура и исследование — [S3.3-universal-video-input.md](../../architecture/S3.3-universal-video-input.md).
Это не приёмка требований: испытания Must не отмечаются PASS, гейт REQ-REL-001 — **NO-GO**. На платах pi1/pi2 ничего не
запускалось. Настоящего устройства UVC и HDMI-генератора нет — аппаратные сценарии **NOT TESTED / BLOCKED**.

Ветка `s3.3-video-input` (база `origin/v2` d44798a).

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

Оборудования нет; запуск на pi1/pi2 этой задаче запрещён. Процедура (после DEV deploy ветки, с разрешения владельца):

| № | Сценарий | Статус | Процедура и критерий |
|---|---|---|---|
| H1 | Обнаружение на Pi 4 | NOT TESTED | pi1 от `sheregesh-ui`: `PYTHONPATH=<выпуск>/software python3 -m sheregesh.videoinput` без устройства и с адаптером: `CAPTURE` — только `uvcvideo` index 0; `video10–12`, ISP, unicam — `other` с причиной; режимы перечислены; отдельно `--no-probe` |
| H2 | Права доступа | NOT TESTED | `id sheregesh-ui`, `ls -l /dev/video*`, `getfacl /dev/video0`: открытие из сеанса cage без `Permission denied` |
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
