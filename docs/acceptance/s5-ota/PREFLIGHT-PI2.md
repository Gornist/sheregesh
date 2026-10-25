# Preflight pi2 перед OTA на `bd74c8c` (2026-10-25, только чтение)

| Что | Факт |
|---|---|
| Плата | серийный `100000009d497e91`, 192.168.1.171 |
| Слот / OTA | активный **A** (образ 57e8fe4), состояние **ROLLED_BACK**, `consistent: true` |
| Корень | `/dev/disk/by-slot/active/system`, 1,1 из 2,4 ГБ |
| /persistent | 974 МБ, свободно 820 МБ |
| DEV-выпуск | `0.1.0~dev0+g980c45ab1c5f` |
| Службы | failed 0; `sheregesh-remote-net` active, NRestarts 0; `sheregesh-health` 14/14 PASS |
| Питание | `throttled=0x0`; uptime 10,5 ч |
| Снимок slot_guard «до» | [`pi2-preflight.json`](pi2-preflight.json) — 53 поля, активный A |

Кодовая база: `59dd462`, `b7534a4`, `5811ab5`, `c26b89c`, `3ee69dd` — предки `bd74c8c` (`git merge-base --is-ancestor`).
После `bd74c8c` в `software/`, `os/`, `tools/image/`, `tools/lab-video/` изменился только `os/docker-build.sh` (кэш
сборки) — образ `bd74c8c` пересобирать не нужно.

pi1 не трогается: B / COMMITTED, `g980c45`.
