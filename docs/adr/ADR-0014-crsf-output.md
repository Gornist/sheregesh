# ADR-0014. Выход CRSF удалённого блока на JR-модуль: протокол, UART, эмулятор ELRS

- **Статус:** Предложено (S8), 2026-10-26
- **Связано:** D6/D7, ADR-0012 (менеджер модуля), ADR-0013 (стики, `ControlReceiver`),
  `docs/hardware/RADIOMASTER-NOMAD.md` (линия отсека, сборка ELRS 4.1.0), `docs/hardware/EDGETX-EXTERNAL-MODULE.md`
  (тайминг, синхронизация 0x3A.0x10, поведение ELRS TX при прекращении кадров).

## 1. Контекст

S7 доводит команды до pi2 (`ControlReceiver`: IDLE/ACTIVE/FAILSAFE, 8 каналов −1000…1000, 24 кнопки). S8 доводит их
до JR-модуля: pi2 становится «хостом» CRSF, как прошивка аппаратуры в отсеке. Физического Nomad на стенде пока нет
(подключение — 2026-10-27), поэтому весь путь проверяется через эмулятор ELRS TX на псевдотерминале, а UART готовится
так, чтобы завтра осталось подключить провода и сменить настройку backend.

Факты из исследования: линия отсека — один провод, полудуплекс; хост ведущий — шлёт кадр каналов 0x16 раз в период,
затем слушает ответ; ELRS 4.1 сам подбирает скорость и полярность; синхронизация 0x3A.0x10 раз в 200 мс сообщает
период и смещение (единицы 0,1 мкс); EdgeTX шлёт 0x16 длиной 25 с байтом состояния взведения; ELRS TX через 1–2 с без
кадров хоста прекращает радиопередачу.

## 2. Решение в целом

```text
ControlReceiver (ADR-0013) ──channels()──► CrsfOutput (поток, период модуля) ──► Port
   IDLE/FAILSAFE → нейтраль                    кадр 0x16 + служебные (ping/параметры)      ├ UartPort: /dev/ttyAMA* (UART3), termios2, 400000 8N1, полудуплекс (эхо вычитывается)
   ACTIVE → каналы пульта                       разбор ответов: 0x3A sync, 0x14, 0x29        └ PtyPort: псевдотерминал ↔ эмулятор ELRS TX (лабораторный)
ModuleManager (ADR-0012) ◄── события backend «crsf»: presence/init/pong/fault по ответам модуля (0x29 device info, 0x14, тишина)
```

- **Новый backend модуля `crsf`** (`kind = "crsf"`, `supports_lab = False` для UART, `True` для pty-эмулятора):
  жизненный цикл модуля определяется по ответам CRSF: DETECTED — первый корректный кадр от модуля; INITIALIZING —
  ping 0x28 → device info 0x29 (имя, прошивка); READY — device info получен и идёт синхронизация 0x3A или link
  statistics 0x14; FAULT — нет корректных кадров от модуля дольше `no_response_s` (по профилю) или ошибки CRC подряд.
- **Выход (`output`) по состоянию:** `none` — модуль не READY или backend не `crsf` (кадры каналов не идут, только
  ping); `neutral` — модуль READY, `ControlReceiver` IDLE или FAILSAFE: идут кадры с нейтральными значениями
  (`failsafe_ch` из настроек, по умолчанию все 0 → центр 992), взведение `false`; `live` — модуль READY и
  `ControlReceiver` ACTIVE: каналы пульта, взведение `true`. Радиолинк при FAILSAFE не рвётся — ровер получает
  нейтраль сразу, а не через 2–3 с собственного failsafe приёмника. Если умирает сам агент pi2, кадры прекращаются, и
  ELRS TX/приёмник уходят в свой failsafe (поведение по `EDGETX-EXTERNAL-MODULE.md`).
- **Период:** до первой синхронизации — 4 мс (как EdgeTX на 400K); после — период и смещение из 0x3A.0x10, границы
  1…50 мс; синхронизация старше 2 с — возврат к 4 мс. Отправка — поток с абсолютным расписанием
  (`time.monotonic`/`clock_nanosleep`-подобный цикл), джиттер измеряется и отдаётся в диагностике.

## 3. Пакеты и файлы (владельцы)

### 3.1. `software/sheregesh/crsf/` — протокол и порты (агент K)
- `frames.py`: CRC8 (полином 0xD5), сборка/разбор кадра `[addr, len, type, payload, crc]`; адреса (0xEE модуль,
  0xEA пульт, 0xC8 полётник — по спецификации CRSF); `pack_channels(ch16: list[int], armed: bool | None) -> bytes`
  — 16 каналов по 11 бит (172…1811, центр 992; −1000…1000 → 992 ± 820) плюс байт взведения, если `armed` не None
  (длина 25, ELRS 4.x); `unpack_channels`; `ping()`, разбор `device_info` (0x29: имя, серийный, hw/sw, число
  параметров), `link_stats` (0x14), `timing` (0x3A.0x10: период, смещение); потоковый разборщик `Parser.feed(bytes)
  -> list[Frame]` с ресинхронизацией и счётчиками `crc_errors`, `garbage_bytes`.
