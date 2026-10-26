# S7 ST: план приёмки передачи стиков и кнопок пульта на удалённый блок

Ветка `v2`, 2026-10-26. Источник истины: [ADR-0013](../../adr/ADR-0013-stick-input-link.md), раздел 5
(ST-01…ST-10). Сценарий стенда: [`tools/probe/s7_sticks_accept.py`](../../../tools/probe/s7_sticks_accept.py).
Суждения без плат проверяет [`tests/unit/test_s7_sticks_accept.py`](../../../tests/unit/test_s7_sticks_accept.py):
разбор ответов, вердикты, перцентили, опросчик против подставного канала пульта, сверку путей полей с настоящими
`SticksView.snapshot()` и `ControlReceiver`.

К pi1 по USB подключена RadioMaster Boxer (EdgeTX) в режиме USB Joystick. Радиовыхода нет ни в одном состоянии
(`output = "none"`): команды доходят до сторожа на pi2 и дальше никуда не идут. ST-xx — критерии спринта, а не
TEST-ID реестра, поэтому статусы требований приёмка не меняет.

## 0. Стенд и права

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Адрес, серийный номер | 192.168.1.92, `10000000568152de` | 192.168.1.171, `100000009d497e91` |
| Служба | `sheregesh-console` (аппаратура — `/dev/input/by-id/usb-OpenTX_Radiomaster_Boxer_Joystick_*-event-joystick`) | `sheregesh-remote-net` (сторож `ControlReceiver`, такт 25 мс) |
| Что читает сценарий | ответ `sticks status` локального канала пульта; `/run/sheregesh-ui/ui-state.json`, ключ `sticks` | `/run/sheregesh-remote/link-status.json`, ключ `control` (`ControlReceiver.health()`); журнал юнита, строки `[remote] control: A -> B (reason) n=… age_ms=…` |
| Команды | `sticks status` / `enable` (`"confirmed": true`) / `disable` — тот же слот `SticksView.setEnabled`, что переключатель экрана «Управление» | только чтение |

Команды пульту подаёт короткий опросчик: он запускается на pi1 по SSH (`python3 -c` из выпуска пульта, без файлов на
плате), шлёт JSON-запросы в сокет пульта (`sheregesh.link.videoselect.ask_console`) и печатает каждый ответ строкой
JSON с временем pi1. Часы плат не сравниваются. Интервалы на пульте считаются по часам pi1 (момент отправки команды →
ответ, в котором видно новое состояние блока), на блоке — по часам pi2 (`age_ms` записи журнала о переходе,
`since_s`/`age_ms` снимка link-status).

Сценарий работает по SSH от `sheregesh-ui`, без sudo. Действия, которые меняют состояние плат:

| Действие | Правило / основание | Шаги |
|---|---|---|
| `sticks enable` / `disable` через локальный канал пульта | канал пульта, сокет 0600 своего пользователя | ST-03…ST-08, finally |
| `systemctl stop/start sheregesh-console` | `os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules` | ST-06; finally — `start` только остановленной службы |
| отложенный `systemctl start sheregesh-console` (страховка, через 120 с) | то же правило | ST-06 |
| `sheregesh-ota-client status`, `readlink …/current`, `boot_id`, `build.json`, `systemctl --failed` | только чтение (`s6_jr_accept.BOARD_CMD`) | ST-10 |

Сценарий **не** выполняет: OTA, запись слотов, `shdeploy`, изменение видео, модуля, юнитов, образа и polkit,
`enable`, `reset-failed`, `pkill`, остановку блока, перезагрузку плат и отключение сетевых интерфейсов. Тест
`test_commands_within_image_polkit_rules` держит сценарий в этих рамках.

Из [`s6_jr_accept.py`](../../../tools/probe/s6_jr_accept.py) берётся импортом, без копий: предпроверка
(`preflight`: серийные номера, OTA обеих плат в покое, не пробная загрузка, снимок «до»), снимки плат и сравнение
JR-10 (`boards`, `judge_ota`), `start_guard`/`systemctl`/`safety_start`. Юниты допускают 5 запусков за 300 с
(первый прогон S6 упал именно на этом), поэтому сценарий держит не больше 4 запусков в окне с учётом запусков из
журнала платы и при необходимости ждёт (пауза пишется в `run.log`). За прогон пульт запускается один раз (ST-06).

## 1. Подготовка (главный агент)

