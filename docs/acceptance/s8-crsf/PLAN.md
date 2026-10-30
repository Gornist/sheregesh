# S8 CR: план приёмки выхода CRSF удалённого блока (эмулятор ELRS TX вместо модуля)

Ветка `v2`, 2026-10-26. Источник истины: [ADR-0014](../../adr/ADR-0014-crsf-output.md), раздел 4 (CR-01…CR-10).
Сценарий стенда: [`tools/probe/s8_crsf_accept.py`](../../../tools/probe/s8_crsf_accept.py). Суждения без плат проверяет
[`tests/unit/test_s8_crsf_accept.py`](../../../tests/unit/test_s8_crsf_accept.py): разбор ответов и журнала блока,
вердикты, пересчёт каналов в отсчёты CRSF (как `sheregesh.crsf.frames`), сверку путей полей с настоящим
`wire.sanitize_module`, цепочку «кадр 0x16 → эмулятор → `crsf.emu` у пульта», опросчик против подставного канала пульта
вместе с `modulectl`.

Физического модуля на стенде нет: backend `crsf-pty-lab` — эмулятор ELRS TX (`sheregesh_emulators.elrs_tx`, «RM Nomad
X-Band (emu)», 50 Гц радиопакетов) на псевдотерминале, дочерний процесс `sheregesh-remote-net`. Радиопередачи нет.
Настоящий Nomad подключается отдельно: [`docs/hardware/NOMAD-WIRING.md`](../../hardware/NOMAD-WIRING.md) и
[`tools/probe/nomad_bringup.py`](../../../tools/probe/nomad_bringup.py). CR-xx — критерии спринта, а не TEST-ID реестра.

## 0. Стенд и права

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Адрес, серийный номер | 192.168.1.92, `10000000568152de` | 192.168.1.171, `100000009d497e91` |
| Служба | `sheregesh-console`, RadioMaster Boxer по USB (USB Joystick), как в S7 | `sheregesh-remote-net`: `ModuleService` с backend `crsf-pty-lab`, `CrsfOutput`, `ControlReceiver` |
| Что читает сценарий | ответы локального канала пульта: `module status` (`status.module` после `sanitize_module`: `state`, `output`, `settings.backend`, `crsf` — снимок `CrsfOutput`, `crsf.emu` — что принял эмулятор) и `sticks status` | журнал юнита: `module: A -> B (reason)`, `module: crsf: A -> B (…)`, `control: A -> B (reason) … age_ms=…`, строки эмулятора `[elrs_tx] …`; `ps` (CR-09); `link-status.json` — только старт (`control`) |
| Команды | `python3 -m sheregesh.link.modulectl configure backend=… / lab silent / lab heal --via console` (тот же путь, что экран «Модуль»), `sticks enable` (`"confirmed": true`) / `disable` | только через пульт; `systemctl` — см. ниже |

Все запросы пульту идут из одного короткого опросчика: он запускается на pi1 по SSH (`python3 -c` из выпуска пульта,
без файлов на плате), сокет пульта берёт `sheregesh.link.videoselect.socket_path()`, `modulectl` вызывает подпроцессом
и печатает каждый ответ строкой JSON с временем pi1. Интервалы на пульте — по часам pi1 (верхняя оценка: статус модуля
доходит до пульта не чаще раза в 0,5 с), на блоке — по часам pi2 (журнал). Часы плат не сравниваются.

Действия, которые меняют состояние плат (SSH от `sheregesh-ui`, без sudo):

| Действие | Правило / основание | Шаги |
|---|---|---|
| `modulectl configure backend=crsf-pty-lab` / `lab-emulator`, `lab silent`, `lab heal` | канал пульта, подтверждение — как кнопка экрана «Модуль» | CR-01, CR-07, CR-09, finally |
| `sticks enable` / `disable` | канал пульта | CR-03, CR-04, CR-06, finally |
| `systemctl stop/start sheregesh-console` (+ страховочный `start` через 120 с) | `os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules` | CR-06 |
| `systemctl restart sheregesh-remote-net` | то же правило | CR-08 (б) |
| `systemctl kill --kill-whom=main -s SIGSTOP` / `SIGCONT` блока (+ страховочный `SIGCONT` на плате через 7,5 с) | то же правило, глагол `kill` | CR-08 (а), finally |
| чтение OTA, DEV-выпуска, `boot_id`, `build.json`, `systemctl --failed` | только чтение (`s6_jr_accept.BOARD_CMD`) | CR-10 |

