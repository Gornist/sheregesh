# S4 — диагностика USB-захватов после доставки (27.10.2026), ≤ 10 мин на адаптер

Дополняет [`../video-ingest/HW-PLAN.md`](../video-ingest/HW-PLAN.md) (шаги P0–P4, D1–D6 остаются в силе) тем, что
изменилось к v2 59ea0c2 и S4: фактический набор утилит в образах, CVBS PAL/NTSC, режимы HDMI, нагрузка CPU, выбор
источника без правки юнита. **Микросхемы и VID:PID до доставки не утверждаются**: таблица гипотез
`video_hw_probe.py` и [`VIDEO_CAPTURE_COMPAT.md`](../../hardware/VIDEO_CAPTURE_COMPAT.md) — только подсказка, решает
`lsusb` и драйвер на месте. Условия — HW-PLAN §0 (разрешение владельца на каждую плату, OTA в покое, без root).

## 1. Утилиты: что есть в образах и на платах

Образы: `build/image/ci-57e8fe4-dev/*.packages.tsv` (pi2, слот A) и `build/image/devbox-b1d95a2/*.packages.tsv`
(pi1, слот B) — **списки пакетов совпадают** (588 строк, отличается только версия `sheregesh`). Проверка на платах —
чтение от `sheregesh-ui`, 25.10 12:30.

| Нужно | Пакет | В образах | На платах (чтение) | Чем пользоваться |
|---|---|---|---|---|
| `lsusb`, `lsusb -t`, `usb-devices` | `usbutils` 1:018-2 | есть | работают на pi1 и pi2; имена производителей показываются (файла `usb.ids` нет — имена из hwdb udev) | как есть |
| `v4l2-ctl --list-devices`, `--list-formats-ext`, `--get-standard`, `--get-input` | `v4l-utils` | **нет** (есть только библиотеки `libv4l-0t64`, `libv4lconvert0t64` 1.30.1) | нет | [`video_hw_probe.py`](../../../tools/probe/video_hw_probe.py) `nodes` / `--json nodes` — QUERYCAP, форматы, размеры, интервалы, входы, ТВ-стандарты для узлов на USB **любого** драйвера; `python3 -m sheregesh.videoinput [--json]` из выпуска (с c6b6abc видит любой захват V4L2, не только `uvcvideo`) |
| `media-ctl` | `v4l-utils` | нет | нет | не нужен для USB-захватов |
| `gst-device-monitor-1.0` | `gstreamer1.0-plugins-base-apps` | нет | нет | `video_hw_probe.py nodes` |
| `dmesg` | `util-linux` | есть | работает от `sheregesh-ui` (`kernel.dmesg_restrict = 0` на pi2; на pi1 — вывод получен) | `dmesg -T \| tail -50` после подключения |
| `journalctl -k` | `systemd` 257 | есть | работает (`sheregesh-ui` в группе `adm`) | `journalctl -k -b --no-pager \| grep -iE 'usb\|uvc\|usbtv\|stk1160\|em28xx\|video'` — ошибки USB: `error -71/-110`, `reset`, `disconnect`, `over-current`, `not enough bandwidth` |
| `modinfo` | `kmod` | есть | `/usr/sbin/modinfo` — **не в PATH** `sheregesh-ui`, вызывать полным путём | модули в ядре 6.18.50+rpt-rpi-v8 **есть как файлы** (pi2): `uvcvideo`, `usbtv`, `stk1160`, `em28xx` (+ `gspca_*`, `cx231xx`, `hdpvr`, `go7007`, `pwc`). Загрузку проверяет только D1 (HW-PLAN P3 — гипотеза снята частично) |
| `udevadm info` | `udev` | есть | есть | `udevadm info -q property /dev/videoN \| grep -E 'ID_(VENDOR\|MODEL\|PATH\|SERIAL)'` — постоянные by-id/by-path |
| `gst-launch-1.0`, `gst-inspect-1.0` | `gstreamer1.0-tools` | есть | есть. Элементы pi2: `v4l2src`, `jpegdec`, `v4l2jpegdec`, `v4l2h264enc`, `v4l2h264dec`, `v4l2convert`, `videoconvert`, `rtspsrc` — есть; `x264enc`, `avdec_h264`, `rtspclientsink` — **нет**; `python3-gi` — **нет** | кодирование только аппаратное `v4l2h264enc`; RTSP-сервер на платах средствами GStreamer не поднять |
| CPU, температура, троттлинг | `procps`, `raspi-utils` | есть (`top`, `ps`, `vcgencmd`); `sysstat`/`mpstat`/`pidstat`/`htop` — нет | есть | `check_video_link.py --only resources` (60 с, /proc/stat, с режимом без видео) или `top -b -d 5 -n 3` |
| дескрипторы, сокеты | `lsof`, `iproute2` (`ss`) | `lsof` нет; `ss` на платах не найден | нет | `/proc/PID/fd`, `/proc/net/tcp*` (так делают `e2e_run`, `check_link`) |

