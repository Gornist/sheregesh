# E2E-13 — план аппаратного OTA pi1 (пульт, A/IDLE → B COMMITTED) и сокращённая приёмка

Цель: доставить на pi1 образ с ретранслятором уведомлений, который на SIGTERM выходит с кодом 0 (02a9b64, есть
в v2), и закрыть E2E-13 (`systemctl stop sheregesh-console` без `status=143`). По образцу [`HW-PLAN.md`](HW-PLAN.md)
(pi2); отличия — роль console, другая плата, исправленный цикл упорядочения, сокращённая приёмка.

## Исходное состояние pi1 (снято только чтением, 2026-10-25 ~11:50 MSK)
- 192.168.1.92, активен **A** (`/dev/disk/by-slot/active/system`), образ 57e8fe4 (`ota_version` 0.1.1792843040),
  OTA `IDLE`, history пуста (OTA на pi1 не выполнялся ни разу), `autoboot` `[all] boot_partition=2`,
  `[tryboot] boot_partition=3`, `tryboot_a_b=1`; `sheregesh-console` active, NRestarts 0; failed — нет.
- Код пульта сейчас — DEV-выпуск g7061b3c. На B выпуск DEV **не переносится**: B запустит код образа. Образ
  кандидата собран из v2 09fca4e, `software/` в нём = g7061b3c (между 7061b3c и v2 изменений в `software/`,
  `tools/image`, `os/` нет) + исправление юнита var-seed — регрессии кода пульта нет.

## Кандидат
- Коммит **v2 09fca4e** (исправление цикла `sheregesh-var-seed`, причина отказа B в цикле 2 —
  [`hardware/cycle2/STOP-2.md`](hardware/cycle2/STOP-2.md) «Причина установлена»). Сборка devbox, `SOURCE_DATE_EPOCH`
  1792917547 → **V = 0.1.1792917547**. Сценарий сборки — `os/docker-build.sh` ветки `ota/e2e13` (syft из GitHub).
- Пакет: путь и SHA-256 — по итогам [`PACKAGE.md`](PACKAGE.md) (§1–§6 с заменой коммита/версии) + гейты ниже.
  **SHA утверждает владелец** до `ota-install --sha256`.
- Пакеты `c30de791…5a82` (цикл 2) и `a09082fb…c86a` (цикл 1) **не ставить**: в обоих образах цикл упорядочения.

## Гейты до платы (все PASS, иначе STOP)
1. `verify.json` 0 FAIL, в т. ч. новый `boot.order`.
2. `tools/image/order_check.sh ОБРАЗ.img.zst 8` → `PASS: циклов упорядочения нет` (systemd-analyze на корне образа).
3. `tools/image/owners_check.sh` нового образа = эталон CI 57e8fe4 (`diff` пуст).
4. В system_a: `notify_relay.py` = `git show 09fca4e:software/sheregesh/notify_relay.py`, `sheregesh-console.service`
   = `tools/image/units/sheregesh-console.service`.
5. Пакет воспроизводим (две сборки, `cmp`), подпись `Good "sheregesh-ota"` по `allowed_signers` плат
   (`74c95610…00c0`), `version_gt_active` против 0.1.1792843040, манифест (commit 09fca4e, V, compatible dev).
6. L3 39/39 в двух режимах: агентом 57e8fe4 (он установит на pi1) и агентом кандидата.

## Шаги на pi1 (каждое действие — только по допуску владельца, [`GATE.md`](GATE.md) §5)

```bash
cd ~/Developer/sheregesh
P=…/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792917547.shota  SHA=<утверждённый>  V=0.1.1792917547
G=tools/deploy/ota-check/slot_guard.py  S=tools/deploy/shdeploy.py  D=docs/acceptance/e2e/ota-pi1
PI1=192.168.1.92  PI2=192.168.1.171  mkdir -p $D
```

| # | Действие | Ожидается | При отказе |
|---|---|---|---|
| 0 | `shasum -a 256 $P` = SHA; `verify_payload.py`; `$S ota-status pi1`; `$S ota-disable pi1`; `$G snap pi1 $PI1 $D/pi1-before.json`; `$G snap pi2 $PI2 $D/pi2-before.json`; **E2E-13 «до»** (п. A ниже — ожидается FAIL `status=143`, фиксирует исходное); git commit | IDLE/A/consistent; снимки годны, `ota.enable_present=no` | ничего не менять |
| 1 | `$S ota-install pi1 --payload $P --sha256 $SHA \| tee $D/install.log` (агент 57e8fe4) | `OK`, `INSTALLED`, target B, V | без tryboot; A не тронут |
| 1a | snap `pi1-after.json`; `compare pi1-before pi1-after --mode install --version $V`; pi2 `--mode unchanged` | PASS (p1/p2/p4 — 0 записанных секторов, p3/p5 записаны) | без tryboot; разбор |
| 2 | snap `pi1-pre-enable.json`; `compare … --mode install`; `$S ota-enable pi1` | `switching` | без tryboot |
| 3 | **Человек у pi1 с питанием.** `$S ota-tryboot pi1` | SSH на **том же адресе .92** через ~25 с | нет SSH 5 мин **или новый адрес DHCP** → STOP, выключить/включить питание pi1 → A (tryboot однократен) → п. «Восстановление» |
| 4 | Стабилизация ≥ 180 с. `ota-status`; snap `pi1-trial.json`; `compare pi1-before pi1-trial --mode trial --version $V`; pi2 `unchanged`; факты (ниже) | `HEALTH_PENDING`, попытка 1; trial PASS; все факты PASS | `ota-abort` → A; `rollback`. Confirm не делать |
| 5 | Решение владельца. `$S ota-confirm pi1 \| tee $D/confirm.json` | `COMMITTED`, active B, V | не повторять; по умолчанию `ota-abort` → A |
| 6 | snap `pi1-committed.json`; `compare … --mode trial` | ровно 3 ожидаемых FAIL (`same.autoboot.sha256`, `bootconfig.autoboot`, `ota.state`) | разбор |
| 7 | Контрольная перезагрузка `systemctl reboot`; snap `pi1-reboot.json` | B основной; 4 ожидаемых FAIL (+`dt.tryboot`=0); health PASS | **страховки tryboot нет**: карта в Mac, на p1 `autoboot.txt` `[all] boot_partition=2`, A → `ota-abort` |
| 8 | **Сокращённая приёмка** (≤ 10 мин, ниже); `$S ota-disable pi1`; финальный snap; pi2 `unchanged` | все пункты PASS | по пункту |

