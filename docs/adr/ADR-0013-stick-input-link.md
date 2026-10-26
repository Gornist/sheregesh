# ADR-0013. Передача стиков и кнопок с пульта на удалённый блок

- **Статус:** Предложено (S7), 2026-10-26
- **Связано:** D6 (управление наземными платформами), ADR-0007 (SHRG-LAB, mTLS), ADR-0012 (внешний модуль),
  `docs/hardware/EDGETX-EXTERNAL-MODULE.md` (USB-джойстик EdgeTX, поведение ELRS при прекращении кадров).

## 1. Контекст

К pi1 по USB подключена аппаратура RadioMaster Boxer (EdgeTX) в режиме USB Joystick: `1209:4f54`,
`/dev/input/by-id/usb-OpenTX_Radiomaster_Boxer_Joystick_*-event-joystick`, 8 осей ABS_X…ABS_RUDDER (коды 0…7,
0…2048, центр ≈1024, fuzz 8, flat 128), 24 кнопки (0x130…0x13f, 0x2c0…0x2c7). Пользователь `sheregesh-ui` в группе
`input` — чтение без новых прав. evdev сообщает только изменения; признака «аппаратура жива», кроме присутствия
USB-устройства, нет. В HID попадают выходы каналов 1…8 после миксов — назначение каналов задаётся в аппаратуре,
«Шерегеш» каналы не трактует и передаёт 1:1.

Задача S7: оси и кнопки с pi1 доходят до pi2, pi2 следит за свежестью и при потере команд переходит в
безопасное состояние. **Радиовыхода нет** (ADR-0012: `output` остаётся `"none"`); получатель на pi2 — точка, к
которой позже подключится аппаратный backend модуля.

## 2. Решение в целом

```text
pi1: /dev/input/eventN → StickSource (поток, evdev без зависимостей) → StickSender 50 Гц
       → PeerClient.send_rc() (последний кадр, без очереди) → SHRG-LAB 1.4 `rc` (mTLS)
pi2: Agent.on_message(rc) → ControlReceiver (сторож свежести 500 мс) → status `rc-state` 10 Гц → pi1 (эхо, задержка)
```

- **Включение передачи — только оператором**, с подтверждением на пульте. После каждого старта пульта передача
  выключена. Отключение USB-устройства выключает передачу (повторное включение — снова оператором).
- **Сторож на pi2** — единственный, кто решает, свежи ли команды. Нет кадра `rc` дольше `timeout_ms` (500, как
  сторож микшера EdgeTX), потеря сеанса или `dev.ok=false` → состояние FAILSAFE: все каналы 0 (центр), кнопки 0.
  `en=false` (оператор выключил передачу) → IDLE с теми же нулевыми значениями, но без FAILSAFE. Возврат в ACTIVE — автоматически по свежим кадрам с `en=true` (так ведут себя RC-каналы);
  каждый переход журналируется. Поведение «газ при failsafe» для конкретной платформы — в ADR аппаратного backend.

## 3. Протокол SHRG-LAB 1.4

`TYPES["1.4"] = TYPES["1.3"] | {"rc", "rc-state"}`; `PROTOCOL_VERSION = "1.4"`. Сессия < 1.4 — `rc` не
отправляется и не исполняется, `rc-state` не отправляется; всё прочее как в 1.3.

### 3.1. `rc` (пульт → блок), до 50 Гц

```json
{"n": 1234, "en": true, "ch": [0, -12, 1000, 3, -1000, 0, 0, 1000], "btn": 5,
 "dev": {"name": "OpenTX Radiomaster Boxer Joystick", "ok": true}}
```
- `n` — номер кадра в сеансе, строго растёт (новое соединение — с 1). Кадр с `n` ≤ последнего принятого
  отбрасывается (счётчик `stale`).
- `ch` — ровно 8 целых, −1000…1000 (ось: min…max → −1000…1000; ось отсутствует — 0). `btn` — целое 0…2^24−1,
  бит i — i-я кнопка в порядке возрастания кода KEY.
- `en=false` — оператор выключил передачу (явный переход в IDLE, не FAILSAFE). `dev.ok=false` — устройство
  пропало (FAILSAFE с причиной `device-lost`).
- Неверное тело → не исполняется, счётчик `bad`, событие в журнале не чаще раза в 10 с.

### 3.2. `rc-state` (блок → пульт), 10 Гц в ACTIVE/FAILSAFE, при каждом переходе — сразу; в IDLE — 1 Гц

