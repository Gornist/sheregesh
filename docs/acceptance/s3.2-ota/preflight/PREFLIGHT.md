# S3.2-OTA — финальный preflight pi2 перед шагом 1 (только чтение)

Дата: 2026-10-24. Стенд: pi1 — пульт (`10000000568152de`, 192.168.1.92), pi2 — удалённый блок (`100000009d497e91`, 192.168.1.171).
Режим: **только чтение**. Записи разделов, `ota-install`, `ota-enable`, tryboot, confirm и перезагрузки **не выполнялись**:
boot_id обеих плат прежний (pi1 `13304bf3…`, pi2 `38bbfcd6…`), состояние OTA pi2 `IDLE`, разрешения переключения нет,
счётчики записанных секторов p1–p5 pi2 не менялись. Релизный гейт — **NO-GO** (все 82 Must — NOT TESTED).

Редакция 2 (по замечаниям владельца к `4b90bb0`): `slot_guard` переписан в строгом режиме — ERROR вместо PASS при недостатке доказательств (§7); добавлены сверка «диск = сумма разделов» и счётчики discard, режимы `unchanged` (pi1) и `trial` (обязательный независимый контроль шага 4, §6); проверено на платах только чтением и регрессионными тестами (24 теста, 53 случая) ([`slot-guard/`](slot-guard)). Поток и его SHA-256 не менялись — пересборка не нужна.

## 1. Вердикт

| Поток | Вердикт | Причина |
|---|---|---|
| `0.1.1792860877`, SHA-256 `71d6722f8f3413afe542362fb1c8d0b24047c76c325ed34f0697e58125d9f37c` (утверждён ранее, коммит `d1dc93d`) | **STOP** | Подпись принимается ключами pi2, установка и tryboot агентом образа плат `57e8fe4` проходят на образе диска (38/38, [`old-agent-check-71d6722f.json`](old-agent-check-71d6722f.json)). Но в потоке `confirm.py` до исправления `a7f1b74`: агент `57e8fe4` не снимает отпечатки `expect`, а старые критерии `same.*` при пустом `expect` проходят, только если файла нет и сейчас. У pi2 есть роль, machine-id, ключи и сертификат, поэтому слот B **никогда не подтвердится**. Шаг 5 недостижим, испытание закончилось бы только откатом. |
| `0.1.1792865618`, SHA-256 `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb` (образ `a7f1b74`) | **READY FOR STEP 1** (повторно, с проверенным независимым контролем §5–§7) — после утверждения владельцем **нового SHA-256** и разрешения шага 1 | См. §4. Решение 1 из `REPORT.md` §9 относилось к `71d6722f…`; для записи нужен новый SHA. |

## 2. Платы (снимки [`pi1-before.txt`](pi1-before.txt) / [`pi2-before.txt`](pi2-before.txt), повтор [`pi1-recheck.txt`](pi1-recheck.txt) / [`pi2-recheck.txt`](pi2-recheck.txt) — без изменений)

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Доступность, серийный номер | SSH sheregesh-ui, `10000000568152de` = `shdeploy.BOARDS` | SSH sheregesh-ui, `100000009d497e91` = `shdeploy.BOARDS` |
| Образ / пакет | `57e8fe4`, `0.1.0~dev0+g57e8fe4`, OTA-версия `0.1.1792843040` | то же |
| Слот A — 4 признака | GPT PARTNAME p2=`boot_a`, p4=`system_a`; `/` = `by-slot/active/system` (ro); device-tree `partition=2`, `tryboot=0`; `by-slot/active/{boot,system}` = p2/p4, `other` = p3/p5 | то же; `/boot/firmware` = p2 (ro), `/bootfs` = p1 |
| Другой слот | не смонтирован | не смонтирован |
| `autoboot.txt` | `[all] tryboot_a_b=1 boot_partition=2`, `[tryboot] boot_partition=3`; SHA-256 `f336517a…ed9f2a` | то же, тот же SHA |
| OTA | `IDLE`, active A, `consistent: true`, разрешения переключения нет | то же |
| DEV Deploy | выпуск `g37adc72c2bb1`, `pending: null` | выпуск `g37adc72c2bb1`, `pending: null`, последняя установка OK |
| Роль и службы | `console.role`; `sheregesh-console` active, перезапусков 0 | `remote.role`; `sheregesh-remote-net` active, перезапусков 0 |
| systemd | failed-юнитов нет; `sheregesh-ota.socket` listening | то же |
| health | PASS 15/15 | PASS 14/14 |
| Инструменты установки старого агента | rsync, mount, umount, tar, gzip | то же |
| persistent свободно | 805M | 815M (`/var` нового слота ~31M) |
| Сертификат устройства | до 2027-10-24, `urn:sheregesh:console:10000000568152de` | до 2027-10-24, `urn:sheregesh:remote:100000009d497e91` |

