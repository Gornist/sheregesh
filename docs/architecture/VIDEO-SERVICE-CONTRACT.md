# Контракт видеослужбы «Шерегеш» (ADR-0016, этап 0)

**Версия контракта:** 10 · **Дата:** 2026-10-29 · **Связано:** ADR-0016, ADR-0015, ADR-0011, ADR-0007.

Этот документ — интерфейс между исполнителями. Код пишется против него; изменение контракта — только через главного
инженера (новая версия документа, запись в журнале изменений внизу).

## 1. Модель

Видео — две самостоятельные службы systemd. Каждая владеет своим конвейером целиком; агент блока и GUI пульта —
только клиенты по локальному сокету.

| | `sheregesh-video-tx` (pi2, роль remote) | `sheregesh-video-rx` (pi1, роль console) |
|---|---|---|
| Владеет | выбором и открытием источника (`videoinput`, `videoingest`, каталог), кодером или сквозным H.264, RTP, SRTP, отправкой, RTCP | приёмом UDP, SRTP, джиттер-буфером, разбором, аппаратным декодером, раздачей кадров |
| Не владеет | сеансом SHRG-LAB, ключами (получает от агента) | сеансом SHRG-LAB, ключами (получает от GUI), показом кадра |
| Клиенты | агент блока (`sheregesh_remote.agent`) | GUI пульта (`sheregesh_console`), инструменты диагностики |
| Выход | UDP SRTP на адрес пульта; состояние | `unixfdsink` (dmabuf, ADR-0015) для GUI; состояние |

**Показ кадров на пульте (обязательства перед GUI, ADR-0015).** Служба rx всегда отдаёт декодированные кадры
в `unixfdsink` (`/run/sheregesh-video/frames-<stream>.sock`, dmabuf v4l2h264dec) и не зависит от того, есть ли
дисплей. Способ показа выбирает GUI, как сейчас:
- **GL** — в процессе GUI `unixfdsrc → glupload → glcolorconvert → qml6glsink`, только при окне на экране и
  плагинах; `VideoReceiver._gl_reconcile` раз в секунду переключает путь;
- **RGBx (без дисплея, запасной)** — дочерний процесс GUI `gst-launch unixfdsrc → videoscale → videoconvert →
  RGBx → fdsink`, кадры читает прежний `VideoReceiver._reader`; так видео проверяется OTA confirm роли console.

Сохраняются без изменений: проба кадров на входе приёмника GL (`_gl_frame`) и `FRAME_WAKE`; поля `ui-state.json`
и `metrics()` — `video.decoded_fps_5s`, `video.link.{received_fps_5s, transport_latency_ms, decode_latency_ms,
seq_gaps}`, `videoSource.{display, live, input}` (их заполняет GUI из `state.json` службы rx и своих счётчиков);
пробы `check_video_link`, `ui_perf`, `ui_minimal_accept`; тесты `tests/unit/test_console_glvideo.py`,
`tests/gui/test_gui_minimal.py`. Для путей TLS (резерв) GUI работает как сейчас.

Поток описывается именем `stream` (строка `[a-z0-9-]{1,16}`). Сейчас один поток `main`; API и сообщения с первого
дня принимают имя потока, второй поток добавляется без смены формата.

Python в службах только управляет: строит конвейер через python3-gi, задаёт свойства, читает статистику не чаще
раза в секунду, обслуживает сокет. Пробников на каждый RTP-пакет нет. Разрешён один пробник на выходе декодера
(не чаще частоты кадров) в службе rx.

## 2. Автомат состояний потока

```
          start            первый кадр             нет кадров > 1 с         кадры снова
 idle ─────────► starting ───────────► streaming ─────────────────► degraded ──────────► streaming
  ▲                 │                     │  ▲                         │
  │ stop            │ ошибка конвейера    │  └──── ключевой кадр ──── recovering ◄── PLI отправлен
  │                 ▼                     ▼                          (ждём IDR)
  └──────────── error (откат: 1, 2, 4, 8, 16, 30 с; после 6 подряд — stuck, ждёт stop/start)
```

- `idle` — нет команды `start` или была `stop`; конвейера и ключа нет.
- `starting` — конвейер построен, кадров ещё нет. Таймаут 5 с → `error`.
- `streaming` — кадры идут (tx: кодер отдаёт; rx: декодер выдал кадр не позже 1 с назад).
- `degraded` — rx: кадров нет > 1 с при живом конвейере. rx шлёт PLI и переходит в `recovering`.
- `recovering` — rx ждёт ключевой кадр; PLI повторяется раз в 1 с, не более 5 раз, затем `degraded` и снова цикл.
  tx: получил PLI → запрос ключевого кадра кодеру; при сквозном H.264 — отметка `keyframe_unsupported` (§7).