Сценарий **не** выполняет: OTA, запись слотов, `shdeploy`, изменение видео, образа, юнитов и polkit, `pkill`,
перезагрузку, отключение интерфейсов; backend `crsf-uart` не включает (на стенде без нового образа его нет —
`uart-not-found`). Тест `test_commands_within_image_polkit_rules` держит сценарий в этих рамках.

Из [`s6_jr_accept.py`](../../../tools/probe/s6_jr_accept.py) и [`s7_sticks_accept.py`](../../../tools/probe/s7_sticks_accept.py)
импортом, без копий: предпроверка (серийные номера, OTA в покое, снимок «до»), `boards`/`judge_ota`,
`start_guard`/`systemctl`/`safety_start` (не больше 4 запусков юнита за 300 с с учётом журнала платы), процессы блока,
`Stand` (выборки, `sticks`), `restore` (службы, передача стиков выключена, блок снова `IDLE`), разбор журнала сторожа.

## 1. Подготовка (главный агент)

1. Обе платы на одном коммите S8 (DEV deploy, как в S7):
   ```bash
   python3 tools/deploy/shdeploy.py deploy pi1 --host 192.168.1.92
   python3 tools/deploy/shdeploy.py deploy pi2 --host 192.168.1.171
   python3 tools/deploy/shdeploy.py status                     # выпуск одинаковый, OTA в покое
   ```
2. Boxer включена, USB Joystick, кабель в pi1 (как S7 §1). Без аппаратуры CR-04 и CR-06 получают BLOCKED.
3. Модуль блока на backend `lab-emulator` и READY (состояние после S6/S7). Проверка:
   `ssh sheregesh-ui@192.168.1.92 'C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl status --via console'`
   — `"support": "ok"`, `telemetry.state` `READY`, `telemetry.settings.backend` `lab-emulator`, `output` `none`.
4. OTA обеих плат в покое (`IDLE`, `COMMITTED` или `ROLLED_BACK`), `tryboot_now` не `true`.
5. **Стики и переключатели аппаратуры в окне CR-04/05 не трогать** (≈ 15 с после строки «СТИКИ И ПЕРЕКЛЮЧАТЕЛИ НЕ
   ТРОГАТЬ» в журнале прогона).

## 2. Запуск

```bash
cd ~/Developer/sheregesh
python3 tools/probe/s8_crsf_accept.py --plan                         # план, без SSH

D=docs/acceptance/s8-crsf/run-$(date +%Y%m%d-%H%M)
python3 tools/probe/s8_crsf_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out $D
#   + --only CR-04,CR-07  отдельные шаги (CR-10 «до/после» — всегда; CR-01 выполняется, если запрошен любой из CR-01…08)
#   + --window 10         окно замера CR-04/05, с
#   + --no-freeze         CR-08 без заморозки агента (только рестарт)
```

В `$D` — `summary.json` (вердикты, замеры, `before`/`after` плат, все запуски опросчика с итогами `modulectl` и
`sticks`, `cleanup`) и `run.log`. Коды выхода: 0 PASS, 1 FAIL, 2 ERROR, 3 BLOCKED. Время — 4–6 мин (из них до минуты —
перезапуск пульта в CR-06 и до 30 с — рестарт блока в CR-08; пауза `start_guard` — до 5 мин, если пульт или блок
недавно перезапускались 4 раза).

## 3. Шаги и ожидаемый результат

Пределы ADR §4: READY ≤ 5 с; период 20 000 мкс ± 1 %; 0x16 к эмулятору 50 Гц ± 2 %; p99 джиттера ≤ 2 мс; CRC 0;
neutral ≤ 600 мс от последнего кадра пульта; FAULT ≤ `no_response_s` + 1 с (`no_response_s` = 2 с, профиль
`radiomaster-nomad`); `uart-disconnected` ≤ 1,5 с. Свои пределы сценария (`LIMITS`): `live_s` 3 (ACTIVE ≤ 1 с + статус
модуля к пульту), `sync_wait_s` 3, `settle_s` 1, `heal_s` 20, `s6_ready_s` 15, `restart_ready_s` 30, `freeze_s` 2,5
(меньше `WatchdogSec=15` и тайм-аута канала пульта).