Отпечатки — SHA-256 файлов, без их содержимого. Закрытые ключи не читались в вывод и не покидали плату. В протоколах нет блоков `PRIVATE KEY`.
Отпечатки pi2: `authorized_keys` `3d3dac47…`, `link.json` `0d0ab5fa…`, `remote.role` `c5415df5…`, `device.key` `ac954571…` (600), `device.crt` `f8a1f8ca…`, `ca.crt` `aebef109…`, `ssh_host_ed25519_key.pub` `7865cb53…`, machine-id `cbc2dc60…`.

**Канал и видео (только чтение; [`link/`](link), [`video/`](video))**: `check_link` health, baseline — PASS 2/2 (2,14 сообщ./с, потерь событий 0, RTT 2,0/13,3 мс, TLSv1.3). `check_video_link` session, stream, telemetry, no-plain, leftovers — PASS 5/5: отправлено 28,6 к/с, принято и декодировано на pi1 28,4 к/с, 1280×720 H.264, разрывов последовательности и ошибок RTP 0, ~1,9 Мбит/с; UDP-видео нет, есть одно TCP 7444. Состояние видео pi2 содержит всё, что требует новый критерий подтверждения: `port 7444`, `tls1.3-mutual`, `active`, `frames 318179`, `sender_fps_5s 30.0`.

**pi1 без изменений**: снимок совпадает с исходным (boot_id, выпуск, отпечатки, autoboot, OTA `IDLE`). На шаге 1 pi1 не затрагивается.

## 3. Аудит рисков

### A. var-seed: достаточно ли рано и не перезаписывает ли

`sheregesh-var-seed.service`: `DefaultDependencies=no`, `After=local-fs.target` (к этому моменту смонтирован `/var` слота из `/persistent/slots/system_X/var` — генератор `slot-perst-generator`), `Before=sysinit.target systemd-tmpfiles-setup.service systemd-sysusers.service`, `ConditionPathExists=!/var/lib/dpkg/status`, `tar -xzf … -C /var --skip-old-files --numeric-owner`.
- Служба выполняется до tmpfiles, sysusers и всех служб `sysinit`/`basic`. Раньше неё в `/var` пишут только службы `local-fs`, например random-seed. Их файлы сохраняются: `--skip-old-files` существующие файлы не перезаписывает.
- Повторных запусков нет: после первого раза в `/var` есть `lib/dpkg/status`.
- Журнал смонтирован отдельно (`/persistent/log/journal`), с образцом он не пересекается.
- Конфигурация, перенесённая агентом в `/var` слота B, не перезаписывается. Это доказано на образе диска («служба var-seed: перенесённая конфигурация и существующие файлы не перезаписаны» — в обоих режимах) и тестом `fill_from_seed`.
- Если служба не сработает, юнит окажется failed. Тогда критерий `failed-units` не PASS, слот не подтверждается, остаётся основной A. Отказ безопасный.
- В образе `a7f1b74` служба включена: `verify.json` → `deploy.varseed PASS`.
- **Оценка: риск закрыт для шага 1.** Сама служба впервые выполнится на шаге 3 (tryboot).

### B. Что доказывает передачу кадров, а что — только наличие сервера

| Признак (pi2) | Что доказывает |
|---|---|
| `sheregesh-remote-net` active, порт 7444 слушается | только то, что сервер запущен |
| `transport tls1.3-mutual`, `sessions`, в журнале `video: session from urn:sheregesh:console:…` | установлен взаимно аутентифицированный сеанс с пультом, но **не** то, что кадры дошли |
| `frames` растёт, `sender_fps_5s > 0` (новый критерий `video` в `confirm.py`) | отправитель пишет кадры в TLS-сокет. Это доказывает отправку, но **не** приём и декодирование |
| pi1: `link.frames`, `frames_decoded`, `decoded_fps`, `seq_gaps`, `stream_sps` 1280×720 | **реальные приём и декодирование** — проверяется только на стороне пульта |

