# OTA CLOSURE — план аппаратного OTA pi2 (A/IDLE → B COMMITTED)

Исходное состояние — [`hardware/REPAIR.md`](hardware/REPAIR.md):
- pi2 на A, образ 57e8fe4, OTA `IDLE`, `consistent: true`;
- `version_active` 0.1.1792843040, `autoboot` `[all]=2`/`[tryboot]=3`;
- разрешение переключения есть.

Кандидат: образ abd9d68, V = `0.1.1792880268`.

Каждое действие с платой выполняется только по допуску [`GATE.md`](GATE.md) §5. Повторных `tryboot`/`confirm` «на всякий случай» нет. DEV deploy в план не входит.

```bash
cd ~/Developer/sheregesh
P=…/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792880268.shota   SHA=<утверждённый владельцем>   V=0.1.1792880268
G=tools/deploy/ota-check/slot_guard.py   S=tools/deploy/shdeploy.py   D=docs/acceptance/ota-closure/hardware/cycle
PI1=192.168.1.92   PI2=192.168.1.171   mkdir -p $D
```

## Почему снимок «до» нужен свежий и что даст `.Spotlight-V100`

Разбор по коду `slot_guard.compare`:

| Режим | Сравнивает `tree.boot` | Требование к разрешению | Вывод |
|---|---|---|---|
| `install`, `unchanged` | да: `same.tree.boot` + `same.boot_id` | `ota.enable_absent` (`ota.enable_present` = `no`) | `pi2-after-repair.json` как «до» **не годится**: `ota.enable_present=YES` → FAIL `ota.enable_absent`. Нужен свежий снимок после `ota-disable`, в той же загрузке, что и установка. `.Spotlight-V100` на p2 будет в обоих снимках, поэтому ложного FAIL `same.tree.boot` нет |
| `trial` | нет (только identity, `serial`, `autoboot.sha256`) | `YES` | Spotlight не влияет. Сравнивать со свежим «до» шага 0 |
| `rollback` | да, против «до» | — | С «до» шага 0 ложного FAIL нет. С `s3.2-ota/step1/pi2-before.json` FAIL будет (как в REPAIR) |

Карту pi2 в Mac во время цикла **не вставлять**. macOS пишет на FAT-разделы, и после этого все сравнения `tree.boot` становятся недействительными.

## Шаги