**Факты на пробной загрузке (шаг 4, только чтение):**
```bash
ssh sheregesh-ui@$PI1 'cut -d" " -f1 /proc/uptime; cat /usr/share/sheregesh/build.json; \
  findmnt -no SOURCE,OPTIONS /persistent /home /etc/ssh /var; systemctl --failed --no-legend; \
  journalctl -b --no-pager | grep -cE "ordering cycle|deleted to break"; \
  systemctl show -p ActiveState,NRestarts sheregesh-console ssh sheregesh-ota.socket; \
  cat /run/sheregesh-ui/link-status.json; python3 -c "import json;print(json.load(open(\"/run/sheregesh-ui/ui-state.json\"))[\"video\"])"; \
  sheregesh-health --json --role console --link-config ~/.config/sheregesh/link.json'
```
GO: uptime ≥ 180; `ota_version` = V, commit 09fca4e; `/persistent` rw, `/home` и `/etc/ssh` с persistent, `/var` с
`slots/system_b/var`; failed пусто; **циклов упорядочения 0**; console active, NRestarts 0; mTLS `connected`
TLSv1.3; видео `remote-tls` `playing` ≥ 25 к/с; health PASS, сетевые проверки PASS (не SKIP); адрес .92.

## Сокращённая приёмка после COMMITTED (≤ 10 мин) → `$D/acceptance.txt`
1. **Версия/OTA/health:** `build.json` (09fca4e, V); `ota-status` COMMITTED/B/V, consistent; `sheregesh-health`
   console PASS; `systemctl --failed` пусто; `sha256sum /usr/lib/sheregesh/software/sheregesh/notify_relay.py` = гейт 4.
2. **E2E-13 (A):** `systemctl stop sheregesh-console` (polkit sheregesh-ui) → через ≤ 10 с
   `systemctl show -p ActiveState,Result,ExecMainStatus,ExecMainCode sheregesh-console` = `inactive`, `success`, `0`;
   `journalctl -u sheregesh-console -b --since -1min` без `status=143` и без `Failed with result`; затем
   `systemctl start sheregesh-console` → `active` за ≤ 30 с, NRestarts 0.
3. **SSH:** новый вход `ssh sheregesh-ui@192.168.1.92 true`; ключ хоста не сменился (known_hosts без предупреждения).
4. **Связь с pi2:** `link-status.json` `connected` TLSv1.3 после п. 2 (переподключение ≤ 30 с); на pi2 сеанс с
   пультом (`/run/sheregesh-remote/link-status.json` `connected`).
5. **Синтетическое видео:** `tools/probe/check_video_link.py --only session,stream` с pi1: `remote-tls` `playing`,
   ≥ 25 к/с, кадры растут.
6. **Журнал:** `journalctl -b -p warning` без новых ошибок служб Шерегеша; запись загрузки на persistent
   (`journalctl --list-boots` показывает текущую загрузку — machine-id прежний).

## Восстановление
- **До confirm (шаги 3–5):** любой отказ → `ota-abort` (если SSH есть) или **цикл питания pi1 человеком** →
  прошивка загрузит A (`[all] boot_partition=2`, tryboot одноразовый). Затем `ota-status` (ROLLED_BACK),
  `slot_guard --mode rollback`, health. Журнал B: если `/persistent` был смонтирован — `journalctl -m`; если нет —
  он потерян (только `/run`), нужна консоль (ниже).
- **После confirm, B не грузится (шаг 7):** выключить, карта в Mac (`diskutil unmountDisk` сразу — Spotlight пишет на
  FAT), на p1 (`bootconfig`) `autoboot.txt` `[all] boot_partition=2`, вернуть карту → A → `ota-abort`.
- **Консоль загрузки для разбора:** `console=ttyAMA0,115200` уже в cmdline (PL011 на GPIO14/15 при
  `disable-bt`): USB-UART 3,3 В — GND (пин 6), RX адаптера ← TXD GPIO14 (пин 8), TX → RXD GPIO15 (пин 10);
  `screen /dev/tty.usbserial-* 115200`. Подключать до шага 3; альтернатива — HDMI-монитор (tty1).

## Риски
- Пульт недоступен на время пробной загрузки (≈ 1–5 мин) и при отказе — до цикла питания: нужен человек у pi1.
- Цикл упорядочения устранён и проверяется гейтом 2 и фактом на плате; иных различий B c1 (дошёл до HEALTH_PENDING)
  и нового образа, кроме кода пульта v2 (= выпуск g7061b3c, проверен E2E на платах) и юнита, нет.
- Шаг 7 без страховки tryboot (как на pi2).
