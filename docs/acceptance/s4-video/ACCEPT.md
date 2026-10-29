# S4 «видео» — приёмка на двух Pi 4 за ≤ 15 минут (QA, агент D)

Ветка `s4/qa` от `v2` 59ea0c2, 2026-10-25. Сценарий: [`tools/probe/s4_accept.py`](../../../tools/probe/s4_accept.py)
(тесты суждения — `tests/unit/test_s4_qa_accept.py`). Регрессии относительно прежних протоколов — §4. Диагностика
USB-захватов после доставки — [`USB-DIAG.md`](USB-DIAG.md). Отчёт — по [`REPORT-TEMPLATE.md`](REPORT-TEMPLATE.md).

Базовая линия: [`../e2e/hw/HW-RESULTS.md`](../e2e/hw/HW-RESULTS.md) (E2E-01…15, слоты A, DEV g7061b3c) и
[`../e2e/ota-pi1/cycle2/retry/RESULT.md`](../e2e/ota-pi1/cycle2/retry/RESULT.md) (pi1 B COMMITTED, E2E-13 PASS).

## 0. Стенд и условия допуска

| | pi1 (пульт) | pi2 (удалённый блок) |
|---|---|---|
| Адрес, серийный номер | 192.168.1.92, `10000000568152de` | 192.168.1.171, `100000009d497e91` |
| Слот, OTA (на 25.10 12:30, чтение) | **B COMMITTED** (образ b1d95a2), `[all]=3`, `[tryboot]=2` | A, ROLLED_BACK (образ 57e8fe4), `[all]=2`, `[tryboot]=3` |
| Код (чтение 25.10 12:30) | DEV deploy `0.1.0~dev0+g59ea0c2b9150` (первый на B, 12:29) | DEV deploy `0.1.0~dev0+g59ea0c2b9150` (12:28, было g7061b3c) |

- **Допуск** (проверяет сценарий, иначе ERROR без действий): серийные номера; OTA обеих плат ∈ {IDLE, COMMITTED,
  ROLLED_BACK} и согласовано; службы `active`. Параллельно — никаких OTA и DEV deploy.
- На обеих платах развёрнут выпуск S4 (агент C, `s4/deploy`) — **до** снимка slot_guard «до»: снимки «до/после»
  делаются внутри одного прогона, иначе `same.deploy.status`/`same.unit.*` дают ожидаемые FAIL (как в HW-RESULTS).
- Тестовый RTSP-сервер агента B ([`tools/probe/s4_rtsp_test_server.py`](../../../tools/probe/s4_rtsp_test_server.py),
  будет) запущен и доступен с pi2; владелец даёт команды его остановки и запуска (`--rtsp-stop/--rtsp-start`,
  выполняются на Mac). Без них VID-07 — SKIP, VID-05/06 — только synthetic (итог «частично»).
- Разрешение владельца в чате на воздействия (все — правами `sheregesh-ui`, юниты и образ не меняются, всё
  возвращается в `finally`): SIGSTOP/SIGCONT конвейера видеоприёма pi2; запись `~/.config/sheregesh/video-input.json`
  на pi2 + SIGHUP службы блока; `--gui` — остановка `sheregesh-console` на pi1 на ≈ 1,5 мин.

```bash
cd ~/Developer/sheregesh
python3 tools/probe/s4_accept.py --plan                                   # план без SSH
D=docs/acceptance/s4-video/run-$(date +%Y%m%d-%H%M)
python3 tools/probe/s4_accept.py --console 192.168.1.92 --remote 192.168.1.171 --out $D --gui \
  --rtsp-url rtsp://<хост сервера>:<порт>/<путь> --rtsp-stop '<команда>' --rtsp-start '<команда>'
# повтор одного шага: --only VID-07 (VID-09 сравнивает снимки только внутри одного прогона)
```

Результат: `$D/summary.json` (вердикты и измерения), `faults.json`, `gui/*.png` + `gui/states.json`,
`check_link/`, `check_video_link/`, `pi{1,2}-{before,after}.json`, `pi{1,2}-compare.txt`.

## 1. Шаги, критерии и что модель

