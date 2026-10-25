# S5 «видео» — приёмка: RTSP-матрица и USB-адаптеры захвата (QA)

Ветка `v2` от 4d562f7, 2026-10-25. Сценарии (запуск с Mac, по SSH от `sheregesh-ui`, без root):

| Средство | Шаги | Тесты суждения (без плат) |
|---|---|---|
| [`tools/probe/s5_rtsp_matrix.py`](../../../tools/probe/s5_rtsp_matrix.py) | S5-01…S5-07 — RTSP-матрица на двух платах | `tests/unit/test_s5_rtsp_matrix.py` |
| [`tools/probe/s5_capture_diag.py`](../../../tools/probe/s5_capture_diag.py) | S5-10 — диагностика одного адаптера на pi2 | `tests/unit/test_s5_capture_diag.py` |
| [`tools/probe/s5_accept.py`](../../../tools/probe/s5_accept.py) (поверх `s4_accept.py`) | S5-01, S5-11…S5-15 — приёмка адаптера | `tests/unit/test_s5_qa_accept.py` |

Проверка средств: `.venv/bin/python -m pytest -q tests/unit -k "s4_qa or s5"`. У каждого сценария есть `--plan`
(шаги без SSH). Отчёт — по [`REPORT-TEMPLATE.md`](REPORT-TEMPLATE.md). Базовая линия — S4
[`../s4-video/REPORT.md`](../s4-video/REPORT.md) (VID-01…09 PASS с BLOCKED, gf404014), уроки S4 учтены (§6).

## 0. Стенд и условия допуска

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Адрес, серийный номер | 192.168.1.92, `10000000568152de` | 192.168.1.171, `100000009d497e91` |
| Роль в S5 | GUI; тестовые RTSP-серверы (8601–8604; h264 — `v4l2h264enc` pi1) | источник видео: RTSP-клиент `rtspsrc` / USB-адаптер |

- **Допуск** (проверяют сценарии, иначе ERROR без действий): серийные номера; OTA обеих плат ∈ {IDLE, COMMITTED,
  ROLLED_BACK} и согласовано; на обеих развёрнут один выпуск S5. Параллельно — никаких OTA и DEV deploy.
- **OTA-шаг (S5-07, S5-15)** сравнивает snap «после» не с жёстким списком исключений (как S4 VID-09 для pi1 на B), а с
  **базовой линией самой платы**: FAIL сравнения «до» с самим собой — свойства состояния (слот B, autoboot) — допустимы
  с той же деталью; любой иной FAIL — изменение за прогон. Так шаг верен при любом слоте платы в покое.
- **Воздействия** (все — правами `sheregesh-ui`, юниты и образ не меняются, всё возвращается в `finally`):
  `~/.config/sheregesh/video-input.json` на pi2 + SIGHUP главному процессу блока (исходный файл возвращается; если
  его не было — synthetic + SIGHUP, затем удаление); серверы `s4_rtsp_test_server.py` на pi1 из `/tmp` с PID-файлами
  `/tmp/s5-rtsp-<порт>.pid` (S4-сервер 8554 не трогается); `--gui` — остановка `sheregesh-console` на pi1 на время
  `gui_capture` (≈ 1,5–3 мин), затем start; адаптер — **человек** вынимает кабель/USB по подсказке.
- **Контракт NETWORK** (агент NETWORK, параллельно): ключ `transport: auto|tcp|udp` в описании RTSP-источника и в
  элементах `rtsp_urls`; сервер `--transports tcp,udp --codec mjpeg|h264`; фактический транспорт — поле
  `status.video.source.transport` (если есть). Сценарий сам определяет, развёрнут ли контракт (`--help` сервера; `grep
  transport` в коде выпуска pi2 или поле статуса), и пишет это в `summary.json → contract`. Пока не развёрнут —
  зависящие ячейки **ERROR «контракт NETWORK не развёрнут»** (не PASS); `auto` у S4-сервера (TCP interleaved) судится.

```bash
cd ~/Developer/sheregesh
# RTSP-матрица (сейчас, после развёртывания выпуска с контрактом NETWORK на обе платы)
python3 tools/probe/s5_rtsp_matrix.py --plan
D=docs/acceptance/s5-video/run-rtsp-$(date +%Y%m%d-%H%M)
python3 tools/probe/s5_rtsp_matrix.py --console 192.168.1.92 --remote 192.168.1.171 --out $D --gui

# каждый адаптер после доставки: диагностика (адаптер ещё не подключён), затем приёмка
python3 tools/probe/s5_capture_diag.py --remote 192.168.1.171 --plug            # → hw/<VID-PID-драйвер>/diag.{json,txt}
D=docs/acceptance/s5-video/run-<имя>-$(date +%Y%m%d-%H%M)
python3 tools/probe/s5_accept.py --source v4l2:<id из diag/status> --out $D --gui [--gui-fault] \
    [--manual-fault cable|usb] [--standard pal|ntsc]
```

