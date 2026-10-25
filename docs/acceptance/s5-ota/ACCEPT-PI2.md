# S5-OTA — короткая функциональная приёмка pi2 после OTA на образ bd74c8c (слот B)

Ветка `v2`, 2026-10-25. Сценарий — [`tools/probe/s5_pi2_accept.py`](../../../tools/probe/s5_pi2_accept.py), тесты
суждений без плат — [`tests/unit/test_s5_pi2_accept.py`](../../../tests/unit/test_s5_pi2_accept.py)
(`.venv/bin/python -m pytest -q tests/unit/test_s5_pi2_accept.py`). Сценарий построен поверх готовых средств и их не
дублирует: `e2e_run.py` (личность платы, `Sampler`, health), `s4_accept.py` / `s5_accept.py` (`Stand`, ожидания,
`judge_fault`, `step_link`, `run_gui`, `restore_config`), `check_link.py`, `check_video_link.py`, `gui_capture.py`,
`slot_guard.py`.

Образ: commit `bd74c8c412028f8f4fb0a4d6b89b738e676db959`, `ota_version` `0.1.1792949168`
([`../s5-video/image-bd74c8c/GATES.md`](../s5-video/image-bd74c8c/GATES.md)). Состав, который проверяем: S5-D1
(блок сохраняет выбор видеоисточника) и п. 7 (лабораторные источники видео pi2,
[`tools/lab-video/pi2/README.md`](../../../tools/lab-video/pi2/README.md)).

Установку, `ota-tryboot`, `ota-confirm`, `ota-abort` и `ota-disable` выполняет главный агент по решению владельца.
Сценарий их не вызывает. Перезагрузку он делает только в фазе committed.

## 0. Стенд, права и что проверено чтением плат

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Адрес, серийный номер | 192.168.1.92, `10000000568152de` | 192.168.1.171, `100000009d497e91`, MAC `d8:3a:dd:69:0f:6e` |
| Сейчас (чтение 2026-10-25 ~18:40) | B, COMMITTED, образ b1d95a2, DEV-выпуск g980c45 | A, ROLLED_BACK, образ 57e8fe4, DEV-выпуск g980c45 |
| Источник shcam | — | `rtsp:1` «shcam Pi5 IMX477» (`rtsp://192.168.1.193:8554/cam`), сейчас активен |

Сейчас `video-input.json` pi2 указывает `synthetic`, а активен `rtsp:1`. Это и есть дефект S5-D1: выбор
пульта не попал в файл. После загрузки bd74c8c блок стартует с источника из файла, то есть с synthetic.

**polkit.** От `sheregesh-ui` каталог `/etc/polkit-1/rules.d` не читается (`Permission denied`), поэтому права сверены
по исходникам образа. Правило 50 между 57e8fe4 и bd74c8c не менялось (`git diff` пуст). Правило 51 в bd74c8c прошло
`verify_image labvideo.polkit` с результатом PASS. Тест `test_commands_within_image_polkit_rules` держит сценарий в этих рамках.

| Действие `sheregesh-ui` | Правило | Используется |
|---|---|---|
| `systemctl start/stop/restart/…` для `sheregesh-remote-net` (любой verb) | `os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules` | P2-06 (trial): `restart` |
| `systemctl reboot` (`login1.reboot`, `reboot-multiple-sessions`) | 50-… | P2-06 (committed) |
| `start/stop/restart` для `sheregesh-lab-usbip@{hdmi,cvbs,camera}`, `sheregesh-lab-vivid`, `sheregesh-lab-vivid-feed@{cvbs,hdmi}` | `51-sheregesh-lab-video.rules` (только эти шесть юнитов) | P2-05 |
| `enable` (`manage-unit-files`), `reset-failed`, другие экземпляры | нет → отказ | P2-05: `enable` **обязан** быть отклонён |
| `kill -HUP <MainPID блока>`, запись `~/.config/sheregesh/video-input.json` | процессы и дом своего пользователя | восстановление в `finally` |

Выбор источника идёт GUI-путём:
`python3 -m sheregesh.link.videoselect <id> --via console` на pi1. Запрос передаётся работающему пульту,
и тот вызывает `Backend.selectVideoSource(id, True)`, то есть то же, что кнопка экрана «Видео». На платах проверено
чтением: `--list --via console` с pi1 вернул `synthetic`, `rtsp:0` (available=false) и `rtsp:1` (active). На pi2
`sheregesh-lab-usbip@hdmi` сейчас `not-found` (юниты п. 7 появятся только с bd74c8c).

