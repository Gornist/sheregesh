# E2E-13 после OTA пульта — сокращённая приёмка (QA, ≤ 10 мин)

Ветка `qa/video-ingest` (база `v2` 43ea9a9), 2026-10-25. Чек-лист для pi1 (`10000000568152de`, роль console) после
OTA с образа `57e8fe4` (слот A) на образ `abd9d68` (слот B). Это закрытие E2E-13 и адресная проверка затронутого,
а не повтор всего E2E. Статусы требований не меняются.

Где E2E-13 сейчас: **FAIL** (известный) на `57e8fe4`. `systemctl stop sheregesh-console` → `status=143`,
`Failed with result 'exit-code'` — ретранслятор `sheregesh-notify-relay` из `/usr` образа. Исправление
(6353c70) есть в образе ≥ 6353c70 и в `abd9d68`. DEV deploy его не доставляет
([`../e2e/hw/HW-RESULTS.md`](../e2e/hw/HW-RESULTS.md), строка 13).

## 1. Что меняется на pi1 — по коду

Две составляющие. Система (`/usr`) работает из образа всегда. Приложение запускает launcher: выпуск DEV deploy,
если он есть, иначе заводской код `/usr/lib/sheregesh`.

**Важно:** OTA переносит в новый слот только `/var/lib/sheregesh` (`update/slots.py`:
`CARRY = ("lib/sheregesh",)` — «выпуски DEV deploy не переносятся»). Значит, после COMMITTED B на pi1 работает
**заводской код `abd9d68`**, а не DEV-выпуск g7061b3c, на котором получены PASS аппаратного E2E.

### 1.1. Система: `git diff 57e8fe4 abd9d68` — `software/`, юниты, слой ОС

| Файл | Что изменилось | Чем исполняется на pi1 |
|---|---|---|
| `software/sheregesh/notify_relay.py` | SIGTERM/SIGINT → код 0 (штатная остановка не «failed»); уборка группы в `finally`; ошибки удаления сокета не меняют код | `/usr/bin/sheregesh-notify-relay` (ExecStart службы пульта) — **цель E2E-13** |
| `tools/image/units/sheregesh-var-seed.service` (новый), `os/layer/sheregesh-lab.yaml` | `/var` нового слота дополняется из образца образа (`/usr/share/sheregesh/var-seed.tar.gz`), если пуст (нет `dpkg/status`); юнит включён | первая загрузка B |
| `software/sheregesh/update/{confirm,model,ota,slots}.py` | агент OTA: подтверждение по критериям, перенос конфигурации | `sheregesh-ota-agent` / `-client` (A, OTA) |
| `software/sheregesh/deploy/installer.py` | время файлов выпуска = время коммита (.ts не «новее» .qm) | `sheregesh-deployd` — только при DEV deploy на B |
| `tools/image/repair_ota_state.py`, `verify_image.py` | инструменты сборки и ремонта состояния | не на плате |

Прочие файлы `software/` из этого диффа — код приложения: `console/*`, `link/{client,tls}.py`, `video.py`,
`videolink.py`, `videoinput.py`, `faults.py`, `pdeath.py`, `remote/agent.py`. Все они уже были в DEV-выпуске
g7061b3c (7061b3c — потомок abd9d68), поэтому для pi1 это не дельта. Дельта приложения — в §1.2.

### 1.2. Приложение: g7061b3c (DEV, до OTA) → `abd9d68` (заводской, после OTA)

`git diff abd9d68 7061b3c -- software` — то, чего на pi1 **не станет** (вариант F, без повторного DEV deploy).
Между 7061b3c и `v2` 43ea9a9 изменений в `software/` нет.

| Коммит | Что пропадает | Сторона | Затрагивает ранее PASS |
|---|---|---|---|
| 7061b3c | `EventLog.raise_` и событие блока `REMOTE-EVENT` с параметром `code` — снова `TypeError` в такте монитора каждые ~5 с, события блока не попадают в журнал пульта | пульт | E2E-12 (события блока на пульте), диагностика |
| 44d02c8, e29402d (`console/journal.py`) | переходы связи и видео в journald пульта | пульт | **E2E-12** (пульт) |
| 9dabdf9 | неблокирующая `VideoReceiver.stop()`: снова `join(2 с)` и `wait()` в потоке GUI при потере связи | пульт | **E2E-08** (отзывчивость GUI при обрыве) |
| 36d6ac1 | приёмник: сброс паузы повтора после восстановления, причина разрыва не затирается | пульт | E2E-06, E2E-10 (время «нет видео» и возврата) |
| b5373b1, 64fecc9 | обобщённый индикатор канала на всех экранах, без мигания «нет связи» | пульт | E2E-04 (вид GUI на снимках) |
| 34c56be, 4e35034, dd6ce77, 17245e0 | детектор тишины пульта, `drain` с тайм-аутом, видеосервер | **блок** | на pi1 не исполняются; pi2 остаётся на g7061b3c |

