<!--
SPDX-FileCopyrightText: 2026 Sheregesh contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->
# Лабораторные источники видео: тестовая камера shcam

Физически на стенде одна камера — **shcam**: Raspberry Pi 5 + IMX477 (разъём CAM0). Всё остальное
(аналоговый CVBS, HDMI, USB-захваты) эмулируется на ней же, с картинкой с этой камеры.

| Что | Где на shcam | Чем видно |
|---|---|---|
| RTSP H.264 | `rtsp://<shcam>:8554/cam` — Baseline 1280×720@30, 3 Мбит/с (mediamtx, `softwareH264`) | источник `rtsp` блока |
| CVBS (аналог) | vivid-000: вход «S-Video» `/dev/shcam/cvbs-cap`, PAL 720×576i25 / NTSC 720×480i29.97 | `sheregesh.videoinput` → `CVBS` |
| HDMI | vivid-001: вход «HDMI» `/dev/shcam/hdmi-cap`, DV timings (по умолчанию 1280×720p30) | `sheregesh.videoinput` → `HDMI` |
| USB-захваты и камера | эмулятор USB/IP `usbip_uvc.py`, порт 3240: `1-1` HDMI→USB `534d:2109`, `1-2` CVBS→USB `534d:0021`, `1-3` камера `1d6b:0102` | на хосте с `vhci-hcd`: настоящий `uvcvideo` → `HDMI` / `CVBS` / `USB` |

**Адрес и параметры `/cam` не менять**: на них держатся результаты S5 (`docs/acceptance/s5-video/hw/shcam/`)
и живое видео на пульте. Подачи vivid читают этот же поток как обычные клиенты и работают с пониженным
приоритетом (`Nice=10`, `CPUWeight=50`); если они мешают потоку, их останавливают первыми.

## Как устроены эмулированные входы

vivid загружен двумя экземплярами (`shcam/etc/modprobe-shcam-vivid.conf`), у каждого — вход и выход. Вход
подключён к своему выходу (управляющий параметр `*_is_connected_to=2`; в ядре 6.18 параметра `loop_video` больше
нет). Служба `shcam-feed@cvbs|hdmi` берёт поток `/cam`, приводит к формату узла и пишет в выход — на входе
появляется картинка камеры с меткой времени.

Тонкость аналога: выход и вход должны совпадать по полю. `v4l2sink` при `field-order` в caps выставляет
`Interlaced-TB/BT`, а вход vivid — `Interlaced`; при несовпадении петля отдаёт шум. Поэтому `shcam-feed` чередует
поля элементом `interlace`, а `capssetter` убирает `field-order` (и задаёт PAR 54/59 для PAL, 11/10 для NTSC).

## Управление (`shcam-sig`)

```
shcam-sig status
shcam-sig cvbs|hdmi ok | nosignal | nolock | cable-out | testpattern
shcam-sig hdmi outofrange
sudo shcam-sig cvbs pal|ntsc
sudo shcam-sig hdmi 1920x1080p30
```

| Команда | Что видит драйвер / `sheregesh.videoinput` |
|---|---|
| `ok` | картинка камеры, статус входа `ok` |
| `nosignal`, `nolock`, `outofrange` | `V4L2_IN_ST_NO_SIGNAL` / `NO_*_LOCK` → «no signal» |
| `cable-out` | вход ни к чему не подключён, **статус остаётся `ok`** — как у захватов, которые без сигнала шлют синее или чёрное поле и не сообщают о потере |
| `testpattern` | таблица генератора vivid вместо камеры |
| `pal`/`ntsc`, режим HDMI | подача останавливается, узлы перенастраиваются, подача стартует в новом формате |

Начальное состояние после загрузки — `/etc/default/shcam-video` (`SHCAM_CVBS_STD`, `SHCAM_HDMI_MODE`,
`SHCAM_FEED_SRC=…/cam` или `test`).

## USB-устройства по USB/IP

`usbip_uvc.py` (служба `shcam-usbip-uvc`) изображает USB-видеоустройства UVC 1.0 и отдаёт их по протоколу USB/IP.
Хост, подключивший устройство, видит обычное USB-устройство: драйвер `uvcvideo`, sysfs, `by-id`/`by-path`,
подключение и отключение. Кадры — с потока `/cam` (MJPEG через `jpegenc` или YUYV), поток идёт через bulk-endpoint.
Модули ядра `dummy_hcd` и `v4l2loopback` в Pi OS отсутствуют, поэтому устройство эмулируется в пользовательском
пространстве, а не гаджетом ядра.