```json
{"v": 1, "state": "ACTIVE", "reason": "fresh", "since_s": 12.3, "n": 1234, "age_ms": 18,
 "rate_hz": 49.8, "gaps": 0, "stale": 0, "bad": 0, "late_max_ms": 41, "failsafes": 1,
 "timeout_ms": 500, "ch": [0, -12, 1000, 3, -1000, 0, 0, 1000], "btn": 5,
 "dev": {"name": "…", "ok": true}, "output": "none"}
```
- `state`: `IDLE` (команд не было или `en=false`), `ACTIVE`, `FAILSAFE`. `reason`: `start`, `fresh`,
  `disabled`, `timeout`, `link-lost`, `device-lost`.
- `ch`/`btn` — то, что сейчас действует на блоке (в FAILSAFE — нули). `n` — последний принятый кадр: пульт
  считает задержку как «сейчас − время отправки кадра n» (часы плат не сравниваются).
- `gaps` — пропуски номеров, `late_max_ms` — наибольший интервал между кадрами за 10 с, `rate_hz` — за 2 с.
- Уточнения реализации (S7, агент R): `gaps` — число пропущенных номеров (n − последний − 1); `age_ms` — `null`, пока
  кадров не было; `late_max_ms` считает интервалы только внутри потока `en=true` (пауза после `en=false` — не
  опоздание; разрыв, вызвавший FAILSAFE `timeout`, — опоздание); `rate_hz` потока моложе 2 с — по его фактической
  длительности. Потеря сеанса в IDLE состояние не меняет; в FAILSAFE причина остаётся первой (`timeout` не заменяется
  на `link-lost` и наоборот).

## 4. Компоненты и владельцы файлов

### 4.1. Блок и протокол (агент R)
- `software/sheregesh/control/__init__.py`, `wire.py` (схемы, `check_rc(body) -> RcFrame | raise BadRc`,
  `sanitize_rc_state`), `receiver.py` — `ControlReceiver(clock, timeout_ms=500, log)`:
  `on_rc(frame, now)`, `on_link_lost(now)`, `tick(now)`, `state()`, `snapshot()`; инъекция часов, без потоков.
- `software/sheregesh/__init__.py` (1.4), `link/codec.py` (`TYPES["1.4"]`), правки версии в тестах, где сессия
  записана числом.
- `software/remote/sheregesh_remote/agent.py`: приём `rc` в сессии ≥ 1.4, `tick` раз в 25 мс в отдельной сопрограмме
  рядом с тикером (не реже 50 мс — сторож точнее 100 мс; ожидания тикера — `delay`, `drain` — сторож не задерживают),
  `on_link_lost` при закрытии или замене сеанса, отправка `rc-state` (в отдельной задаче), строка
  `control = health()` в `link-status.json`, журнал `control: IDLE -> ACTIVE (fresh) …`; `--rc-timeout-ms` (50…10000,
  по умолчанию 500).
- `software/sheregesh/link/client.py`:
  - `send_rc(body) -> str` (`queued` | `offline` | `unsupported`): хранит **только последний** кадр (не очередь),
    будит поток канала немедленно (задержка отправки ≤ 5 мс, без ожидания таймаута сокета 0,2 с: `select` на сокете
    канала и сокете пробуждения). `body = {"en", "ch", "btn", "dev"}`; номер `n` ставит поток канала при отправке
    (новое соединение — с 1), ключ `n` в `body` не используется; `dev.name` обрезается до 128 символов; неверное
    тело — `BadRc` (`ValueError`);
  - разбор `rc-state` → `snapshot()["rc_state"]` (через `sanitize_rc_state`) + `rx_at` (monotonic приёма),
    `rx_age_s`, `latency_ms` (приём − отправка кадра `n`; `null`, если кадра нет в кольце); новое соединение — `null`;
  - `snapshot()["rc_sent"] = {"n", "at", "pending", "ring"}` — последний отправленный кадр (0/`null` — не было),
    кадр ждёт отправки, кольцо `[[n, at], …]` времён отправки последних 64 кадров (monotonic клиента).
- Тесты: `tests/unit/test_control_receiver.py`, `test_control_wire.py`, `tests/system/test_rc_link.py`
  (loopback: 50 Гц → ACTIVE, эхо `ch`; прекращение кадров → FAILSAFE ≤ timeout+100 мс; закрытие сеанса →
  FAILSAFE `link-lost`; `en=false` → IDLE; пир 1.3 — `rc` не исполняется, пульт получает `unsupported`).

