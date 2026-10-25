# S5-OTA — лабораторный образ v2 `bd74c8c` для OTA pi2: сверка, файловые испытания, пакет, план (2026-10-25)

**Итог: READY FOR OWNER DECISION.** Образ сверен с `bd74c8c`, L3 39/39 в обоих режимах агента, пакет подписан и
воспроизводим. **На платы ничего не ставилось.** `ota-install`/`ota-enable`/`ota-tryboot`/`ota-confirm` не
выполнялись. С pi2 сняты только данные чтением: `ota-status`, снимок `slot_guard snap`, факты по SSH.

**Пакет на утверждение владельцу.** `ota-install --sha256` выполняется только по этому SHA:

| | |
|---|---|
| Файл (Mac, не в git) | `/Users/peachbite/Developer/sheregesh/build/image/devbox-bd74c8c/ota-1/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792949168.shota` |
| SHA-256 пакета | **`9bbdcd6c7d06f1388a006357e9232ae75769b29482f98c4865902825bc468ccf`** (329 519 059 Б) |
| Версия V | `0.1.1792949168` > pi1 B `0.1.1792924537` > pi2 A `0.1.1792843040` |
| Манифест | `60e1adfd…eeee`, commit `bd74c8c412028f8f4fb0a4d6b89b738e676db959`, compatible `sheregesh-lab-pi4-dev-1` |
| Образ | `image_sha256` `0ecf3aa9…0eb2`; `img.zst` `96f29ceb…cbac` ([`../s5-video/image-bd74c8c/GATES.md`](../s5-video/image-bd74c8c/GATES.md)) |

Не ставить отозванные и устаревшие пакеты: `a09082fb…c86a` и `c30de791…5a82` — в их образах цикл упорядочения.
Пакеты `d38cc0f0…4910` и `0dbe43e1…774e` предназначены для pi1.

## 1. Содержимое образа = `bd74c8c`

| Проверка | Итог | Файл |
|---|---|---|
| Артефакты devbox → Mac, `shasum -c SHA256SUMS` | OK ×5 | `sha256sums-check.txt` |
| .deb образа против дерева репозитория. Код `build_deb.build_tree`; `software/`, `tools/image` в checkout = `bd74c8c` | 88 = 88 файлов, включая `.qm`, обёртки и юниты. Отличается только `build.json` (коммит checkout), как в build-e2e13 | `deb-vs-repo.diff` |
| Пакет установлен в образе | `build.json` = bd74c8c, V; dpkg `0.1.0~dev0+gbd74c8c` `install ok installed`; `md5sum -c` по `sheregesh.md5sums` — OK, 88 файлов | `image-inspect.txt` |
| Юниты `tools/image/units` (10) в system_a | SHA-256 = репозиторий, 644, 0:0 | `inspect-vs-repo.json` |
| S5-D1: `sheregesh-remote-net` | `ReadWritePaths=-…/.config/sheregesh`, `ReadOnlyPaths=-…/link.json -…/remote.role`; юнит в образе = репозиторий (`6e53bd6e…`) | `inspect-vs-repo.json` |
| lab-video по `manifest.tsv` | 10 из 10: SHA-256 = источник, права из манифеста, владелец 0:0. Юнитов `sheregesh-lab-*` ровно три, других `sheregesh-*` нет | `inspect-vs-repo.json` |
| Лабораторные юниты включены? | **Нет.** `static`: нет `[Install]` и ссылок в `/etc/systemd`. Включены те же 7 юнитов, что и раньше | `image-inspect.txt` |
| order_check (systemd 257, 8 прогонов) — повтор | PASS: циклов нет | `order-check.txt` |
| Лабораторные юниты и order_check | `order_check` смотрит `default.target`+`sysinit.target`, а статические lab-юниты в эту транзакцию **не входят**. Поэтому проведена отдельная проверка: `systemd-analyze verify` шести экземпляров (`vivid`, `vivid-feed@cvbs/hdmi`, `usbip@hdmi/cvbs/camera`) отдельно и вместе с `default.target sysinit.target`, 8 прогонов. Циклов нет, предупреждений verify нет | `image-inspect.txt` |
| Пакеты дистрибутива против образа b1d95a2 (OTA pi1 прошёл) | `packages.tsv` совпадают, кроме строки `sheregesh` | — |
| owners_check | = эталон CI 57e8fe4 (GATES образа); файлы lab-video 0:0 | `../s5-video/image-bd74c8c/owners.txt` |

