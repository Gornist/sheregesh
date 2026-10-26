# S6 JR: план сокращённой приёмки внешнего модуля (лабораторный эмулятор)

Ветка `v2`, 2026-10-26. Источник истины: [ADR-0012](../../adr/ADR-0012-external-module-manager.md), разделы 11
(JR-01…JR-10) и 12.4 (отказы). Сценарий стенда: [`tools/probe/s6_jr_accept.py`](../../../tools/probe/s6_jr_accept.py).
Суждения без плат проверяет [`tests/unit/test_s6_jr_accept.py`](../../../tests/unit/test_s6_jr_accept.py). Тот же
сценарий на loopback (два процесса, без плат) прогоняет
[`tests/gui/test_s6_jr_loopback.py`](../../../tests/gui/test_s6_jr_loopback.py), и судят его те же функции
`judge_*`.

Модуль физически не подключается. На pi2 работает только лабораторный эмулятор: дочерний процесс
`sheregesh-remote-net`, `python3 -s -m sheregesh_emulators.jrmodule`. Выхода управления нет ни в одном состоянии
(`output = "none"`, ADR §12.3). JR-xx — это критерии спринта, а не TEST-ID реестра, поэтому статусы требований приёмка
не меняет.

## 0. Стенд и права

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Адрес, серийный номер | 192.168.1.92, `10000000568152de` | 192.168.1.171, `100000009d497e91`, MAC `d8:3a:dd:69:0f:6e` |
| Служба | `sheregesh-console` | `sheregesh-remote-net` (агент и дочерний эмулятор) |
| Что читает сценарий | `/run/sheregesh-ui/ui-state.json`, ключ `module` (ADR §10.4) | `/run/sheregesh-remote/link-status.json`, ключ `module` (`ModuleService.health()`), `ps` |
| Команды | `python3 -m sheregesh.link.modulectl … --via console` (локальный канал пульта, слоты `ModuleView` с подтверждением) | только `systemctl` из правила polkit и `pkill` своего процесса эмулятора |

Сценарий работает по SSH от `sheregesh-ui`, без sudo. Ниже все действия, которые меняют состояние плат. Тест
`test_commands_within_image_polkit_rules` держит сценарий в этих рамках.

| Действие | Правило / основание | Шаги |
|---|---|---|
| `systemctl start/stop/restart sheregesh-remote-net` | `os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules` | JR-08d, JR-09, F-01…F-03, finally |
| `systemctl start/stop sheregesh-console` | то же правило | F-02, finally |
| `systemctl reboot` pi2 (`login1.reboot`) | то же правило; **только с `--reboot`** и только при COMMITTED и `tryboot_now=false` | JR-09 |
| `pkill -KILL -u sheregesh-ui -f '[s]heregesh_emulators[.]jrmodule'` | свой процесс | JR-08b |
| перенос каталогов в `~/.config/sheregesh/` (`remote-config` → `s6-jr-<время>/…`, порча `state.json` и `revisions/*/*.yaml` в рабочей копии) | дом своего пользователя, служба остановлена | F-03 |
| `sheregesh-ota-client status`, `readlink …/current`, `boot_id`, `build.json`, `systemctl --failed` | только чтение | JR-10 |

Сценарий **не** выполняет: OTA (`ota-*`), запись слотов, `shdeploy`, изменение видео, юнитов, образа и polkit,
`enable`, `reset-failed`, а также отключение сетевых интерфейсов (обрыв Ethernet проверяется только вручную, см. F-04).
Каталоги настроек не удаляются. Перед каждой остановкой службы на плате запускается отложенный `systemctl start`
(через 60–120 с): если Mac или SSH пропадут, служба поднимется сама. Для уже запущенной службы этот `start` ничего не
делает.

## 1. Подготовка (главный агент)

1. Обе платы на одном коммите S6. Это делает DEV deploy:
   ```bash
   python3 tools/deploy/shdeploy.py deploy pi1 --host 192.168.1.92
   python3 tools/deploy/shdeploy.py deploy pi2 --host 192.168.1.171
   python3 tools/deploy/shdeploy.py status                     # выпуск на обеих одинаковый, OTA в покое
   ```
2. Связь CONNECTED, сессия 1.3. Проверка: `ssh sheregesh-ui@192.168.1.92 'C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl status --via console'`
   должен вернуть `"support": "ok"` и `"session": "1.3"`.
3. OTA обеих плат в покое (`IDLE`, `COMMITTED` или `ROLLED_BACK`), `tryboot_now` не `true`. Иначе предпроверка
   сценария выйдет с ERROR и ничего не сделает.
