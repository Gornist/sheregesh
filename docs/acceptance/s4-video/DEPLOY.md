# S4 «видео на блоке» — развёртывание DEV-выпуском на стенд Pi 4

Agent A (Integration / Deployment), ветка `s4/deploy` от v2 `59ea0c2`. Платы только читались по SSH
(`sheregesh-ui@192.168.1.92` pi1, `@192.168.1.171` pi2), 2026-10-25 ~12:30 GMT; ничего не развёртывалось.

## Итог

**Совместимо.** Код v2 (и S4, если он остаётся в `software/` и использует те же элементы GStreamer) разворачивается
DEV-выпуском на оба текущих образа без нового базового образа. Блокеров нет. Есть одна неточность процедуры
переключения источника (SIGHUP, см. Н-1) и две особенности, которые надо знать при проверках (Н-2, Н-3).
Новый образ нужен только для HDMI через CSI (tc358743) и для `avdec_h264` — оба в S4 не требуются.

## 1. Фактическое состояние плат (отличается от вводной)

| | pi1 (пульт) | pi2 (блок) |
|---|---|---|
| слот / образ | B, `b1d95a2` (пакет `0.1.0~dev0+gb1d95a2`), COMMITTED | A, `57e8fe4` (пакет `0.1.0~dev0+g57e8fe4`) |
| DEV-выпуск current | **`0.1.0~dev0+g59ea0c2b9150`** (установлен 12:29:07Z) | **`0.1.0~dev0+g59ea0c2b9150`** (12:28:03Z) |
| previous | **нет** | `0.1.0~dev0+g7061b3c52ee8` |
| в releases/ | 59ea0c2 | 1cd20fd, 59ea0c2, 7061b3c |
| служба | sheregesh-console active, NRestarts=0 | sheregesh-remote-net active, NRestarts=0, `hello proto=1.1 -> ok`, `video input: starting -> streaming` |
| video-input.json | — (не нужен пульту) | **нет** → блок шлёт синтетику |
| устройства захвата | нет (только служебные узлы bcm2835) | нет (`python3 -m sheregesh.videoinput --no-probe`: только auxiliary-узлы; lsusb — только хаб VIA) |

То есть вводная «pi1 без DEV-выпуска, pi2 на g7061b3c» уже устарела: главный агент поставил `59ea0c2` на обе платы.

## 2. Зависимости нового кода и что есть в образах

Видео строится через `gst-launch-1.0` подпроцессом (без PyGObject/`gi`); Python — только стандартная библиотека
(`videoinput.py`: ioctl V4L2 через `fcntl`/`struct`, без v4l2-ctl/udev-модулей). Пакетные списки
`build/image/ci-57e8fe4-dev/…packages.tsv` и `build/image/devbox-b1d95a2/…packages.tsv` **совпадают по составу и
версиям** (GStreamer 1.26.2: base/good/bad/tools, libopenh264-8, libv4l; ядро 6.18.50+rpt-rpi-v8; Python 3.13.5).

Проверка `gst-inspect-1.0 --exists` на обеих платах (одинаково):

| элемент | где используется | плагин / пакет | pi1 | pi2 |
|---|---|---|---|---|
| v4l2src | вход V4L2/UVC/CVBS | video4linux2 / plugins-good | ok | ok |
| rtspsrc | вход RTSP | rtsp / plugins-good (`libgstrtsp.so`, 1.26.2) | ok | ok |
| rtph264depay, rtpjpegdepay, rtph264pay, rtpjpegpay | RTSP, канал | rtp / plugins-good | ok | ok |
| rtpjitterbuffer | приём пульта | rtpmanager / plugins-good | ok | ok |
| rtpstreampay, rtpstreamdepay | RFC 4571 по TLS | rtp / plugins-good | ok | ok |
| jpegdec, jpegenc | MJPEG | jpeg / plugins-good | ok | ok |
| h264parse | H.264 | videoparsersbad / plugins-bad | ok | ok |
| v4l2h264enc, v4l2h264dec | аппаратный кодек Pi 4 | video4linux2 / plugins-good | ok | ok |
| openh264enc, openh264dec | запасной кодек | openh264 / plugins-bad | ok | ok |
| **avdec_h264** | запасной декодер | gstreamer1.0-libav | **НЕТ** | **НЕТ** |
| videotestsrc, videoconvert, videoscale, videorate, timeoverlay, textoverlay | синтетика, масштаб | plugins-base | ok | ok |
| udpsrc, udpsink, fdsrc, fdsink, fakesink | локальный профиль, пробы | good / core | ok | ok |