- `error` — конвейер упал. Перезапуск **внутри службы** с откатом; ключ и поколение не меняются, если упала только
  приёмная часть rx; у tx любой перезапуск конвейера = новое поколение (§5, требуется новый ключ от агента).
- Переходы пишутся в `transitions` состояния (последние 20) с временем и причиной.

## 3. API службы: сокет управления

- Путь: `/run/sheregesh-video/ctl.sock` (`RuntimeDirectory=sheregesh-video`, режим каталога 0700, владелец
  `sheregesh-ui`). Проверка `SO_PEERCRED`: uid клиента == uid службы, иначе соединение закрывается без ответа.
- Кадрирование: JSON UTF-8, одна строка — одно сообщение, не длиннее 16 КиБ.
- Запрос: `{"v": 1, "id": <int>, "cmd": "<имя>", "args": {...}}`. Ответ: `{"v": 1, "id": <int>, "ok": true,
  "result": {...}}` или `{"v": 1, "id": <int>, "ok": false, "error": "<код>", "detail": "<текст>"}`.
- Коды ошибок: `bad-request`, `unknown-cmd`, `unknown-stream`, `stale` (сеанс или поколение старее текущего),
  `busy` (поток в `starting`), `pipeline` (конвейер не построен), `unsupported` (нет элемента или режима),
  `forbidden` (нарушение §6).

Команды tx:

| cmd | args | Смысл |
|---|---|---|
| `start` | `stream, session, gen, ssrc, peer {ip, port, rtcp_port}, srtp {cipher, key_b64}, source <spec>, profile {width, height, fps, bitrate}` | запустить поток поколения `gen` |
| `stop` | `stream, reason` | остановить, уничтожить ключ |
| `select-source` | `stream, source <spec>` | сменить источник; ответ содержит `need_new_gen: true` — агент обязан выдать новый `start` |
| `keyframe` | `stream` | запросить ключевой кадр |
| `status` | — | то же, что `state.json` |
| `catalog` | — | в этом спринте `unsupported`: каталог и выбор источника остаются в агенте (перенос — следующий спринт); источник приходит в `start` и `select-source` как `spec` агента |

Команды rx:

| cmd | args | Смысл |
|---|---|---|
| `start` | `stream, session, gen, ssrc, peer {ip}, local {port, rtcp_port}, srtp {cipher, key_b64}, codec` | ждать поток поколения `gen` от `peer.ip` |
| `stop` | `stream, reason` | остановить, уничтожить ключ |
| `status` | — | то же, что `state.json` |

Необязательные поля портов (v6): tx `peer.rtcp_local_port` и rx `local.rtcp_peer_port` — когда tx и rx на одном
хосте (испытания); по умолчанию оба 7445. Задержка джиттер-буфера rx — переменная окружения службы
`SHEREGESH_VIDEO_JITTER_MS` (по умолчанию 20; на pi1 0/5/20 мс дали одинаковую задержку).

Правила: `start` с тем же `(session, gen)` повторно — идемпотентен; с `session` или `gen` меньше текущего — `stale`;
ключ никогда не возвращается ни в одном ответе.

## 4. Состояние: `/run/sheregesh-video/state.json`

Пишется атомарно (временный файл + rename) раз в секунду и при смене состояния. Схема v1:

```json
{"v": 1, "role": "tx|rx", "version": "<версия пакета>", "time": 1793000000.0,
 "streams": {"main": {
   "state": "idle|starting|streaming|degraded|recovering|error|stuck",
   "transport": "srtp", "session": "<hex32>", "gen": 3, "ssrc": 123456789, "peer": "192.168.1.92",
   "since_s": 12.3, "frames": 3600, "fps": 30.0, "bitrate_kbps": 6000,
   "rtp": {"packets": 0, "lost": 0, "jitter_ms": 0.0, "reordered": null},
   "srtp": {"auth_fail": null, "replay_fail": null, "unknown_ssrc": 0},
   "rtcp": {"pli_sent": 0, "pli_received": 0, "keyframes_requested": 0, "keyframe_unsupported": false},
   "latency_ms": {"p50": null, "p95": null, "max": null, "samples": 0},
   "last_frame_age_s": 0.03, "restarts": 0, "error": "",
   "source": {}, "nft_dropped": null,
   "transitions": [[1793000000.0, "streaming", "first frame"]]}}}
```

