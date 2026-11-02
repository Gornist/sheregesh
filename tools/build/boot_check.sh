#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Загрузочный тест образа до железа (ADR-0018, этап boot): весь диск образа в QEMU virt (aarch64, TCG) с ядром
# Debian, настоящий systemd образа — пространства имён, cgroup и песочница юнитов как на плате.
#   boot_check.sh ОБРАЗ.img.zst console|remote|fresh|offline|buildhost [МЕТКА]
# console/remote — маркер роли в /home (как после pair, без link.json); fresh — свежая плата без роли;
# offline — fresh в сети без маршрута по умолчанию и без DNS (REQ-SYS-004): DHCP не выдаёт router/dns, весь трафик
# моста пишется tcpdump; пакетов из машины за пределы подсети стенда и запросов DNS быть не должно, юнитов с
# записями err (кроме аппаратных ALLOW_FAILED) — тоже.
# Набор машины — tools/build/vm/make_vmkit.sh (~/shbuild/vm). Сеть: tap + мост внутри контейнера, DHCP — dnsmasq с
# журналом (имя клиента — как его видит роутер). Изменения пишутся в оверлей qcow2, образ не меняется.
# Проверки (проба vm/probe.sh печатает «SHBOOT …» в консоль): упавших юнитов нет (кроме аппаратных ALLOW_FAILED);
# имя после загрузки, перезапуска systemd-hostnamed и продления DHCP; имя в запросах DHCP; источник времени у
# console/fresh, у remote до pair — нет; sshd: root только по ключу, ключ root = ключ sheregesh-ui; /var засеян.
# buildhost (ADR-0019) — образ сборочного хоста Pi 5: MBR, корень — раздел 2, без ролей; проверки: упавших юнитов нет,
# имя, DHCP, расширение корня без ошибки, sshd по ключу (root тоже), docker запущен и отвечает, инструменты сборки.
# Отличия от платы (не проверяются): ядро Pi, прошивка, tryboot, VideoCore, дисплей, GPIO/UART, Bluetooth.
set -euo pipefail
IMG="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; SC=${2:?console|remote|fresh}; LABEL=${3:-$$}
case "$SC" in console) WANT=pi1;; remote) WANT=pi2;; fresh|offline) WANT=;; buildhost) WANT=pi5;;
  *) echo "сценарий: console|remote|fresh|offline|buildhost" >&2; exit 2;; esac
VMSC=$SC; [ "$SC" = offline ] && VMSC=fresh
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

# Ускорение: на arm64-хосте с /dev/kvm (Pi 5, ADR-0019) гость идёт на реальном процессоре; иначе (devbox x86) —
# эмуляция TCG, как раньше. SHBOOT_NO_KVM=1 — принудительно TCG (сравнение, отладка). Контейнер без расширенных прав:
# устройство передаётся явно.
KVMDEV=(); QM=virt; QCPU=cortex-a72; QACCEL=tcg
if [ -z "${SHBOOT_NO_KVM:-}" ] && [ "$(uname -m)" = aarch64 ] && [ -c /dev/kvm ]; then
  KVMDEV=(--device /dev/kvm); QM=virt,gic-version=host; QCPU=host; QACCEL=kvm
