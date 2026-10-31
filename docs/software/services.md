<!-- SPDX-FileCopyrightText: 2026 Sheregesh contributors -->
<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->

# Службы «Шерегеш» (REQ-SW-001)

Юниты systemd образа — `tools/image/units/` (устанавливаются в `/usr/lib/systemd/system`). Таблицу сверяет с юнитами
тест `tests/tools/test_services_doc.py`: каждый юнит описан, `Restart=` и `WatchdogSec=` совпадают.

**Роль** — по условиям юнита: `remote.role` / `console.role` в `~sheregesh-ui/.config/sheregesh/`. «Пульт» — нет
`remote.role` (роль пульта или плата без роли на стенде), «блок» — есть `remote.role`, «без роли» — ни одного файла
роли (стендовый эмулятор), «обе» — условий по роли нет.

**Обязательные службы** — постоянно работающие (`Type=notify`): у каждой `WatchdogSec` и `Restart=`; состояние видно в
диагностике пульта (для блока — через канал управления). Остальные юниты разовые (`oneshot`), по таймеру или сокету:
watchdog к ним неприменим — они завершаются, а итог виден в журнале и `systemctl status`.

## Постоянные службы

| Юнит | Роль | Что делает | Restart= | WatchdogSec= | Зависимости |
|---|---|---|---|---|---|
| sheregesh-console.service | пульт | GUI пульта (cage): канал mTLS к блоку по `link.json` или стендовый эмулятор на 127.0.0.1 | always | 20 | After: systemd-user-sessions, seatd, sheregesh-remote-emu, network.target; Conflicts: getty@tty1 |
| sheregesh-video-rx.service | пульт | Видеослужба пульта: приём UDP, SRTP, джиттер-буфер, аппаратный декодер, кадр для GUI | on-failure | 15 | After: network.target, local-fs.target, sheregesh-video-nft; BindsTo: sheregesh-video-nft |
| sheregesh-remote-net.service | блок | Удалённый блок: канал mTLS 1.3 по `link.json`, телеметрия, команды, видеосеанс | on-failure | 15 | After: network.target, local-fs.target |
| sheregesh-video-tx.service | блок | Видеослужба блока: источник, кодер, RTP/SRTP, отправка на пульт | on-failure | 15 | After: network.target, local-fs.target, sheregesh-video-nft-remote; BindsTo: sheregesh-video-nft-remote |
| sheregesh-remote-emu.service | без роли | Стендовый эмулятор блока на 127.0.0.1:7700 и видео 5600, синтетические данные | on-failure | 15 | After: local-fs.target |

## Разовые, по таймеру и по сокету

| Юнит | Тип | Роль | Что делает | Restart= | WatchdogSec= | Зависимости |
|---|---|---|---|---|---|---|
| sheregesh-hostname.service | oneshot | обе | Имя системы по роли (пульт → pi1, блок → pi2) до старта сети | — | — | After: local-fs.target; Before: systemd-networkd, systemd-hostnamed, network-pre.target, sysinit.target |
| sheregesh-var-seed.service | oneshot | обе | Дополнение `/var` из образца образа при первой загрузке слота после A/B (существующее не перезаписывает) | — | — | After: local-fs.target; Before: sysinit.target, systemd-tmpfiles-setup |
| sheregesh-ota-boot.service | oneshot | обе | Учёт загрузки A/B: слот, пробная загрузка, TRYBOOT → HEALTH_PENDING или ROLLED_BACK | — | — | After: local-fs.target |
| sheregesh-ota.socket | socket | обе | Локальный сокет `/run/sheregesh-ota.sock` агента A/B-обновления (root, группа sheregesh-ui) | — | — | — |
| sheregesh-ota@.service | на соединение | обе | Агент A/B-обновления на одно соединение: status, install, enable, disable, tryboot, confirm, abort | — | — | — |
| sheregesh-deploy@.service | oneshot | обе | DEV-выпуск: установка подписанного выпуска, откат, заводской код | — | — | After: local-fs.target |
| sheregesh-health.service | oneshot | обе | Диагностическая проверка здоровья (только чтение), отчёт `/run/sheregesh-health/report.json` | — | — | After: local-fs.target |
| sheregesh-health.timer | timer | обе | Проверка здоровья через 2 мин после загрузки, затем раз в 10 мин | — | — | — |
| sheregesh-time-role.service | oneshot | обе | Источники времени chrony по роли (ADR-0017): пульт — пул Debian и local stratum 10, раздаёт блоку; блок — только пульт | — | — | After: chrony; Requires: chrony; PartOf: chrony |
| sheregesh-time-peer@.service | oneshot | обе | Адрес второй платы для chrony | — | — | After: sheregesh-time-role; Requisite: chrony |
| sheregesh-time-save.service | oneshot | обе | Отметка времени при выключении для платы без RTC (mtime файла дрейфа в `/persistent`) | — | — | After: local-fs.target; Before, Conflicts: shutdown.target |
| sheregesh-time-tick.service | oneshot | обе | Отметка времени по таймеру | — | — | — |
| sheregesh-time-tick.timer | timer | обе | Отметка раз в 10 мин: потеря питания без выключения отбрасывает время не больше чем на 10 мин | — | — | — |
| sheregesh-video-nft.service | oneshot | пульт | Таблица nftables видео пульта: UDP 7444/7445 только с адреса блока сеанса | — | — | After: local-fs.target, nftables; PartOf: nftables |
| sheregesh-video-nft-remote.service | oneshot | блок | Таблица nftables видео блока: UDP 7445 (RTCP) только с адреса пульта сеанса | — | — | After: local-fs.target, nftables; PartOf: nftables |
| sheregesh-video-allow@.service | oneshot | обе | Адрес второй платы сеанса в таблицу nftables видео | — | — | After: sheregesh-video-nft, sheregesh-video-nft-remote |

Черновик таблиц составлен локальной моделью Offrail по текстам юнитов и проверен построчно (`Restart=`,
`WatchdogSec=`, зависимости, роли по условиям); 2026-10-31.
