#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Загрузочный тест образа до железа (ADR-0018, этап boot): весь диск образа в QEMU virt (aarch64, TCG) с ядром
# Debian, настоящий systemd образа — пространства имён, cgroup и песочница юнитов как на плате.
#   boot_check.sh ОБРАЗ.img.zst console|remote|fresh [МЕТКА]
# console/remote — маркер роли в /home (как после pair, без link.json); fresh — свежая плата без роли.
# Набор машины — tools/build/vm/make_vmkit.sh (~/shbuild/vm). Сеть: tap + мост внутри контейнера, DHCP — dnsmasq с
# журналом (имя клиента — как его видит роутер). Изменения пишутся в оверлей qcow2, образ не меняется.
# Проверки (проба vm/probe.sh печатает «SHBOOT …» в консоль): упавших юнитов нет (кроме аппаратных ALLOW_FAILED);
# имя после загрузки, перезапуска systemd-hostnamed и продления DHCP; имя в запросах DHCP; источник времени у
# console/fresh, у remote до pair — нет; sshd: root только по ключу, ключ root = ключ sheregesh-ui; /var засеян.
# Отличия от платы (не проверяются): ядро Pi, прошивка, tryboot, VideoCore, дисплей, GPIO/UART, Bluetooth.
set -euo pipefail
IMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; SC=${2:?console|remote|fresh}; LABEL=${3:-$$}
case "$SC" in console) WANT=pi1;; remote) WANT=pi2;; fresh) WANT=;; *) echo "сценарий: console|remote|fresh" >&2; exit 2;; esac
HERE=$(cd "$(dirname "$0")" && pwd)
KIT=${SHBOOT_KIT:-$HOME/shbuild/vm}
[ -s "$KIT/vmlinuz" ] && [ -s "$KIT/modules.ext4" ] || { echo "нет набора машины: $HERE/vm/make_vmkit.sh" >&2; exit 2; }
# Юниты без железа Pi в virt. Каждый — с причиной; новые сюда — только осознанно.
ALLOW_FAILED="hciuart.service seatd.service sheregesh-console.service sheregesh-video-tx.service sheregesh-video-rx.service"
mkdir -p "${SHBOOT_TMP:=$HOME/shbuild/tmp}"; W=$(mktemp -d "$SHBOOT_TMP/shboot.XXXXXX")   # не /tmp: tmpfs 7,5 ГБ
NAME=shboot-${LABEL//[^a-zA-Z0-9]/-}-$SC
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true
            [ -n "${SHBOOT_KEEP:-}" ] && { echo "SHBOOT_KEEP: $W" >&2; return; }
            docker run --rm -v "$W":/w debian:trixie sh -c 'rm -rf /w/*' >/dev/null 2>&1 || true; rm -rf "$W"; }
trap cleanup EXIT
cp "$HERE/vm/init" "$HERE/vm/probe.sh" "$W/"

echo "== $(date -u +%T) машина: $SC"
set +e
docker run --rm --name "$NAME" --cap-add NET_ADMIN --device /dev/net/tun \
  -v "$IMG":/img.zst:ro -v "$KIT":/kit:ro -v "$W":/w -e SC="$SC" shbuild-qemu bash -euo pipefail -c '
  ver=$(cat /kit/kernel.version)
  zstd -qdc /img.zst > /w/disk.img
  qemu-img create -q -f qcow2 -F raw -b /w/disk.img /w/overlay.qcow2
  # initramfs: busybox, модули для дисков и сети (по зависимостям, распакованные), init, проба
  mkdir -p /w/ird/bin /w/ird/mods /w/ird/proc /w/ird/sys /w/ird/dev /w/mods/lib/modules
  tar -C /w/mods/lib/modules -xf /kit/modules.tar
  cp /kit/busybox /w/ird/bin/busybox; cp /w/init /w/ird/init; cp /w/probe.sh /w/ird/probe.sh; chmod 755 /w/ird/init
  : > /w/ird/modules.order
  for m in virtio_pci virtio_blk virtio_net virtio_console ext4; do
    modprobe -d /w/mods -S "$ver" --show-depends "$m" | sed -n "s/^insmod \([^ ]*\).*/\1/p"
  done | awk "!seen[\$0]++" | while read -r ko; do
    b=$(basename "$ko"); b=${b%.xz}; b=${b%.zst}
    case "$ko" in *.xz) xz -dc "$ko" > "/w/ird/mods/$b";; *.zst) zstd -qdc "$ko" > "/w/ird/mods/$b";; *) cp "$ko" "/w/ird/mods/$b";; esac
    echo "$b" >> /w/ird/modules.order
  done
  (cd /w/ird && find . | cpio -o -H newc --quiet | gzip -1) > /w/initrd.gz
  rm -rf /w/mods /w/ird
  ip link add br0 type bridge; ip addr add 172.30.77.1/24 dev br0; ip link set br0 up
  ip tuntap add tap0 mode tap; ip link set tap0 master br0; ip link set tap0 up
  dnsmasq --no-daemon --user=root --interface=br0 --bind-interfaces --port=0 --dhcp-range=172.30.77.100,172.30.77.150,2m \
    --log-dhcp --log-facility=/w/dnsmasq.log 2>/dev/null &
  timeout 1500 qemu-system-aarch64 -M virt -cpu cortex-a72 -smp 4 -m 3072 -no-reboot \
    -kernel /kit/vmlinuz -initrd /w/initrd.gz -append "console=ttyAMA0 shboot.scenario=$SC panic=-1 systemd.show_status=0" \
    -drive if=virtio,format=qcow2,file=/w/overlay.qcow2 -drive if=virtio,format=raw,file=/kit/modules.ext4,readonly=on \
    -netdev tap,id=n0,ifname=tap0,script=no,downscript=no -device virtio-net-pci,netdev=n0,romfile= \
    -device virtio-serial-pci -chardev file,id=pr,path=/w/probe.log -device virtserialport,chardev=pr,name=shboot \
    -serial file:/w/serial.log -monitor none -display none
  echo "qemu код $?"
  chmod 644 /w/dnsmasq.log /w/serial.log /w/probe.log 2>/dev/null || true
  rm -f /w/disk.img /w/overlay.qcow2'
