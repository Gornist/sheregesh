# pi2: ремонт state.json агента OTA (2026-10-25)

Решение владельца: «это чини сам» (сетевого пути нет — `GATE.md` §4), «выключай».

1. 01:19 — снимок до выключения: [`pi2-before-poweroff.json`](pi2-before-poweroff.json) (slot_guard, 53 поля,
   активен B, boot_id `bd962e88…` как в step5), [`ota-status-before-poweroff.json`](ota-status-before-poweroff.json),
   [`state-on-board-before-poweroff.json`](state-on-board-before-poweroff.json).
2. `systemctl poweroff` от sheregesh-ui на B — штатно, плата перестала отвечать на ping.
3. Карта в картридере мака, раздел опознан по GUID раздела GPT `45060026-07fc-6508-1875-00471802f811` → `disk5s6`;
   `diskutil unmountDisk disk5` (FAT-разделы не тронуты).
4. `python3 tools/image/repair_ota_state.py --disk disk5s6 --confirm disk5s6` (sudo — владелец):
   - содержимое: TRYBOOT/B, есть `"expect": {}` — совпадает побайтно с файлом, прочитанным на плате до выключения;
   - копия → [`state-before-repair.json`](state-before-repair.json);
   - debugfs `link` + `unlink`: `/sheregesh-ota/state.json` → `state.json.a7f1b74` (inode 5921, 398 байт);
     `enable.json`, `enable.json.sig`, `.lock` не тронуты;
   - `e2fsck -fn`: `PERSISTENT: 6107/65536 files (0.1% non-contiguous), 38781/262144 blocks`, код 0;
   - `Disk disk5 ejected`.

Ожидается после загрузки: прошивка — A (`[all]` boot_partition=2, tryboot одноразовый уже израсходован), агент
57e8fe4 создаёт `IDLE`, active=A, version_active 0.1.1792843040; `ota-status` работает, `consistent: true`.