Не изменились между g7061b3c и `abd9d68`: `link/` (клиент канала пульта), `protocol.py`, `compat-matrix.yaml`,
`sheregesh/video.py` (конвейеры). Протокол на обеих сторонах 1.1 — пара «pi1 abd9d68 / pi2 g7061b3c» совместима
([`../e2e/MATRIX.md`](../e2e/MATRIX.md) §4.1).

**Вариант D** — после E2E-13 владелец решает сделать DEV deploy текущего `v2` на pi1 (слот B). Тогда §1.2
исчезает, остаётся только §1.1. **E2E-13 выполнять до DEV deploy**: проверка `image` требует
`code_root = /usr/lib/sheregesh` (заводской код).

## 2. Ранее PASS — что проверять адресно, что нет

PASS — из [`../e2e/hw/HW-RESULTS.md`](../e2e/hw/HW-RESULTS.md) (обе платы на A, DEV g7061b3c).

| E2E | Затронуто? | Почему | Проверка после OTA (шаг §3) |
|---|---|---|---|
| 01 загрузка пульта | **да** | новый образ, слот B, `sheregesh-var-seed` | шаг 2: failed нет, служба active, первый кадр, var-seed |
| 02 загрузка блока | нет | pi2 не меняется | — |
| 03 связь, mTLS | частично | код `link/` тот же; ключи и сертификаты устройства (`~/.local/share/sheregesh/link`) должны пережить смену слота | шаг 3: `health`, `identity`, `connect` (10-минутный обмен не повторяется) |
| 04 GUI показывает состояние | да (вариант F) | QML индикатора старше (b5373b1, 64fecc9) | вне 10 мин: снимок `gui_capture.py` по решению владельца; иначе записать «вид GUI — заводской abd9d68» |
| 05 видео отображается | да | приёмник пульта старше (36d6ac1, 9dabdf9) | шаг 4: `session`, `stream`, `leftovers` |
| 06 потеря видео ≤ 3 с | **да** | 36d6ac1 | шаг 4: `e2e_run --fault sender-kill`, 1 цикл (`video_off_s` ≤ 3 с) |
| 07 обрыв обнаружен (≤ 5 с) | нет | код канала пульта тот же; сторона блока — pi2 без изменений | — (кабель не повторяется) |
| 08 GUI отзывчив | **да** (вариант F) | 9dabdf9 отсутствует — ожидается деградация | шаг 4: `gui_stale_max_s` из `e2e_run` (косвенно); при F — записать как известный риск |
| 09 восстановление связи ≤ 10 с | нет | код канала тот же | — |
| 10 видео возобновляется ≤ 10 с | **да** | 36d6ac1 | шаг 4: `video_back_s` того же цикла |
| 11 повторные разрывы | нет (L4) | ресурсы: пульт старше, но 28 циклов не повторяются в 10 мин | — |
| 12 журналы | **да** | пульт — journal.py отсутствует (F); блок — без изменений | шаг 5: при F ожидается 0 строк `link:`/`video:` от пульта — **известная регрессия**, не новый дефект |
| **13 app-stop** | **цель** | ретранслятор `/usr` исправлен | шаг 1 |
| 14 повторный запуск | **да** | изменён тот же ретранслятор (код выхода, уборка группы) | шаг 1: `app-restart`, `app-kill` |
| 15 устойчивость 30 мин | нет (не повторяется) | конвейеры (`sheregesh/video.py`) и канал не менялись; установившийся режим подтверждает `stream` шага 4. Полный прогон — только по решению владельца | — |
| watchdog (`watchdog-freeze`) | нет | путь SIGABRT ретранслятора не менялся (128+6); `killpg` перенесён в `finally` — выполнялся и раньше | — (по желанию, +1 мин) |
| монитор (дефект 7061b3c) | **да** (F) | исправление отсутствует | шаг 5: при F `TypeError` ожидается — известная регрессия |

## 3. Чек-лист (≤ 10 мин)

Только после **COMMITTED** pi1 на B и снятого разрешения (`ota-disable`). Во время пробной загрузки
(`HEALTH_PENDING`) службу пульта не останавливать: это влияет на критерии подтверждения. Регрессии OTA
(`slot_guard compare`) — в плане OTA (A), здесь не дублируются.

```bash
cd ~/Developer/sheregesh
PI1=<адрес pi1 после OTA>  PI2=192.168.1.171      # адрес по DHCP после tryboot мог смениться
D=docs/acceptance/video-ingest/e2e13; mkdir -p $D
R1="ssh sheregesh-ui@$PI1"
```