`<id>` — `v4l2:<VID:PID[:serial]>` или имя by-path из `status.video.sources` блока (`--source v4l2` — единственный
захват). `--source-cmd 'CMD {source}'` — выбор источника своей командой вместо файла.

## 1. Порядок, время и критерии

Вердикты: **PASS**; **FAIL** — доказано несоответствие; **ERROR** — испытание не состоялось или не доказательно;
**BLOCKED** — нет оборудования (§2); **SKIP** — не запрошено. Ни ERROR, ни BLOCKED никогда не PASS. Итог:
FAIL > ERROR > BLOCKED > PASS > SKIP.

### 1.1. RTSP-матрица — `s5_rtsp_matrix.py`, ≈ 7 мин (с `--gui` ≈ 10 мин)

Серверы pi1: **8601** mjpeg tcp,udp · **8602** h264 tcp,udp · **8603** mjpeg только tcp · **8604** mjpeg только udp.
Ячейки — `rtsp_urls` = `rtsp:0…9` (разные пути: `videosources.active_id` ищет источник по адресу).

| ID | Мин | Действие | PASS | FAIL | ERROR | Реально / модель |
|---|---|---|---|---|---|---|
| S5-01 | 0,3 | службы обеих плат, failed, health; пульт CONNECTED (ui-state) | active/running, failed нет, health PASS, CONNECTED | иное | SSH | реально |
| S5-02 | 1 | `rtsp:0…5` = {udp, tcp, auto} × {mjpeg, h264} @ 8601/8602: выбор файлом; ожидание; кадры; транспорт | блок `streaming` с этим адресом ≤ 15 с; пульт `live` (rtsp); `video.frames_decoded` растёт за 2 с; фактический транспорт = заданному (auto — любой, записывается) | нет streaming/live, кадры стоят, транспорт другой, статус и сокеты расходятся | транспорт не определён; контракт не развёрнут | реально (сеть, rtspsrc, UDP/TCP, декодер пульта); камера — тестовый сервер |
| S5-03 | 1 | `rtsp:6` auto@только-tcp, `rtsp:7` auto@только-udp, `rtsp:8` udp@только-tcp, `rtsp:9` tcp@только-udp | 6 → tcp, 7 → udp (как S5-02); 8/9: за 15 с ни разу `streaming`, на блоке `error`/`connect-error`/`no-signal`, пульт «нет видео», затем synthetic ≤ 15 с | streaming при недоступном транспорте; видео на пульте; synthetic не вернулся (зависание) | контракт не развёрнут | реально |
| S5-04 | 0,5 | при `rtsp:2` (auto, mjpeg) — kill сервера 8601 на 6 с, запуск снова | «нет видео» ≤ 3 с; на блоке `connect-error`/`no-signal`; `live` ≤ 10 с после запуска, без действий | > 3 / > 10 с, не наблюдалось | ячейка не дошла до пульта | **модель** «камера/кодер пропал» (остановка процесса сервера) |
| S5-05 | 1,5 | synthetic → rtsp:0 → rtsp:4 → rtsp:8 (отказ) → rtsp:2 → synthetic × `--switches`; ресурсы до/после | каждое ≤ 15 с; 1 конвейер на плате, сирот/зомби нет, fd ±3, потоки ±2, MainPID и NRestarts прежние (обе службы) | зависание, рост fd/потоков, лишний конвейер | — | реально |
| S5-06 | 3 | `--gui`: `gui_capture --by select --select rtsp:0…7,synthetic` — кнопка «Видео» (`selectVideoSource`) | для каждой положительной ячейки снимок `NN-selected-rtsp_N`: источник rtsp, `frame_changed=true` (таймкод сервера) | кадр стоит, источник не тот, отказ выбора | без `--gui` (ui-state не доказательство) | рендеринг Qt offscreen, без cage/HDMI пульта |
| S5-07 | 2,5 | журналы с начала прогона, Traceback, failed; `check_video_link --only session,stream,leftovers`; slot_guard «до/после» | как S4 VID-08; OTA: только FAIL базовой линии платы (+ `same.unit.sheregesh-console` при `--gui`) | что-то найдено / изменение | нет summary / ERROR slot_guard | реально |