Поэтому `confirm` на pi2 сам по себе передачу кадров не доказывает. **Независимая процедура на pi1 после пробной загрузки pi2 (шаг 4), до решения о confirm:**
1. `python3 tools/probe/check_video_link.py --console 192.168.1.92 --remote 192.168.1.171 --out docs/acceptance/s3.2-ota/step4/video --only session,stream,no-plain,leftovers`. PASS обязателен для всех четырёх. В `stream.json`: `received_frames > 0`, `decoded_frames > 0`, `decoded_fps ≥ 0.9·received_fps`, `seq_gaps = bad_rtp = decoder_restarts = 0`, `stream_sps` 1280×720.
2. Номер сеанса на pi1 (`video.sessions`) вырос по сравнению с шагом 0: пульт переподключился к **новому** слоту, а не показывает старый буфер. В `session.json` `remote_log` — сеанс после времени загрузки pi2 (сверить с `uptime` pi2).
3. `check_link --only health,baseline` — телеметрия: потерь событий 0, `seq_gaps` 0.
4. Изображение на экране пульта этими проверками **не подтверждается** (GUI без дисплея не PASS). Это делает человек у экрана pi1, отдельной отметкой.

### C. Физическое восстановление

| Что | Состояние |
|---|---|
| Доступ к питанию pi2 | нужен человек у платы на шагах 3–5. На шаге 1 перезагрузок нет, но человек должен быть досягаем |
| Картридер | Mac (ранее карты pi1/pi2 определялись как `disk5`; перед любой записью — полный список дисков и GUID разделов) |
| Проверенный образ восстановления | `build/image/ci-57e8fe4-dev/sheregesh-lab-dev-pi4.img.zst` — `SHA256SUMS` OK, это образ, работающий сейчас на платах; `tools/image/flash_sd.py` |
| Уникальные данные pi2 | `device.key`/`device.crt` (личность mTLS), ключи хоста SSH, `authorized_keys`, `link.json`, роль, machine-id — **копии нет** и быть не должно (закрытые ключи плату не покидают) |
| Исходная microSD pi2 | старая система до миграции. **Резервной копией этих сертификатов она не является**; хранится как есть |
| Восстановление личности после перезаписи карты | новая регистрация: `tools/lab-pki/labpki.py` с CA на Mac (`~/.sheregesh-lab-ca/ca.crt`, `ca.key` есть), новые ключи хоста SSH (обновить known_hosts), назначение роли. Отпечатки pi2 сменятся. Это допустимо для лаборатории, но это **не** возврат прежних ключей |

Шаг 1 не затрагивает persistent-данные: на образе диска «общие данные … не изменились», на плате — независимая сверка отпечатков (§5). Аварийная перезапись карты на шаге 1 не нужна ни при каком исходе: основной A не пишется, а `FAILED`/`INSTALLED` на A — безопасное состояние.

### D. Первичная установка — старым агентом

Шаги 1 и 3 (install, enable, tryboot) выполняет агент **образа на платах** (`57e8fe4`). Агент из потока работает только начиная с шага 3, уже на слоте B (on-boot, confirm). Что из нового старому агенту недоступно и как это учтено:

| Новое в `a7f1b74` | У агента `57e8fe4` | Учтено |
|---|---|---|
| `expect` — отпечатки при установке | нет | `confirm.py` нового слота при пустом `expect` проверяет **наличие** (`present.*`); сравнение с «до» — **обязательный независимый контроль** `slot_guard compare --mode trial` на шаге 4 (§6) |
| `fill_from_seed` при установке | нет: `/var` слота B — копия `/var` A (rsync) с переносом конфигурации, без `dpkg` нового образа | `sheregesh-var-seed` при первой загрузке B дополняет из образца без перезаписи |
| повторный перенос конфигурации перед tryboot | нет (r7) | **правило процесса: между шагами 1 и 3 конфигурацию pi2 не менять** (роль, `link.json`, DEV Deploy) |
| `ota-install --sha256` (shdeploy) | на стороне Mac, от агента не зависит | применяется |
| состояние без поля `expect` | пишет | новый `model.py` читает его (`expect` по умолчанию `{}`; тест `test_confirm_after_old_agent_install_checks_presence_not_comparison`) |