`avdec_h264` отсутствует, но на Pi 4 `h264_elements()` выбирает `v4l2h264dec` (проба проходит, видео с блока
сейчас идёт). Строка-заглушка `"avdec_h264"` в `VideoSource.decode`/`video.stream_sender_args` используется, только
если проба H.264 целиком не нашла связки — на стенде этого не бывает. В образ не нужен.

Права и ядро:
- `id sheregesh-ui` на обеих платах: группа **video (44)** есть; `/dev/video*`, `/dev/media*` — `root:video 0660`
  (на pi1 ещё ACL сеанса). Юнит `sheregesh-remote-net` не ограничивает устройства (нет `PrivateDevices`/`DeviceAllow`),
  `ProtectHome=read-only` чтению `~/.config/sheregesh/video-input.json` не мешает.
- Модули есть в обоих образах: `kernel/drivers/media/usb/{uvc/uvcvideo,usbtv/usbtv,stk1160/stk1160,em28xx/em28xx}.ko.xz`,
  vermagic `6.18.50+rpt-rpi-v8`; загрузятся по modalias при подключении (сейчас не загружены — устройств нет).
  `/lib/udev/rules.d/60-persistent-v4l.rules` есть → ссылки `/dev/v4l/by-id|by-path` появятся.
- `tc358743.ko.xz` (HDMI→CSI) в ядре есть, но `config.txt` = `boot_ramdisk=1`, оверлеи внутри `boot.img`:
  `dtoverlay=tc358743` без нового образа не включить.
- RTSP: юнит блока пускает только частные/link-local сети (`IPAddressAllow`), `RestrictAddressFamilies` — INET/INET6/UNIX:
  камера должна быть в 10/8, 172.16/12, 192.168/16 или 169.254/16.

## 3. Совместимость кода

