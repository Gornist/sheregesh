<!--
SPDX-FileCopyrightText: 2026 Sheregesh contributors
SPDX-License-Identifier: GPL-3.0-or-later
-->
# Контракт эмулированных источников shcam для софта шерегеша

Что видит V4L2 на эмулированных входах shcam (Raspberry Pi 5 + IMX477, ядро 6.18.50+rpt-rpi-2712). Всё измерено
на плате 2026-10-25, а не выведено из документации. Устройство — [README.md](README.md). Сырой вывод
`python3 -m sheregesh.videoinput` с подключёнными устройствами: [`samples/videoinput-shcam-2026-10-25.txt`](samples/videoinput-shcam-2026-10-25.txt)
и `.json` (v2 80b916e).

## 1. Устройства USB/IP (`usbip_uvc.py`, порт 3240)

Общее для всех трёх:
- USB 2.0 high-speed, класс устройства 0xEF/0x02/0x01 (IAD), одна конфигурация, UVC **1.00**.
- Интерфейс 0 — VideoControl. Цепочка: входной терминал id 1 `ITT_CAMERA` (0x0201) → выходной терминал id 2
  `TT_STREAMING`. Блока обработки и interrupt-endpoint нет, управлений (`bmControls`) нет, все запросы класса к VC → STALL.
- Интерфейс 1 — VideoStreaming, только alt 0, **bulk** IN 0x81, 512 байт. Probe/commit 26 байт,
  `dwMaxPayloadTransferSize` 16384. Остановка потока — `CLEAR_FEATURE(ENDPOINT_HALT)`.
- V4L2: драйвер `uvcvideo`, вход `Camera 1`, `ENUMINPUT.status` всегда 0. `QUERYSTD`, `QUERY_DV_TIMINGS` → ENOTTY.
- На каждый кадр одна частота (`bFrameIntervalType` 1). NV12 и H.264 нет. Только MJPEG и YUYV (GUID YUY2).

| busid | Профиль | VID:PID | bcdDevice | iManufacturer / iProduct / iSerial | `videoinput` |
|---|---|---|---|---|---|
| 1-1 | `hdmi-ms2109` | 534d:2109 | 21.00 | MACROSILICON / USB Video / — | `HDMI` |
| 1-2 | `cvbs-ms2106` | 534d:0021 | 1.21 | MACROSILICON / AV TO USB2.0 / — | `CVBS` |
| 1-3 | `camera` | 1d6b:0102 | 1.00 | sheregesh / shcam UVC camera / shcam-0001 | `USB` |

Форматы (индекс формата / индекс кадра по порядку):

| Профиль | Формат 1 | Формат 2 |
|---|---|---|
| `hdmi-ms2109` | MJPEG: 1920×1080@30, 1280×720@30, 640×480@30 | YUYV: 640×480@30 |
| `cvbs-ms2106` | MJPEG: 720×576@25, 720×480@30 | YUYV: 720×576@25, 720×480@30 |
| `camera` | MJPEG: 1280×720@30, 640×480@30 | YUYV: 640×480@30 |

Ссылки на устройства: `/dev/v4l/by-id/usb-MACROSILICON_USB_Video-video-index0`,
`usb-MACROSILICON_AV_TO_USB2.0-video-index0`, `usb-sheregesh_shcam_UVC_camera_shcam-0001-video-index0`.
`by-path` — `platform-vhci_hcd.0-usb-0:<порт>:1.0-video-index0`. `bus_info` — `usb-vhci_hcd.0-<порт>`.
Измерено: MJPEG 1280×720 около 30 кадр/с (`v4l2-ctl` 31,2 кадр/с), YUYV 640×480 тоже идёт.

**Насколько это похоже на настоящие адаптеры.** Идентификаторы взяты из ваших `HDMI_USB_IDS` / `CVBS_USB_IDS`.
По `usb.ids` `534d:0021` — «MS210x Video Grabber [EasierCAP]», то есть UVC-захват CVBS на MacroSilicon. Режимы
профилей — предположение, не снимок настоящего устройства. Что важно для логики PAL/NTSC: **UVC-захват CVBS не даёт
`S_STD`/`QUERYSTD`**, стандарт виден только по выбранному кадру (720×576@25 = PAL, 720×480@30 = NTSC). Если
пришедший 27–28.10 адаптер окажется `534d:0021`, выбор PAL/NTSC для него нужно делать по размеру кадра, а не
через `norm=`. Снимок его дескрипторов (`lsusb -v -d 534d:0021`) стоит сравнить с этим профилем и поправить профиль
(`PROFILES` в `usbip_uvc.py`).