### 4.2. Пульт (агент J)
- `software/console/sheregesh_console/sticks.py`:
  - `EvdevJoystick` — чтение `/dev/input/event*` через `os`/`fcntl`/`struct`/`select` (без python-evdev);
    поиск устройства: `/dev/input/by-id/*-event-joystick`, иначе первое event-устройство с ABS_X и ABS_Y;
    `EVIOCGABS` — диапазоны, `EVIOCGBIT(EV_KEY)` — список кнопок; переподключение при выдёргивании (опрос раз в 1 с);
  - `StickSource` (поток): текущее состояние осей/кнопок, `present`, имя;
  - `StickSender` (поток, 50 Гц): при `enabled and present and connected` шлёт `client.send_rc(...)`;
    выключение → один кадр `en=false`; пропажа устройства → один кадр `dev.ok=false`, затем `enabled=False`.
- `backend.py`: `@Property sticks` (`SticksView`: `present`, `deviceName`, `enabled`, `axes[8]`, `buttons`,
  `remoteState`, `remoteStateText`, `remoteReason`, `remoteAxes[8]`, `rateHz`, `latencyMs`, `gaps`,
  `failsafes`, `support` — `ok|no-link|old-remote|no-device`); слоты `setEnabled(bool, confirmed)`.
- `qml/SticksPage.qml`, вкладка «Управление» в `Main.qml` (следующий индекс после «Модуль», F9): 8 полос осей
  (локально и эхо с блока рядом), сетка 24 кнопок, имя устройства, переключатель передачи с подтверждением,
  состояние блока крупно («Нет команд», «Команды идут», «FAILSAFE: нет свежих команд»), частота, задержка,
  пропуски; плашка «Радиовыхода нет (S7): команды доходят до блока, модулю не передаются».
- `app.py` (`PAGE_NAMES`, ключ `sticks` в `dump_ui`), `control.py` (команда `sticks`: `status`, `enable`,
  `disable` — для QA; enable из локального канала только с флагом `confirmed`), переводы.
- Тесты: разбор потока событий evdev из байтов, нормализация осей, горячее подключение (подставной каталог),
  GUI offscreen со снимками состояний в `docs/acceptance/s7-sticks/screenshots-offscreen/`.

### 4.3. Приёмка (агент Q)
- `tools/probe/s7_sticks_accept.py` (по образцу `s6_jr_accept.py`, с `start_guard`), `tests/unit/test_s7_sticks_accept.py`,
  `docs/acceptance/s7-sticks/PLAN.md`. Проверки ST-01…ST-08 — раздел 5.

## 5. Приёмка S7 на стенде

| ID | Проверка | Критерий |
|---|---|---|
| ST-01 | pi1 видит аппаратуру | `sticks status`: present, 8 осей, 24 кнопки |
| ST-02 | Передача выключена по умолчанию | после старта пульта `enabled=false`; блок IDLE в начале прогона, после перезапуска пульта — не ACTIVE (остаётся FAILSAFE `link-lost` до свежих кадров) |
| ST-03 | Включение | блок ACTIVE ≤ 1 с, `rate_hz` ≥ 45, `gaps` = 0 за 30 с |
| ST-04 | Эхо значений | `ch` блока совпадают с осями пульта (±1) при неподвижных стиках; при движении — ручной шаг |
| ST-05 | Задержка | медиана `latency` ≤ 30 мс, 99-й перцентиль ≤ 100 мс за 30 с |
| ST-06 | Пульт остановлен | `systemctl stop sheregesh-console` → блок FAILSAFE (`timeout` или `link-lost`) ≤ 600 мс от последнего кадра |
| ST-07 | Выключение оператором | `disable` → блок IDLE (`disabled`), не FAILSAFE |
| ST-08 | Отключение USB | ручной шаг: FAILSAFE `device-lost`; после возврата — передача выключена |
| ST-09 | Нет выхода | `output = none` во всех снимках |
| ST-10 | OTA-state | как в JR-10 |

## 6. Чего нет в S7

Радиовыхода, CRSF и UART; калибровки и инверсии осей на стороне «Шерегеша» (делается в аппаратуре); второго
источника команд; UDP-канала (TCP+TLS достаточно для стенда, задержка измеряется ST-05).