4. Ручная перезагрузка pi2 в JR-09 (`--reboot`) и ручной обрыв Ethernet (`--manual-ethernet`) — это решения
   владельца. Без флагов сценарий их не выполняет.

## 2. Запуск

```bash
cd ~/Developer/sheregesh
python3 tools/probe/s6_jr_accept.py --plan                        # план, без SSH

D=docs/acceptance/s6-jr/run-$(date +%Y%m%d-%H%M)
python3 tools/probe/s6_jr_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out $D
#   + --reboot            JR-09 ещё и через перезагрузку pi2 (только COMMITTED, tryboot_now=false; иначе подшаг BLOCKED)
#   + --manual-ethernet   F-04: человек у стенда вынимает и возвращает кабель pi2 по подсказкам
#   + --only JR-05,JR-08  отдельные шаги (JR-10 «до/после» выполняется всегда)
#   + --hold 30           JR-08c: удержание EMU-UNAVAILABLE (по умолчанию 30 с, ADR §11)
```

В `$D` пишутся `summary.json` (вердикты, замеры, `before`/`after` плат, все вызовы `modulectl`, `cleanup`) и
`run.log`. Коды выхода: 0 PASS, 1 FAIL, 2 ERROR, 3 BLOCKED.

Время: около 7–9 минут без `--reboot` и `--manual-ethernet`. С `--reboot` добавится загрузка pi2 (1,5–2 мин), с
`--manual-ethernet` — около минуты.

## 3. Шаги и ожидаемый результат

Пределы взяты из ADR §11 (`LIMITS`): `ready_s` 15, `detect_s` 5, `fault_s` 6, `recover_s` 20, `crash_recover_s` 15,
`unavailable_s` 30, `hold_s` 30, `service_restart_s` 30, `reboot_s` 300. Свои пределы сценария: `gui_lag_s` 2
(JR-04), `console_start_s` 60 (F-02, загрузка QML на Pi 4), `eth_detect_s` 5 и `eth_back_s` 15 (F-04; 5 и 10 с из
REQ-SYS-002 плюс первый `status` блока). Время отсчитывается от запуска SSH-команды на Mac, то есть включает SSH и
запуск Python на плате (≈ 0,5–1,5 с). Это верхняя оценка.