Совместимость проверена на образе диска: установка и tryboot кодом агента из `git archive 57e8fe4` (`run.py --agent-rev 57e8fe4`). Поток `0.1.1792865618` — **38/38 PASS** ([`old-agent-check-a7f1b74.json`](old-agent-check-a7f1b74.json)). Подтверждение слота, установленного старым агентом, — модульные тесты `test_ota_s32.py` (наличие при пустом `expect` → подтверждается; отсутствующий ключ → не подтверждается).

## 4. Поток для шага 1

- Образ: CI run `40119731641`, коммит `a7f1b744b830446904e23b3409655705b74c8923`, `SOURCE_DATE_EPOCH 1792865618`, SHA-256 образа `497a6e0c…1280`; `verify.json` 71/71 PASS; `SHA256SUMS` OK.
- Поток `sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792865618.shota`: **SHA-256 `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb`**. Две независимые сборки дали одинаковый SHA (воспроизводимо). Манифест: [`ota-payload-manifest-a7f1b74.json`](ota-payload-manifest-a7f1b74.json).
- Подпись: ключ `sheregesh-deploy` (`SHA256:5wJfw7Qw…lOZUw`) — в `allowed_signers` pi2, namespace `sheregesh-ota`. `compatible` `sheregesh-lab-pi4-dev-1` = pi2 (DEV-MODE). Версия `0.1.1792865618` > активной `0.1.1792843040`.
- Образ диска, агент репозитория: **38/38 PASS** ([`file-check-a7f1b74.json`](file-check-a7f1b74.json)): установка, подпись, обрыв, чужая подпись, A и autoboot не тронуты, общие данные, `/var` из образца, var-seed без перезаписи, tryboot, повторная установка, abort.
- Образ диска, агент `57e8fe4` (как на плате): **38/38 PASS** ([`old-agent-check-a7f1b74.json`](old-agent-check-a7f1b74.json)) — `/var` B без образца (дополнит var-seed), `expect` нет, без повторного переноса перед tryboot.

## 5. Процедура шага 1 — запись только неактивного слота B pi2 (**не выполнялась**; только по разрешению владельца)

Предусловия (если не выполнено — STOP): владелец утвердил SHA-256 `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb`; pi1 и pi2 в состоянии §2; человек досягаем для pi2; pi1 не трогается; конфигурация pi2 до шага 3 не меняется.

```bash
cd ~/Developer/sheregesh
P=build/ota/a7f1/sheregesh-ota_sheregesh-lab-dev-pi4_0.1.1792865618.shota
SHA=f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb
V=0.1.1792865618
G=tools/deploy/ota-check/slot_guard.py
D=docs/acceptance/s3.2-ota/step1
mkdir -p $D
# 0. Снимок «до» и исходные проверки (только чтение); код ≠ 0 у любой команды — STOP
shasum -a 256 $P                                                       # = $SHA, иначе STOP
python3 tools/deploy/shdeploy.py ota-status pi2 > $D/ota-status-before.json   # IDLE, active A, consistent
python3 $G snap pi2 192.168.1.171 $D/pi2-before.json                   # OK, активный слот A; ERROR (код 2) — STOP
python3 $G snap pi1 192.168.1.92  $D/pi1-before.json
git add $D/pi2-before.json $D/pi1-before.json && git commit -m "S3.2-OTA шаг 1: снимки «до»"   # исходные отпечатки — в протоколе до записи
python3 tools/probe/check_video_link.py --console 192.168.1.92 --remote 192.168.1.171 --out $D/video-before --only session,stream
# 1. Запись неактивного слота B (единственное изменяющее действие)
python3 tools/deploy/shdeploy.py ota-install pi2 --payload $P --sha256 $SHA | tee $D/install.log
# 2. Независимая сверка «после» (только чтение)
python3 $G snap pi2 192.168.1.171 $D/pi2-after.json
python3 $G compare $D/pi2-before.json $D/pi2-after.json --mode install --version $V | tee $D/pi2-compare.txt     # PASS (код 0)
python3 $G snap pi1 192.168.1.92  $D/pi1-after.json
python3 $G compare $D/pi1-before.json $D/pi1-after.json --mode unchanged --version $V | tee $D/pi1-compare.txt  # PASS (код 0)
python3 tools/probe/check_link.py --console 192.168.1.92 --remote 192.168.1.171 --out $D/link-after --only health,baseline
python3 tools/probe/check_video_link.py --console 192.168.1.92 --remote 192.168.1.171 --out $D/video-after --only session,stream,leftovers
python3 tools/deploy/shdeploy.py ota-status pi2 > $D/ota-status-after.json
```

