# S3.2-OTA шаг 1 — запись неактивного слота B pi2: **GO** (2026-10-24)

Процедура — [`../preflight/PREFLIGHT.md`](../preflight/PREFLIGHT.md) §5. Пакет `0.1.1792865618`, SHA-256 `f6e192346705c21c19a394da09f2e7d221d02c176adc02a3e727f0f1e8046edb`, сверен `--sha256` до передачи. Переключение, enable, tryboot, confirm и перезагрузки **не выполнялись**.

| Контрольная точка | Результат |
|---|---|
| Предусловия | SHA = утверждённому; OTA `IDLE`, активный A, `consistent`; полей `video_*` нет; снимки «до» годны и зафиксированы в Git до записи (`60642de`); видео до записи PASS |
| `install.log` | подпись принята, `IDLE→VERIFIED→INSTALLING→INSTALLED`, «слот B записан и проверен; основной слот не изменён», target B, версия `0.1.1792865618` |
| `slot_guard --mode install` pi2 ([`pi2-compare.txt`](pi2-compare.txt)) | **PASS 44/44**: p1/p2/p4 записано 0 и отброшено 0 секторов; p3 = 268435456 байт, p5 = 2684354560 байт; `autoboot.txt` прежний (`[all]=2`, `[tryboot]=3`); boot_id, слоты, монтирование ro, хеши boot/system A, все отпечатки идентичности, службы (InvocationID, NRestarts), `deploy.status` — прежние; разрешения нет; B не смонтирован; health PASS |
| `slot_guard --mode unchanged` pi1 ([`pi1-compare.txt`](pi1-compare.txt)) | **PASS 44/44** |
| Канал и видео после записи | `check_link` health, baseline PASS; `check_video_link` session, stream, leftovers PASS — тот же сеанс, декодировано 29,87 к/с, разрывов и ошибок 0 |
| `ota-status` после ([`ota-status-after.json`](ota-status-after.json)) | `INSTALLED` → B `0.1.1792865618`, активный A, `tryboot_now: false`, `consistent`, autoboot `{2, 3}` |

Состояние pi2: B записан и проверен агентом, основной слот A, autoboot `[all]=2`, разрешения переключения нет — безопасно. До шага 3 **не менять** DEV Deploy и конфигурацию pi2 (риск D). Шаг 4 сверяет идентичность против [`pi2-before.json`](pi2-before.json) (`slot_guard --mode trial`, PREFLIGHT §6).

Следующие шаги — каждый только по отдельному решению владельца: 2 `ota-enable pi2`, 3 `ota-tryboot pi2` (человек у pi2), 4 проверки на B, 5 `ota-confirm pi2`.