- `port.py`: `UartPort(path, baud=400000, half_duplex=True)` — `os.open`, termios2 через `fcntl.ioctl`
  (`TCGETS2`/`TCSETS2`, `BOTHER`, произвольная скорость), 8N1, raw, неблокирующий; при `half_duplex` отправленные
  байты вычитываются как эхо и отбрасываются (с проверкой совпадения — расхождение = коллизия, счётчик); на pty
  termios2 может не поддерживаться — тогда стандартный termios без скорости. `PtyPort` — пара `os.openpty()`.
  `find_uart3()` — устройство tty, у которого `/sys/class/tty/<x>/device/of_node` указывает на `serial@7e201600`
  (UART3 BCM2711); нет — `None`.
- `output.py`: `CrsfOutput(port, source, clock=time.monotonic, log=None)`: поток отправки по §2; `source()` →
  `(mode: "none"|"neutral"|"live", ch: list[int] (8), btn: int)`; каналы 1–8 — оси, 9–16 — нули (кнопки в каналы
  не отображаются в S8, передаются только для диагностики); служебная очередь (ping, чтение параметров);
  `snapshot()` → `{mode, period_us, synced, sync_age_ms, frames_sent, jitter_us_p99, crc_errors, collisions,
  link: {rssi, lq, snr, rf_mode, tx_power} | None, device: {name, sw, serial} | None, last_rx_ms}`.
  *Уточнено при реализации (агент K):* `pack_channels` принимает оси −1000…1000 (16 значений), сырые отсчёты —
  `pack_channels_raw`; `unpack_channels(payload) -> (16 отсчётов, armed | None)`, `axis_to_crsf`/`crsf_to_axis`.
  `CrsfOutput(port, source, clock, log, wait=None, on_frame=None, status_byte=True, ping_interval_s=0.1)`;
  `start()/stop()/step()/run(n)/request_ping()/queue(frame)/forget_device()`. В `snapshot()` дополнительно:
  `offset_us, pings_sent, service_sent, overruns, crc_errors_run, garbage_bytes, rx_frames, write_errors,
  source_errors, armed, ch` (8 осей последнего кадра). `mode none` — только ping раз в 100 мс; device info
  забывается после 2 с без ответов модуля (ping возобновляется), link — после 2 с.
- Эмулятор — `python3 -m sheregesh_emulators.elrs_tx` (`software/emulators/sheregesh_emulators/elrs_tx.py`; ядро
  `ElrsTxEmulator` и `LoopbackPort` для тестов в одном процессе там же, отдельного `crsf/emulator.py` нет):
  эмулятор ELRS TX на pty — отвечает на ping device info «RM Nomad X-Band (emu)», шлёт 0x3A.0x10 раз в 200 мс
  (период по умолчанию 20 000 мкс = 50 Гц пакетов, параметр), link statistics 0x14 раз в 200 мс; принимает 0x16,
  проверяет CRC и интервалы, хранит последние каналы и взведение; без кадров хоста > 1 с — состояние
  `uart-disconnected` (не шлёт sync, как ELRS); лабораторные сценарии `silent` (модуль молчит), `garbage` (мусор в
  линии), `crc` (портит CRC ответов), `heal`. Состояние эмулятора (последние каналы, частота, джиттер, взведение)
  отдаётся строкой JSON в stdout для агента (как jrmodule).
- Тесты: `tests/unit/test_crsf_frames.py` (эталонные кадры с известными байтами и CRC, 11-бит упаковка, граничные
  значения), `test_crsf_port.py` (termios2-структуры, эхо полудуплекса на фейковом порте), `test_crsf_output.py`
  (SimClock: период до/после sync, нейтраль/live, служебные кадры), `test_elrs_tx_emulator.py`.

### 3.2. Интеграция на pi2 (агент H)
- `software/sheregesh/module/`: backend `crsf` в `make_backend`/`check_backend` (`UartCrsfBackend`,
  `PtyCrsfLabBackend` — порождает эмулятор через `video.spawn`, как jrmodule), события менеджеру по ответам CRSF;
  `output` в `snapshot()` теперь `none|neutral|live` + блок `crsf` (снимок `CrsfOutput`); профиль
  `radiomaster-nomad`: `interface: uart-crsf`, `baud: 400000`, `no_response_s`, ожидаемое имя устройства
  (`"RM Nomad"` — префикс).
- `config/schemas/remote-1.json`: `backend: lab-emulator | crsf-pty-lab | crsf-uart`; `uart: {device: "auto"|path,
  baud, half_duplex}`; `failsafe_ch: [8 int]` (по умолчанию нули). Значение по умолчанию остаётся `lab-emulator`
  (S6). Переключение backend — `module-cmd configure` (подтверждение на пульте).