## 2. Подготовка (один раз, ~3 мин)

HW-PLAN P0–P3 без изменений, с поправками:

- **P1 (без устройства)** — `$R2 "lsusb; lsusb -t; $P usb; $P nodes --no-probe; journalctl -k -b --no-pager -q | grep -ciE 'usb.*(error|fail|reset)'"`.
  Ожидается: корневые концентраторы и `2109:3431` (концентратор VIA на плате Pi 4), 14 узлов платформы, 0 ошибок USB.
  Так было 25.10 на обеих платах.
- **P3** — `$R2 'for m in uvcvideo usbtv stk1160 em28xx; do /usr/sbin/modinfo -F filename $m; done'`.
- **P4 — снято**: в v2 источник выбирается **без правки юнита** — файлом `~/.config/sheregesh/video-input.json` рядом с
  `link.json` и SIGHUP службе блока (`agent.py`: `video_spec`, `reload_video`; смена гарантированно останавливает
  старый конвейер до запуска нового). Удаление файла + SIGHUP источник **не** возвращает (конфигурация «not found» не
  перечитывается) — возврат явной записью `{"source": "synthetic"}`.

```bash
vin() { ssh sheregesh-ui@$PI2 "umask 077; printf '%s' '$1' > ~/.config/sheregesh/video-input.json && \
  kill -HUP \$(systemctl show -p MainPID --value sheregesh-remote-net)"; }
src() { ssh sheregesh-ui@$PI2 'python3 -c "import json;print(json.load(open(\"/run/sheregesh-remote/link-status.json\"))[\"video\"][\"source\"])"'; }
vin '{"source":"v4l2","device":"","mode":"auto","format":"auto","standard":"auto"}'; sleep 4; src
```

`device` пустой — первый найденный захват; иначе by-id/by-path из `video_hw_probe.py nodes`. Формы описания —
`videoinput.open_source`: `v4l2|uvc` (`device`, `mode` `auto|WxH@fps`, `format` `auto|yuyv|uyvy|mjpeg|h264`,
`standard` `auto|pal|ntsc`), `rtsp` (`url`, `codec`, `latency_ms`, `timeout_s`), `synthetic`.

**Выбор источника в GUI** пульта в v2 59ea0c2 отсутствует (GUI только показывает источник). Если выпуск S4 (агент
C, `s4/gui`) добавит выбор — дополнительный шаг G: выбрать источник в GUI, убедиться, что на блоке `src` сменился, и
снять `gui_capture --by video` (рендеринг, не ui-state). Каким каналом пульт меняет источник блока — открытый вопрос.

## 3. Адаптер — не более 10 минут

Шаги D1–D6 HW-PLAN; ниже — что добавляется по типу входа. Время по часам Mac — в `$D/steps.txt`.

### Общие (все адаптеры), 4 мин

| # | Мин | Действие | Запись |
|---|---|---|---|
| U1 | 1 | подключить; `lsusb; lsusb -t; $P usb; dmesg -T \| tail -30; journalctl -k -b -q \| grep -iE 'usb.*(error\|fail\|reset)'` | VID:PID, скорость (480/5000), драйверы интерфейсов, ошибки USB при перечислении. Нет драйвера → «несовместимо (нет драйвера)», дальше не идти |
| U2 | 1 | `$P nodes; $P --json nodes > $D/nodes.json`; `udevadm info -q property /dev/videoN \| grep ID_` | узел захвата, драйвер, форматы, **все** размеры/частоты (замена `--list-formats-ext`), входы, стандарты |
| U3 | 1 | источник в службу блока: `vin '{"source":"v4l2",…}'`; `src` через 4 с; `$R2 'pgrep -c -f "gst-launch-1.0.*rtpstreampay"'` | `state` (`streaming`/`no-signal`/`no-device`/`error`), `format`, `width×height`, `fps_actual`, ровно 1 конвейер |
| U4 | 1 | `python3 tools/probe/check_video_link.py --console $PI1 --remote $PI2 --out $D/res --only resources` (60 с) | CPU pi2/pi1 (одно ядро = 100 %), RAM, температура, троттлинг при потоке с захвата — сравнение с synthetic (pi1 3,0 % / pi2 0,5 % без видео) |