Пояснения к командам:
- Шаги 0 и 2 только читают.
- `ota-install` до передачи сверяет SHA-256 файла с `--sha256` и при несовпадении отказывает.
- Агент на плате проверяет подпись и совместимость, пишет только `by-slot/other` (p3, p5), перечитывает записанное и сверяет с манифестом. Затем копирует `/var` A в `/persistent/slots/system_b/var`.
- tryboot, enable и confirm не вызываются. Перезагрузки нет.
- Снимок `pi2-before.json` — исходный протокол отпечатков и для шага 1, и для шага 4 (§6). Он фиксируется в Git **до** записи.

| Контрольная точка | GO | STOP |
|---|---|---|
| `install.log` | последняя строка `result OK`, состояние `INSTALLED`, target B, версия `0.1.1792865618` | `REJECTED`/`FAILED` → разбор; **без** tryboot и без повторов |
| `slot_guard compare --mode install` pi2 — **независимо от агента** | вердикт **PASS**, а именно: `untouched.p1.bootconfig`, `untouched.p2.boot_a`, `untouched.p4.system_a` — записано и отброшено 0 секторов; `written.p3.boot_b ≥ 268435456`, `written.p5.system_b ≥ 2684354560` байт; снимки годны (счётчик диска = сумме разделов, активный слот A по пяти признакам, серийный номер pi2); `same.boot_id`; `same.slot.*`, `same.dt.tryboot`, `same.mnt.*`, `mnt.root_ro`; `same.autoboot.sha256` и `bootconfig.autoboot` (разбор файла: `[all]=2`, `[tryboot]=3`); `same.tree.*`; `same.fp.*` (роль, ключи, сертификат, `link.json`, ключи хоста, machine-id); `same.unit.*` (InvocationID, NRestarts); `same.deploy.status`; `ota.enable_absent`; `other_slot_not_mounted`; `ota.state` `INSTALLED`; health PASS | **FAIL** (код 1) или **ERROR** (код 2) |
| `slot_guard compare --mode unchanged` pi1 | PASS: на p1–p5 pi1 ни одного сектора, boot_id, отпечатки, службы, OTA прежние | FAIL или ERROR |
| Канал и видео после записи | `check_link` health, baseline PASS; `check_video_link` session, stream, leftovers PASS (тот же сеанс, кадры декодируются на pi1) | FAIL → разбор (нагрузка записи на microSD допустима только во время записи) |

При STOP:
- Ничего не делать с платой, без отдельного решения. Состояние `INSTALLED`/`FAILED` при активном и основном A безопасно: autoboot `[all]=2`, разрешения переключения нет.
- `ota-abort` старого агента на основном слоте только сбрасывает состояние (без перезагрузки). Его вызывают только по решению.
- Перезапись карты не нужна, A не изменялся (§3 C).

## 6. Обязательный независимый контроль шага 4 (после пробной загрузки, до решения о confirm)

Первую установку выполняет агент `57e8fe4`, поэтому поля `expect` нет. `confirm.py` нового слота при этом проверяет только **наличие** роли, ключей и сертификата, а не совпадение с прежними. **PASS `confirm.py` недостаточен для решения владельца.** Обязательный внешний критерий шага 4:

```bash
D4=docs/acceptance/s3.2-ota/step4
python3 $G snap pi2 192.168.1.171 $D4/pi2-trial.json
python3 $G compare docs/acceptance/s3.2-ota/step1/pi2-before.json $D4/pi2-trial.json --mode trial --version $V | tee $D4/pi2-trial-compare.txt
```

GO только при вердикте **PASS** (FAIL или ERROR → `ota-abort`, обычная перезагрузка → A):
- `same.fp.*` против исходного снимка шага 0: файл роли `remote.role`, `link.json`, `device.key`, `device.crt`, `ca.crt`, `authorized_keys`, ключи хоста SSH (`.pub`), machine-id. Закрытый ключ хоста дополнительно подтверждает само SSH-соединение: `StrictHostKeyChecking=yes`, сервер доказывает владение прежним ключом.
- `same.serial`, `same.autoboot.sha256`, `bootconfig.autoboot` (`[all]=2` — основной по-прежнему A).
- `slot.trial_B`: корень, boot, by-slot и device-tree согласованно на B; `dt.tryboot = 1`; `rebooted`.
- `build.version = 0.1.1792865618`; `ota.state` `TRYBOOT`/`HEALTH_PENDING` → B.
- `untouched_since_boot.p2/p4`: прежний слот A с начала этой загрузки не писался.
- `unit.ssh`, `unit.sheregesh-remote-net` active без перезапусков; `failed` none; health PASS; `ota.enable_present`; A не смонтирован.