1. Обе платы на одном коммите S7 (протокол SHRG-LAB 1.4). Это делает DEV deploy:
   ```bash
   python3 tools/deploy/shdeploy.py deploy pi1 --host 192.168.1.92
   python3 tools/deploy/shdeploy.py deploy pi2 --host 192.168.1.171
   python3 tools/deploy/shdeploy.py status                     # выпуск на обеих одинаковый, OTA в покое
   ```
2. Boxer включена, в меню USB выбран режим **Joystick**, кабель в pi1. Проверка устройства:
   `ssh sheregesh-ui@192.168.1.92 'ls -l /dev/input/by-id/ | grep -i joystick'`.
3. Пульт отвечает командой `sticks` и видит аппаратуру:
   ```bash
   ssh sheregesh-ui@192.168.1.92 'C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -c "
   from sheregesh.link.videoselect import ask_console, socket_path; import json
   r = ask_console(socket_path(), {\"cmd\": \"sticks\", \"op\": \"status\"}, 5)
   print(r[\"result\"], json.dumps({k: r[\"sticks\"][k] for k in (\"support\", \"present\", \"enabled\", \"device\")}))"'
   ```
   Ожидается `ok` и `"support": "ok"`, `"present": true`, `"enabled": false`, `device.axes` 8, `device.buttons` 24.
4. OTA обеих плат в покое (`IDLE`, `COMMITTED` или `ROLLED_BACK`), `tryboot_now` не `true`. Иначе предпроверка
   сценария выйдет с ERROR и ничего не сделает.
5. **Стики и переключатели аппаратуры в окне ST-03/04/05 не трогать** (≈ 35 с после строки `sticks ['enable', 'poll',
   'poll']` в журнале). Если человек у стенда — ручные шаги ST-04 и ST-08 по флагу `--manual`.

## 2. Запуск

```bash
cd ~/Developer/sheregesh
python3 tools/probe/s7_sticks_accept.py --plan                        # план, без SSH

D=docs/acceptance/s7-sticks/run-$(date +%Y%m%d-%H%M)
python3 tools/probe/s7_sticks_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out $D
#   + --manual            ST-04 (движение стиков, переключатель) и ST-08 (USB) — человек у стенда по подсказкам
#   + --only ST-03,ST-06  отдельные шаги (ST-10 «до/после» выполняется всегда)
#   + --window 30         окно замера ST-03/ST-05, с (по умолчанию 30, ADR §5)
```

В `$D` пишутся `summary.json` (вердикты, замеры, `before`/`after` плат, все запуски опросчика с итогами команд,
`cleanup`) и `run.log`. Коды выхода: 0 PASS, 1 FAIL, 2 ERROR, 3 BLOCKED.

Время: около 3 минут без `--manual` (из них 35 с — окно замера и до минуты — перезапуск пульта в ST-06), с
`--manual` — ещё 1–2 минуты. Если в журнале платы за последние 5 минут уже 4 запуска пульта (недавний DEV deploy),
ST-06 подождёт до 5 минут.

## 3. Шаги и ожидаемый результат

Пределы из ADR §5: ACTIVE ≤ 1 с, `rate_hz` ≥ 45, `gaps` = 0 за 30 с, эхо ±1, задержка: медиана ≤ 30 мс и p99 ≤ 100 мс
за 30 с, FAILSAFE ≤ 600 мс от последнего кадра. Свои пределы сценария (`LIMITS`): `idle_s` 1 (ST-07, как ST-03),
`settle_s` 2,5 (`rate_hz` блока считается за 2 с — первые секунды после ACTIVE не в счёт), `stop_detect_s` 10,
`console_start_s` 60 (QML на Pi 4), `device_lost_s` 10, `replug_s` 20. Время на пульте — от отправки команды до
ответа `status`, где видно новое состояние (опрос 20 Гц, представление пульта обновляется раз в 50 мс): это верхняя
оценка.