## 2. Совместимость состояния и причины прошлых STOP

- **pi2 сейчас** (только чтение, `pi2-ota-status.json`, `pi2-snap-readonly.json` 53 поля, `pi2-facts-readonly.txt`):
  - A (p2/p4), образ 57e8fe4, `ROLLED_BACK`, `consistent: true`, autoboot `[all]=2`/`[tryboot]=3`;
  - разрешения переключения нет;
  - health 14/14, failed нет, `remote-net` active, NRestarts 0;
  - mTLS с pi1 `connected` TLSv1.3, видео 7444 `tls1.3-mutual` active 30 к/с;
  - `/persistent` 820M свободно; `allowed_signers` `74c95610…00c0` = ключ пакета.
- **Путь состояния** — `state-compat.txt`, 17/17 PASS. Проверено кодом `update/model.py` обоих коммитов на одном
  `state.json`, сценарии «успех», «откат» и «цикл питания»:
  1. 57e8fe4: ROLLED_BACK → VERIFIED → INSTALLING → INSTALLED → TRYBOOT.
  2. Кандидат читает TRYBOOT без `expect` и переводит в HEALTH_PENDING (попытка 1), затем confirm → COMMITTED.
  3. После `abort` 57e8fe4 читает состояние, которое сохранил кандидат: поле `expect` при пустом значении не
     пишется. Снова принимает install.
- **Аппаратные прецеденты.**
  - История pi2 уже содержит переход `ROLLED_BACK->VERIFIED->INSTALLING`.
  - Путь «57e8fe4 install → tryboot → confirm агентом кандидата» пройден на pi1: `../e2e/ota-pi1/cycle2/retry/RESULT.md`.
  - `software/sheregesh/update`, `health.py`, `deploy/` в bd74c8c = b1d95a2.
- **Причины STOP закрыты в этом образе.**
  - Цикл 2 (`sheregesh-var-seed` упорядочен раньше `local-fs` → B без `/persistent`): юнит = исправленный (`8fce3de6…`,
    `After=local-fs.target`, `Before=sysinit.target systemd-tmpfiles-setup.service`), `verify_image boot.order` PASS,
    order_check PASS, образец `var-seed.tar.gz` в образе; на system_a `/var` без `dpkg/status` (дополнит var-seed).
  - Цикл 1 (uid 1000): owners_check = CI.
- **Отличие от pi1.** Подтверждение роли **remote** на плате ещё не выполнялось: pi2 цикл 1 дошёл только до
  HEALTH_PENDING. Критерии `confirm.py` для remote: `remote-net` active без перезапусков, сеанс mTLS с пультом,
  **видеосеанс** (7444, `tls1.3-mutual`, active, frames > 0, `sender_fps_5s` > 0), health PASS с сетевыми проверками.
  Значит, на шаге 5 пульт pi1 обязан принимать видео с pi2.

## 3. Файловое испытание L3 (`tools/deploy/ota-check/run.py`, devbox, база — образ CI 57e8fe4)

| Режим | Итог | Установка | Файлы |
|---|---|---|---|
| Агент 57e8fe4 (установит на pi2) | **39/39 PASS**, обязательные PASS | 25,7 с | `l3-agent-57e8fe4.{json,log}` |
| Агент кандидата (bd74c8c) | **39/39 PASS**, обязательные PASS | 26,3 с | `l3-agent-candidate.{json,log}` |

`l3-summary.txt`. В обоих режимах `payload_sha256` = утверждаемому.

Охват испытания:
- отказы: чужая подпись отвергается (REJECTED, B не тронут); обрыв потока → FAILED, A и autoboot не тронуты, tryboot запрещён;
- установка: install → INSTALLED, B = манифест, A не изменён; общие данные (home, ключи, сертификаты, роль, /etc/ssh, machine-id) не изменились;
- `/var` нового слота: у старого агента он без образца, служба var-seed дополняет его, не перезаписывая;
- переход: tryboot → autoboot `[all]=2`/`[tryboot]=3`; повторная установка в TRYBOOT отвергается (REJECTED);
- **откат**: `abort` → основной A; в конце A и данные не изменились.

Аппаратный счёт `slot_guard` («44/0/0») в L3 не входит: он снимается на плате по плану ниже.

## 4. Пакет

