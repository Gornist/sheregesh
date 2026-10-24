# S3.2-OTA — preflight pi1 (пульт) перед шагом 1, только чтение (2026-10-25)

pi1: `10000000568152de`, 192.168.1.92, роль console. Режим — **только чтение** по SSH `sheregesh-ui`, BatchMode, строгая проверка ключа хоста. Не выполнялись: ota-install/enable/tryboot/confirm/abort, перезагрузки, DEV Deploy, systemctl stop/restart, записи. boot_id до и после — `13304bf3…` (сверка `unchanged` — §7). **pi2 не затрагивался совсем**, в том числе проверочными инструментами (§2). Релизный гейт — NO-GO.

## 1. Вердикт: **STOP** для пакета `a7f1b74`. READY FOR STEP 1 (pi1) — только с пакетом из образа **≥ d1258f3**

| Причина | Суть | Где исправлено |
|---|---|---|
| D1 confirm: health от root без `--link-config` | на B критерии `health`, `health.network` FAIL у здоровой платы, слот не подтверждается (pi2, шаг 5) | v2 `6353c70` |
| D2 `state.json`: поле `expect` | новый агент на B пишет `"expect": {}` в общий `state.json`. После возврата на A агент `57e8fe4` падает (`UpdateState(**json)` → TypeError, воспроизведено кодом `57e8fe4`): failed `sheregesh-ota-boot`, health FAIL, `ota-status/abort/install` не работают. Починить по сети нельзя (`state.json` root 644, sudo у `sheregesh-ui` нет). **Откат (§6) превращается в аварию агента** | v2 `d1258f3` |
| Нет пакета | образ ≥ d1258f3 не собран: исчерпана квота артефактов CI | — |

В пакете `a7f1b74` (`0.1.1792865618`, SHA `f6e19234…6edb`) **оба дефекта не исправлены**. Шаг 1 им не выполнять: записать слот можно, но ни подтверждение, ни чистый откат недостижимы. Остальные условия для pi1 выполнены (§2–§5). Когда будет пакет ≥ d1258f3, перед шагом 1 повторить §3 (подпись, SHA) и §4 (образ диска) на нём. После утверждения нового SHA владельцем вердикт — READY FOR STEP 1.

## 2. Состояние pi1 (снимки [`pi1-snap-1.json`](pi1-snap-1.json), [`pi1-snap-2.json`](pi1-snap-2.json), [`ota-status.json`](ota-status.json), [`pi1-facts.txt`](pi1-facts.txt), [`pi1-facts-2.txt`](pi1-facts-2.txt))

| Признак | Значение |
|---|---|
| slot_guard snap | годен: 53 поля, ошибок нет, активный A по пяти признакам (корень `system_a` ro, `/boot/firmware` p2 ro, by-slot active p2/p4, other p3/p5, device-tree `partition=2`, `tryboot=0`); диск = Σ разделов (755057) |
| Образ / пакет | `57e8fe4`, `0.1.0~dev0+g57e8fe4`, OTA `0.1.1792843040`; агент без `confirm.py` |
| OTA | `IDLE`, active A, target нет, `consistent: true`, `tryboot_now: false`, переключение не разрешено (нет `enable.json`), compatible `sheregesh-lab-pi4-dev-1` |
| autoboot.txt | `[all] tryboot_a_b=1 boot_partition=2`, `[tryboot] boot_partition=3`, SHA `f336517a…ed9f2a` (как у pi2) |
| Роль, службы | `console.role` (`remote.role` нет); `sheregesh-console` active/running, NRestarts 0; `ssh`; `sheregesh-ota.socket` listening; failed-юнитов нет; сеанс cage — logind session 492, seat0, tty1 |
| health `--role console --link-config …/link.json` | PASS 15/15: config.link, config.console, link.state connected TLSv1.3, link.versions, certs.* (device до 2027-10-24, `urn:sheregesh:console:10000000568152de`; ca до 2028-10-23; key 0600) |
| Канал и видео (файлы pi1) | `link-status.json`: connected, TLSv1.3, `urn:sheregesh:remote:100000009d497e91`, seq_gaps 0. `ui-state.json` video: `remote-tls`, `playing`, декодировано 30 к/с, 1280×720 H.264, потерь и плохих RTP 0 |
| Источник видео | полей `video_*` нет: ревизия `r000001` в `/var/lib/sheregesh/config`, `link.json` — synthetic по умолчанию |
| DEV Deploy (A) | выпуск `0.1.0~dev0+ga0ba6311bee4`, pending нет |
| persistent | свободно 834850816 Б (≈ 796 МБ, 82 %); `/var` A ≈ 74 МБ; `slots/system_b/var` ≈ 31 МБ (из образа) |
| Инструменты старого агента | rsync, mount, umount, tar, gzip, ssh-keygen, python `lzma` есть (`xz` не нужен) |
| Отпечатки (SHA-256 файлов) | `authorized_keys` `3d3dac47…`, `link.json` `1ec96e87…`, `console.role` `68f83dfa…`, `device.key` `c64c7581…` (600), `device.crt` `f054b25d…`, `ca.crt` `aebef109…`, `ssh_host_ed25519_key.pub` `c3a93f6f…`, machine-id (sha) `1575b530…`. В протоколах нет блоков `PRIVATE KEY` |
| `allowed_signers` | один ключ `sheregesh-deploy`, ED25519 `SHA256:5wJfw7Qw…lOZUw` = `~/.ssh/sheregesh-deploy.pub`; namespaces `sheregesh-deploy,sheregesh-ota,sheregesh-ota-enable`; файл SHA `74c95610…00c0` |

