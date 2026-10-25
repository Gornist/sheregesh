# OTA pi1 (пульт) — COMMITTED B, E2E-13 PASS (2026-10-25)

Пакет `0dbe43e1ad2e30b4fc3028b1375cbf8706dafc00749ca8c083c6f22f1c5a774e` (образ b1d95a2, V 0.1.1792924537),
утверждён владельцем; план — `docs/acceptance/ota-closure/PI1-PLAN.md`.

| Шаг | Итог | Доказательство |
|---|---|---|
| Первая попытка tryboot (12:01) | прошивка загрузила A, следов B нет; ROLLED_BACK; разбор — `../TRYBOOT-FAIL.md` | `../` |
| 1 повторная установка B | INSTALLED; slot_guard install 44/0/0 | `pi1-compare-install.txt` |
| 3 tryboot (повтор по решению владельца) | B (p3, b1d95a2), ядро через ~22 с, адрес .92, циклов 0 | `after-tryboot.txt` |
| 4 факты пробной загрузки (uptime 191 с) | GO: /persistent p6, /var = slots/system_b/var, failed нет, console active, mTLS TLS 1.3, видео remote-tls 29,8 к/с, health 15/15; trial 26/0/0; pi2 unchanged 44/0/0 | `trial-facts.txt`, `pi1-compare-trial.txt` |
| 5 ota-confirm | OK, все критерии PASS, COMMITTED B | `confirm.json` |
| 6 та же загрузка | ровно 3 ожидаемых FAIL; autoboot `[all]=3`, `[tryboot]=2` | `pi1-compare-committed.txt` |
| 7 контрольная перезагрузка | B основной (p3), SSH 21 с, running, failed нет, циклов 0, health 15/15; 4 ожидаемых FAIL (+dt.tryboot=0) | `after-reboot.txt`, `pi1-compare-reboot.txt` |
| 8 сокращённая приёмка | версия/OTA/health PASS; **E2E-13 PASS** (stop → inactive, success, ExecMainStatus 0, «Deactivated successfully», без 143; start → active); SSH (ключ хоста прежний); связь pi1↔pi2 connected; видео session/stream PASS 30 к/с; журнал без Traceback и ошибок юнитов sheregesh-*; ota-disable; pi2 unchanged 44/0/0 | `acceptance.txt` |

E2E-13 «до» (образ 57e8fe4): failed, status=143 — `../../e2e13-before.txt`.

После COMMITTED на B работает заводской код образа b1d95a2 (DEV-выпуск слота A на B не переносится).
Замечание: `vc4-drm swiotlb buffer is full` (3 записи) — GPU без дисплея под cage, не службы «Шерегеша».