| Шаг | Воздействие | PASS, если |
|---|---|---|
| CR-10 «до» | чтение обеих плат | снимок OTA, DEV-выпуска, образа, `boot_id`, `failed`. Предпроверка: серийные номера, OTA в покое, пульт отвечает `module status` (`support ok`) и `sticks status` |
| CR-01 | `modulectl configure backend=crsf-pty-lab --wait 30`, опрос 10 Гц | `result ok`; READY ≤ 5 с — по журналу pi2 (от строки `configure (req N): backend=crsf-pty-lab` до `-> READY`), если журнала нет — верхняя оценка по пульту; `settings.backend = crsf-pty-lab`; `crsf.device.name = «RM Nomad X-Band (emu)»`. Если модуль не дошёл до READY, CR-02…CR-08 — BLOCKED |
| CR-02 | те же снимки, ещё до 3 с | `crsf.synced = true` хотя бы раз; в последних трёх снимках `synced` и `period_us` = 20 000 ± 200 |
| CR-03 | `sticks disable`, опрос 3 с | после первого `output neutral` (+1 с) все снимки `neutral` (и `crsf.mode` совпадает); `crsf.emu.ch` — все центр (992); `crsf.emu.armed = false` |
| CR-04 | `sticks enable`, окно `--window` (10 с) — sticks status и module status 10 Гц | `output live` ≤ 3 с; в окне только `live`; оси пульта неподвижны (разброс ≤ 20, иначе ERROR — стики трогали); каналы эмулятора 1–8 = `axis_to_crsf(оси пульта)` ±1 шаг 11 бит (не меньше шума осей: 1 + ⌈разброс · 0,82⌉); 9–16 = 992, если блок их сообщает (S8 сообщает только 1–8 — см. §5); `armed = true` |
| CR-05 | то же окно | `crsf.emu.rate_hz` (частота принятых эмулятором 0x16) 49…51 Гц во всех снимках; `crsf.jitter_us_p99` (CrsfOutput) ≤ 2000 мкс, `crsf.emu.jitter_us_p99` ≤ 5000 мкс (включает задержки самого эмулятора, ADR-0014 §4); прирост `crsf.crc_errors` и `crsf.emu.crc_errors` за окно — 0 |
| CR-06 | при `live`: страховочный `start`, `systemctl stop sheregesh-console`; журнал pi2; `start` | в журнале `control: ACTIVE -> FAILSAFE (timeout \| link-lost)` и `crsf: live -> neutral`; (время записи о neutral − (время записи о FAILSAFE − `age_ms`)) ≤ 600 мс; пульт снова отвечает ≤ 60 с; затем `output neutral`, `crsf.emu.ch` — центр, `armed = false` |
| CR-07 | `modulectl lab silent --wait 15`, опрос; `lab heal` | FAULT ≤ 3 с — по журналу pi2 (строка команды `… silent …` → `-> FAULT`), иначе верхняя оценка по пульту; во всех снимках FAULT `output none`; после `heal` READY ≤ 20 с |
| CR-08 (а) | страховочный `SIGCONT` (через 7,5 с), `systemctl kill --kill-whom=main -s SIGSTOP sheregesh-remote-net`, 2,5 с, `SIGCONT` | в журнале строка эмулятора `[elrs_tx] host uart-disconnected` не позже 1,5 с после SIGSTOP (часы pi2); после SIGCONT — READY и `neutral` ≤ 20 с (кратковременный FAULT `MOD-NO-RESPONSE` после разморозки допустим: блок 2,5 с не видел ответов) |
| CR-08 (б) | `systemctl restart sheregesh-remote-net` | READY и `output neutral` ≤ 30 с; backend по-прежнему `crsf-pty-lab` (настройка сохранена) |
| CR-09 | `modulectl configure backend=lab-emulator --wait 30` | READY ≤ 15 с на `lab-emulator`; `output none` во всех снимках после READY; процесс `sheregesh_emulators.jrmodule` ровно один, `elrs_tx` нет |
| CR-10 «после» | чтение обеих плат | на обеих OTA, слот, `tryboot`, образ, DEV-выпуск, `boot_id` прежние; `systemctl --failed` пуст |

Порядок: CR-10 «до» → 01 → 02 → 03 → 04/05 (одно окно) → 06 → 07 → 08 → 09 → finally → CR-10 «после».

