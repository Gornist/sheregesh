# OTA pi1 — пометки по ходу
- Шаг 1a: `slot_guard install` 43 PASS / 1 FAIL — `same.unit.sheregesh-console` (InvocationID сменился). Причина —
  E2E-13 «до» (stop/start службы пульта в 10:12:28–34 GMT) выполнен после снимка «до» в той же загрузке
  (boot_id 4eac70ab… одинаков); текущий InvocationID aacf7569… = значение «после», ActiveEnterTimestamp 10:12:34.
  Запись разделов чистая: p1/p2/p4 — 0 секторов, p3/p5 — ровно размер образа; ota.state INSTALLED. pi2 unchanged 44/0.