rc=$?
set -e
S="$W/probe.log"
[ -s "$W/serial.log" ] || { echo "ERROR: нет журнала консоли машины (код $rc)"; exit 3; }
grep -q "^SHBOOT end" "$S" 2>/dev/null || { echo "ERROR: проба не завершилась (зависание загрузки?); хвост консоли:"
  sed 's/\x1b\[[0-9;:]*m//g' "$W/serial.log" | tail -40; exit 3; }
# каждая строка пробы обязательна: потерянная строка — провал, а не «пусто = хорошо»
for k in state failed hostname1 static hostname2 hostname3 default sources chrony permitroot rootkey var; do
  grep -q "^SHBOOT $k=" "$S" || { echo "ERROR: в итоге пробы нет строки $k"; exit 3; }
done
v() { sed -n "s/^SHBOOT $1=//p" "$S" | tail -1 | tr -d '\r'; }

FAIL=0
ok()  { echo "PASS $1: $2"; }
bad() { echo "FAIL $1: $2"; FAIL=1; }
eq()  { if [ "$2" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2 (ожидалось $3)"; fi; }
echo "состояние системы: $(v state)"
allowed() { for a in $ALLOW_FAILED; do [ "$a" = "$1" ] && return 0; done; return 1; }
unexpected=""; for u in $(v failed); do allowed "$u" || unexpected="$unexpected$u "; done
eq "упавшие юниты (кроме аппаратных)" "${unexpected% }" ""
case "$(v state)" in running|degraded) ok "загрузка завершена" "$(v state)";; *) bad "загрузка завершена" "$(v state)";; esac
want=${WANT:-$(v default)}
eq "имя после загрузки" "$(v hostname1)" "$want"
eq "статическое имя (hostnamectl)" "$(v static)" "$want"
eq "имя после перезапуска hostnamed" "$(v hostname2)" "$want"
eq "имя после продления DHCP" "$(v hostname3)" "$want"
names=$( (sed -n 's/.*client provides name: //p' "$W/dnsmasq.log" 2>/dev/null || true) | tr -d '\r' | sort -u | tr '\n' ' ')
eq "имя в запросах DHCP" "${names% }" "$want"
src=$(v sources)
if [ "$SC" = remote ]; then eq "источники времени до pair (remote — только пульт)" "$src" ""
elif [ -n "$src" ]; then ok "источник времени ($SC)" "$src"; else bad "источник времени ($SC)" "нет — плата без времени, pair невозможен"; fi
eq "chrony активен" "$(v chrony)" "active"
eq "sshd: root только по ключу" "$(v permitroot)" "without-password"
eq "ключ root = ключ sheregesh-ui" "$(v rootkey)" "same"
eq "/var засеян" "$(v var)" "seeded"
if [ $FAIL != 0 ]; then echo "--- журналы упавших юнитов"; sed -n 's/^SHBOOT log //p' "$S" | tail -60; fi
exit $FAIL