| Шаг | Воздействие | PASS, если |
|---|---|---|
| JR-10 «до» | чтение обеих плат | снимок OTA, DEV-выпуска, образа, `boot_id`, `failed` взят. Предпроверка: OTA в покое |
| JR-01 | — | служба `active`; `backend_state = running`; ровно один процесс `jrmodule`, его родитель — `MainPID` службы; `pid` в link-status совпадает |
| JR-02 | `modulectl status` | `support ok`, сессия 1.3; `telemetry.profile` = `radiomaster-nomad` / «RadioMaster Nomad»; `profileText` содержит «RadioMaster Nomad»; `labBanner = true` |
| JR-03 | `reinit --expect READY` | `result ok`; в истории после команды есть `INITIALIZING → READY (init-ok)`; `0 < init_ms ≤ 5000`; READY на блоке ≤ 15 с |
| JR-04 | (та же команда) | на пульте `READY` и «Готов» (тот же или более новый `seq`) не позже 2 с после READY в link-status |
| JR-05 | `lab detach`, `lab attach` | «Модуль отсутствует» ≤ 5 с; после attach «Готов» ≤ 15 с |
| JR-06 | `lab iface-error`, затем `lab hang` | iface: `FAULT`, `stateText` начинается с «Ошибка», `errorText` содержит «Ошибка интерфейса», `faultEvent = true` ≤ 6 с. hang: первый `FAULT` после команды — `MOD-NO-RESPONSE` ≤ 6 с, на пульте «Ошибка» |
| JR-07 | `lab heal` после каждого отказа | READY ≤ 20 с без `reset`/`reinit`; в истории `FAULT → RECOVERING (recover-attempt)`, переходов `cmd-*` нет; последняя ошибка (`active=false`) видна в `errorText`; `faultEvent = false` |
| JR-08a | `lab crash` | `FAULT EMU-EXITED` → READY ≤ 15 с; новый PID; `restarts` +1; процесс один |
| JR-08b | `pkill -KILL` эмулятора | то же |
| JR-08c | `lab emu-unavailable`, удержание, `lab emu-available`, `reset` | `FAULT EMU-UNAVAILABLE` ≤ 30 с; за удержание нет процессов `jrmodule`, `restarts` не растёт, `backend_state` не `running`, в журнале нет новых «emulator started» (если журнал доступен `sheregesh-ui`); после `reset` READY ≤ 15 с |
| JR-08d | `systemctl restart sheregesh-remote-net` | пульт показал `no-link`, затем READY ≤ 30 с; эмулятор один, его предок — новый `MainPID`; прежний процесс исчез; `NRestarts` блока и пульта не выросли |
| JR-09 | `configure enabled=false` → restart службы [→ reboot pi2] → `configure enabled=true` | после перезапуска `DISABLED` на блоке и на пульте (в новом сеансе), процессов `jrmodule` нет, `settings.revision` та же, что после configure, `source = store`; после `enabled=true` READY ≤ 15 с. Перезагрузка — только при COMMITTED и `tryboot_now=false`, иначе подшаг BLOCKED |
| F-01 | `systemctl stop` блока на 8 с, потом `start` («pi1 раньше pi2») | `no-link` ≤ 5 с; пока блок остановлен, «Готов» по старым данным не показан ни в одной выборке; после start READY ≤ 30 с |
| F-02 | `stop` пульта → `restart` блока → `start` пульта («pi2 раньше pi1», перезапуск GUI) | блок без пульта дошёл до READY ≤ 15 с; новый пульт показал «Готов» ≤ 60 с; ни одной выборки «Готов» при `support ≠ ok`; история с пульта не пуста и содержит READY |
| F-03 | при остановленной службе: (1) `remote-config` убран, (2) испорчен `state.json`, (3) испорчены все `revisions/*/*.yaml`; затем исходный каталог возвращается | (1) `source = factory`; (2) `recovered`, в `notice` есть `CFG-…`; (3) `reset`, `notice` CFG-RESET, создан `remote-config.corrupt-*`; во всех случаях READY ≤ 30 с; после возврата `source = store` и исходная ревизия |
| F-04 | только `--manual-ethernet`: человек вынимает кабель pi2, ждёт ≥ 10 с и возвращает его | `no-link` ≤ 5 с; «Готов» ≤ 15 с после возврата; у модуля на блоке `seq`, `pid`, `restarts`, `state` прежние (переходов не было) |
| OUT | все ответы `modulectl` | `output = "none"` в каждом снимке `status.module` |
| JR-10 «после» | чтение обеих плат | на обеих: OTA (`state`, `committed`, `slot`, `tryboot_now`, `target`, версии, `consistent`, `default_part`, образ), DEV-выпуск, `build.json` и `boot_id` прежние (`boot_id` pi2 может смениться только при `--reboot`); `systemctl --failed` пуст |

Порядок следует ADR §11: JR-10 «до» → 01 → 02 → 03/04 → 05 → 06/07 (iface) → 06/07 (hang) → 08a–d → 09 → F-01 → F-02
→ F-03 → F-04 → OUT → JR-10 «после». Если JR-01 не нашёл `module` в link-status или в JR-02 `support ≠ ok`, активные
шаги дальше получают BLOCKED: платы не на S6, и воздействовать на них нельзя.

**finally** выполняется всегда. Обе службы запускаются (`start` для работающей службы ничего не делает), затем
`lab emu-available`, `lab heal`, `lab attach`, `reset`. Если `enabled`/`auto_init`/`auto_recover`/`profile`
отличаются от исходных, они возвращаются через `configure`. В конце сценарий ждёт исходного состояния (READY, а если
модуль был выключен — DISABLED). Каталог настроек F-03 возвращается внутри шага, тоже в `finally`.

### Вердикт приёмки

**PASS**, если JR-01…JR-10, F-01…F-03 и OUT — PASS. BLOCKED допустим только у подшага перезагрузки JR-09, когда
OTA pi2 не в покое; это фиксируется в отчёте. F-04 без `--manual-ethernet` — SKIP. Тогда в отчёте нужно записать, что
обрыв Ethernet проверен только в L3 (`tests/gui/test_e2e_gui_link.py`, `tests/system/test_e2e_link_net.py`) и
эквивалентом в loopback (перезапуск блока), а ручной шаг не выполнялся.