Вместе с этим обязательны независимая проверка приёма видео на pi1 (§3 B) и `check_link`. Решение о confirm владелец принимает по трём независимым источникам: `slot_guard trial`, проверки pi1 и `confirm.py` (`checks` в ответе агента).

## 7. Независимость контроля и пределы доказательной силы

**Нет ложного PASS.** Снимок выполняется под `set -euo pipefail`. Каждое поле — одна команда или конвейер; при коде ≠ 0 или пустом выводе поле записывается как `!ERR`. Python требует полного набора обязательных полей (53), отвергает пустые значения, значения не в формате SHA-256 и SHA-256 пустого потока (`e3b0c442…`). Неполный снимок (нет маркера `!END`), сбой SSH или bash, тайм-аут, чужой серийный номер, неожиданная разметка, неоднозначный активный слот, отсутствующий файл роли или идентичности — всё это **ERROR** (код 2). Сравнение с отсутствующим полем невозможно: такие снимки отвергаются до сравнения. Проверено тестами ([`slot-guard/regression-tests.txt`](slot-guard/regression-tests.txt): 24 теста, 53 случая — PASS; запуск unittest, как в CI):

| Случай | Результат |
|---|---|
| нормальная установка | PASS |
| частичный снимок (нет поля, прерван сбор) | ERROR |
| ошибка отдельной команды (`!ERR`), пустое значение, SHA пустого потока | ERROR |
| отсутствующий файл, недоступный каталог, нечитаемый файл (настоящий bash) | ERROR |
| неудачный SSH (код 255), усечённый вывод, сбой bash после конца | ERROR |
| чужой серийный номер, снимки разных плат | ERROR |
| неоднозначный активный слот, диск ≠ сумме разделов | ERROR |
| запись или discard на p2/p4, смена активного слота | FAIL |
| запись на bootconfig, изменение `autoboot.txt` | FAIL |
| изменение идентичности (ключ, сертификат, роль, `link.json`, machine-id, ключ хоста, `authorized_keys`) | FAIL |
| неожиданный перезапуск службы, перезагрузка | FAIL |
| незавершённая запись (system_b недописан) | FAIL |
| шаг 4: подмена любого отпечатка, B без tryboot, запись на A, перезапуски, другая версия | FAIL |

На платах (только чтение): pi2 — два годных снимка, `install`-сравнение без записи дало PASS 41, **FAIL 3** (записи не было), ERROR 0; pi1 `unchanged` — PASS 44; чужая плата и недоступный адрес — ERROR. Первый прогон дал ERROR из-за ошибки в самом снимке (пустое поле `failed`) — строгий режим сработал на настоящей плате.

**Откуда берутся доказательства неизменности A и bootconfig:**

| Утверждение | Источник | От агента OTA |
|---|---|---|
| p1, p2, p4 не писались | ядро: `/sys/block/mmcblk0/mmcblk0pN/stat`, поля 7 (записано секторов) и 14 (отброшено), при том же `boot_id` | независимо |
| запись в обход разделов (`/dev/mmcblk0`) не скрыта | ядро учитывает ввод-вывод раздела и в разделе, и в целом диске; проверяется `диск = Σ разделов` (на pi2 сейчас 263889 = 263889) | независимо |
| активный слот | 5 признаков: раздел корня по MAJ:MIN (`/sys/dev/block`), раздел `/boot/firmware`, ссылки `by-slot`, `partition` из device-tree прошивки, PARTNAME GPT | независимо (by-slot создаёт udev-правило образа, не агент) |
| `autoboot.txt` | SHA-256 файла на `/bootfs` и разбор секций самим `slot_guard` | независимо |
| A смонтирован только для чтения, не перезагружался | `findmnt`, `boot_id` | независимо |
| состояние `INSTALLED` / версия | `sheregesh-ota-client status` | **от агента** — только как подтверждение, не доказательство неизменности A |