Вердикты: **PASS**; **FAIL** — доказано несоответствие; **ERROR** — испытание не состоялось или не доказательно
(никогда не PASS); **SKIP** — не запрошено / нет условия. Итог: FAIL > ERROR > PASS; SKIP итог не улучшает.

| ID | Мин | Действие (средство) | PASS | FAIL | ERROR | Реально / модель |
|---|---|---|---|---|---|---|
| VID-01 | 0,3 | `systemctl show` служб пульта и блока, `systemctl --failed`, `sheregesh-health --json` обеих ролей (e2e_run) | обе службы `active/running`, failed нет, health PASS | любое не так | SSH/health не ответили | реально |
| VID-02 | 1 | `check_link.py --only health,identity` (без перезапуска пульта); `ui-state.peerLink/linkTls`, `link-status.tls`, `video.transport` | check_link 2/2 PASS; CONNECTED; управление TLSv1.3; видео `tls1.3-mutual` | что-то из PASS не выполнено | нет `summary.json` | реально (Ethernet напрямую pi1↔pi2) |
| VID-03 | 2 (с 04) | `--gui`: `gui_capture.py --by video` на pi1 (тот же код выпуска, Qt offscreen/software, настоящая связь с pi2); источник synthetic pi2 | PNG `01-02-stream-{dashboard,video}`; в `states.json` снимка источник `synthetic` | на снимке потока источник не synthetic | без `--gui` или снимков нет — **ERROR** (`ui-state.json` не доказательство) | реально, кроме пути cage/Wayland/DRM/HDMI (дисплея нет — BLOCKED: hardware) |
| VID-04 | — | пара снимков страницы «Видео» с интервалом 1 с; сравнение **только области кадра** `videoView` (таймкод `timeoverlay` в кадре) | `frame_changed = true` | `false` (кадр застыл) | снимка нет | реально |
| VID-05 | 2–3 | (а) synthetic: `SIGSTOP` группы процессов конвейера видеоприёма pi2 на 6 с (страховочный `SIGCONT` на плате через 15 с), ×2; (б) RTSP: переключение на RTSP, `--rtsp-stop` на 6 с, ×2. Выборка `ui-state` pi1 и `link-status` pi2 каждые 0,25 с; при `--gui` — снимки `03-04-no-signal` | пульт перестал показывать кадры (`videoSource.live=false`) ≤ 3 с; на блоке наблюдалось `no-signal` (а) / `connect-error` или `no-signal` (б); снимок: кадр не обновляется (`frame_changed=false`) | > 3 с, не наблюдалось, или при отказе кадр меняется | отказ не внесён | **модель**: (а) «источник перестал отдавать кадры» (как вынутый HDMI/CVBS у захвата, который замолкает); (б) «камера/кодер RTSP недоступны». Захваты, шлющие синее поле без статуса входа, моделью не покрываются (USB-DIAG) |
| VID-06 | (с 05) | после `SIGCONT` / `--rtsp-start` — без каких-либо действий | `live=true` ≤ 10 с; снимок `05-06-recovered-no-signal`: кадр сменяется | > 10 с или не вернулось | — | модель (как VID-05); восстановление — реальное поведение Ingest |
| VID-07 | 2 | `video-input.json` + SIGHUP: synthetic → rtsp → synthetic ×3; ресурсы до/после (e2e_run.resources + зомби) | каждое переключение: блок `streaming` нужного типа и пульт `live` с тем же `input` ≤ 15 с; после: ровно 1 конвейер на плате, сирот и зомби нет, NRestarts и MainPID прежние, дескрипторы ±3, потоки ±2 | зависание (> 15 с), лишний конвейер, рост ресурсов | — | реально (RTSP-сервер тестовый) |
| VID-08 | 1,5 | журналы с начала прогона (юниты `sheregesh-*`, `notify-relay`), Traceback по всей системе, `--failed`; `check_video_link.py --only session,stream,leftovers` | 0 Traceback; нет `Failed with result`, `Main process exited, code=`, `stopped: result≠success`; failed нет; check_video_link 3/3 | что-то найдено | нет `summary.json` | реально. Исключение по шуму: `vc4-drm … swiotlb` (GPU без дисплея, RESULT.md цикла 2). Остановка пульта в VID-03 обязана дать `result=success` (это попутно E2E-13) |
| VID-09 | 1 | slot_guard `snap` обеих плат до и после; `compare --mode unchanged` | обе платы: PASS slot_guard без исключений (направление — от слота снимка «до», `cf200e2`), обязательные проверки `slot.active_unchanged`, `bootconfig.autoboot`, `health`, `failed` есть и прошли; по снимку: слот по ядру не сменился и совпадает с агентом OTA, OTA в покое (IDLE/COMMITTED/ROLLED_BACK), пробной загрузки нет, основной раздел autoboot — активный слот; при `--gui` на pi1 допустим `same.unit.sheregesh-console` (пульт перезапускался шагом) | любой иной FAIL или нарушение состояния платы | ERROR slot_guard или нет снимков | реально |