| # | Мин | Команда | PASS | FAIL → действие |
|---|---|---|---|---|
| 0 | 1 | `$R1 'tr -d "\0" </proc/device-tree/serial-number; dpkg-query -W sheregesh; cat /usr/share/sheregesh/build.json; readlink /var/lib/sheregesh-deploy/current; sheregesh-ota-client status; sheregesh-deployd status' > $D/0-versions.txt` | серийный `10000000568152de`; `build.json` commit `abd9d68`; active B, `COMMITTED`; DEV-выпуска нет; `code_root` `/usr/lib/sheregesh` | другое — STOP, это не та конфигурация |
| 1 | 1,5 | `python3 tools/probe/check_pi_deploy.py --board pi1 --host $PI1 --out $D/1-deploy --only image,app-stop,app-restart,app-kill`; затем `$R1 'journalctl -u sheregesh-console --since "-3 min" --no-pager \| grep -E "status=\|Failed with result\|Main process exited"; systemctl show sheregesh-console -p ActiveState,Result,NRestarts'` | **E2E-13:** `app-stop` PASS — после stop `inactive`, в журнале нет `status=143` и `Failed with result` за остановку; после start `active`, `Result=success`; сирот `cage`/`sheregesh_console` нет. `image` PASS. **E2E-14:** `app-restart`, `app-kill` PASS | `app-stop` FAIL — E2E-13 FAIL на abd9d68: журнал и `processes_after` в протокол, разбор по `docs/acceptance/s3.1/app-stop-closure.md` |
| 2 | 1 | `$R1 'systemctl --failed --no-legend; systemd-analyze; systemctl show sheregesh-var-seed -p ActiveState,Result,ConditionResult; journalctl -b -u sheregesh-var-seed --no-pager \| tail -3; test -f /var/lib/dpkg/status && echo dpkg-ok; systemctl show sheregesh-console -p StatusText,ActiveEnterTimestampMonotonic; sheregesh-health --json' > $D/2-boot.txt` | **E2E-01:** failed нет; служба пульта active; `StatusText` — «first frame shown» (sd_notify приложения); var-seed либо отработал («/var дополнен»), либо пропущен по условию (`dpkg/status` уже был); `dpkg-ok`; health без FAIL по `config.*` | failed-юниты или нет `dpkg/status` — дефект переноса `/var` (A, OTA) |
| 3 | 2 | `python3 tools/probe/check_link.py --console $PI1 --remote $PI2 --out $D/3-link --only health,identity,connect` | **E2E-03:** 3/3 PASS: та же идентичность устройства, mTLS TLSv1.3, `link.versions` 1.1/1.1 | `identity` FAIL — ключи не пережили смену слота (A) |
| 4 | 2,5 | `python3 tools/probe/check_video_link.py --console $PI1 --remote $PI2 --out $D/4-video --only session,stream,leftovers`; `python3 tools/probe/e2e_run.py --console $PI1 --remote $PI2 --out $D/4-e2e --fault sender-kill --cycles 1 --gap 10` | **E2E-05:** stream ≥ 29 к/с за окно, потерь 0, сирот нет. **E2E-06:** `video_off_s` ≤ 3 с. **E2E-10:** `video_back_s` ≤ 10 с. **E2E-08:** `gui_stale_max_s` записан (без порога SRS) | превышение — сравнить с g7061b3c (`../e2e/hw/e2e-sender-kill/`). При F причина ожидаема (§1.2: 36d6ac1) — записать, не чинить |
| 5 | 1 | `$R1 'journalctl -u sheregesh-console --since "-6 min" --no-pager \| grep -cE "(link\|video): "; journalctl -u sheregesh-console --since "-6 min" --no-pager \| grep -c TypeError'` | **E2E-12** и монитор. Вариант D: строк > 0, `TypeError` 0. Вариант F: строк 0 и `TypeError` > 0 — **ожидаемо** (§1.2), итог «известная регрессия заводского кода abd9d68» | в варианте D — FAIL E2E-12 |

Итог в `$D/RESULT.md`: E2E-13 PASS/FAIL. По остальным — PASS, «известная регрессия (F)» или FAIL. Записать
вариант (F/D) и ревизию инструментов (`git rev-parse HEAD` на Mac): инструменты Mac менялись после abd9d68
(`../ota-closure/GATE.md` §6).

## 4. Открытые вопросы

1. Вариант после E2E-13: оставить заводской `abd9d68` (F — известные регрессии E2E-08/12 и монитора) или сделать
   DEV deploy текущего `v2` на слот B (D)? Решение владельца.
2. При F пульт старше блока (pi2 — g7061b3c): совместимость по коду подтверждена (протокол 1.1), но смешанная пара
   на платах в этом направлении (пульт старше) не испытывалась. Прецедент S3.3 §5.2 — обратный.