| # | Действие | Ожидается | STOP | При отказе | Риск |
|---|---|---|---|---|---|
| 0 | `shasum -a 256 $P` = SHA; `verify_payload.py` (`allowed_signers` плат); `$S ota-status pi2`; **`$S ota-disable pi2`**; `$G snap pi2 $PI2 $D/pi2-before.json`; `$G snap pi1 $PI1 $D/pi1-before.json` → **git commit до записи**; `check_link --only health,baseline`; `check_video_link --only session,stream` | SHA совпадает, `Good "sheregesh-ota"`, `IDLE`/A/`consistent`; снимки годны, `ota.enable_present=no`, health PASS, failed none | любой код ≠ 0; снимок ERROR | ничего не менять | нет: чтение и отзыв разрешения |
| 1 | `$S ota-install pi2 --payload $P --sha256 $SHA \| tee $D/install.log` (выполняет **агент 57e8fe4**) | `result OK`, `INSTALLED`, target B, `version_target` V. `/var` B без образца — дополнит `sheregesh-var-seed` при первой загрузке. `expect` нет | `REJECTED`/`FAILED` | без tryboot; A — основной и не тронут; `ota-abort` (только сброс) — по решению | сбой питания во время записи → `INSTALLING` → `FAILED`, A цел. Питание не отключать |
| 1a | snap pi2 → `pi2-after.json`; `compare pi2-before pi2-after --mode install --version $V`; pi1 `--mode unchanged`; `check_link`/`check_video_link … leftovers`; `ota-status` | **PASS**: `untouched.p1/p2/p4` = 0 секторов, `written.p3/p5` ≥ размера, `same.boot_id`, `same.tree.*`, `same.fp.*`, `same.unit.*`, `ota.enable_absent`, `ota.state INSTALLED`; pi1 PASS | FAIL/ERROR | без tryboot; разбор | — |
| 2 | snap `pi2-pre-enable.json`; `compare pi2-before pi2-pre-enable --mode install` (PASS, как в s3.2 step3); `$S ota-enable pi2` | `switching` для 100000009d497e91 | FAIL; отказ enable | без tryboot | нет |
| 3 | Человек у pi2 с питанием; pi1 показывает видео. `$S ota-tryboot pi2` (агент 57e8fe4: перенос конфигурации перед tryboot не делает) | перезагрузка; SSH через ~25 с; B в пробной загрузке | SSH нет > 5 мин | ждать; затем выключить питание → прошивка вернёт A (tryboot однократен); `rollback` | паника или зависание B → A |
| 4 | **Стабилизация**: uptime ≥ 180 с, канал и видео установились. `ota-status`; snap `pi2-trial.json`; `compare pi2-before pi2-trial --mode trial --version $V`; pi1 `unchanged`; `check_link --only health,baseline,identity,ports`; `check_video_link --only session,stream,telemetry,no-plain,leftovers`; факты критериев (п. ниже) | `HEALTH_PENDING`, попытка 1, B, V; trial **PASS** (identity против шага 0, `slot.trial_B`, `dt.tryboot=1`, `build.version=V`, `untouched_since_boot.p2/p4`, `remote-net` NRestarts 0); видео декодируется на pi1; все факты критериев PASS | любой FAIL/ERROR или нестабильность | **`ota-abort`** → A; `rollback --trial`. Confirm не делать | confirm одноразовый: с ложным FAIL — потеря попытки |
| 5 | Решение владельца по трём источникам: trial, pi1, факты. `$S ota-confirm pi2 \| tee $D/confirm.json` (**агент кандидата**; критерии вычисляются один раз, без ожидания) | `OK`, `COMMITTED`, active B, `version_active` V. Обязательные `checks` PASS (`present.*` — наличие; совпадение доказал trial) | `FAILED` | состояние `TRYBOOT` (попытка 1). **Автоматически не повторять.** Разобрать `checks`. По умолчанию `ota-abort` → A (агент 57e8fe4 прочтёт состояние: `expect` нет) → `rollback`. Второй `ota-tryboot` — только по решению владельца при понятной временной причине; это последняя попытка | — |
| 6 | Та же загрузка: snap `pi2-committed.json`; `compare pi2-before pi2-committed --mode trial` | ровно 3 ожидаемых FAIL: `same.autoboot.sha256`, `bootconfig.autoboot`, `ota.state`; остальное PASS. Вручную: `autoboot` `[all]=3;[tryboot]=2`, `ota.status` COMMITTED/B/V | иной FAIL | разбор; плата на B — основном | — |
| 7 | Контрольная перезагрузка: `ssh sheregesh-ui@$PI2 systemctl reboot`. Затем snap `pi2-reboot.json`; `compare pi2-before pi2-reboot --mode trial`; `check_link`/`check_video_link`; health | B загружен как основной. Ожидаемые FAIL: `same.autoboot.sha256`, `bootconfig.autoboot`, `ota.state`, `dt.tryboot` (=0); остальное PASS. Вручную: active B по всем признакам, `dt.tryboot=0`, COMMITTED→COMMITTED, health PASS, failed none | B не загрузился / иной FAIL | **автоматического возврата нет** (A больше не основной): выключить, карта в Mac, на p1 `autoboot.txt` `[all] boot_partition=2`, A → `ota-abort` (57e8fe4 сбросит в IDLE) | единственный шаг без страховки tryboot |
| 8 | `$S ota-disable pi2`; `ota-status`; финальный snap; pi1 `unchanged` против `pi1-before` | разрешения нет; COMMITTED B | — | — | — |

**Факты критериев (шаг 4)** — только чтение. Это сухая проверка тех же критериев: confirm сам не ждёт стабилизации, критерии вычисляются один раз.

```bash
ssh sheregesh-ui@$PI2 'cut -d" " -f1 /proc/uptime; cat /run/sheregesh-remote/link-status.json; \
  systemctl show -p ActiveState,NRestarts sheregesh-remote-net ssh; systemctl --failed --no-legend; \
  cat /usr/share/sheregesh/build.json; findmnt -no SOURCE,OPTIONS /persistent /home /etc/ssh; \
  sheregesh-health --json --role remote --link-config ~/.config/sheregesh/link.json'
```

GO при выполнении всех условий:
- uptime ≥ 90 (с запасом ≥ 180);
- `connected` и `tls`;
- `video`: `port 7444`, `tls1.3-mutual`, `active`, `frames > 0`, `sender_fps_5s > 0`;
- `remote-net` и `ssh` active, NRestarts 0;
- failed пусто;
- `ota_version` = V;
- `/persistent` rw;
- health PASS, а `config.link`, `link.state`, `certs.device`, `certs.ca`, `certs.key-mode` — PASS, не SKIP.

**Ограничения.**
- Режима `commit` у `slot_guard` нет. Шаги 6–7 проверяются через `--mode trial` с перечисленными ожидаемыми FAIL и ручной сверкой этих полей.
- После COMMITTED `slot_guard install` для следующего цикла с B неприменим: жёстко требуются A и `[all]=2`.
- После COMMITTED B запускает заводской код образа: выпуск DEV на B не переносится. Проверка app-stop — отдельно, после цикла.