### CVBS / S-Video (аналоговые), +5 мин

| # | Мин | Действие | PASS | Наблюдение (не критерий) |
|---|---|---|---|---|
| C1 | 1,5 | PAL: `vin '{"source":"v4l2","standard":"pal","mode":"auto"}'`; `src`; `$P frames --device /dev/videoN --caps 'video/x-raw,format=YUY2,width=720,height=576,framerate=25/1' --seconds 10` (перед `frames` устройство освободить: `vin '{"source":"synthetic"}'` — Ingest закрывает узел; потом вернуть) | `streaming`, 720×576, ≈ 25 к/с | `standard` в `src`; поле чересстрочное — артефакты гребёнки |
| C2 | 1,5 | NTSC — то же с `"standard":"ntsc"`, 720×480, 30000/1001 — **только если источник умеет NTSC**; иначе записать «источник только PAL» | `streaming`, 720×480, ≈ 29,97 к/с | что делает драйвер при несовпадении стандарта (кадры с полосами, `no-signal`, `error`) |
| C3 | 2 | потеря/возврат RCA при выбранном источнике, опрос HW-PLAN D5 (ui-state pi1 + link-status pi2): снять сигнал на 15 с, вернуть | пульт «нет видео»/`no-signal` ≤ 3 с; `streaming` и `live` ≤ 10 с после возврата без переподключения USB | сообщает ли драйвер потерю (`NO_H_LOCK` → `no-signal` без перезапуска конвейера, 055c5d1) или кадры просто прекращаются; шлёт ли «синее поле» — тогда пульт показывает его как видео (FAIL по смыслу, вывод для B) |

### HDMI, +5 мин

| # | Мин | Действие | PASS | Наблюдение |
|---|---|---|---|---|
| H1 | 1,5 | фактические режимы: из U2 — список FourCC × размер × частота; `vin` с `mode` 1280x720@30, затем 1920x1080@30 (если есть); `src` | `streaming`, `fps_actual` ≈ заявленной | какие режимы в MJPEG, какие в YUYV (YUYV высоких режимов на USB 2.0 обычно с малой частотой — проверить, не утверждать); `format auto` что выбрал |
| H2 | 0,5 | сменить режим источника HDMI (ноутбук 720p ↔ 1080p) при работающем потоке | поток продолжается или восстанавливается ≤ 10 с | масштабирует ли захват сам (размер потока не меняется) |
| H3 | 2 | потеря/возврат HDMI-кабеля (опрос D5): снять на 15 с, вернуть | «нет видео»/`no-signal` ≤ 3 с; `live` ≤ 10 с после возврата | захваты без статуса входа часто шлют синее/чёрное поле — тогда на блоке `streaming`, на пульте «видео»: записать как ограничение (`videoingest`: детектора пустого кадра нет) |
| H4 | 1 | горячее отключение USB на 10 с (D6) | `no-device` → `streaming` ≤ 10 с, конвейер 1, NRestarts прежний, связь не рвалась | вернулся ли узел под другим `/dev/videoN` (поиск по by-id/by-path) |

Итого по адаптеру: 4 + 5 = 9 мин (+1 мин запас). Задержку «стекло-стекло» — только для HDMI, оценка по HW-PLAN §3.
После адаптера: `vin '{"source":"synthetic"}'`, отключить, `$P nodes --no-probe > $D/after.txt`.

## 4. Протокол

Сырые выводы — `docs/acceptance/video-ingest/hw/<имя по итогу U1>/`; сводка — `hw/RESULTS.md`; карточка — в
`VIDEO_CAPTURE_COMPAT.md` (§5 перечня), только из измерений. В отчёт S4 — раздел «Аппаратные проверки».

## 5. Открытые вопросы

1. ~~Таблица «Что принимает ПО v2» в `VIDEO_CAPTURE_COMPAT.md` устарела~~ — обновлена под 59ea0c2 (блок: любой V4L2, UYVY, PAL/NTSC, `no-signal`, выбор файлом; пульт — только `uvcvideo`).
2. Где подключать захват в целевой схеме — pi2 (план) или pi1 (локальный вход S3.3). План для pi2.
3. Есть ли на стенде композитный источник с NTSC и HDMI-источник с движущимся изображением — без них C2/H1–H3 BLOCKED (no source).
4. Выбор источника из GUI пульта: канал (команда протокола? файл?) не определён в v2; ждём решение B/C.