| Проверка | Итог | Файл |
|---|---|---|
| `shdeploy ota-build --build-report` ×2 | rc 0; две сборки — `cmp` пакета и манифеста совпадают (**REPRODUCIBLE**) | `ota-build-{1,2}.log`, `payload-sha256.txt` |
| payload-check (код агента репозитория, `allowed_signers` плат `74c95610…00c0`) | `Good "sheregesh-ota"` ED25519 `SHA256:5wJfw7Qw…lOZUw`; `compressed_sha256_ok` boot и system; trailing 0; `version_gt_active`; PASS | `payload-check.json` |
| Манифест против build-report и репозитория | все 13 пунктов PASS; units = `tools/image/units`; `image_verify` 76 проверок, 0 FAIL | `manifest-check.json`, `ota-payload-manifest.json` |

Подпись выполнена ключом `~/.ssh/sheregesh-deploy` на Mac. Закрытый ключ не выводился и не копировался.
На devbox передаются только поток, образ-база, открытый `allowed_signers` и разрешение для ТЕСТОВОГО серийного номера.

## 5. План аппаратного OTA pi2 (каждый шаг — только по решению владельца; повторных tryboot/confirm нет)

Основа — [`../ota-closure/HW-PLAN.md`](../ota-closure/HW-PLAN.md) и [`../ota-closure/PI1-PLAN.md`](../ota-closure/PI1-PLAN.md),
отличия — исходное `ROLLED_BACK`, роль remote, лабораторные проверки S5 после COMMITTED.

```bash
cd ~/Developer/sheregesh          # чистое дерево v2; tools/deploy и software = bd74c8c или новее без изменений в них
P=/Users/peachbite/Developer/sheregesh/build/image/devbox-bd74c8c/ota-1/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792949168.shota
SHA=9bbdcd6c7d06f1388a006357e9232ae75769b29482f98c4865902825bc468ccf   V=0.1.1792949168
S=tools/deploy/shdeploy.py  G=tools/deploy/ota-check/slot_guard.py  D=docs/acceptance/s5-ota/hardware
PI1=192.168.1.92  PI2=192.168.1.171  mkdir -p $D;  git rev-parse HEAD > $D/tools-revision.txt
```