**Доступность метрик (v9).** `null` — метрика не измеряется или ещё не измерена; измеренный ноль — `0`. Читатель
не превращает `null` в `0` (ни в GUI, ни в отчётах, ни в разностях окна): «нет данных» и «ошибок нет» — разные
итоги, и проверка, требующая нуля, при `null` не проходит.
- `rtp.packets`, `rtp.lost`, `rtp.jitter_ms` — `null` с начала поколения до первой статистики `rtpbin`; у tx `lost` и
  `jitter_ms` всегда `null` (это наблюдения приёмника).
- `rtp.reordered`, `srtp.auth_fail`, `srtp.replay_fail` — `null`: текущий конвейер (`srtpdec` без счётчиков,
  `rtpbin` без счётчика перестановок) их не измеряет. Появятся — числом, без смены схемы.
- `srtp.unknown_ssrc`, `rtcp.*` — счётчики службы, с начала поколения `0`.

**Совместимость (v9).** Схема остаётся `v: 1`: изменилось только множество значений — у перечисленных полей `int`
расширен до `int | null` (`jitter_ms` — `number | null`). Состояние прежних служб (нули) читается новыми читателями
как есть; читатели обязаны принимать `null` у этих полей. Новое поколение сбрасывает выборки и счётчики прежнего.

**Что измеряет `latency_ms` (v6, уточнено в v9):** `capture_to_decoded` — «кадр захвачен на блоке → кадр
декодирован на пульте».
- Начало — PTS кадра в источнике tx (часы конвейера tx в шкале NTP), переданный расширением `rtphdrextntp64` на
  каждом пакете (`every-packet`). Это момент захвата, не отправки: в задержку входят кодер, сеть, джиттер-буфер,
  депакетизация и декодер.
- Конец — выход декодера rx (`src`-пад декодера), часы пульта.
- Единицы — мс; окно — последние 600 кадров (`p50`/`p95`/`max`, `samples`).
- Нужна синхронизация часов: смещение `clock_offset_s` — «часы блока (tx) минус часы пульта (rx)», как
  `set-clock-offset.offset_s` (§6, v7); задержка = время декодирования − (метка захвата − смещение). Его сообщает GUI
  командой `set-clock-offset {offset_s}`. До неё и при её потере `latency_ms` = `null`, `samples` = 0. Значение ≤ 0 —
  ошибка смещения или метки, не измерение: приёмка его не принимает.
- Поля `latency_ms` есть только у rx.

На пульте эта метрика публикуется как `link.capture_to_decoded_ms`. Метрики пути TLS — другие и называются иначе:
`transport_latency_ms` — «отправка пакета блоком → приём пультом» (смещение часов по PING/PONG),
`decode_latency_ms` — «последний пакет кадра записан в декодер → кадр RGBx» (часы пульта). У SRTP их нет (`null`,
`samples` 0), у TLS нет `capture_to_decoded_ms`. Сравнивать SRTP и TLS — только по метрике с одинаковыми границами;
сумма `transport_latency_ms + decode_latency_ms` с `capture_to_decoded_ms` не сравнима (нет захвата и кодера).

**Добавление rx:** команда `set-clock-offset {offset_s}` (float).

## 5. Сеанс, поколение, ключ

- `session` — 128 бит случайных (`secrets.token_hex(16)`), создаёт агент блока на каждый новый сеанс управления.
- `gen` — целое, строго растёт в пределах `session`, начиная с 1. Другой `session` вытесняет прежний; вытесненные
  `session` служба помнит (не меньше 16) и отвечает на них `stale`. У tx пара (session, gen) израсходована с запуска
  конвейера: повторный `start` с ней после ошибки или `stop` — `stale` (не расходуется при `no-device` до запуска). Новое поколение — при новом `start` tx: новый
  сеанс, смена источника, любой перезапуск конвейера tx.
- Новое поколение = новый ключ (`secrets.token_bytes`) **и** новый SSRC (`secrets.randbits(32)`, ≠ 0, ≠ прежнему).
  Номера RTP на пути SRTP не переписываются (`RtpContinuity` не используется).