fi
echo "== $(date -u +%T) машина: $SC (ускорение $QACCEL)"
set +e
# Образ распаковывается один раз на задание (shbuild.boot → SHBOOT_RAW, разреженный файл) — сценарии берут его
# только для чтения как основу оверлея; без SHBOOT_RAW (ручной запуск) распаковка своя, тоже разреженная.
RAWARGS=(); [ -n "${SHBOOT_RAW:-}" ] && [ -s "$SHBOOT_RAW" ] && RAWARGS=(-v "$SHBOOT_RAW":/disk.raw:ro)
docker run --rm --name "$NAME" --cap-add NET_ADMIN --device /dev/net/tun ${KVMDEV[@]+"${KVMDEV[@]}"} ${RAWARGS[@]+"${RAWARGS[@]}"} \
  -v "$IMG":/img.zst:ro -v "$KIT":/kit:ro -v "$W":/w -e SC="$VMSC" -e OFFLINE="$([ "$SC" = offline ] && echo 1)" \
  -e QM="$QM" -e QCPU="$QCPU" -e QACCEL="$QACCEL" shbuild-qemu bash -euo pipefail -c '
  ver=$(cat /kit/kernel.version)
  tm() { echo "   +${SECONDS}с $1"; }
  if [ -e /disk.raw ]; then BASE=/disk.raw; tm "образ общий на задание"
  else zstd -qdf --sparse /img.zst -o /w/disk.img; BASE=/w/disk.img; tm "образ распакован"; fi
  qemu-img create -q -f qcow2 -F raw -b $BASE /w/overlay.qcow2
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
  rm -rf /w/mods /w/ird; tm "initramfs готов"
  ip link add br0 type bridge; ip addr add 172.30.77.1/24 dev br0; ip link set br0 up
  ip tuntap add tap0 mode tap; ip link set tap0 master br0; ip link set tap0 up
  nogw=; [ -n "$OFFLINE" ] && nogw="--dhcp-option=3 --dhcp-option=6"   # без маршрута и DNS (REQ-SYS-004)
  dnsmasq --no-daemon --user=root --interface=br0 --bind-interfaces --port=0 --dhcp-range=172.30.77.100,172.30.77.150,2m \
    $nogw --log-dhcp --log-facility=/w/dnsmasq.log 2>/dev/null &
  tcpdump -q -n -U -i br0 -w /w/net.pcap 2>/dev/null & TD=$!
  tm "сеть и dnsmasq готовы, старт машины"
  timeout 1500 qemu-system-aarch64 -M "$QM" -cpu "$QCPU" -accel "$QACCEL" -smp 4 -m 3072 -no-reboot \
    -kernel /kit/vmlinuz -initrd /w/initrd.gz -append "console=ttyAMA0 shboot.scenario=$SC panic=-1 systemd.show_status=0" \
    -drive if=virtio,format=qcow2,file=/w/overlay.qcow2 -drive if=virtio,format=raw,file=/kit/modules.ext4,readonly=on \
    -netdev tap,id=n0,ifname=tap0,script=no,downscript=no -device virtio-net-pci,netdev=n0,romfile= \
    -device virtio-serial-pci -chardev file,id=pr,path=/w/probe.log -device virtserialport,chardev=pr,name=shboot \
    -serial file:/w/serial.log -monitor none -display none
  echo "qemu код $?"; tm "машина остановлена"
  sleep 1; kill $TD 2>/dev/null; wait $TD 2>/dev/null || true
  # трафик машины (172.30.77.100–150) за пределы подсети стенда: IPv4 кроме broadcast/multicast, IPv6 кроме
  # link-local/multicast; DNS — 53 и 853 в любую сторону
  tcpdump -n -r /w/net.pcap "ip and src net 172.30.77.0/24 and not src host 172.30.77.1 and not dst net 172.30.77.0/24 and not dst host 255.255.255.255 and not ip multicast" 2>/dev/null > /w/net-ext4.txt || true
  tcpdump -n -r /w/net.pcap "ip6 and not dst net fe80::/10 and not dst net ff00::/8" 2>/dev/null > /w/net-ext6.txt || true
  tcpdump -n -r /w/net.pcap "port 53 or port 853" 2>/dev/null > /w/net-dns.txt || true
  tcpdump -n -r /w/net.pcap 2>/dev/null | wc -l > /w/net-total.txt || true
  chmod 644 /w/dnsmasq.log /w/serial.log /w/probe.log /w/net* 2>/dev/null || true
  rm -f /w/disk.img /w/overlay.qcow2'
rc=$?
set -e
S="$W/probe.log"
[ -s "$W/serial.log" ] || { echo "ERROR: нет журнала консоли машины (код $rc)"; exit 3; }
grep -q "^SHBOOT end" "$S" 2>/dev/null || { echo "ERROR: проба не завершилась (зависание загрузки?); хвост консоли:"
  sed 's/\x1b\[[0-9;:]*m//g' "$W/serial.log" | tail -40; exit 3; }
if [ "$SC" = buildhost ]; then
  KEYS="state failed hostname growroot rootfs bootfs addr gw sshd permitroot password rootkey docker dockerinfo groups tools sources wdt zram swapunit"
else
  KEYS="state failed hostname1 static hostname2 hostname3 default sources chrony permitroot rootkey var gw dns errunits"
fi
# каждая строка пробы обязательна: потерянная строка — провал, а не «пусто = хорошо»
for k in $KEYS; do
  grep -q "^SHBOOT $k=" "$S" || { echo "ERROR: в итоге пробы нет строки $k"; exit 3; }
done
v() { sed -n "s/^SHBOOT $1=//p" "$S" | tail -1 | tr -d '\r'; }