| # | Действие | Ожидается (GO) | STOP → что делать |
|---|---|---|---|
| 0 | Подготовка: `shasum -a 256 $P` (= SHA); `python3 docs/acceptance/s3.2-ota/pi1-preflight/tools/verify_payload.py . $P build/image/devbox-bd74c8c/allowed_signers sheregesh-lab-pi4-dev-1 0.1.1792843040`; `$S ota-status pi2 \| tee $D/ota-status-0.json`; `$S ota-disable pi2`; `$G snap pi2 $PI2 $D/pi2-before.json`; `$G snap pi1 $PI1 $D/pi1-before.json`; `python3 tools/probe/check_link.py --console $PI1 --remote $PI2 --out $D/link-before --only health,baseline`; `python3 tools/probe/check_video_link.py --console $PI1 --remote $PI2 --out $D/video-before --only session,stream`. **Затем git commit до записи** | SHA совпадает; подпись `Good`; `ROLLED_BACK`/A/`consistent`; снимки без ERROR; `ota.enable_present=no`; health PASS; видео идёт | любой код ≠ 0, снимок ERROR, состояние не ROLLED_BACK/IDLE на A → ничего не менять |
| 1 | `$S ota-install pi2 --payload $P --sha256 $SHA \| tee $D/install.log` — установку выполняет агент 57e8fe4 | `OK`, `INSTALLED`, target B, `version_target` V; `expect` нет | `REJECTED`/`FAILED` → tryboot не делать; A основной и цел; разбор |
| 1a | snap `pi2-after.json`; `$G compare $D/pi2-before.json $D/pi2-after.json --mode install --version $V \| tee $D/pi2-compare-install.txt`; pi1 `--mode unchanged`; `check_video_link … --only leftovers` | PASS (≈ 44/0/0): p1/p2/p4 — 0 записанных секторов, p3/p5 записаны, тот же boot_id, отпечатки и юниты те же; pi1 PASS | FAIL/ERROR → tryboot не делать; разбор |
| 2 | **Конфигурацию не менять между 1 и 3**: агент 57e8fe4 не переносит её перед tryboot. snap `pi2-pre-enable.json`; `compare … --mode install`; `$S ota-enable pi2 \| tee $D/ota-enable.txt` | compare PASS; `switching` для 100000009d497e91 | FAIL; enable отвергнут → tryboot не делать |
| 3 | **Человек у питания pi2, пульт pi1 показывает видео; при возможности — UART (PI1-PLAN «Консоль»)**. `date -u > $D/tryboot-time.txt; $S ota-tryboot pi2 \| tee $D/tryboot.txt` | перезагрузка; SSH на **том же адресе .171** за ≈ 25–60 с | нет SSH 5 мин **или новый адрес DHCP** (`collect_boot.py find --mac d8:3a:dd:69:0f:6e`) → STOP: выключить и включить питание pi2 → прошивка загрузит A (tryboot однократен) → «Восстановление» |
| 4 | Стабилизация ≥ 180 с. `$S ota-status pi2 \| tee $D/ota-status-trial.json`; snap `pi2-trial.json`; `compare pi2-before pi2-trial --mode trial --version $V`; pi1 `unchanged`; `check_link --only health,baseline,identity,ports`; `check_video_link --only session,stream,telemetry,no-plain,leftovers`; **факты** (ниже) → `$D/trial-facts.txt` | `HEALTH_PENDING`, попытка 1, B, V; trial PASS; на pi1 видео декодируется; все факты GO | любой FAIL/ERROR → `$S ota-abort pi2` (→ A), confirm **не** делать → «Восстановление» |
| 5 | Решение владельца по trial, pi1 и фактам. `$S ota-confirm pi2 \| tee $D/confirm.json` — confirm выполняет агент кандидата; критерии вычисляются один раз | `OK`, `COMMITTED`, active B, `version_active` V; все обязательные `checks` PASS (`present.*`, `unit.sheregesh-remote-net`, `mtls`, `video`, `health`, `health.network`) | `FAILED` → состояние TRYBOOT; **не повторять**; разобрать `checks`; по умолчанию `ota-abort` → A |
| 6 | Та же загрузка: snap `pi2-committed.json`; `compare pi2-before pi2-committed --mode trial` | ровно 3 ожидаемых FAIL: `same.autoboot.sha256`, `bootconfig.autoboot`, `ota.state`; autoboot `[all]=3;[tryboot]=2` | иной FAIL → разбор; плата на B, B основной |
| 7 | Контрольная перезагрузка `ssh sheregesh-ui@$PI2 systemctl reboot`; snap `pi2-reboot.json`; compare `--mode trial`; `check_link`/`check_video_link`; health | B основной; 4 ожидаемых FAIL (+`dt.tryboot`=0); health PASS; failed нет; циклов 0 | **страховки tryboot нет**: B не загрузился → выключить, карта в Mac (`diskutil unmountDisk` сразу), на p1 `autoboot.txt` `[all] boot_partition=2`, A → `ota-abort` |
| 8 | Проверки S5 на B (≤ 15 мин, `$D/acceptance.txt`): **S5-D1** — выбрать источник с пульта, `systemctl restart sheregesh-remote-net`, выбор сохранился (`video-input.json`), `link.json`/`remote.role` не изменились; **п. 7** — пять пунктов `tools/lab-video/pi2/README.md` «Что проверено»; `$S ota-disable pi2`; финальный snap; pi1 `unchanged` | все PASS | по пункту; слот не откатывать без решения владельца |

**Факты на пробной загрузке (шаг 4, только чтение):**
```bash
ssh sheregesh-ui@$PI2 'cut -d" " -f1 /proc/uptime; cat /usr/share/sheregesh/build.json; \
  findmnt -no SOURCE,OPTIONS /persistent /home /etc/ssh /var; systemctl --failed --no-legend; \
  journalctl -b --no-pager | grep -cE "ordering cycle|deleted to break"; \
  systemctl show -p ActiveState,NRestarts sheregesh-remote-net ssh sheregesh-ota.socket; \
  systemctl is-active sheregesh-lab-vivid sheregesh-var-seed; cat /run/sheregesh-remote/link-status.json; \
  sheregesh-health --json --role remote --link-config ~/.config/sheregesh/link.json'
```