- Шифр по умолчанию `aes-128-gcm` (ключ 16 + соль 12 = 28 байт). Если на GStreamer 1.26.2 GCM не работает в связке
  `srtpenc`/`srtpdec` — `aes-128-icm` + `hmac-sha1-80` (16 + 14 = 30 байт). `null` запрещён.
- rx отдаёт ключ `srtpdec` только для текущего `ssrc` (сигнал `request-key`); пакеты других SSRC отбрасываются
  и считаются в `srtp.unknown_ssrc`.

## 6. SHRG-LAB 1.5: сообщения видеосеанса

`TYPES["1.5"] = TYPES["1.4"] | {"video-key", "video-key-ack", "video-stop", "video-fallback", "video-renew"}`. В `hello` обеих сторон — поле
`video_transports: ["srtp", "tls"]` (что сторона умеет) и у пульта `video_rx_port`.

| Тип | Направление | body |
|---|---|---|
| `video-key` | блок → пульт | `{stream, session, gen, transport: "srtp", ssrc, codec, profile {width, height, fps, bitrate}, srtp {cipher, key_b64}, rtcp_port}` |
| `video-key-ack` | пульт → блок | `{stream, session, gen, ok, error?}` |
| `video-stop` | блок → пульт | `{stream, session, gen, reason}` |
| `video-renew` | пульт → блок | `{stream, session, gen, reason}` — приёмник потерял поколение; блок выдаёт новое (новый ключ и SSRC), не чаще раза в 2 с |
| `video-fallback` | пульт → блок | `{stream, session, gen, reason}` — пульт переходит на TLS (ADR-0011) до конца сеанса управления |

- Пульт принимает `video-key`, только если: сеанс 1.5, `session` — текущий видеосеанс этого сеанса управления (первый
  `video-key` после `hello` его задаёт), `gen` > последнего принятого. Иначе — `video-key-ack {ok: false, error:
  "stale"}`, ничего не применяется.
- Блок шлёт видео поколения `gen` только после `video-key-ack {ok: true}` с тем же `(session, gen)`; ack с другими
  значениями игнорируется.
- Уточнения схемы (v4): `profile.bitrate` — кбит/с, целое; `codec` — `h264` (значения `[a-z0-9]{1,16}` допускаются
  схемой на будущее); `rtcp_port` по умолчанию 7445; `error` в `video-key-ack` — `stale` | `bad-request` |
  `unsupported`, при `ok: true` поля нет; `video-key` того же `gen`, пришедший после `video-stop`, — `stale`; в
  состоянии потока `idle` допустимы `session: ""` и `ssrc: 0`.
- Команды сокета обязаны быть идемпотентными: клиент повторяет запрос один раз после обрыва соединения; обработчик
  вызывается из потока каждого соединения и должен быть потокобезопасным.
- Поле `key_b64` маскируется при любой записи сообщения в журнал (`"key_b64": "***"`); в телеметрию и `state.json` не
  попадает.
- Уточнения v7 (по итогам E1, E2): пульт с `link.json` `video.transport: "tls"` просто не объявляет `srtp` в
  `video_transports` — блок о настройке не знает; отказ `sheregesh-video-allow@` на пульте → `video-key-ack {ok: false,
  error: "unsupported"}` и сразу `video-fallback`; ошибка `start` службы rx → только `video-fallback` (без ack ok:false);
  `set-clock-offset.offset_s` — «часы блока минус часы пульта», секунды; ответ `status` служб — тот же объект, что
  `state.json` (`{"streams": {"<stream>": {…}}}`); после отката `status.video.transport` блока = `"tls"`; блок без
  ack 10 с считает поколение неудачным; в счётчик «5 неудач подряд» блока входят `need-new-gen`, `stuck`, отказ
  `start`, таймаут ack и смена поколения службой; режим видео пульта в `ui-state.json` — `remote-srtp`
  (OTA confirm и пробы принимают и `remote-tls`, и `remote-srtp`).
- **Новое поколение по запросу пульта (v8).** Повторять приёмнику тот же ключ нельзя: SRTP считает пакеты 48-битным
  индексом (16 бит номера RTP + счётчик переполнений ROC), а приёмник, начавший с середины потока, полагает ROC = 0.
  После ~65 тыс. пакетов (≈ 2 мин при 6 Мбит/с) он отбрасывает всё как подложное; после обрыва длиннее ~32 тыс. пакетов
  ROC тоже не угадать. Поэтому при перезапуске службы rx (её `state.json` не про текущее поколение) или если кадров нет
  дольше 5 с (`degraded`/`recovering`/`error`/`stuck`) пульт шлёт `video-renew` (не чаще раза в 10 с), блок отвечает
  новым `video-key`. Найдено на платах 2026-10-27: перезапуск rx через ~2 мин потока — 0 пакетов при 6,3 Мбит/с от tx.
