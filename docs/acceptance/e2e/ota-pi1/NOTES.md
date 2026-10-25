# OTA pi1 — пометки по ходу
- Шаг 1a: `slot_guard install` 43 PASS / 1 FAIL — `same.unit.sheregesh-console` (InvocationID сменился). Причина —
  E2E-13 «до» (stop/start службы пульта в 10:12:28–34 GMT) выполнен после снимка «до» в той же загрузке
  (boot_id 4eac70ab… одинаков); текущий InvocationID aacf7569… = значение «после», ActiveEnterTimestamp 10:12:34.
  Запись разделов чистая: p1/p2/p4 — 0 секторов, p3/p5 — ровно размер образа; ota.state INSTALLED. pi2 unchanged 44/0.
- Шаг 3: tryboot → B (p3, 09fca4e) за 23 с, адрес .92 прежний, **циклов упорядочения 0** (исправление var-seed
  работает), HEALTH_PENDING попытка 1, /persistent смонтирован (загрузка в журнале persistent), связь TLS 1.3,
  видео 30 к/с, ретранслятор 61381d34… ([`trial-facts.txt`](trial-facts.txt)).
- **Отказ гейта confirm:** `console-setup.service` failed — `setupcon` пересоздаёт кэш раскладки, если конфигурация
  не старше кэша; в образе devbox у `/etc/default/keyboard`, `/etc/default/console-setup` и `cached_*` одинаковое
  mtime (2026-10-25 09:10:12 UTC) → попытка записи на корень ro → exit 1. В образе CI 57e8fe4 кэш новее конфигурации.
  Тот же отказ был в цикле 1 OTA CLOSURE (тогда ошибочно приписан владельцам файлов). confirm не выполнялся.
- **E2E-13 на пробной загрузке исправленного образа — PASS** ([`e2e13-after-trialB.txt`](e2e13-after-trialB.txt)):
  stop → inactive, Result=success, ExecMainStatus=0, «Deactivated successfully», без status=143; start → active,
  NRestarts 0, связь connected. «До» (57e8fe4): failed, 143 ([`e2e13-before.txt`](e2e13-before.txt)).
- `ota-abort` → A за 23 с: ROLLED_BACK, consistent, `slot_guard --mode rollback` 38/0/0, health 15/15, failed нет;
  `ota-disable`.
