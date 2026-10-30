#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Загрузка корня образа (system_a) под настоящим systemd в контейнере — до раскатки на железо (решение владельца
# 2026-10-30: нерабочее на платы не выкатывать). Корень только для чтения, /home — из раздела persistent образа с
# маркером роли, DHCP-сервер dnsmasq — соседний контейнер (имя клиента в его журнале — как его видит роутер).
#   tools/image/boot_check.sh ОБРАЗ.img.zst console|remote [SSH_PORT]
# Проверки: имя системы pi1/pi2 после загрузки, после DHCP, после перезапуска systemd-hostnamed и продления аренды;
# имя в запросах DHCP; root по SSH ключом с этой машины (порт SSH_PORT на localhost, по умолчанию 2222).
# Код 0 — всё PASS. Нужен Docker с привилегированными контейнерами (colima vz / devbox).
# Docker монтирует свои /etc/hostname и /etc/hosts — точка входа снимает их до запуска /sbin/init.
set -euo pipefail
IMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; ROLE=${2:?console|remote}; PORT=${3:-2222}
case "$ROLE" in console) WANT=pi1;; remote) WANT=pi2;; *) echo "роль: console|remote" >&2; exit 2;; esac
TAG=shboot-$$; NET=$TAG; W=$(mktemp -d "$(dirname "$IMG")/.boot.XXXXXX")
cleanup() { docker rm -f "$TAG" "$TAG-dhcp" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true
            docker rmi "$TAG:root" >/dev/null 2>&1 || true; rm -rf "$W"; }
trap cleanup EXIT

# корень system_a → tar; persistent/home → каталог (владельцы и права сохраняются)
docker run --rm --privileged -v "$IMG":/img.zst:ro -v "$W":/w debian:trixie bash -euo pipefail -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends zstd fdisk >/dev/null 2>&1
  zstd -qdc /img.zst > /w/img
  part() { sfdisk -d /w/img | awk -F"[=,]" -v n="name=\"$1\"" "index(\$0, n){gsub(/ /,\"\"); print \$2, \$4}"; }
  read -r s z < <(part system_a); mkdir /sys_a && mount -o ro,noload,loop,offset=$((s*512)),sizelimit=$((z*512)) /w/img /sys_a
  read -r s z < <(part persistent); mkdir /pers && mount -o ro,noload,loop,offset=$((s*512)),sizelimit=$((z*512)) /w/img /pers
  tar -C /sys_a --numeric-owner -cf /w/root.tar .
  mkdir -p /w/persistent && cp -a /pers/. /w/persistent/
  umount /sys_a /pers; rm /w/img'
docker import -q "$W/root.tar" "$TAG:root" >/dev/null; rm "$W/root.tar"
UID_UI=$(docker run --rm --entrypoint /usr/bin/id "$TAG:root" -u sheregesh-ui)
docker run --rm -v "$W":/w debian:trixie sh -euc "
  d=/w/persistent/home/sheregesh-ui/.config/sheregesh; mkdir -p \$d; echo urn:test > \$d/$ROLE.role
  chown -R $UID_UI:$UID_UI /w/persistent/home/sheregesh-ui/.config; chmod 0700 /w/persistent/home/sheregesh-ui"

docker network create --subnet 172.30.77.0/24 "$NET" >/dev/null
docker run -d --name "$TAG-dhcp" --network "$NET" --ip 172.30.77.2 --cap-add NET_ADMIN debian:trixie sh -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq dnsmasq >/dev/null 2>&1
  exec dnsmasq -k --log-dhcp --log-facility=- --port=0 --dhcp-range=172.30.77.100,172.30.77.150,2m' >/dev/null
for _ in $(seq 60); do docker logs "$TAG-dhcp" 2>&1 | grep -q "DHCP, IP range" && break; sleep 2; done

docker run -d --name "$TAG" --network "$NET" --privileged --cgroupns=private --read-only \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp --tmpfs /var \
  -v "$W/persistent":/persistent -v "$W/persistent/home":/home -p "127.0.0.1:$PORT:22" \
  --entrypoint /bin/sh "$TAG:root" -c 'umount /etc/hostname /etc/hosts 2>/dev/null; exec /sbin/init' >/dev/null
x() { docker exec "$TAG" "$@"; }
for _ in $(seq 90); do st=$(x systemctl is-system-running 2>/dev/null || true)
  case "$st" in running|degraded) break;; esac; sleep 2; done
sleep 15                                              # networkd: DHCP и запуск systemd-hostnamed

FAIL=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1: $2"; else echo "FAIL $1: $2 (ожидалось $3)"; FAIL=1; fi; }
check "hostname после загрузки" "$(x hostname)" "$WANT"
check "статическое имя (hostnamectl)" "$(x hostnamectl --static)" "$WANT"
check "юнит sheregesh-hostname" "$(x systemctl is-active sheregesh-hostname)" "active"
x systemctl restart systemd-hostnamed; sleep 2
check "hostname после перезапуска hostnamed" "$(x hostname)" "$WANT"
x networkctl renew eth0 >/dev/null 2>&1 || true; sleep 6
check "hostname после продления DHCP" "$(x hostname)" "$WANT"
names=$(docker logs "$TAG-dhcp" 2>&1 | sed -n 's/.*client provides name: //p' | sort -u | tr '\n' ' ')
check "имя в запросах DHCP" "${names% }" "$WANT"
ROOTSSH=$(ssh -p "$PORT" -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -o ConnectTimeout=5 root@127.0.0.1 id -u 2>/dev/null || echo нет)
check "root по SSH ключом" "$ROOTSSH" "0"
echo "--- systemctl --failed"; x systemctl --failed --no-legend || true
echo "--- журнал имени"; x journalctl -b --no-pager | grep -E "Hostname set|sheregesh-hostname\[" | tail -6 || true
exit $FAIL