**Почему для pi1 `unchanged` с исключениями, а не ручные критерии.** Режим `unchanged` писался для платы на A
(`slot.active_unchanged` требует A; `bootconfig.autoboot` — `[all]=2`). Для B COMMITTED проверено на настоящем снимке
(`pi1-final.json` цикла 2 сам с собой): FAIL ровно эти два, PASS 42 — неизменность разделов (p1–p5, счётчики
записи ядра), `ota.status`, `boot_id`, `dt.tryboot`, отпечатки ключей, точки монтирования, `deploy.status`,
`ota.enable_absent` проверяются как обычно. Поэтому шаг применим с явным списком исключений (тест
`test_pi1_b_committed_real_snapshot_two_expected_fails`). Изменение slot_guard (режим `unchanged-b`) — не в зоне QA.

**Погрешность моментов.** Пульт пишет `ui-state.json` раз в секунду: времена VID-05/06 — с погрешностью ≈ +1,3 с
(e2e MATRIX §2), то есть 3 с по `ui-state` — консервативно. Блок пишет `link-status.json` раз в 2 с
(`agent.py`: сразу — только смена «есть/нет пульта»), поэтому состояние источника на блоке судится как «наблюдалось»,
а не по времени; обрыв 6 с ≥ `no_signal_s` 2 с + период 2 с + запас.

**Время.** Предпроверка и снимки 1 + VID-01/02 1,3 + VID-03/04 2 + VID-05/06 2–3 + VID-07 2 + VID-08 1,5 + VID-09 1
≈ 11–12 мин; с запасом на SSH — ≤ 15 мин. Без RTSP — ≈ 8 мин.

## 2. Если шаг не прошёл

- Сценарий восстанавливает исходный источник явной записью `{"source": "synthetic"}` + SIGHUP (удаление файла +
  SIGHUP источник **не** меняет: `agent.video_spec` — «not found», конфигурация не перечитана), затем, если файла не
  было, удаляет его. Проверить вручную: `ssh sheregesh-ui@192.168.1.171 'cat ~/.config/sheregesh/video-input.json;
  python3 -c "import json;print(json.load(open(\"/run/sheregesh-remote/link-status.json\"))[\"video\"][\"source\"])"'`.
- Пульт после `--gui`: `systemctl is-active sheregesh-console` = `active`; если нет — `systemctl start sheregesh-console`.
- Конвейер pi2 «застыл» (`T` в `ps -o stat`): страховочный SIGCONT срабатывает через 15 с; Ingest сам перезапускает
  конвейер без кадров через 10 с (`stall_restart_s`).
- FAIL VID-09 по pi2 или неожиданный FAIL pi1 — STOP, ничего не исправлять, сообщить владельцу.

## 3. Что не проверяется этим шагом

Реальные устройства захвата (CVBS/HDMI/UVC) — после доставки 27.10, [`USB-DIAG.md`](USB-DIAG.md). HDMI-выход пульта,
физический дисплей и сенсор — BLOCKED: hardware unavailable. REQ-P-012 (10 подач питания) — BLOCKED (нет управления
питанием). Задержка «стекло-стекло» — только оценка (video-ingest HW-PLAN §3).

## 4. Регрессии относительно базовой линии

`git diff b1d95a2..v2 -- software/` — **пусто**: заводской код слота B pi1 (образ b1d95a2) совпадает с `software/`
v2; `os/`, `tools/` — тоже без изменений. Значит, E2E-13 PASS (RESULT.md цикла 2) переносится на v2 как есть.