Если сценарий прерван (Ctrl+C, обрыв SSH), восстановление вручную:
```bash
ssh sheregesh-ui@192.168.1.171 'systemctl --no-ask-password start sheregesh-remote-net; ls -d ~/.config/sheregesh/s6-jr-*'
ssh sheregesh-ui@192.168.1.92  'systemctl --no-ask-password start sheregesh-console'
# если есть ~/.config/sheregesh/s6-jr-<время>/orig: остановить блок, перенести текущий remote-config в
# s6-jr-<время>/after/, вернуть orig на место remote-config, запустить блок
M='C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl'
ssh sheregesh-ui@192.168.1.92 "$M lab emu-available --via console; $M reset --via console; $M status --via console"
```
Каталоги `~/.config/sheregesh/s6-jr-*` можно удалить вручную после разбора: сценарий сам ничего не удаляет.

## 4. Ручные шаги

1. **F-04, Ethernet.** Нужен флаг `--manual-ethernet` и человек у стенда. По подсказке он вынимает кабель **pi2**
   (на стороне pi2 или свитча; pi1 и Mac остаются в сети) и нажимает Enter в момент действия. Через ≥ 10 с
   возвращает кабель и снова нажимает Enter. Ожидается: на экране «Модуль» пульта «Нет связи с удалённым блоком»,
   кнопки неактивны, «Готов» по старым данным не показывается. После возврата — «Готов» без действий оператора,
   история догоняет. Модуль на блоке всё это время работает и не делает переходов. Отключать интерфейс командой
   на плате нельзя: оборвётся SSH, по которому сценарий читает блок.
2. **Визуальный осмотр на дисплее pi1** (по желанию, не влияет на вердикт). Во время JR-05…JR-07 открыть вкладку
   «Модуль» (F8 / Ctrl+8) и убедиться, что плашка «Лабораторный эмулятор, не физический JR-модуль» видна, а тексты
   совпадают с ui-state. Доказательство уровня CI для всех состояний — снимки offscreen агента C в
   [`screenshots-offscreen/`](screenshots-offscreen/).
3. **`--reboot`.** Перед запуском убедиться, что `shdeploy.py status` показывает для pi2 `COMMITTED`, а не пробную
   загрузку. Сценарий проверяет это сам и в противном случае не перезагружает.

## 5. Что не повторяем

Ниже то, что уже испытано на стенде. Модуль этого не меняет, и S6 не трогает эти части (ADR §1, §14).

| Что | Где испытано |
|---|---|
| Ethernet, mTLS 1.3 канала управления, переподключение | S3.1 ([`../s3.1/REPORT.md`](../s3.1/REPORT.md)), E2E ([`../e2e/REPORT.md`](../e2e/REPORT.md)) |
| Видеоканал TLS, Video Ingest, выбор источника, лабораторные источники видео | S3.1-VIDEO, S4, S5, [`../video-ingest/`](../video-ingest/), [`../s5-ota/ACCEPT-PI2.md`](../s5-ota/ACCEPT-PI2.md) |
| GUI в целом (вкладки 0…6, плитки, локальный канал пульта) | S4/S5 GUI, `tests/gui/*` |
| OTA, A/B, slot_guard | S5-OTA ([`../s5-ota/REPORT.md`](../s5-ota/REPORT.md)). Здесь только JR-10: ничего не изменилось |

Из S6 здесь проверяется только новое: подсистема модуля на pi2, протокол 1.3 (`status.module`, `module-cmd`), экран
«Модуль» и `ui-state.module`, команда `module` локального канала, настройки `remote-config`.