- **Откат решает пульт** (только он видит кадры): нет первого кадра за 5 с после ack или `start` службы rx вернул
  ошибку → пульт шлёт `video-fallback`, останавливает rx (`stop`), ставит `sheregesh-video-allow@none` и
  подключается к TLS-видео блока, как сейчас. Блок на `video-fallback` с текущими (session, gen) останавливает tx
  (`stop`) и только после этого принимает TLS-видеосеанс; с другими (session, gen) — игнорирует.
- **Источником владеет кто-то один.** В режиме SRTP устройство открыто службой tx; агент в это время конвейер TLS
  (`Ingest`) не запускает. Переход на TLS — сначала `stop` службы tx, затем `Ingest` агента. Возврат к SRTP — только
  в новом сеансе управления (Ingest агента остановлен до `start` tx).
- Выбор транспорта при старте видеосеанса: обе стороны объявили `srtp` и `link.json` пульта не задаёт
  `video.transport: "tls"` → SRTP; иначе TLS (ADR-0011, без изменений). Однократный откат на TLS до конца сеанса
  управления, если SRTP не дал первого кадра за 5 с после ack или `start` службы вернул ошибку. Переключения обратно
  внутри сеанса нет. Активный транспорт — в `status.video.transport`.

## 7. Ограничения, известные заранее

- Сквозной H.264 с камеры RTSP: ключевой кадр по PLI не гарантирован (`keyframe_unsupported: true`); время
  восстановления ограничено интервалом ключевых кадров камеры — фиксируется в состоянии и в отчёте.
- Запреты стенда: `v4l2jpegdec`, `GST_TRACERS=latency` с аппаратным кодером, CBR, `v4l2convert` перед кодером.

## 8. nftables и права

- Таблица `inet sheregesh_video`: набор `peer4` (ipv4_addr), набор `peer6` (ipv6_addr), цепочка `input` (hook input,
  priority −10): для `udp dport { 7444, 7445 }` — `ip saddr @peer4 accept`, `ip6 saddr @peer6 accept`, иначе
  `counter drop`. Правило стоит до conntrack-исключений (`ct state` не используется для этих портов), так что
  установленное состояние conntrack обход не даёт. Порты TCP (SSH, SHRG-LAB 7443, TLS-видео 7444/tcp) не трогаются.
- Наборы пусты при загрузке. Две таблицы и два юнита загрузки: `sheregesh-video-console.nft` /
  `sheregesh-video-nft.service` (пульт, порты 7444, 7445) и `sheregesh-video-remote.nft` /
  `sheregesh-video-nft-remote.service` (блок, порт 7445); роль — по `remote.role`. Службы видео связаны с юнитом своей
  таблицы `BindsTo=` + `After=`: нет таблицы — нет службы. Юниты таблиц — `PartOf=nftables.service`: перезапуск
  штатного nftables (`flush ruleset`) перезагружает таблицу с пустыми наборами, то есть закрытой.
- Служба rx проверяет таблицу без root: `systemctl is-active sheregesh-video-nft.service` при `start` и раз в секунду;
  не `active` → поток в `error` с `error: "firewall"`, конвейер остановлен (вдобавок к `BindsTo=` юнита).
- Адрес ставит GUI пульта (адрес блока) и агент блока (адрес пульта) при начале сеанса управления —
  `systemctl start sheregesh-video-allow@<адрес>.service`, в конце — `@none`. Адрес ставит
  `sheregesh-video-allow@<адрес>.service` (oneshot, root): заменяет содержимое `peer4`/`peer6` одним
  адресом; экземпляр `none` очищает наборы. Проверка имени экземпляра — строгий IPv4 или IPv6 (не адрес — отказ).
- polkit (в обычном образе, не только dev): учётная запись `sheregesh-ui` может `start` только юнит-шаблона
  `sheregesh-video-allow@*.service`; на остальное — прежние правила.
- На блоке такая же таблица для `udp dport 7445` (RTCP от пульта).

## 9. Раскладка файлов по трекам (этап 1)