`check_link` и `check_video_link` **не запускались**: все их сценарии, включая health, baseline, session и stream, подключаются по SSH к pi2 (`--remote`), а pi2 трогать запрещено (идёт OTA-цикл). Канал и видео подтверждены только файлами состояния pi1, без независимых счётчиков. Полный неразрушающий набор — обязательная часть шага 0 (§5), когда pi2 свободна.

## 3. Пакет ([`payload-check-a7f1b74.json`](payload-check-a7f1b74.json), инструмент [`tools/verify_payload.py`](tools/verify_payload.py))

Проверка кодом агента репозитория (`ota.Payload`) с `allowed_signers` pi1 (только открытый ключ): подпись `Good "sheregesh-ota" signature for sheregesh-deploy`, compatible = pi1, разметка `image-rota-gpt-ab-1`, версия `0.1.1792865618` > активной `0.1.1792843040`. SHA-256 сжатых образов boot/system совпадают с манифестом, лишних байт нет, SHA файла `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb`. Механически **PASS**, по содержанию — **не годен** (D1, D2).

## 4. Образ диска (devbox-lan, `--remote /srv/build/pi1-preflight-check`, база `ci-57e8fe4-dev`)

Пакет `a7f1b74`, оба агента — **PASS 38, FAIL 1** ([`file-check-old-agent.log`](file-check-old-agent.log)/[`.json`](file-check-old-agent.json) с `--agent-rev 57e8fe4`, [`file-check-repo-agent.log`](file-check-repo-agent.log)/[`.json`](file-check-repo-agent.json) с агентом репозитория `338c0a7`). PASS: подпись, чужая подпись и обрыв, запись B (SHA = манифест, установка 27 с), A, autoboot и общие данные не тронуты, конфигурация пульта перенесена, `/var` A не изменён, var-seed дополняет без перезаписи, tryboot `[all]=2/[tryboot]=3`, abort → A. Единственный FAIL — ожидаемый: «confirm — health с профилем пользователя стенда» (в слоте вызов без `--link-config`, D1). D2 этим испытанием не покрыт (агент нового слота не загружается); его закрывает модульный тест v2 `tests/unit/test_update_state_compat.py`. Повторить оба прогона на пакете ≥ d1258f3: ожидается 39/39.

## 5. Миграция `/var` для роли console (агент `57e8fe4` выполняет шаги 1 и 3)