FAIL=0
ok()  { echo "PASS $1: $2"; }
bad() { echo "FAIL $1: $2"; FAIL=1; }
eq()  { if [ "$2" = "$3" ]; then ok "$1" "$2"; else bad "$1" "$2 (ожидалось $3)"; fi; }
echo "состояние системы: $(v state)"
if [ "$SC" = buildhost ]; then
  case "$(v state)" in running|degraded) ok "загрузка завершена" "$(v state)";; *) bad "загрузка завершена" "$(v state)";; esac
  eq "упавшие юниты" "$(v failed | sed 's/ *$//')" ""
  eq "имя" "$(v hostname)" "$WANT"
  eq "расширение корня (shbuild-growroot)" "$(v growroot)" "success/done"
  case "$(v rootfs)" in /dev/vda2\ ext4*) ok "корень" "$(v rootfs)";; *) bad "корень" "$(v rootfs)";; esac
  case "$(v bootfs)" in *vfat) ok "/boot/firmware" "$(v bootfs)";; *) bad "/boot/firmware" "$(v bootfs)";; esac
  case "$(v addr)" in 172.30.77.*) ok "адрес по DHCP" "$(v addr)";; *) bad "адрес по DHCP" "$(v addr)";; esac
  eq "маршрут по умолчанию" "$(v gw | sed 's/ *$//')" "172.30.77.1"
  eq "sshd активен" "$(v sshd)" "active"
  eq "sshd: root только по ключу" "$(v permitroot)" "without-password"
  eq "sshd: пароли выключены" "$(v password)" "no"
  eq "ключ root = ключ shbuild" "$(v rootkey)" "same"
  eq "docker активен" "$(v docker)" "active"
  case "$(v dockerinfo)" in [0-9]*\ *\ aarch64) ok "docker info отвечает" "$(v dockerinfo)";; *) bad "docker info отвечает" "$(v dockerinfo)";; esac
  case " $(v groups) " in *" docker "*) ok "shbuild в группе docker" "$(v groups)";; *) bad "shbuild в группе docker" "$(v groups)";; esac
  eq "инструменты сборки" "$(v tools | sed 's/ *$//')" "git zstd rsync python3 growpart resize2fs nvme tmux"
  eq "NTP (timesyncd)" "$(v sources)" "yes"
  eq "аппаратный сторож systemd" "$(v wdt)" "30s"
  case "$(v zram)" in *zram*) ok "zram-подкачка" "$(v zram)";; *) bad "zram-подкачка" "нет";; esac
  eq "shbuild-swap (в тесте диск мал — пропуск без ошибки)" "$(v swapunit)" "success"
  if [ $FAIL != 0 ]; then echo "--- журналы упавших юнитов"; sed -n 's/^SHBOOT log //p' "$S" | tail -60
    echo "--- сеть"; sed -n 's/^SHBOOT net //p' "$S" | tail -60; fi
  exit $FAIL
fi
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
if [ "$SC" = offline ]; then
  eq "маршрут по умолчанию (offline)" "$(v gw | sed 's/ *$//')" ""
  eq "серверы DNS (offline)" "$(v dns | sed 's/ *$//')" ""
  [ -s "$W/net-total.txt" ] || bad "захват трафика" "нет net.pcap"
  ext=$(cat "$W/net-ext4.txt" "$W/net-ext6.txt" 2>/dev/null | wc -l); dns=$(wc -l < "$W/net-dns.txt" 2>/dev/null || echo x)
  echo "захват: пакетов $(cat "$W/net-total.txt" 2>/dev/null)"
  eq "пакеты за пределы подсети стенда" "$ext" "0"
  eq "запросы DNS" "$dns" "0"
  [ "$ext" = 0 ] || head -5 "$W/net-ext4.txt" "$W/net-ext6.txt"
  [ "$dns" = 0 ] || head -5 "$W/net-dns.txt"
  # записи err, не связанные с сетью: аппаратные юниты и их запуск из init.scope; правило alsa-utils Debian
  # (GOTO без метки) — то же на плате. Остальные записи err в offline — провал.
  errs=$(sed -n 's/^SHBOOT errlog //p' "$S" | while IFS= read -r l; do
    u=${l%%:*}; m=${l#*: }; allowed "$u" && continue
    for a in $ALLOW_FAILED; do case "$m" in ("Failed to start $a"*) continue 2;; esac; done
    case "$u:$m" in ("systemd-udevd.service:/usr/lib/udev/rules.d/90-alsa-restore.rules:"*"has no matching label"*) continue;; esac
    echo "$l"; done)
  eq "записи err в журнале без сети (кроме аппаратных)" "$(printf %s "$errs" | grep -c .)" "0"
  [ -z "$errs" ] || printf '%s\n' "$errs" | head -10
fi
if [ $FAIL != 0 ]; then echo "--- журналы упавших юнитов"; sed -n 's/^SHBOOT log //p' "$S" | tail -60; fi
exit $FAIL