| Трек | Разрешено создавать и менять | Нельзя трогать |
|---|---|---|
| **A** конвейеры | `software/sheregesh/videosvc/__init__.py`, `videosvc/pipelines.py`, `tests/unit/test_videosvc_pipelines.py`, `tests/system/test_videosvc_srtp_loopback.py` | всё остальное |
| **B** протокол и сокет | `software/sheregesh/videosvc/session.py` (сеанс, поколение, ключ, проверка stale, маскирование), `videosvc/ctl.py` (сервер и клиент сокета, SO_PEERCRED), `videosvc/state.py` (схема и атомарная запись состояния), `tests/unit/test_videosvc_session.py`, `tests/unit/test_videosvc_ctl.py`, `tests/unit/test_videosvc_state.py` | `link/codec.py`, `link/client.py`, `agent.py` (это этап 2) |
| **C** образ и права | `tools/image/units/sheregesh-video-tx.service`, `sheregesh-video-rx.service`, `sheregesh-video-allow@.service`, `sheregesh-video-nft.service`, `tools/image/nft/sheregesh-video.nft`, `tools/image/bin/sheregesh-video-allow` (помощник для юнита), правило polkit для обычного образа (новый файл в `os/layer/`), изменения `tools/image/build_deb.py` и `tools/image/verify_image.py` и `os/layer/*.yaml` только для этих файлов, тесты в `tests/tools/` | `software/**` |

`videosvc/__init__.py` создаёт трек A; трек B его не меняет. Точка входа служб (`videosvc/tx.py`, `videosvc/rx.py`) —
этап 2 (трек D), после слияния A и B. Изменения `link/codec.py`, `link/client.py`, `agent.py`, `sheregesh_console/*` —
этап 3, только главный инженер, после согласования с сессией S8.

## Журнал изменений контракта

- v10 (2026-10-29) — §4: знак смещения часов для `latency_ms` приведён к §6 (v7): «часы блока минус часы пульта»;
  прежний текст §4 («приёмник минус передатчик») противоречил §6, а rx вычитал смещение с неверным знаком (стенд
  2026-10-29: p50 от −57 до +132 мс между поколениями); значение ≤ 0 приёмкой не принимается.
- v9 (2026-10-29) — §4: доступность метрик — `null` для неизмеряемых (`reordered`, `auth_fail`, `replay_fail`) и
  ещё не измеренных (`rtp.*` до первой статистики), измеренный ноль — `0`; схема `v: 1` с расширением типов;
  границы `latency_ms` (`capture_to_decoded`) и имя на пульте `link.capture_to_decoded_ms` вместо
  `transport_latency_ms` (обзор 2026-10-28).
- v1 (2026-10-27) — первая версия.
- v8 (2026-10-27) — §6: `video-renew` (пульт → блок) вместо повтора того же ключа приёмнику — счётчик ROC SRTP.
- v7 (2026-10-27) — §6: уточнения по итогам треков E1/E2 (настройка tls на пульте, отказ allow@, ошибка start rx,
  знак смещения часов, схема ответа status, транспорт после отката, неудачи поколения, режим remote-srtp).
- v6 (2026-10-27) — §4: что измеряет `latency_ms` (захват → декодирован); §3: необязательные RTCP-порты,
  `SHEREGESH_VIDEO_JITTER_MS`; §5: порядок сеансов и израсходованные поколения (по итогам трека D).
- v5 (2026-10-27) — §6: `video-fallback` (откат решает пульт), правило «источником владеет кто-то один»; §3: `catalog`
  в этом спринте `unsupported`; §8: проверка таблицы службой rx через `systemctl is-active`, кто ставит адрес;
  протокол 1.5 = 1.4 + video-key, video-key-ack, video-stop, video-fallback.
- v4 (2026-10-27) — §6: единицы и допустимые значения полей, ошибки ack, `video-key` после `video-stop`,
  значения `idle`, идемпотентность и потокобезопасность команд (по итогам трека B).
- v3 (2026-10-27) — §8: две таблицы и два юнита загрузки (по ролям), `BindsTo=` служб видео, `PartOf=nftables.service`
  (по итогам трека C); §4: `nft_dropped` — `null`, пока счётчик не отдаёт привилегированный компонент (служба от
  `sheregesh-ui` счётчики nftables прочитать не может).
- v2 (2026-10-27) — §1: показ кадров на пульте — rx всегда отдаёт `unixfdsink`, GUI выбирает GL или запасной RGBx
  (без дисплея); список сохраняемых полей, проб и тестов GUI (по ответу сессии S8). На треки A/B/C не влияет.