`git diff 7061b3c..v2 -- software/` (код HW-RESULTS → v2): 14 файлов, +1712/−200, коммиты ba4f4f8, e761fd0, 43e8743,
c6b6abc, 055c5d1, 3e147dd, 909c925. Образ pi1 сменился 57e8fe4 → b1d95a2 (OTA), pi2 — прежний 57e8fe4.

| Изменение | Затронутые ранее PASS | Адресная проверка | Повторять полностью? |
|---|---|---|---|
| **3e147dd** Ingest: видеосервер блока больше не запускает генератор сам — источником и его процессами владеет `videoingest.Ingest` (перезапуск внутри сеанса, `RtpContinuity`, killpg при смене) | E2E-05, 06, 10, 11 (конвейеры, сироты), 15 (накопление ресурсов) | VID-03…07 (ресурсы до/после, ровно 1 конвейер, зомби); `check_video_link --only session,stream,leftovers` (VID-08); `e2e_run --fault pause --cycles 3` (≈ 2,5 мин) — обрыв управления при Ingest | 30-мин soak — **нет**, если VID-07 не показал роста дескрипторов/потоков; при росте — soak 30 мин |
| то же: **смена семантики sender-kill** — SIGKILL генератора больше не завершает видеосеанс, Ingest перезапускает генератор в том же сеансе | E2E-06 (sender-kill 5/5), `check_video_link sender-kill` | средства обновлены (s4/qa): `check_video_link.sc_sender_kill` и `e2e_run --fault sender-kill` судят перезапуск генератора (новый PID) и возврат кадров ≤ 10 с в том же сеансе (новый сеанс допустим); «нет видео» — наблюдение (может не наступить, если перезапуск быстрее NO_VIDEO_AFTER_S 1,5 с). Адресно: `e2e_run --fault sender-kill --cycles 3` (≈ 1,5 мин) | нет |
| e761fd0, 43e8743, ba4f4f8 GUI: источник видео на «Видео» и в плитке связи, чёрное поле без сигнала, текст плитки при обрыве | E2E-04 (снимок «связь потеряна»), 05, 06 | VID-03/04 (поток), VID-05/06 рендеринг; **плюс** один `gui_capture --by link` с `stop-start` блока на 10 с (≈ 1,5 мин) — заодно подтверждает исправление переполнения плитки 1280×800 | нет |
| c6b6abc, 055c5d1, 909c925 `videoinput`: V4L2 (usbtv/stk1160/HDMI/UVC), RTSPSource, NO_H_LOCK, `source` в INFO видеосеанса | E2E-05 (INFO сеанса), протокол 1.1 | VID-02, VID-03, VID-08 `session`; устройства — USB-DIAG | нет |
| `link/client.py`: необязательное `status.video.source` (протокол 1.1 без изменений, санитизация) | E2E-03, 04 | VID-02 (`check_link health,identity`); смешанные версии — только модульно (`video-ingest/gui/*legacy-1.1*`), на стенде обе платы одного выпуска | `check_link` 10/10 — нет |
| `console/app.py`, `backend.py` (+47) | E2E-01, 13, 14 | VID-01; остановка/запуск пульта в VID-03 с проверкой `result=success` (E2E-13, E2E-14 попутно) | 10 перезагрузок — нет (загрузочный путь не менялся) |
| OTA, slot_guard, агент обновлений — **без изменений** с 7061b3c | E2E регрессии OTA | VID-09 (`unchanged` обеих плат) | полную OTA-регрессию — нет |
| образ b1d95a2 на pi1 (`sheregesh-var-seed`, порядок console-setup) | E2E-01 (загрузка пульта) | контрольная перезагрузка B уже сделана (RESULT.md шаг 7: SSH 21 с, службы running, failed нет); время первого кадра на B не мерилось | не в объёме S4 |

Не повторяются без причины: 30-мин soak (E2E-15), 10 перезагрузок (REQ-P-012), 10 кабелей (REQ-SYS-002 п.4;
кабель — 3/3 в HW-RESULTS, сетевой код не менялся), полная OTA-регрессия.