GO, если выполнено всё:
- uptime ≥ 180; `ota_version` = V, commit bd74c8c;
- монтирование: `/persistent` rw (p6), `/home` и `/etc/ssh` с persistent, `/var` = `slots/system_b/var`;
- failed пусто; **циклов упорядочения 0**; `var-seed` active (дополнил `/var`);
- `sheregesh-lab-vivid` **inactive** (не включён);
- `remote-net` и `ssh` active, NRestarts 0;
- `connected`, `tls`; `video`: port 7444, `tls1.3-mutual`, active, `frames` > 0, `sender_fps_5s` > 0;
- health PASS: `config.link`, `link.state`, `certs.device`, `certs.ca`, `certs.key-mode` — PASS, не SKIP;
- адрес .171.

**Восстановление.**
- **До confirm (шаги 3–5)**:
  1. Любой отказ → `ota-abort` (если есть SSH) или **цикл питания pi2 человеком** → прошивка загрузит A
     (`[all] boot_partition=2`).
  2. Затем `ota-status` (ожидается `ROLLED_BACK`, consistent), `$G compare pi2-before pi2-after-rollback --mode rollback --version $V [--trial pi2-trial.json]`, health.
  3. Если состояние не согласовано со слотами (`consistent: false`) — `tools/image/repair_ota_state.py` по
     [`../ota-closure/hardware/REPAIR.md`](../ota-closure/hardware/REPAIR.md). Тоже только по решению владельца.
  4. Журнал B: если `/persistent` монтировался — `journalctl --list-boots` / `-b <id>`; если нет — журнал утерян, нужен UART/HDMI.
- **После confirm** — шаг 7, ручной возврат через `autoboot.txt`.

**Что фиксировать** (каталог `$D`, коммит после каждого шага):
- `ota-status-*.json`, `install.log`, `ota-enable.txt`, `tryboot*.txt`, `confirm.json`;
- снимки `pi{1,2}-*.json` и сравнения `*-compare-*.txt`;
- каталоги `link-before`, `video-*`; `trial-facts.txt`, `acceptance.txt`, `tools-revision.txt`;
- время каждого шага (UTC), адрес платы после каждой загрузки;
- при STOP — `STOP-N.md` с разделами «установлено / исключено / не установлено».

## Найденные проблемы и замечания

1. **devbox после перезагрузки и реорганизации.** `/srv/build` пуст и принадлежит root (нет прав `nick`, `sudo -n`
   нет), Docker root теперь `/var/lib/docker`.
   - `ota-check/run.py` запущен с `--remote /home/nick/shimg/s5ota-l3-*`.
   - Очистка в `run.py` в этом случае не срабатывает: запасной путь удаляет только `/srv/build/<имя>`. Каталоги с
     файлами root удалены вручную через `docker run -v ~/shimg:/b … rm -rf`.
   - Код инструмента не менялся. Предложение: запасной путь очистки — `-v $(dirname remote)`.
2. **Лабораторные юниты не входят в область order_check**: они статические. Цикл проверен отдельно (§1), циклов нет.
3. **Источник видео pi2 сейчас rtsp (shcam .193)**, но выбран во время работы и не сохранён (на A нет S5-D1);
   в `video-input.json` записано `synthetic`.
   - На B после загрузки источник будет `synthetic`. Это достаточно для критерия `video` (frames > 0).
   - Выбор shcam после COMMITTED повторить: с S5-D1 он сохранится.
4. **Confirm роли remote на плате впервые.** Требует живого видеосеанса с pi1 в момент confirm.
   - pi1 (B, b1d95a2) должен быть `connected` и принимать видео; иначе confirm FAILED, а это потеря попытки.
   - Отсюда факты шага 4 и STOP при неготовности pi1.
5. **На B запускается заводской код образа bd74c8c, а не DEV-выпуск g980c45** слота A: `/var` у каждого слота свой.
   Различие в `software/` между 980c45 и bd74c8c — только `videoinput.py` (+4 строки). DEV-выпуск остаётся на A для
   отката.
6. **`build-report.json` дописан после самопроизвольной перезагрузки devbox** (отклонение в GATES образа).
   Влияния на пакет нет:
   - `ota-build` берёт `commit`, `source_date_epoch`, `config`, `image_sha256`;
   - сверены SHA образа, `verify.json` и .deb.

Вспомогательные сценарии этой приёмки — каталог `tools/`; все работают только чтением файлов или образа:
- `deb_vs_repo.py`, `image_inspect.sh`, `inspect_vs_repo.py` — содержимое образа;
- `state_compat.py` — путь состояния OTA;
- `manifest_check.py`, `l3_summary.py` — манифест и итог L3.