## 1. Порядок и время

| Шаг | Кто | Что | Время |
|---|---|---|---|
| A | главный агент | `ota-install` → `ota-enable` → `ota-tryboot`: pi2 загружается в B однократно | загрузка ~1–2 мин |
| B | главный агент | дождаться SSH по 192.168.1.171 (если адрес сменился — STOP) и uptime ≥ 90 с (гейт confirm) | ~1,5 мин |
| **C** | сценарий | **фаза trial: P2-01…P2-06 на TRYBOOT/HEALTH_PENDING**, без перезагрузки; в конце slot_guard «до confirm» | **6–8 мин** (+2 с `--gui`) |
| D | владелец / главный агент | решение по §3; `ota-confirm` | ~0,5 мин |
| **E** | сценарий | **фаза committed: P2-01, P2-06 (reboot), P2-07** | **~2,5 мин + загрузка ~1,5–2 мин** |
| F | главный агент | `ota-disable` (как на pi1, по решению владельца) | — |

```bash
cd ~/Developer/sheregesh
python3 tools/probe/s5_pi2_accept.py --plan                       # план фазы trial, без SSH
python3 tools/probe/s5_pi2_accept.py --plan --phase committed

# C — пробная загрузка B, до confirm
T=docs/acceptance/s5-ota/pi2-trial-$(date +%Y%m%d-%H%M)
python3 tools/probe/s5_pi2_accept.py --phase trial --out $T --gui       # без --gui на 2 мин быстрее (нет снимков GUI)

# D — только после решения (§3)
python3 tools/deploy/shdeploy.py ota-confirm pi2 --host 192.168.1.171
python3 tools/deploy/shdeploy.py ota-status pi2 --host 192.168.1.171    # COMMITTED, active B

# E — после confirm
C=docs/acceptance/s5-ota/pi2-committed-$(date +%Y%m%d-%H%M)
python3 tools/probe/s5_pi2_accept.py --phase committed --out $C --baseline $T
```

Результаты пишутся в `--out`. Там лежат `summary.json` (вердикты, доказательства, `durations_s`, `restore`), журнал `run.log`,
`video-input.orig.json`, `check_link/`, `check_video_link/`, `gui-select/` (PNG и `states.json`). Фаза trial
добавляет `baseline.json` (OTA и boot_id pi1, OTA pi2) и `pi2-before-confirm.json`, фаза committed — `pi2-after-commit.json`
и `pi2-compare-commit.txt`. Коды выхода: 0 PASS, 1 FAIL, 2 ERROR, 3 BLOCKED.

## 2. Проверки

Вердикты: PASS | FAIL | ERROR (испытание не доказательно, никогда не PASS) | BLOCKED (нет условия: оборудования,
юнита, ожидаемого слота) | SKIP (не в этой фазе). Итог: FAIL > ERROR > BLOCKED > PASS.