**Пределы:**
- Счётчики живут с момента загрузки. Они доказывают отсутствие записи за время между снимками при том же `boot_id`, но ничего не говорят о том, что было до загрузки, и обнуляются при перезагрузке. В `trial` они доказывают только «с начала этой загрузки».
- Учитывается только ввод-вывод через блочный слой ядра Linux. Запись прошивкой или загрузчиком до старта ядра, внутренние операции контроллера microSD (перенос блоков, сбой носителя) и запись в обход блочного слоя не видны.
- Счётчик — число секторов, а не их адреса. «≥ размера образа» для p3/p5 не доказывает, что записано **правильное** содержимое. Это доказывает агент повторным чтением и сверкой SHA-256 с манифестом (зависимое доказательство). Независимо прочитать сырые разделы без root нельзя.
- Сверка «диск = Σ разделов» опирается на учёт ядра 6.18 (ввод-вывод раздела добавляется к диску). Срез согласован (диск до и после чтения разделов одинаков), но не атомарен.
- `tree.boot` — хеши всех файлов FAT boot_a, но не метаданных FAT и свободного места. `tree.system` — хеши только `/usr/lib/sheregesh`, `/usr/share/sheregesh`, `/usr/bin`, `/etc/systemd`, а не всего раздела. Это дополнительные, частичные доказательства. Основное — нулевые счётчики при смонтированном только для чтения A.
- Закрытые ключи хоста SSH без root не читаются. Их неизменность подтверждает успешное соединение со строгой проверкой ключа хоста.
- `sha256` от `device.key` — отпечаток файла, сам ключ в вывод не попадает. Перед записью снимки проверены на отсутствие блоков `PRIVATE KEY`.

**После шага 1 не делать** (каждое — отдельное решение владельца): `ota-enable`, `ota-tryboot`, `ota-confirm`, изменения `autoboot.txt`, включение watchdog, любые действия с pi1. До шага 3 не менять конфигурацию pi2 (риск D). Протокол шага 1 — в `docs/acceptance/s3.2-ota/step1/`.

## 8. Влияние S3.3 (слияние `a0ba631`, 2026-10-24) на preflight шага 1

После слияния S3.3 обе платы штатным DEV Deploy обновлены до выпуска `0.1.0~dev0+ga0ba6311bee4`; OTA pi2 был `IDLE`, запись слота не начиналась ([S3.3 REPORT §5](../../s3.3-video-input/REPORT.md)).

- **Снимки §2 устарели.** На pi1 и pi2 изменились `deploy.status` (выпуск `a0ba631` вместо `g37adc72c2bb1`) и InvocationID служб ролей. Идентичность, роли, сертификаты, слоты, `autoboot.txt`, система A не изменились; на p1–p5 ни одного сектора (`slot_guard compare --mode unchanged`, [S3.3 `hw/`](../../s3.3-video-input/hw)). Исходный протокол шага 1 — свежий снимок шага 0 по §5: процедура это уже требует.
- **Код приложения на слоте B** (факт шага 3): агент `57e8fe4` перенёс в `/var` слота B конфигурацию, но не `/var/lib/sheregesh-deploy` — на B работает код приложения образа `a7f1b74`, а не выпуск DEV `a0ba631`. Правило D остаётся в силе: между шагами 1 и 3 DEV Deploy и конфигурацию pi2 не менять.
- **Источник видео — только synthetic.** Если на pi1 выбрать `video_source: uvc` или `disabled`, критерий `video` в `confirm.py` пульта не пройдёт: он ждёт `remote-tls`. Если удалённый блок запустить с `--video-source uvc` без устройства, в состоянии нет поля `video` и критерий `video` блока тоже не пройдёт. Сейчас полей `video_*` нет на обеих платах, юниты не менялись. Перед шагом 1 проверить, что полей `video_*` в ревизиях `/var/lib/sheregesh/config` нет.
- **Критерии health, `confirm.py`, проверочные инструменты** S3.3 не меняет. `check_link version` исправлен (`--no-video` у временного сервера) — к шагам OTA не относится.
- **app-stop остаётся OPEN.** Ретранслятор запускается из образа, поэтому DEV Deploy исправление `02a9b64` не доставляет. На шаге 4 критерий `systemctl stop sheregesh-console` → `inactive` проверяет именно ретранслятор образа `a7f1b74` (в пакете исправление есть).
- **Пакет не меняется:** `0.1.1792865618`, SHA-256 `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb`. Вердикт READY FOR STEP 1 сохраняется при условии свежего снимка шага 0.