| Шаг | Воздействие | PASS, если |
|---|---|---|
| ST-10 «до» | чтение обеих плат | снимок OTA, DEV-выпуска, образа, `boot_id`, `failed` взят. Предпроверка: серийные номера, OTA в покое, пульт отвечает `sticks status` |
| ST-01 | `sticks status` | `present = true`; `device.axes = 8`, `device.buttons = 24`; имя устройства не пустое; в ui-state `sticks.present = true` |
| ST-02 | — (и после перезапуска пульта в ST-06) | при старте прогона `enabled = false` в ответе и в ui-state, блок `IDLE` (link-status `control.state`); после перезапуска пульта снова `enabled = false`, блок не `ACTIVE` (обычно `FAILSAFE (link-lost)` — из FAILSAFE блок выходит только по свежим кадрам, ADR §2) |
| ST-03 | `sticks enable` (`confirmed`), опрос 20 Гц `--window` с | `remote.state = ACTIVE` ≤ 1 с; в окне `rate_hz` ≥ 45 в каждом rc-state (эхо) и в каждом снимке `control` блока (ACTIVE не меньше 2,5 с); `gaps` не изменились ни в эхе, ни на блоке |
| ST-04 | то же окно, стики неподвижны | разброс осей пульта за окно ≤ 20 (иначе ERROR: стики трогали); на выборках, где оси не менялись 3 опроса подряд, `remote.ch` (действующие `ch` блока из rc-state) = `axes` ±1 и `remote.btn` = `buttons`. С `--manual` — ещё три позы (см. §4): сдвиг ≥ 300 или другие кнопки, `ch`/`btn` блока совпадают с позой ±1 |
| ST-05 | то же окно | по одному значению `remote.latency_ms` на каждый rc-state в ACTIVE (приём rc-state клиентом − отправка кадра `n` по кольцу `rc_sent`), не меньше 50: медиана ≤ 30 мс, p99 (ближайший ранг) ≤ 100 мс; сводка пульта `latency.median_ms`/`p99_ms` (его окно 30 с) — в тех же пределах |
| ST-06 | при ACTIVE: страховочный `start`, `systemctl stop sheregesh-console`, затем `start` | блок `FAILSAFE` с причиной `timeout` или `link-lost`; от последнего кадра до перехода ≤ 600 мс — по `age_ms` в записи журнала о переходе (если журнал недоступен `sheregesh-ui` — по `age_ms`/`since_s` снимка, ±50 мс); пульт снова отвечает `sticks status` с `support = ok` ≤ 60 с |
| ST-07 | `enable` → ACTIVE → `sticks disable` | `remote.state = IDLE`, `reason = disabled` ≤ 1 с; ни одного FAILSAFE после disable (эхо, link-status, журнал); `control.failsafes` не вырос; блок в link-status `IDLE (disabled)` |
| ST-08 | только `--manual`: `enable` → ACTIVE → человек выдёргивает USB → возвращает | после выдёргивания блок `FAILSAFE (device-lost)`, пульт `present = false`, `enabled = false`; после возврата устройство снова `present` ≤ 20 с, во всех выборках 3 с после этого `enabled = false`, блок не `ACTIVE` |
| ST-09 | все снимки | `output = "none"` в каждом `control` link-status и в каждом rc-state из ответов пульта |
| ST-10 «после» | чтение обеих плат | на обеих: OTA (`state`, `committed`, `slot`, `tryboot_now`, `target`, версии, `consistent`, `default_part`, образ), DEV-выпуск, `build.json`, `boot_id` прежние; `systemctl --failed` пуст |

Порядок: ST-10 «до» → 01 → 02 (старт) → 03/04/05 (одно окно) [→ позы ST-04] → 06 (+ 02 после перезапуска) → 07 →
08 → finally → 09 → ST-10 «после». Если в link-status блока нет `control` (блок не S7), `support ≠ ok` или
аппаратура не видна, активные шаги получают BLOCKED; ST-01 и ST-02 всё равно записываются (только чтение).

**finally** выполняется всегда. Остановленная служба запускается (`start` работающей не вызывается — лимит запусков
не тратится), сценарий ждёт ответа пульта. Если до прогона блок был `IDLE`, а сейчас нет (после ST-06 он
`FAILSAFE (link-lost)`, после ST-08 — `FAILSAFE (device-lost)`), делается `enable` → ACTIVE → `disable`: блок снова
`IDLE (disabled)`, следующий прогон начнётся с чистого ST-02. Последним всегда идёт `sticks disable`; если выключение
не подтверждено, в `summary.json` пишется `cleanup_warning`.

### Вердикт приёмки

**PASS**, если ST-01…ST-07, ST-09 и ST-10 — PASS. ST-08 без `--manual` — SKIP, а у ST-04 тогда проверен только покой
(в подробностях: «движение стиков — ручной шаг, не выполнялся»). В отчёте нужно записать, что выдёргивание USB и
движение стиков проверены только без плат: `tests/unit/test_console_sticks.py` (горячее подключение на подставном
каталоге, кадр `dev.ok=false`), `tests/system/test_rc_link.py` (loopback: прекращение кадров → FAILSAFE, `en=false` →
IDLE, пир 1.3).