- `agent.py`: источник для `CrsfOutput` — `ControlReceiver` (режим по §2), статус `status.module.crsf`, журнал
  переходов режима выхода (`crsf: neutral -> live`).
- `module/wire.py`: `sanitize_module` пропускает `output ∈ {none, neutral, live}` и блок `crsf`.
- Тесты: `tests/unit/test_module_crsf_backend.py` (SimClock + эмулятор в процессе), `tests/system/test_crsf_chain.py`
  (loopback: пульт `send_rc` → агент → `CrsfOutput` → pty → эмулятор: каналы эмулятора = каналы пульта; FAILSAFE →
  нейтраль ≤ timeout+100 мс; модуль молчит → FAULT и `output none`; агент убит → эмулятор `uart-disconnected` ≤ 1,5 с);
  поправить `test_module_no_output.py` под новый смысл `output`.

### 3.3. Пульт (агент G)
- Страница «Модуль»: строка выхода (`Выход: нет / нейтраль / команды`), блок CRSF (устройство, период и синхронизация,
  частота кадров, джиттер, RSSI/LQ, ошибки CRC), выбор backend (лабораторный / эмулятор CRSF / UART) через
  `configure` с подтверждением.
- Страница «Управление»: плашка по фактическому выходу — «Радиовыхода нет» (`none`), «Выход CRSF: нейтраль»
  (`neutral`), «Выход CRSF: команды идут на модуль» (`live`), с пометкой «эмулятор модуля», если backend
  `crsf-pty-lab`; в эхо — каналы, полученные эмулятором модуля (третья полоса или числа), если есть.
- Снимки offscreen в `docs/acceptance/s8-crsf/screenshots-offscreen/`.

### 3.4. Приёмка и пакет на завтра (агент Q)
- `tools/probe/s8_crsf_accept.py` (по образцу s7, с `start_guard`), `tests/unit/test_s8_crsf_accept.py`,
  `docs/acceptance/s8-crsf/PLAN.md`. Проверки — §4.
- `tools/probe/nomad_bringup.py` — пошаговое включение настоящего модуля завтра: (1) найти UART3 и проверить, что
  линия в покое (чтение без отправки 2 с); (2) ping → device info; (3) 10 с нейтрали, sync и link stats; (4) по
  команде оператора — переключить backend на `crsf-uart`; каждый шаг печатает результат и останавливается при ошибке.
- `docs/hardware/NOMAD-WIRING.md` — схема подключения к pi2: UART3 (GPIO4 TX — контакт 7, GPIO5 RX — контакт 29,
  GND — контакт 9), резистор в линии TX (номинал по измерению уровня, ориентир 1 кОм — проверить), питание модуля
  только от XT30; порядок измерений до подключения (`RADIOMASTER-NOMAD.md` §6), что делать при 5 В на линии.

### 3.5. Образ (главный агент)
- `os/layer/sheregesh-lab.yaml`: `dtoverlay=uart3` в `config.txt`; проверка в `tools/image/verify_image.py`.
  На платы — только новым образом по решению владельца; до этого `crsf-uart` на стенде недоступен (backend сообщает
  `uart-not-found`), проверяется `crsf-pty-lab`.

## 4. Приёмка S8 на стенде (с эмулятором модуля)

| ID | Проверка | Критерий |
|---|---|---|
| CR-01 | Backend `crsf-pty-lab` включается | `configure backend=crsf-pty-lab` → модуль READY ≤ 5 с, device «RM Nomad X-Band (emu)» |
| CR-02 | Синхронизация | `synced`, период = периоду эмулятора (20 000 мкс ± 1 %) |
| CR-03 | Нейтраль | передача выключена → `output neutral`, каналы эмулятора = 992 (центр) |
| CR-04 | Команды | передача включена → `output live`, каналы эмулятора = каналы пульта (±1 шаг 11 бит), взведение true |
| CR-05 | Частота и джиттер | кадры 0x16 к эмулятору 50 Гц ± 2 %, p99 джиттера ≤ 2 мс, ошибок CRC 0 |
| CR-06 | Потеря команд | остановка пульта → FAILSAFE → `neutral` ≤ 600 мс, эмулятор получает центр |
| CR-07 | Модуль молчит | сценарий `silent` → FAULT ≤ `no_response_s`+1 с, `output none`; `heal` → READY |
| CR-08 | Агент pi2 перезапущен | эмулятор `uart-disconnected` ≤ 1,5 с; после рестарта READY и `neutral` |
| CR-09 | Возврат к S6 | `configure backend=lab-emulator` → поведение S6, `output none` |
| CR-10 | OTA-state | как JR-10 |

## 5. Чего нет в S8

Физического модуля и радиопередачи (завтра); передачи кнопок в каналы CRSF (только оси 1–8); телеметрии приёмника
ровера в GUI сверх link statistics; настройки ELRS-параметров (мощность, частота пакетов) из GUI — только чтение
device info; TBS Crossfire.
