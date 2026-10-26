# Контракт видеослужбы «Шерегеш» (ADR-0016, этап 0)

**Версия контракта:** 1 · **Дата:** 2026-10-27 · **Связано:** ADR-0016, ADR-0015, ADR-0011, ADR-0007.

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
| `catalog` | — | список источников (как `status.video.sources` сейчас) |

Команды rx:

| cmd | args | Смысл |
|---|---|---|
| `start` | `stream, session, gen, ssrc, peer {ip}, local {port, rtcp_port}, srtp {cipher, key_b64}, codec` | ждать поток поколения `gen` от `peer.ip` |
| `stop` | `stream, reason` | остановить, уничтожить ключ |
| `status` | — | то же, что `state.json` |

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
   "rtp": {"packets": 0, "lost": 0, "jitter_ms": 0.0, "reordered": 0},
   "srtp": {"auth_fail": 0, "replay_fail": 0, "unknown_ssrc": 0},
   "rtcp": {"pli_sent": 0, "pli_received": 0, "keyframes_requested": 0, "keyframe_unsupported": false},
   "latency_ms": {"p50": null, "p95": null, "max": null, "samples": 0},
   "last_frame_age_s": 0.03, "restarts": 0, "error": "",
   "source": {}, "nft_dropped": 0,
   "transitions": [[1793000000.0, "streaming", "first frame"]]}}}
```

Поля `latency_ms` — только у rx (время отправки — расширение RTP `rtphdrextntp64`, смещение часов — от GUI в
команде `start` не передаётся: служба считает «время отправки → кадр декодирован» по своим часам и смещению
`clock_offset_s`, которое GUI сообщает командой `set-clock-offset {offset_s}`; до неё `latency_ms` = null).

**Добавление rx:** команда `set-clock-offset {offset_s}` (float).

## 5. Сеанс, поколение, ключ

- `session` — 128 бит случайных (`secrets.token_hex(16)`), создаёт агент блока на каждый новый сеанс управления.
- `gen` — целое, строго растёт в пределах `session`, начиная с 1. Новое поколение — при новом `start` tx: новый
  сеанс, смена источника, любой перезапуск конвейера tx.
- Новое поколение = новый ключ (`secrets.token_bytes`) **и** новый SSRC (`secrets.randbits(32)`, ≠ 0, ≠ прежнему).
  Номера RTP на пути SRTP не переписываются (`RtpContinuity` не используется).
- Шифр по умолчанию `aes-128-gcm` (ключ 16 + соль 12 = 28 байт). Если на GStreamer 1.26.2 GCM не работает в связке
  `srtpenc`/`srtpdec` — `aes-128-icm` + `hmac-sha1-80` (16 + 14 = 30 байт). `null` запрещён.
- rx отдаёт ключ `srtpdec` только для текущего `ssrc` (сигнал `request-key`); пакеты других SSRC отбрасываются
  и считаются в `srtp.unknown_ssrc`.

## 6. SHRG-LAB 1.5: сообщения видеосеанса

`TYPES["1.5"] = TYPES["1.4"] | {"video-key", "video-key-ack", "video-stop"}`. В `hello` обеих сторон — поле
`video_transports: ["srtp", "tls"]` (что сторона умеет) и у пульта `video_rx_port`.

| Тип | Направление | body |
|---|---|---|
| `video-key` | блок → пульт | `{stream, session, gen, transport: "srtp", ssrc, codec, profile {width, height, fps, bitrate}, srtp {cipher, key_b64}, rtcp_port}` |
| `video-key-ack` | пульт → блок | `{stream, session, gen, ok, error?}` |
| `video-stop` | блок → пульт | `{stream, session, gen, reason}` |

- Пульт принимает `video-key`, только если: сеанс 1.5, `session` — текущий видеосеанс этого сеанса управления (первый
  `video-key` после `hello` его задаёт), `gen` > последнего принятого. Иначе — `video-key-ack {ok: false, error:
  "stale"}`, ничего не применяется.
- Блок шлёт видео поколения `gen` только после `video-key-ack {ok: true}` с тем же `(session, gen)`; ack с другими
  значениями игнорируется.
- Поле `key_b64` маскируется при любой записи сообщения в журнал (`"key_b64": "***"`); в телеметрию и `state.json` не
  попадает.
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
- Наборы пусты при загрузке. Службу rx systemd не запускает, если таблица не загружена
  (`Requires=`/`After=` юнита загрузки таблицы); служба при старте проверяет наличие таблицы и без неё в `streaming`
  не переходит (`error: "firewall"`).
- Адрес ставит `sheregesh-video-allow@<адрес>.service` (oneshot, root): заменяет содержимое `peer4`/`peer6` одним
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

- v1 (2026-10-27) — первая версия.