Доля тёмного поля для RTSP не критерий: кадр тестового сервера сам почти чёрный (0,99, S4 D-3) — меняется таймкод.

**Фактический транспорт.** Поле статуса блока, если NETWORK его даёт, — главное; сокеты процесса `gst-launch … rtspsrc`
на pi2 — сверка (или единственный источник): `/proc/PID/fd` → inode → `/proc/net/udp{,6}`, `/proc/net/tcp{,6}`
(`ss`/`lsof` на платах нет, USB-DIAG §1). Есть UDP-сокеты у процесса → udp (RTSP-управление при этом тоже TCP);
нет UDP и есть TCP ESTABLISHED к порту сервера → tcp (interleaved). Расхождение статуса и сокетов — FAIL.

### 1.2. Адаптер — после доставки, на каждый ≤ 15 мин (диагностика ≤ 5 + приёмка 5–10)

| ID | Мин | Действие | PASS | FAIL | ERROR / BLOCKED | Реально / модель |
|---|---|---|---|---|---|---|
| S5-10 | ≤ 5 | `s5_capture_diag.py --plug`: lsusb + sysfs «до», подсказка подключить, «после» (новые VID:PID, скорость 480/5000), драйвер интерфейсов, `dmesg` (без root) / `journalctl -k` / «недоступен», `/dev/video*`, by-id/by-path, `video_hw_probe --json nodes` (форматы, размеры, интервалы, входы с флагами статуса, стандарты, QUERYSTD); CVBS — PAL и NTSC (`norm=`), HDMI — верхние 4 режима (каждый FourCC): `v4l2src num-buffers ! CAPS ! fakesink` — частота по PTS, CPU pi2 по `/proc/stat`; `frames` — сигнал / поле / нет кадров | это **измерение**, не приёмка: итог `OK` (есть режим с изображением) записывается в карточку `VIDEO_CAPTURE_COMPAT.md` только из `diag.json` | `NO-DRIVER` (несовместимо), `NO-NODE`, `NO-SIGNAL` (проверить источник) | нет устройства → `NO-DEVICE` = BLOCKED; служба держит узел → `BUSY` (`--free`) | реально |
| S5-01 | 1,3 | службы + `check_link --only health,identity` (S4 VID-01/02) | как S4 | | | реально |
| S5-11 | 2 | `--gui`: `gui_capture --by select --select v4l2:<id>,synthetic` | снимок `03-04-selected-v4l2_<id>`: вход hdmi/cvbs/usb/v4l2, `frame_changed=true`; возврат на synthetic GUI-путём — кадр меняется | кадр стоит (источник без движения — дать движущееся изображение), отказ выбора, вход не тот | без `--gui` — ERROR; нет адаптера — BLOCKED | реально, кроме cage/HDMI пульта |
| S5-12 | 1 | источник v4l2 файлом; **человек** вынимает кабель CVBS/HDMI по подсказке (Enter в момент действия, ±1 с), держит ≥ 8 с (`--manual-fault usb` — отключает USB) | пульт «нет видео» ≤ 3 с; на блоке `no-signal` (`usb` — `no-device`); `--gui-fault`: снимок отказа есть, поле погашено — **только если поток был ярким** (§3) | > 3 с; блок всё время `streaming` (адаптер шлёт поле без статуса входа — ограничение класса устройства, записывается) | нет адаптера — BLOCKED | **реально** (кабель) |
| S5-13 | — | человек возвращает кабель | `live` ≤ 10 с без вмешательства (`--gui-fault`: кадр меняется после возврата) | > 10 с / не вернулось | — | реально |
| S5-14 | 1 | synthetic ↔ v4l2:<id> × 2; ресурсы до/после | как S5-05 | как S5-05 | нет адаптера — BLOCKED | реально |
| S5-15 | 2,5 | как S5-07 | как S5-07 | | | реально |

Последовательность на адаптер: подключить источник сигнала (CVBS: камера/ТВ-выход с PAL; HDMI: ноутбук с часами или
видео — **движение обязательно**, иначе `frame_changed=false`) → S5-10 → S5-01, S5-11…S5-15 → отключить. NTSC
проверяется только если источник умеет NTSC (иначе в отчёте «источник только PAL», а не FAIL адаптера).

## 2. BLOCKED — правила

- Нет адаптера / камеры на стенде, адаптера нет в `status.video.sources` блока или `available=false` →
  S5-11…S5-14 **`BLOCKED: hardware unavailable`** (никогда не PASS), S5-10 — `NO-DEVICE`.