- **Шаг 1:** `/persistent/slots/system_b/var` = `rsync --delete` из `/var` записанного system (≈ 20 КБ) + `/var/lib/sheregesh` с A (`r000001`, владелец uid 1000 сохраняется). **Не переносится** `/var/lib/sheregesh-deploy`: на B пульт запустится из кода образа (≥ 6353c70 содержит a0ba631 и исправление app-stop `02a9b64`), не из выпуска DEV `a0ba631`. После confirm DEV Deploy на B начинается с заводского кода; откатиться на выпуск A на B нельзя.
- **Шаг 3:** старый агент не переносит конфигурацию повторно перед tryboot. ⇒ **между шагами 1 и 3 конфигурацию pi1 не менять**: ревизии, `link.json`, видеоисточник, DEV Deploy.
- **Первая загрузка B:** `sheregesh-var-seed` (`dpkg/status` нет) дополняет `/var` из образца без перезаписи. Журнал — отдельный bind `/persistent/log/journal` (тот же machine-id, общий журнал). Сеанс cage: `PAMName=login`, logind и seatd поднимаются из образца `/var`, tty1 — `Conflicts=getty@tty1`. `StateDirectory=sheregesh` уже существует.
- **Возврат на A:** `/var` A не трогается. Изменения конфигурации, сделанные на B, на A не попадают (REQ-CFG-006).
- **Риски для пульта:** экран гаснет на время tryboot (pi2: SSH через 23 с) и при откате. Пока критерии не PASS, канал управления отсутствует. Отказ var-seed → failed-юнит → не подтверждается, остаётся A. Свободного места хватает (нужно ≈ 60 МБ). На A сейчас app-stop (при останове console — `Failed with result exit-code`, status 143, см. журнал), поэтому **не останавливать службу пульта на шагах 3–4**.

## 6. Критерии подтверждения console и сухой расчёт ([`confirm-gates-dry.json`](confirm-gates-dry.json))

Код v2 (`confirm.gates`, роль console): `slot`, `version`, `persistent`, `present.*` (установка старым агентом: `expect` пуст), `unit.ssh`, `unit.sheregesh-ota.socket`, `unit.sheregesh-console` active и NRestarts 0, `failed-units`, `uptime ≥ 90 с`, `health` PASS, `health.network`, `mtls` (`connected` и `TLSv1.3`), `video` (`remote-tls`, `playing`, ≥ 25 к/с). `Probe.health("console")` при исполнении вызывает `sheregesh-health --json --role console --link-config /home/sheregesh-ui/.config/sheregesh/link.json` — исправление D1 действует и для console.

На pi1 модуля `update/confirm.py` нет (образ `57e8fe4`), поэтому на плате прогон **не запускался**. Вместо него: факты собраны только чтением ([`pi1-gate-facts.json`](pi1-gate-facts.json), [`tools/pi1_gate_facts.py`](tools/pi1_gate_facts.py)), а `confirm.gates` репозитория вычислен на Mac ([`tools/eval_gates.py`](tools/eval_gates.py)): **17 PASS, 0 FAIL, 2 N/A** (`slot` и `version` вне пробной загрузки). На шаге 4 `video` и `mtls` зависят от pi2: она должна работать и вещать, видеоисточник — только synthetic.

## 7. Независимый контроль возврата на A (новый режим `slot_guard compare --mode rollback`, 28 тестов PASS)

Сравниваются снимок шага 0 и снимок после возврата; `--trial` — снимок шага 4. **PASS** только при всех условиях:
- `slot.back_on_A` (пять признаков), `dt.tryboot = 0`;
- `rebooted` (boot_id ≠ шага 0) и `rebooted_after_trial` (≠ пробной загрузки, пробная была на B);
- прежние `autoboot.sha256`, `bootconfig.autoboot` [all]=2, все `same.fp.*` идентичности;
- прежние `package`, `build`, `tree.boot`, `tree.system` — содержимое A не менялось;
- прежний `deploy.status` — `/var` A и выпуск DEV на месте;
- `untouched_since_boot.p3/p5` (B не писался с загрузки);
- `ota.state` `ROLLED_BACK`/`IDLE` без target, `unit.ssh` и `unit.sheregesh-console` без перезапусков, failed нет, health PASS.