**Что DEV deploy доставляет, а что нет.** Пакет — все файлы `build_deb.SOURCES` из коммита; launcher
(`/usr/lib/sheregesh/software/sheregesh/deploy/launcher.py`, из образа) ставит `PYTHONPATH` на `software`,
`software/emulators`, `software/console`, `software/remote` выпуска. Значит весь пакет `sheregesh.*`, которым
пользуются `sheregesh_remote` и `sheregesh_console` (videoinput, videoingest, videolink, video, link/*, monitor,
faults, protocol), берётся **из выпуска**. Из `/usr` образа всегда: launcher, notify_relay, health, deployd, ota,
юниты, polkit.

`git diff --stat b1d95a2 v2` — только `docs/`: pi1-образ содержит ровно код v2. `57e8fe4..v2` вне `docs/`/`tests/`:

| компонент из /usr | изменение после 57e8fe4 | влияние на видео pi2 |
|---|---|---|
| юниты | только новый `sheregesh-var-seed.service` (OTA); `sheregesh-remote-net.service` **без изменений** | нет |
| launcher, bundle | без изменений | нет: формат пакета, `PYTHON_PATHS`, `COMPATIBLE` совпадают |
| deployd (installer.py) | +4 строки: mtime файлов выпуска = время коммита | нет (касалось .ts/.qm пульта; на pi1 уже есть) |
| notify_relay | код 0 на SIGTERM | на блоке не используется (только пульт; на pi1 уже есть) |
| health | без изменений | нет |
| update/{confirm,ota,slots,model} | OTA S3.2 | нет |

Итого: ничего, что DEV deploy не доставит и что влияет на видео, после образов не менялось.

**Агент блока с аргументами юнита 57e8fe4** (`--link-config … --status-file … --event-interval 5`, без видеоаргументов):
`video_spec()` → `--video-source` не задан → читается `video-input.json` рядом с `link.json`; файла нет →
`{"source": "synthetic"}`. Видеоканал включён по `link.json` (`video` по умолчанию `true`), кодек — проба (сейчас
h264/v4l2h264enc), SIGHUP-обработчик ставится, когда поднят видеосервер. Подтверждено журналом pi2 на 59ea0c2 и
тестами (85 passed: `tests/system/test_video_input_agent.py`, `tests/system/test_e2e_link_net.py`,
`tests/unit/test_videoinput.py`, `tests/unit/test_deploy.py`, `tests/tools/test_deploy_cli.py`).

**Протокол 1.1.** `status.video.source` — необязательное поле (`link/client._sanitize_video_source`); пульт без
поддержки его отбрасывает, блок без поля → пульт показывает «синтетический, legacy». Поэтому порядок pi2 → pi1
безопасен в окне, когда блок новый, а пульт старый. Если S4 (B/C) добавит **новые типы сообщений** пульт→блок
(например, выбор источника с пульта), порядок pi2 → pi1 тоже правильный: новый блок обязан терпеть их отсутствие.
Ветки `s4/pipeline` и `s4/gui` на момент проверки не отличались от v2 — сверить после их слияния (см. §6).

**Окно READY.** Установщик перезапускает только службу роли и ждёт READY (`TimeoutStartSec=60` у блока) плюс окно без
перезапусков. На pi2 от launcher до `LISTENING` ~20 с (проба кодеков gst-launch). S4 не должен добавлять синхронных
проб до `sdnotify.ready()` больше чем на ~30 с, иначе установка уйдёт в ROLLED_BACK.

**Автоматический откат ловит только падения.** Критерий установщика — служба в READY и без перезапусков в окне.
Нерабочее видео (нет кадров, ошибка конвейера) откат **не** вызывает — проверять руками (§5).

### Находки

**Н-1 (процедура/код B): `systemctl kill -s HUP sheregesh-remote-net` убивает конвейер.**
По умолчанию `systemctl kill` шлёт сигнал всем процессам cgroup (`--kill-whom=all`). Конвейер видеоприёма —
`gst-launch-1.0` (через `setpriv --pdeathsig KILL`, своя группа процессов, но та же cgroup); SIGHUP он не
обрабатывает → завершается. `Ingest._poll_attached` видит выход раньше или одновременно с `switch()`:
состояние кратко `error: pipeline exited with code …`, `restarts += 1`, перезапуск с паузой. При **неизменённом**
файле (`video config unchanged`) это лишний разрыв видео. Правильная команда:

    systemctl kill --kill-whom=main -s HUP sheregesh-remote-net

(systemd 257 на платах; polkit-глагол тот же `kill`). Поправить в `docs/architecture/S3.3-universal-video-input.md:296`
и везде, где B/C будут это вызывать (GUI источника, если пульт будет менять источник через ssh — не будет: у пульта
нет прав на блок). На железе SIGHUP ещё ни разу не подавался (в журнале pi2 за 3 загрузки нет `video config`/`video source:`).

**Н-2: удаление `video-input.json` + SIGHUP не возвращает синтетику.** `video_spec(reload=True)` при отсутствии
файла бросает `ValueError … not found` → `video config not reloaded`, источник прежний. Вернуться к синтетике:
записать `{"source": "synthetic"}` и подать SIGHUP (или удалить файл и перезапустить службу).

**Н-3: файл писать атомарно.** Частично записанный JSON при SIGHUP → `video config not reloaded` (безопасно, но
источник не сменится). Писать через временный файл и `mv` в том же каталоге.

**polkit.** `/etc/polkit-1/rules.d` недоступен sheregesh-ui на чтение, `pkcheck` в образе нет — проверено по коду:
`os/layer/sheregesh-dev-access.d/50-sheregesh-dev.rules` (не менялся с 57e8fe4) даёт `YES` на
`org.freedesktop.systemd1.manage-units` для `sheregesh-remote-net.service` **без ограничения глагола** → `kill`
(в т. ч. `--kill-whom=main`), `restart` разрешены. Фактический вызов — первым шагом проверки после развёртывания (§5.3).

## 4. Штатный откат (по коду `installer.py`)

- `rollback`: берёт `state.previous`; если его нет или он равен current → **REJECTED «нет предыдущего выпуска
  (используйте factory)»**, ничего не трогает. Иначе проверка целостности (`launcher.verify_release`) и то же
  переключение с наблюдением за службой; при неудаче — возврат на то, что было.
- `factory`: снимает `current` → заводской `/usr/lib/sheregesh` образа; если уже заводской — `OK «уже заводской код»`.
- После `deploy` S4: previous = `g59ea0c2b9150` на **обеих** платах → `rollback` работает и на pi1 (сейчас на pi1
  previous нет, `rollback` до S4-deploy был бы REJECTED — это штатно, не ошибка).
- GC хранит current, previous и 3 новейших каталога — 59ea0c2 не удалится.
- `factory` на pi2 = код 57e8fe4 (видеоприёма нет: блок шлёт синтетику, как до S3.3); на pi1 = b1d95a2 (= v2).
- Прерванное переключение (обрыв ssh, перезагрузка до подтверждения) — `pending` с другим boot_id: launcher сам
  выбирает прежний выпуск; следующий запуск установщика снимает pending.
- Тесты: `test_rollback_and_factory_actions`, `test_rollback_without_previous_is_refused`,
  `test_first_bad_release_falls_back_to_factory` — проходят.

## 5. Развёртывание по шагам (делает главный агент)

Из чистого рабочего дерева на коммите S4 (пакет берётся из `HEAD`, незакоммиченное не входит). Нужны `~/.ssh/sheregesh-deploy`
и `pyside6-lrelease`.

### 5.0. Перед

```sh
git status --short                      # пусто
git rev-parse --short=12 HEAD           # запомнить → <SHA>
python3 tools/deploy/shdeploy.py status # pi1: current g59ea0c2b9150, previous —; pi2: current g59ea0c2b9150, previous g7061b3c52ee8
```

### 5.1. pi2 (блок) — первым

```sh
python3 tools/deploy/shdeploy.py deploy pi2
# ожидается: install → OK «0.1.0~dev0+g<SHA> работает», previous 0.1.0~dev0+g59ea0c2b9150,
#            sheregesh-remote-net: READY, без перезапусков
```

Проверки (только чтение):

```sh
ssh sheregesh-ui@192.168.1.171 'journalctl -u sheregesh-remote-net -b --no-pager -n 60 | grep -E "launcher|LISTENING|VIDEO|video input|hello|video config"'
#   sheregesh-launcher: sheregesh_remote: выпуск 0.1.0~dev0+g<SHA>
#   LISTENING port=7443 tls=1.3 mutual …; VIDEO codec=h264 port=7444 transport=tls1.3-mutual
#   (после переподключения пульта) hello from console proto=1.1 -> ok; video input: starting -> streaming
ssh sheregesh-ui@192.168.1.171 'systemctl show -p ActiveState,NRestarts sheregesh-remote-net; cat /run/sheregesh-remote/link-status.json'
#   active, NRestarts=0; connected=true, video — сеанс и счётчики пакетов растут
```

Старый пульт (59ea0c2) с новым блоком: картинка на pi1 идёт, телеметрия без ошибок.

### 5.2. pi1 (пульт)

```sh
python3 tools/deploy/shdeploy.py deploy pi1
# ожидается: OK, previous 0.1.0~dev0+g59ea0c2b9150, sheregesh-console: READY, без перезапусков
ssh sheregesh-ui@192.168.1.92 'journalctl -u sheregesh-console -b --no-pager -n 80 | grep -E "launcher|link|video|Traceback|Error" | tail -20'
#   sheregesh-launcher: sheregesh_console: выпуск 0.1.0~dev0+g<SHA>; без Traceback
```

Сквозная проверка канала и видео (синтетика, не меняет плат):

```sh
python3 tools/probe/check_video_link.py --console 192.168.1.92 --remote 192.168.1.171 --out /tmp/s4-video --only session,stream,telemetry,leftovers
```

### 5.3. Смена источника на блоке (когда подключено устройство/камера)

```sh
ssh sheregesh-ui@192.168.1.171 'cd /tmp && PYTHONPATH=/var/lib/sheregesh-deploy/current/software python3 -s -m sheregesh.videoinput'
#   узлы CAPTURE, by-id/by-path, режимы, входы и стандарты (служебные узлы bcm2835 не открываются)
ssh sheregesh-ui@192.168.1.171 'cd ~/.config/sheregesh && printf "%s\n" "{\"source\": \"v4l2\", \"device\": \"/dev/v4l/by-id/…\", \"mode\": \"auto\", \"format\": \"auto\"}" > .video-input.json.tmp && mv .video-input.json.tmp video-input.json'
ssh sheregesh-ui@192.168.1.171 'systemctl kill --kill-whom=main -s HUP sheregesh-remote-net'   # Н-1: только главный процесс
ssh sheregesh-ui@192.168.1.171 'journalctl -u sheregesh-remote-net -n 10 --no-pager | grep -E "video source:|video config|video input"'
#   video source: source=synthetic -> device=…, source=v4l2 …; video input: … -> starting -> streaming
```

RTSP: `{"source": "rtsp", "url": "rtsp://192.168.x.y:554/path", "codec": "h264"}`. Возврат к синтетике —
`{"source": "synthetic"}` + тот же SIGHUP (Н-2). Если `systemctl kill` откажет по polkit — это находка для образа
(правило), временный обход — `systemctl restart sheregesh-remote-net` (разрешён тем же правилом; файл читается при старте).

### 5.4. Откат

```sh
python3 tools/deploy/shdeploy.py rollback pi1    # → g59ea0c2b9150 (пульт первым: старый пульт со старым блоком)
python3 tools/deploy/shdeploy.py rollback pi2    # → g59ea0c2b9150
# аварийно — заводской код образа:
python3 tools/deploy/shdeploy.py factory pi2     # код 57e8fe4: только синтетика, без видеоприёма
python3 tools/deploy/shdeploy.py factory pi1     # код b1d95a2 (= v2)
python3 tools/deploy/shdeploy.py status
```

Откат выпуска не удаляет `video-input.json`: на 59ea0c2 он читается так же (тот же видеоприём v2); на factory pi2
(57e8fe4) файл игнорируется. После отката проверки — как в 5.1/5.2 (строка launcher с нужным выпуском).

## 6. Что требует нового базового образа

Для S4 в объёме «видеоприём на блоке через V4L2/UVC/USB-захват/RTSP + GUI пульта» — **ничего**.

| что | когда нужно | минимальная доставка |
|---|---|---|
| `dtoverlay=tc358743` (HDMI→CSI-2) | только если вход HDMI через CSI-мост, а не USB-адаптер | правка `os/layer/sheregesh-lab.yaml` (строка с `dtoverlay=disable-wifi`), новый образ → OTA A/B (`shdeploy ota-build/ota-install/ota-tryboot/ota-confirm`); `config.txt` вне `boot.img` содержит только `boot_ramdisk=1` |
| `gstreamer1.0-libav` (`avdec_h264`) | только для машины без аппаратного/openh264 декодера; на Pi 4 не нужно | пакет в слой → образ → OTA |
| правило polkit для SIGHUP | только если 5.3 покажет отказ `kill` (по коду не ожидается) | правка `50-sheregesh-dev.rules` → образ → OTA |
| изменения юнитов, launcher, notify_relay, health, deployd в S4 | если B/C их тронут | DEV deploy их **не** доставляет → образ → OTA; до этого — не полагаться на них |

Контроль после слияния B и C: `git diff --stat 59ea0c2 HEAD -- tools/image/units os software/sheregesh/deploy/launcher.py software/sheregesh/notify_relay.py software/sheregesh/health.py software/sheregesh/deploy/installer.py`
должен быть пуст; новые элементы GStreamer — сверить с таблицей §2 (`gst-inspect-1.0 --exists ЭЛЕМЕНТ` на плате).