Если сценарий прерван (Ctrl+C, обрыв SSH), восстановление вручную:
```bash
ssh sheregesh-ui@192.168.1.92 'systemctl --no-ask-password start sheregesh-console'
ssh sheregesh-ui@192.168.1.92 'C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -c "
from sheregesh.link.videoselect import ask_console, socket_path
print(ask_console(socket_path(), {\"cmd\": \"sticks\", \"op\": \"disable\"}, 5)[\"result\"])"'
```
Передача после старта пульта и так выключена; страховочный `start` пульта, запущенный перед ST-06, сработает сам
через 120 с.

## 4. Ручные шаги (`--manual`)

1. **ST-04, движение.** Сразу после окна замера сценарий трижды просит принять позу и нажать Enter, не отпуская её
   ещё ≈ 3 с (опрос 2 с): левый стик вверх до упора; правый стик вправо до упора; любой переключатель (SA/SB) в
   другое положение или нажатая кнопка. Затем — вернуть всё как было и нажать Enter. На экране «Управление» пульта
   (F9) полосы «локально» и «блок» должны двигаться вместе. Какие оси и кнопки отвечают за стики, задают миксы
   EdgeTX; «Шерегеш» каналы не трактует, поэтому сценарий требует только сдвига ≥ 300 хотя бы по одной оси или
   других кнопок.
2. **ST-08, USB.** По подсказке человек выдёргивает USB-кабель аппаратуры **из pi1** (не трогая Ethernet и питание) и
   сразу нажимает Enter. Ожидается: на экране «FAILSAFE: нет свежих команд», переключатель передачи выключен,
   устройство «нет». Затем кабель возвращается, снова Enter: устройство видно ≤ 20 с (опрос раз в 1 с плюс
   перечисление USB), передача **остаётся выключенной** — повторное включение только оператором.
3. **Визуальный осмотр на дисплее pi1** (по желанию, не влияет на вердикт): вкладка «Управление», плашка
   «Радиовыхода нет (S7): команды доходят до блока, модулю не передаются», крупное состояние блока, частота,
   задержка, пропуски. Снимки offscreen всех состояний — в `screenshots-offscreen/` (агент J).

## 5. Что не повторяем

Ниже то, что уже испытано на стенде. Передача стиков этого не меняет.

| Что | Где испытано |
|---|---|
| Ethernet, mTLS 1.3 канала управления, переподключение, обрыв кабеля | S3.1 ([`../s3.1/REPORT.md`](../s3.1/REPORT.md)), E2E ([`../e2e/REPORT.md`](../e2e/REPORT.md)) |
| Видео, выбор источника | S3.1-VIDEO, S4, S5 |
| Внешний модуль, эмулятор Nomad, `status.module` | S6 ([`../s6-jr/REPORT.md`](../s6-jr/REPORT.md)) |
| OTA, A/B, slot_guard | S5-OTA ([`../s5-ota/REPORT.md`](../s5-ota/REPORT.md)). Здесь только ST-10: ничего не изменилось |

Из S7 здесь проверяется только новое: протокол 1.4 (`rc`, `rc-state`), сторож свежести на pi2, чтение evdev на pi1,
передача 50 Гц и её включение/выключение, ключ `control` в link-status, ключ `sticks` в ui-state, команда `sticks`
локального канала. Обрыв Ethernet при ACTIVE отдельно не проверяется: для сторожа он неотличим от прекращения кадров
(ST-06, `timeout` или `link-lost`), а переподключение канала уже принято в E2E.

## 6. Без плат (до стенда)

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_s7_sticks_accept.py tests/unit/test_s6_jr_accept.py
.venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_control_receiver.py tests/unit/test_control_wire.py \
    tests/unit/test_console_sticks.py tests/system/test_rc_link.py tests/gui/test_gui_sticks.py
```

`test_s7_sticks_accept.py` гоняет опросчик в отдельном процессе против подставного сокета с протоколом `control.py`,
шаг окна ST-03/04/05 — на синтетических ответах и снимках link-status, а пути полей и запись журнала сверяет с
настоящими `SticksView.snapshot()` и `ControlReceiver`.