```
sudo python3 /usr/local/lib/shcam/usbip_attach.py attach <shcam> 1-1    # на хосте (root): HDMI→USB
python3 usbip_attach.py list <shcam>                                     # что предлагает сервер
sudo python3 usbip_attach.py detach <порт vhci>                          # отключить
shcam-sig usb hdmi-ms2109 blue | black | freeze | ok | unplug            # на shcam
```

| Профиль | `sheregesh.videoinput` | Форматы |
|---|---|---|
| `hdmi-ms2109` (`1-1`) | `HDMI` (по `HDMI_USB_IDS`) | MJPEG 1920×1080, 1280×720, 640×480 @30; YUYV 640×480@30 |
| `cvbs-ms2106` (`1-2`) | `CVBS` (по `CVBS_USB_IDS`) | MJPEG/YUYV 720×576@25, 720×480@30 |
| `camera` (`1-3`) | `USB` | MJPEG 1280×720, 640×480 @30; YUYV 640×480@30 |

`blue`/`black` — однотонное поле (так MS2109 показывает отсутствие сигнала), `freeze` — повтор последнего кадра,
`unplug` — разрыв USB/IP: хост видит `USB disconnect`, файл состояния возвращается в `ok`, повторный `attach`
«вставляет» устройство обратно. Проверено на самой shcam через `vhci` (2026-10-25): MJPEG 1280×720 ≈30 к/с,
YUYV 640×480, все сбои. Сервер слушает всю сеть стенда без проверки клиента — только для лаборатории.

## Установка

Карта: Raspberry Pi OS Lite 64-bit (trixie), cloud-init `user-data`: hostname `shcam`, пользователь с sudo и ключом
SSH; в `config.txt` — `camera_auto_detect=0` и `dtoverlay=imx477,link-frequency=909000000,cam0`.

mediamtx ставится вручную (бинарник релиза проверяется по `checksums.sha256`):

```
B=https://github.com/bluenviron/mediamtx/releases/download/v1.21.3
curl -sSfLO $B/mediamtx_v1.21.3_linux_arm64.tar.gz && curl -sSfLO $B/checksums.sha256
grep linux_arm64 checksums.sha256 | sha256sum -c && sudo mkdir -p /opt/mediamtx &&
  sudo tar -xzf mediamtx_v1.21.3_linux_arm64.tar.gz -C /opt/mediamtx
```

Остальное — с машины разработчика:

```
SHCAM_SSH_OPTS="-o UserKnownHostsFile=$HOME/.ssh/known_hosts.shcam" \
  tools/lab-video/shcam/install.sh cam@192.168.1.193 [--with-mediamtx]
```

`--with-mediamtx` дополнительно кладёт `shcam/etc/mediamtx.yml` и `shcam/units/mediamtx.service`
(`TimeoutStopSec=5`: процесс камеры `mtxrpicam` не завершается по SIGTERM, без тайм-аута остановка висит 90 с).

## Ограничения

- vivid — не USB: подключение и отключение USB и ссылки `by-id` проверяются через эмулятор USB/IP, не через vivid.
- В эмуляторе у всех профилей вход UVC — «камера»: внешний терминал «композитный разъём» `uvcvideo` в цепочку не
  берёт. V4L2-вход поэтому называется `Camera 1` и у CVBS-профиля; ТВ-стандартов (`VIDIOC_S_STD`) у UVC нет.
- Оба экземпляра vivid получают одинаковый `by-path` (`platform-vivid.0-…`); выбирать вход — по `/dev/shcam/*` или `/dev/videoN`.
- Для аналогового входа `sheregesh.videoinput` перечисляет режимы 640×480/1280×720/1920×1080 (vivid отдаёт непрерывный
  диапазон размеров), а не 720×576 стандарта.
- На Pi 5 служебные узлы камеры `rp1-cfe` попадают в список захватов `sheregesh.videoinput` (их нет в `AUX_DRIVERS`).
- shcam без радиатора греется до ~64 °C под нагрузкой; питание общее с pi1, USB-PD не согласован.
