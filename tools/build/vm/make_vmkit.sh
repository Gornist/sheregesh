#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Набор для загрузочного теста в QEMU virt (ADR-0018): ядро Debian arm64 и его модули, busybox arm64 (initramfs),
# образ контейнера с qemu-system-aarch64 и dnsmasq. Делается один раз; повторный вызов — ничего не качает.
#   make_vmkit.sh [КАТАЛОГ]   (по умолчанию ~/shbuild/vm)
# Почему не контейнер с корнем образа: на x86 он работает через qemu-user, а тот не поддерживает сетевые
# пространства имён и песочницу systemd — journald, resolved и юниты с PrivateNetwork ложно падают (devbox 2026-10-30).
set -euo pipefail
KIT=${1:-$HOME/shbuild/vm}
mkdir -p "$KIT"
if [ -s "$KIT/vmlinuz" ] && [ -s "$KIT/modules.ext4" ] && tar -tf "$KIT/modules.tar" | grep -q "/modules.dep$" && [ -x "$KIT/busybox" ] && docker image inspect shbuild-qemu >/dev/null 2>&1; then
  echo "$KIT"; exit 0
fi
docker run --rm -v "$KIT":/kit debian:trixie bash -euo pipefail -c '
  export DEBIAN_FRONTEND=noninteractive
  dpkg --add-architecture arm64
  apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends e2fsprogs kmod >/dev/null
  cd /tmp
  apt-get download -qq linux-image-arm64:arm64 >/dev/null
  meta=$(ls linux-image-arm64_*.deb); dpkg-deb -f "$meta" Depends | tr "," "\n" | grep -o "linux-image-[0-9][^ ]*" | head -1 > real
  apt-get download -qq "$(cat real):arm64" busybox-static:arm64 >/dev/null
  mkdir k b
  dpkg-deb -x linux-image-[0-9]*.deb k; dpkg-deb -x busybox-static_*.deb b
  M=k/usr/lib/modules; [ -d "$M" ] || M=k/lib/modules          # trixie: merged /usr
  ver=$(ls "$M"); echo "ядро $ver" >&2
  mkdir -p d/lib && mv "$M" d/lib/modules && M=d/lib/modules
  depmod -b d "$ver"                                        # modules.dep в пакете нет — его делает установка
  cp k/boot/vmlinuz-$ver /kit/vmlinuz
  tar -C "$M" -cf /kit/modules.tar "$ver"
  # модули — отдельным диском vdb (поверх /usr/lib/modules образа): udev грузит их как обычно
  rm -f /kit/modules.ext4; mke2fs -q -t ext4 -L shboot-modules -d "$M" /kit/modules.ext4 1G
  echo "$ver" > /kit/kernel.version
  cp b/usr/bin/busybox /kit/busybox; chmod 755 /kit/busybox
  chown -R '"$(id -u):$(id -g)"' /kit'
docker build -q -t shbuild-qemu - >/dev/null <<'EOF'
FROM debian:trixie
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
    qemu-system-arm qemu-utils dnsmasq-base iproute2 cpio zstd xz-utils fdisk ca-certificates kmod >/dev/null \
 && rm -rf /var/lib/apt/lists/*
EOF
echo "$KIT"