| ID | Фаза | Что и критерий | Ручной эквивалент (pi2 = `ssh sheregesh-ui@192.168.1.171`) |
|---|---|---|---|
| P2-01 | обе | Загружен слот B, `consistent`. Trial: `TRYBOOT`/`HEALTH_PENDING`, `target` B, `tryboot_now` true, `version_target` 0.1.1792949168. Committed: `COMMITTED`/B, autoboot `[all]`=3. `build.json`: commit bd74c8c, та же `ota_version`. Код заводской (`/var/lib/sheregesh-deploy/current` нет; иначе FAIL, обход — `--allow-dev-release`). failed 0, `sheregesh-health` PASS, uptime записывается. Если слот, состояние или образ не те, P2-03…06 получают BLOCKED и воздействий нет | `sheregesh-ota-client status`; `cat /usr/share/sheregesh/build.json`; `readlink /var/lib/sheregesh-deploy/current`; `systemctl --failed`; `sheregesh-health --json --role remote --link-config ~/.config/sheregesh/link.json`; `cut -d' ' -f1 /proc/uptime` |
| P2-02 | trial | SSH (`id -un` = sheregesh-ui); `check_link --only health,identity` PASS; пульт: `peerLink` CONNECTED, `linkTls` TLSv1.3; блок: видео `tls1.3-mutual`; `check_video_link --only session` PASS | `python3 tools/probe/check_link.py --console 192.168.1.92 --remote 192.168.1.171 --out D --only health,identity` |
| P2-03 | trial | Выбор `synthetic` GUI-путём → блок streaming synthetic ≤ 25 с, пульт live synthetic. За окно 3 с `frames_decoded` выросли на ≥ 20, `decoded_fps_5s` > 0. С `--gui` ещё снимок `gui_capture --by select`: вход synthetic, кадр за 1 с сменился | pi1: `C=$(readlink -f /var/lib/sheregesh-deploy/current 2>/dev/null \|\| echo /usr/lib/sheregesh); PYTHONPATH=$C/software python3 -m sheregesh.link.videoselect synthetic --via console`; `cat /run/sheregesh-ui/ui-state.json` |
| P2-04 | trial | То же для `rtsp:1` (shcam, H.264 1280×720@30). Если shcam нет в списке или он available=false — BLOCKED | `… videoselect rtsp:1 --via console` |
| P2-05 | trial | (а) `systemctl start sheregesh-lab-usbip@hdmi` без пароля, `v4l2:534d:2109` за ≤ 30 с появляется в списке как `hdmi`, выбор, live. (б) `stop` («выдернуть»): «нет видео» ≤ 3 с, блок `no-device`, пульт `display=no-device`. `start`: возврат ≤ 10 с. (в) `systemctl enable sheregesh-lab-usbip@hdmi.service` отклонён polkit (Access denied / Interactive authentication required). Отказ из-за «Read-only file system» означает, что polkit пропустил: FAIL. (г) `start sheregesh-lab-vivid sheregesh-lab-vivid-feed@cvbs`, вход CVBS vivid в списке, выбор, live; `lab-vivid cvbs nosignal`: блок `no-signal`, пульт «нет сигнала», `ok`: возврат. (д) Блок переводится на synthetic, только потом `stop` usbip, feed, vivid. После этого: failed 0, модуль vivid выгружен | `systemctl --no-ask-password start\|stop sheregesh-lab-usbip@hdmi`; `/usr/lib/sheregesh/lab-video/lab-vivid status\|cvbs nosignal\|cvbs ok`; `systemctl --no-ask-password enable sheregesh-lab-usbip@hdmi.service` |
| P2-06 | trial | S5-D1. Выбирается источник, отличный от активного (`rtsp:1`, если активен synthetic). `video-input.json` описывает его (`source`, `url` = `rtsp_urls[1]`), `rtsp_urls` целы. Затем `systemctl restart sheregesh-remote-net`: MainPID сменился, пульт получил **новый** видеосеанс (`video.sessions` вырос) с этим источником live ≤ 60 с, активен тот же id, файл прежний | `cat ~/.config/sheregesh/video-input.json`; `systemctl --no-ask-password restart sheregesh-remote-net` |
| P2-06 | committed | То же через `systemctl reboot`. Перезагрузка выполняется только при COMMITTED, слоте B и `[all]`=3. Ждём смену boot_id и возврат SSH ≤ 300 с, затем новый видеосеанс с тем же источником ≤ 180 с. Фиксируются `reboot.down_s/up_s`, `ready_s`, `ready_after_ssh_s`. После перезагрузки снова COMMITTED/B | `systemctl --no-ask-password reboot` |
| P2-07 | committed | Агент OTA: COMMITTED, B, `tryboot_now` false. slot_guard «после» сравнивается с `pi2-before-confirm.json` (mode `unchanged` плюс базовая линия самой платы). Допустимы **только** отличия фиксации: `autoboot.txt` (`[all]`=3) и запись в bootconfig p1, `ota.status`, отзыв разрешения YES→no. После перезагрузки ещё boot_id, `dt.tryboot`=0, InvocationID юнитов (active, NRestarts 0) и сброшенные счётчики ввода-вывода; взамен проверяется, что слот A с начала загрузки не писался. Любое иное отличие (tree.system/boot, build, отпечатки, deploy.status, health, failed) — FAIL. pi1: OTA B/COMMITTED, версия, согласованность и boot_id прежние | `python3 tools/deploy/ota-check/slot_guard.py snap pi2 192.168.1.171 OUT.json` |

