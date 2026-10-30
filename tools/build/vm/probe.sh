#!/bin/sh
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Проба внутри загруженного образа (ADR-0018): строки «SHBOOT ключ=значение» в отдельный канал virtio-serial
# (консоль системы с ними не перемешивается; без канала — в консоль), затем выключение. Type=simple: задание
# запуска завершается сразу, is-system-running --wait не ждёт саму пробу.
P=/dev/virtio-ports/shboot; [ -e "$P" ] || P=/dev/ttyAMA0
out() { echo "SHBOOT $*" >> "$P"; }
out "start"
timeout 900 systemctl is-system-running --wait >/dev/null 2>&1
out "state=$(systemctl is-system-running)"
sleep 20                                          # DHCP, systemd-hostnamed, роль chrony
out "failed=$(systemctl list-units --failed --no-legend --plain | awk '{print $1}' | tr '\n' ' ')"
out "hostname1=$(hostname)"
out "static=$(hostnamectl --static 2>/dev/null)"
systemctl restart systemd-hostnamed; sleep 3
out "hostname2=$(hostname)"
networkctl renew eth0 >/dev/null 2>&1; sleep 10
out "hostname3=$(hostname)"
out "default=$(cat /usr/share/sheregesh/hostname.default 2>/dev/null || echo sheregesh-dev)"
out "sources=$(cat /run/sheregesh-time/sources/*.sources 2>/dev/null | grep -E '^(pool|server) ' | tr '\n' ';')"
out "chrony=$(systemctl is-active chrony)"
out "permitroot=$(sshd -T -C user=root,host=t,addr=127.0.0.1 2>/dev/null | awk '$1=="permitrootlogin"{print $2}')"
rk=$(head -1 /root/.ssh/authorized_keys 2>/dev/null | cut -d' ' -f1-2)
uk=$(head -1 /home/sheregesh-ui/.ssh/authorized_keys 2>/dev/null | cut -d' ' -f1-2)
[ -n "$rk" ] && [ "$rk" = "$uk" ] && out "rootkey=same" || out "rootkey=${rk:-none}"
out "var=$(test -s /var/lib/dpkg/status && echo seeded || echo empty)"
for u in $(systemctl list-units --failed --no-legend --plain | awk '{print $1}'); do
  journalctl -b -u "$u" --no-pager -n 6 -o cat 2>/dev/null | while IFS= read -r l; do out "log $u: $l"; done
done
out "end"
systemctl poweroff