- Нет источника сигнала нужного типа (NTSC, HDMI с движением) → соответствующий пункт `BLOCKED: no source`.
- Дисплей/HDMI-выход пульта, сенсор — `BLOCKED: hardware unavailable` (снимки — Qt offscreen, как в S4).
- RTSP-матрица не зависит от оборудования; неполный контракт NETWORK — ERROR, не BLOCKED.

## 3. «Нет сигнала» на HDMI: почему по состоянию блока, а поле кадра — только при ярком потоке

При отказе пульт гасит поле кадра — `gui_capture` меряет долю почти чёрных пикселей `frame_dark` (S4: ≥ 0,8). Но
поток HDMI может быть тёмным и сам (тёмный рабочий стол, заставка, чёрный экран источника, как кадр тестового RTSP с
долей 0,99): тогда «чёрное поле» при отказе ничего не доказывает. Поэтому:

1. Основной критерий S5-12 — **состояние источника на блоке** (`no-signal`/`no-device` в `link-status.json`) и «нет
   видео» на пульте (`ui-state.videoSource.live=false` ≤ 3 с). Это прямые наблюдения, не зависящие от картинки.
2. Снимок отказа (`--gui-fault`) существует, только если пульт ≥ 2 с показывал состояние отказа (`gui_capture --by
   video`) — это рендеринг подписи «Нет сигнала».
3. Доля тёмного поля — дополнительный критерий, **только если снимок потока был ярким** (`frame_dark ≤ 0,5`); иначе в
   протоколе — «критерий по полю кадра неприменим».
4. Адаптер, который без сигнала шлёт синее/чёрное поле и не сообщает статус входа (многие MS2109/MS2130, часть
   EasyCAP), на блоке остаётся `streaming` — это **FAIL S5-12** с пометкой «ограничение класса устройства»
   (`videoingest`: детектора пустого кадра нет); S5-10 покажет это заранее как `blank` при снятом сигнале.

## 4. Время

RTSP: предпроверка и snap 1 + S5-01 0,3 + S5-02 1 + S5-03 1 + S5-04 0,5 + S5-05 1,5 + S5-07 2,5 ≈ 7–8 мин; `--gui`
(S5-06) + 3 мин ≈ 10 мин. Адаптер: S5-10 ≤ 5 мин (`--budget 300` обрезает режимы) + приёмка 1 + 1,3 + 2 + 1 + 1 + 2,5
≈ 9 мин; `--gui-fault` + 2 мин. Время по часам Mac пишется в `summary.json → duration_min` / `diag.json → duration_s`.

## 5. Если шаг не прошёл

- Источник блока: `ssh sheregesh-ui@192.168.1.171 'cat ~/.config/sheregesh/video-input.json; python3 -c "import json;
  print(json.load(open(\"/run/sheregesh-remote/link-status.json\"))[\"video\"][\"source\"])"'` — сценарии возвращают
  исходный файл в `finally`; вручную — записать исходное содержимое и `kill -HUP $(systemctl show -p MainPID --value
  sheregesh-remote-net)`.
- Серверы pi1: `ssh sheregesh-ui@192.168.1.92 'for p in 8601 8602 8603 8604; do test -e /tmp/s5-rtsp-$p.pid && kill
  $(cat /tmp/s5-rtsp-$p.pid); done'` (журналы — `/tmp/s5-rtsp-<порт>.log`).
- Пульт после `--gui`: `systemctl is-active sheregesh-console` = `active`; если нет — `systemctl start sheregesh-console`.
- FAIL S5-07/S5-15 по OTA — STOP, ничего не исправлять, сообщить владельцу.

## 6. Уроки S4, учтённые в средствах

- `pkill -f` в той же ssh-команде убивал свою оболочку: везде шаблоны `'[/]tmp/gui_capture.py'`, `'[g]st-launch…'`.
- `gui_capture` на pi1: `PYTHONPATH` с `software/emulators` (как `launcher.py`).
- «Нет сигнала» по доле тёмного — только для яркого источника (§3).
- Выбор источника файлом сохраняет прочие ключи (`rtsp_urls`); возврат — явной записью, не удалением (удаление +
  SIGHUP источник не меняет).

## 7. Что не проверяется

Путь cage/Wayland/DRM/HDMI пульта и сенсор (BLOCKED: hardware); задержка «стекло-стекло» (оценка, HW-PLAN §3); 10
подач питания (REQ-P-012, нет управления питанием); RTSP с настоящей IP-камерой (нет камеры — BLOCKED, матрица — на
тестовом сервере); RTSPS/аутентификация (не в объёме S5).