**Восстановление** (в `finally`, обязательно: S5-D1 теперь реально сохраняет выбор). Исходный
`video-input.json`, снятый в начале фазы, записывается явно, затем SIGHUP главному процессу блока. После этого
активный источник сверяется с исходным (`summary.json → restore`). Если файл или источник не вернулись, в журнал
пишется «ВНИМАНИЕ». Перед остановкой vivid блок переводится на synthetic: если узел vivid держит видеоприём, `modprobe -r`
откажет и юнит станет failed (README «не проверено», п. 5).

Проверено только тестами без плат (20 тестов). В частности, суждение P2-07 проверено на настоящих снимках фиксации pi1
(`docs/acceptance/e2e/ota-pi1/cycle2/retry`): пробная → та же загрузка, пробная → перезагрузка, пробная → `ota-disable`
дают PASS; изменения tree.system, запись в слот A, HEALTH_PENDING и NRestarts дают FAIL. На платах выполнено только
чтение: личность, OTA, список источников через пульт.

## 3. Решение о confirm и критерии STOP

`ota-confirm` агент выполнит только при всех обязательных гейтах (`software/sheregesh/update/confirm.py`):
слот и версия, persistent, отпечатки, `sheregesh-remote-net` active и NRestarts 0, **failed-units пусто**,
uptime ≥ 90 с, health PASS, mTLS, **видео: сеанс есть, кадров > 0**. Фаза trial оставляет блок на исходном источнике
с живым видео.

**STOP: confirm не выполнять, решение `ota-abort` за владельцем** (abort на пробной загрузке — это обычная
перезагрузка, прошивка вернёт A):
1. P2-01 FAIL или ERROR: не B, не пробная загрузка, не тот образ или версия, failed не пуст, health не PASS.
2. Адрес pi2 сменился, SSH не вернулся, ключ хоста другой (это красный флаг, см. OTA CLOSURE).
3. P2-02 FAIL: нет mTLS или связи pi1↔pi2.
4. P2-03 или P2-04 FAIL: регресс синтетического или RTSP-видео относительно работающего сейчас g980c45.
5. После P2-05 есть failed-юниты. Гейт failed-units отклонит confirm, а `reset-failed` эти юниты по polkit не получают.
   Подтвердить такую загрузку нельзя, только abort с разбором.
6. Восстановление не удалось (`restore_warning`) и блок без живого видео. Сначала вернуть источник (videoselect) и
   проверить гейт video, затем решать.

**Не блокирует confirm само по себе (дефект в отчёт, решение владельца):** P2-05 FAIL при пустом failed, то есть
неисправность лабораторного эмулятора п. 7, dev-функции. Сюда относятся пункты «не проверено» README: `IPAddressAllow`
и трафик USB/IP после выхода помощника (тогда 534d:2109 не появится или не пойдут кадры), `udevadm settle` в
песочнице, загрузка CPU подачей vivid. **P2-06 FAIL (S5-D1)** означает, что не работает главная цель образа: confirm
не рекомендуется, но решает владелец. ERROR — повторить шаг один раз (`--only P2-0N`), затем решать.

**STOP в фазе committed:** P2-06 BLOCKED («перезагрузка запрещена») означает, что confirm не зафиксировал B. SSH не
вернулся за 300 с: искать плату `python3 tools/probe/collect_boot.py find --mac d8:3a:dd:69:0f:6e` и не
перезагружать повторно. P2-07 FAIL — разбор `pi2-compare-commit.txt`. pi1 изменился (boot_id или OTA) — выяснить,
не работы ли это владельца у стенда (общий БП с shcam, S5-D2), прежде чем считать дефектом.

## 4. Ограничения

- `ui-state` — сведения, а не доказательство рендеринга. Визуальное доказательство дают только снимки `--gui`
  (программный рендерер Qt, путь cage/HDMI не проверяется). Без `--gui` у P2-03/04 в подробностях пометка
  «без снимка GUI».
- Пороги отказа (≤ 3 с «нет видео», ≤ 10 с возврат) взяты по аналогии из REQ-SW-002, как в S4/S5. Для перезапуска и
  перезагрузки есть только таймауты (60/300/180 с), длительности фиксируются.
- В trial `--gui` останавливает `sheregesh-console` на pi1 примерно на 1,5 мин. Это видно только по InvocationID пульта,
  а P2-07 сверяет на pi1 лишь OTA и boot_id.