Разрешение (`enable.json`) abort не отзывает (INFO), отзыв — `ota-disable` по решению. С пакетом без D2 снимок даст ERROR (`ota.status` `!ERR`) или FAIL (`failed`). Прогон на настоящем снимке (снимок против себя) — единственный FAIL `rebooted`, как и ожидалось. Сверка «до/после» этого preflight — `unchanged` PASS 44/44 (p1–p5 ни сектора, boot_id, отпечатки, службы, OTA прежние) ([`pi1-unchanged-compare.txt`](pi1-unchanged-compare.txt)).

## 8. Процедура шагов 0–5 для pi1 (НЕ выполнялась; каждый шаг — отдельное решение владельца)

Предусловия: пакет из образа ≥ d1258f3, SHA утверждён; pi2 в устойчивом состоянии (не посреди OTA, вещает); человек у pi1 с доступом к питанию и экрану; конфигурацию pi1 не менять от шага 0 до 5.

```bash
P=build/ota/<rev≥d1258f3>/…shota; SHA=<утверждённый>; V=<версия>; G=tools/deploy/ota-check/slot_guard.py
D=docs/acceptance/s3.2-ota/pi1; C="--console 192.168.1.92 --remote 192.168.1.171"
# 0. только чтение; любой не 0 — STOP
shasum -a 256 $P; python3 $G snap pi1 192.168.1.92 $D/step1/pi1-before.json; python3 $G snap pi2 192.168.1.171 $D/step1/pi2-before.json
python3 tools/deploy/shdeploy.py ota-status pi1 > $D/step1/ota-status-before.json       # IDLE, A, consistent, без разрешения
git add $D/step1/*-before.json && git commit -m "…снимки «до»"                           # до записи
python3 tools/probe/check_link.py $C --out $D/step1/link-before --only health,baseline,ports,identity
python3 tools/probe/check_video_link.py $C --out $D/step1/video-before --only session,stream,telemetry,no-plain,leftovers
# 1. запись B
python3 tools/deploy/shdeploy.py ota-install pi1 --payload $P --sha256 $SHA | tee $D/step1/install.log      # result OK, INSTALLED → B
python3 $G snap pi1 192.168.1.92 $D/step1/pi1-after.json && python3 $G compare $D/step1/pi1-before.json $D/step1/pi1-after.json --mode install --version $V
python3 $G snap pi2 192.168.1.171 $D/step1/pi2-after.json && python3 $G compare $D/step1/pi2-before.json $D/step1/pi2-after.json --mode unchanged --version $V
# 2. разрешение   3. пробная загрузка (человек у pi1; нет SSH 5 мин → выключить/включить питание → A)
python3 tools/deploy/shdeploy.py ota-enable pi1; python3 tools/deploy/shdeploy.py ota-tryboot pi1
# 4. на B, не раньше 90 с после загрузки: независимая идентичность + канал/видео + ota-status (HEALTH_PENDING)
python3 $G snap pi1 192.168.1.92 $D/step4/pi1-trial.json && python3 $G compare $D/step1/pi1-before.json $D/step4/pi1-trial.json --mode trial --version $V
python3 tools/probe/check_link.py $C --out $D/step4/link --only health,baseline,identity,ports
python3 tools/probe/check_video_link.py $C --out $D/step4/video --only session,stream,telemetry,no-plain,leftovers   # + человек: картинка на экране
# 5. подтверждение (только при PASS всего шага 4, в той же загрузке)
python3 tools/deploy/shdeploy.py ota-confirm pi1          # result OK, COMMITTED; затем snap «после», DEV Deploy, закрытие app-stop
# Откат (любой FAIL/ERROR шага 4 или отказ confirm): ota-abort pi1 (на B — обычная перезагрузка) или питание; затем
python3 $G snap pi1 192.168.1.92 $D/rollback/pi1-after.json && python3 $G compare $D/step1/pi1-before.json $D/rollback/pi1-after.json --mode rollback --version $V --trial $D/step4/pi1-trial.json
```

GO и STOP — как в [`../preflight/PREFLIGHT.md`](../preflight/PREFLIGHT.md) §5–§6, с заменой ролей: в шаге 1 `install` pi1 + `unchanged` pi2; в шаге 4 `trial` pi1 — `unit.sheregesh-console`; `confirm` смотреть `checks` ответа (19 критериев console). На шаге 4 службу пульта не останавливать.