## 2. vivid (эмулированные входы на самой shcam)

| | Аналог | HDMI |
|---|---|---|
| Узел | `/dev/shcam/cvbs-cap` (номер `/dev/videoN` меняется от загрузки к загрузке) | `/dev/shcam/hdmi-cap` |
| sysfs | драйвер `vivid`, `bus_info` `platform:vivid-000`, `by-id` нет | `platform:vivid-001`, `by-id` нет |
| `by-path` | **нет** (оба экземпляра дают `platform-vivid.0-…`, ссылка достаётся второму) | `platform-vivid.0-video-index0` (так и есть, не `.1`) |
| Вход | «S-Video 000-0», тип Camera, caps `V4L2_IN_CAP_STD` | «HDMI 001-0», caps `V4L2_IN_CAP_DV_TIMINGS` |
| `videoinput` | `CVBS`, стандарты от NTSC до SECAM, текущий PAL | `HDMI` |
| Режимы в `videoinput` | 640×480/1280×720/1920×1080@25 (vivid даёт непрерывный диапазон), **не** 720×576 | то же @30 |

Сбои (`shcam-sig cvbs|hdmi <режим>`). ENUMINPUT `status`: 0x2 — `V4L2_IN_ST_NO_SIGNAL`, 0x100 — `V4L2_IN_ST_NO_H_LOCK`.

| Режим | Аналог: status / QUERYSTD / кадры | HDMI: status / QUERY_DV_TIMINGS / кадры |
|---|---|---|
| `ok` | 0 / 0xff (PAL) / кадры камеры | 0 / 1280×720 / кадры камеры |
| `nosignal` | **0x2** / 0 / кадры **идут** (шум) | **0x2** / **ENOLINK** / **кадров нет** (DQBUF ждёт) |
| `nolock` | **0x100** / 0 / кадры идут (шум) | **0x100** / **ENOLCK** / кадров нет |
| `outofrange` | — | **0x100** / **ERANGE** / кадров нет |
| `cable-out` | 0 / 0xff / кадры идут (шум, яркость около 99) | 0 / 1280×720 / кадры идут (шум) |
| `testpattern` | 0 / 0xff / таблица vivid | то же |

То есть: аналог vivid без сигнала продолжает отдавать кадры (шум) и сообщает об этом только флагами статуса. HDMI vivid
без сигнала кадры прекращает и сообщает через статус и `QUERY_DV_TIMINGS`. Смена режима HDMI
(`sudo shcam-sig hdmi 1920x1080p30`) меняет DV timings на входе: подача останавливается, оба узла перенастраиваются.
Сигналы «смена источника» (`V4L2_EVENT_SOURCE_CHANGE`) отдельно не проверял.

## 3. Сбои USB-устройств (`shcam-sig usb <профиль> <режим>`)

| Режим | Что видит хост |
|---|---|
| `ok` | кадры камеры |
| `blue` / `black` | кадры **идут**, однотонное поле (синее: RGB около (15, 14, 240)). Статус входа 0: UVC о потере сигнала не сообщает, как MS2109 |
| `freeze` | кадры идут, данные побайтно одинаковые, временные метки растут |
| `unplug` | соединение USB/IP рвётся → `vhci_hcd: disconnect device`, `usb 5-N: USB disconnect`, **узел `/dev/videoN` и ссылки by-id/by-path исчезают**. Идущий поток: `VIDIOC_DQBUF` → **EINVAL** у `v4l2-ctl` (не ENODEV). Повторный `attach` возвращает устройство (номер узла может совпасть) |
| поток камеры недоступен | URB не завершаются: DQBUF ждёт, кадров нет, ошибки нет |

## 4. Подключение к pi2

Эмулятор готов: служба `shcam-usbip-uvc` на `192.168.1.193:3240` (DHCP), работает после перезагрузки.

На хосте-потребителе нужен **root**, иначе не сделать `modprobe vhci-hcd` и запись в
`/sys/devices/platform/vhci_hcd.0/attach`. Пакет `usbip` не нужен, достаточно `usbip_attach.py`. Модули
`vhci-hcd` и `usbip-core` в ядре pi2 (6.18.50+rpt-rpi-v8) **есть**. У `sheregesh-ui` sudo нет, поэтому без
изменения образа (или разового root вручную) pi2 подключиться не может. Помощник root для pi2 (п. 7) в эту часть
не входит: владелец ограничил её Pi 5 с камерой и её софтом; решение — за владельцем.