## 6. Без плат (до стенда)

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_s6_jr_accept.py tests/gui/test_s6_jr_loopback.py
```

Loopback поднимает блок (`sheregesh_remote --module --remote-config-dir … --status-file …`) и пульт
(`sheregesh_console --peer 127.0.0.1:PORT` offscreen с `SHEREGESH_UI_STATE` и своим сокетом) отдельными процессами,
а команды подаёт через `modulectl --via console`. Покрыто: JR-01…JR-09; отказы §12.4 — перезапуск блока (SIGKILL:
эмулятор уходит вместе с агентом, пульт переподключается сам), перезапуск пульта, отсутствующие, повреждённые и
недоступные настройки (`factory` / `recovered` / `reset` / `memory`, `configure → not-saved`); блок протокола 1.2
(«не поддерживает внешний модуль (протокол 1.2)», команды `unsupported`). Инвариант `output = "none"` проверяется в
каждом снимке, в том числе в коротких состояниях: снимок через сокет пульта берётся раз в 0,3 с.

## 7. План лабораторных испытаний после получения модуля

Основа — [`docs/hardware/RADIOMASTER-NOMAD.md`](../../hardware/RADIOMASTER-NOMAD.md), разделы 6 (что измерить) и 7
(открытые вопросы). Распиновки, уровни, полярность и скорость обмена Nomad в открытых источниках не подтверждены
(раздел 7, пп. 1–4), поэтому этот план их **не предполагает**: всё это результаты измерений, а не исходные данные.
Подключение модуля к GPIO/UART pi2 до измерений M-N01, M-N03…M-N05 и решения по посреднику (раздел 7, п. 4)
запрещено.

### 7.1. Условия

- Только стенд, вне S6. Передача — в пределах местных правил. До первой подачи питания подключены антенны или
  эквиваленты нагрузки 50 Ом на 1 Вт в обоих диапазонах. Модуль не вскрывается (NOMAD §6).
- Протокол каждого измерения: `docs/hardware/measurements/<ID>.md`, как в `PRE-CAD-CHECKLIST.md`.
- Программная граница прежняя: `HardwareBackendStub` отказывает в конструкторе, `output = "none"`. Аппаратный
  бэкенд и любой выход управления появятся только после ADR канала управления (D6) и пересмотра ограничения ADR-0008
  на протоколы радиооборудования (NOMAD §7, п. 16).

### 7.2. Этапы

| Этап | Измерения (NOMAD §6) | Что даёт | Ворота перехода |
|---|---|---|---|
| A. Без хоста, только чтение | M-N02 (прошивка, домен, `UART_INVERTED`, backpack), M-N12 (когда включаются Wi‑Fi/BT), M-N13 (разъём ↔ диапазон) | идентичность модуля для `IDENTITIES`/профиля, вопросы 9, 12, 15 | версия ELRS и домен зафиксированы; домен допустим на месте |
| B. Электрика без Pi | M-N01 (прозвонка Micro/Nano без питания), M-N06/M-N07 (питание: отсек и XT30, ток по уровням, пусковой ток), M-N08 (нагрев) | таблица контактов с фото, источник питания блока (вопросы 1, 7, 11, 14) | известно, какой контакт — сигнал, а какой — питание. Без этого к модулю ничего не подключается |
| C. Сигнальная линия, логический анализатор | M-N03 (уровни, покой, полярность), M-N04 (скорости 400K…3,75M), M-N05 (полудуплекс, задержка ответа), M-N11 (поведение при включении) | выбор посредника или прямого UART (вопрос 4), параметры профиля: `init_timeout_s`, `response_timeout_s`, `ping_interval_s` | совместимость с 3,3 В GPIO Pi 4 доказана измерением (вопрос 3) или выбран посредник |
| D. Потеря хоста | M-N10 (пропали кадры от хоста → что делает модуль и через сколько срабатывает failsafe приёмника) | критерий PASS для ADR канала управления: остановка платформы при потере связи (вопрос 6) | время до failsafe известно и вошло в ADR D6 |
| E. Мощность ВЧ | M-N09 (по диапазонам, через аттенюаторы) | достаточность 100 мВт для стенда (вопрос 8) | — |

### 7.3. Что повторяем из S6 с настоящим модулем (после ADR D6 и аппаратного бэкенда)

Повторяется та же приёмка (`s6_jr_accept.py`, если менеджер и протокол останутся прежними), но с физическими
действиями вместо лабораторных сценариев. Расширение сценария — отдельная задача после ADR:

| JR | Эмулятор (S6) | С модулем |
|---|---|---|
| JR-01 | процесс `jrmodule` — потомок службы | аппаратный бэкенд в состоянии `running`, устройство канала открыто только службой |
| JR-02 | `IDENTITIES["radiomaster-nomad"]` | идентичность из ответа модуля совпадает с данными этапа A (прошивка, модель) |
| JR-03/04 | `init_s` = 1 с | `init_ms` в пределах `init_timeout_s`, который выбран по M-N05/M-N11 |
| JR-05 | `lab detach/attach` | модуль физически вынут из отсека и вставлен обратно (питание по этапу B) |
| JR-06/07 | `lab iface-error/hang/heal` | обрыв сигнальной линии и её возврат; модуль выключен/включён (M-N11) |
| JR-08 | crash/SIGKILL/emu-unavailable | авария и перезапуск службы; устройство канала недоступно |
| JR-09, JR-10 | без изменений | без изменений |
| OUT | `output = "none"` | пока ADR D6 не принят — по-прежнему `"none"`; новые проверки выхода задаёт тот ADR, не этот план |

Открытые вопросы NOMAD §7, которые блокируют начало этапов: 1–4 (этап C и подключение к Pi), 6 (этап D и ADR D6),
7 (питание, этап B), 9 (домен, этап A), 16 (ADR-0008 и эмулятор, если тот формирует кадры CRSF).
