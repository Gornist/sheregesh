# Цикл 1 (образ abd9d68 devbox, пакет a09082fb…c86a) — STOP на шаге 4, confirm не выполнялся (2026-10-25)

- Шаги 0–3 PASS: ota-disable, снимки «до», install агентом 57e8fe4 (slot_guard install 44/0/0, pi1 unchanged 44/0/0),
  pre-enable 44/0/0, ota-enable, ota-tryboot → B загрузился за 15 с, build.json = abd9d68, HEALTH_PENDING, попытка 1.
- **Отказ:** `systemctl is-system-running` = degraded — `console-setup.service` failed (`/etc/console-setup is not
  writable`, ФС только для чтения). Критерий confirm «failed-units = нет» не выполнился бы.
- **Причина — дефект образа devbox, не кода «Шерегеша»:** 41 файл/каталог корневой ФС B принадлежит uid 1000
  (= sheregesh-ui на плате): `/etc`, `/usr`, `/etc/systemd/system/*.service`, генераторы systemd, хуки initramfs
  ([`b-uid1000-files.txt`](b-uid1000-files.txt)). На pi1 (образ CI 57e8fe4) `/etc` — root:root. Образ собран с
  `SHEREGESH_BUILD_AS_ROOT=1` (эмуляция arm64 на x86, без `podman unshare`): rpi-image-gen копирует overlay-файлы
  из своего клона и слоёв с владельцем исходников; клон и /work/src принадлежат builder (uid 1000). В обычном режиме
  userns отображает uid 1000 → 0, в режиме root — нет. `verify_image` (71/0/0) владельцев не проверяет.
- **Действие:** диагностика сохранена ([`b-failed-units.txt`](b-failed-units.txt), [`pi2-trial.json`](pi2-trial.json),
  [`ota-status-trial.json`](ota-status-trial.json)); state.json без поля expect; `ota-abort` → обычная перезагрузка →
  A за 23 с; `ROLLED_BACK`, consistent, ota-boot success, failed нет; `slot_guard --mode rollback` 38/0/0
  ([`pi2-compare-rollback.txt`](pi2-compare-rollback.txt)); health 14/14.
- Пакет a09082fb…c86a **отозван из использования** (не устанавливать). Цикл исправления 1: сборка с владельцем root
  для overlay-источников + независимая проверка владельцев в образе против эталона CI 57e8fe4.