**finally** выполняется всегда: `SIGCONT` блоку (если замораживали), остановленные службы запускаются (`start` только
остановленной), передача стиков выключается и блок возвращается в `IDLE (disabled)`, если был в `IDLE` до прогона
(`s7_sticks_accept.restore`), backend возвращается в `lab-emulator` (если он другой) и последним — ещё раз
`sticks disable`. Если выключение передачи или возврат `lab-emulator` не подтверждены — в `summary.json`
`cleanup_warning`.

Если сценарий прерван, восстановление вручную:
```bash
ssh sheregesh-ui@192.168.1.171 'systemctl --no-ask-password kill --kill-whom=main -s SIGCONT sheregesh-remote-net'
ssh sheregesh-ui@192.168.1.92 'systemctl --no-ask-password start sheregesh-console'
ssh sheregesh-ui@192.168.1.92 'C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -m sheregesh.link.modulectl configure backend=lab-emulator --via console'
ssh sheregesh-ui@192.168.1.92 'C=$(readlink -f /var/lib/sheregesh-deploy/current); PYTHONPATH=$C/software python3 -c "
from sheregesh.link.videoselect import ask_console, socket_path
print(ask_console(socket_path(), {\"cmd\": \"sticks\", \"op\": \"disable\"}, 5)[\"result\"])"'
```

### Вердикт приёмки

**PASS**, если CR-01…CR-10 — PASS. Что проверяется только автоматически на стенде, что вручную, что — без плат:

| Что | Как |
|---|---|
| CR-01…CR-10 | автоматически, без участия человека (нужна только неподвижная аппаратура в окне CR-04/05) |
| Движение стиков и переключатели → каналы эмулятора | вручную, по желанию: экран «Управление» pi1 — полосы «локально», «блок» и каналы эмулятора двигаются вместе; плашка «Выход CRSF: команды идут на модуль (эмулятор модуля)» |
| Каналы 9–16 = 992 | без плат: `tests/unit/test_crsf_output.py` (CrsfOutput упаковывает 9–16 центром), `test_crsf_frames.py` |
| `uart-disconnected` при гибели агента (а не заморозке) | без плат: `tests/system/test_crsf_chain.py` (на стенде эмулятор умирает вместе с агентом — pdeathsig) |
| Настоящий Nomad, радиопередача, `crsf-uart` | не в S8 CR: `NOMAD-WIRING.md` + `nomad_bringup.py` после нового образа с `dtoverlay=uart3` |

## 4. Визуальный осмотр (по желанию, не влияет на вердикт)

Во время окна CR-04/05 на дисплее pi1: вкладка «Модуль» — «Выход: команды», блок CRSF (устройство «RM Nomad X-Band
(emu)», период 20 000 мкс, синхронизация, частота кадров, джиттер, ошибки CRC 0), плашка «эмулятор модуля»; вкладка
«Управление» — «Выход CRSF: команды идут на модуль». Снимки offscreen всех состояний — `screenshots-offscreen/`
(агент G).

## 5. Ограничения и что не повторяем

- `status.module.crsf.emu.ch` у блока S8 — только оси 1–8 в шкале пульта −1000…1000 (`module/crsf._emu_view`); сценарий
  переводит их обратно в отсчёты 11 бит (`axis_to_crsf`) и сравнивает в шагах 11 бит. Каналы 9–16 эмулятор принимает,
  но блок их не сообщает — они проверяются без плат.
- Частота 0x16 судится по эмулятору (`rate_hz` за его окно 1 с), потому что статус модуля доходит до пульта с
  задержкой и счётчик `frames_sent` по часам пульта дал бы погрешность в процентах; `frames_sent` пишется справочно.
- Не повторяем: Ethernet и mTLS канала (S3.1, E2E), видео (S4/S5), жизненный цикл модуля на `lab-emulator` (S6 JR),
  передачу стиков, сторож свежести и `rc-state` (S7 ST), OTA (S5-OTA). Здесь новое — backend `crsf-pty-lab`, режимы
  выхода `none/neutral/live`, `CrsfOutput`, эмулятор ELRS TX, поле `crsf` в `status.module`, `configure backend`.

## 6. Без плат (до стенда)

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_s8_crsf_accept.py tests/unit/test_s7_sticks_accept.py \
    tests/unit/test_s6_jr_accept.py
.venv/bin/python -m pytest -q -p no:cacheprovider tests/unit/test_crsf_frames.py tests/unit/test_crsf_port.py \
    tests/unit/test_crsf_output.py tests/unit/test_elrs_tx_emulator.py tests/unit/test_module_crsf_backend.py \
    tests/system/test_crsf_chain.py
```
