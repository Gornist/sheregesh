#!/bin/bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Установить эмулированные входы vivid на shcam (Raspberry Pi 5, Pi OS trixie, пользователь с sudo):
#   tools/lab-video/shcam/install.sh cam@192.168.1.193 [--with-mediamtx]
# Ставит v4l-utils и GStreamer, vivid с параметрами, udev-имена /dev/shcam/*, shcam-sig/shcam-feed и службы,
# эмулятор USB/IP (usbip_uvc.py, порт 3240) в /usr/local/lib/shcam.
# --with-mediamtx — ещё и etc/mediamtx.yml + units/mediamtx.service (бинарник /opt/mediamtx ставится отдельно, см. README).
# Повторный запуск безопасен: файлы перезаписываются, службы перезапускаются; mediamtx без флага не трогается.
set -euo pipefail
HOST=${1:?usage: install.sh user@host [--with-mediamtx]}
WITH_MTX=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
read -ra EXTRA <<< "${SHCAM_SSH_OPTS:-}"   # например SHCAM_SSH_OPTS="-o UserKnownHostsFile=~/.ssh/known_hosts.shcam"
SSH=(ssh -o BatchMode=yes ${EXTRA[@]+"${EXTRA[@]}"})
STAGE=/tmp/shcam-install

"${SSH[@]}" "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
scp -q -o BatchMode=yes ${EXTRA[@]+"${EXTRA[@]}"} -r "$HERE/bin" "$HERE/etc" "$HERE/units" "$HERE/../usbip_uvc.py" \
    "$HERE/../usbip_attach.py" "$HOST:$STAGE/"
"${SSH[@]}" "$HOST" "WITH_MTX='$WITH_MTX' STAGE=$STAGE bash -s" <<'REMOTE'
set -euo pipefail
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq v4l-utils gstreamer1.0-tools gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav >/dev/null
sudo install -m 0644 $STAGE/etc/modprobe-shcam-vivid.conf /etc/modprobe.d/shcam-vivid.conf
sudo install -m 0644 $STAGE/etc/modules-load-shcam-vivid.conf /etc/modules-load.d/shcam-vivid.conf
sudo install -m 0644 $STAGE/etc/99-shcam-vivid.rules /etc/udev/rules.d/99-shcam-vivid.rules
[ -e /etc/default/shcam-video ] || sudo install -m 0644 $STAGE/etc/shcam-video.default /etc/default/shcam-video
sudo install -m 0755 $STAGE/bin/shcam-sig /usr/local/sbin/shcam-sig
sudo ln -sf /usr/local/sbin/shcam-sig /usr/local/bin/shcam-sig
sudo install -m 0755 $STAGE/bin/shcam-feed /usr/local/bin/shcam-feed
sudo install -m 0644 $STAGE/units/shcam-vivid-setup.service $STAGE/units/shcam-feed@.service \
    $STAGE/units/shcam-usbip-uvc.service /etc/systemd/system/
sudo install -D -m 0644 $STAGE/usbip_uvc.py /usr/local/lib/shcam/usbip_uvc.py
sudo install -D -m 0644 $STAGE/usbip_attach.py /usr/local/lib/shcam/usbip_attach.py
if [ "$WITH_MTX" = --with-mediamtx ]; then
    sudo install -D -m 0644 $STAGE/etc/mediamtx.yml /etc/mediamtx/mediamtx.yml
    sudo install -m 0644 $STAGE/units/mediamtx.service /etc/systemd/system/mediamtx.service
fi
sudo systemctl daemon-reload
sudo udevadm control --reload
# vivid с другими параметрами (или без) — перезагрузить модуль; занятые узлы держат только наши подачи
for u in shcam-feed@cvbs shcam-feed@hdmi; do sudo systemctl stop "$u" 2>/dev/null || true; done
want="2"; have=$(cat /sys/module/vivid/parameters/n_devs 2>/dev/null || echo none)
if [ "$have" != "$want" ] || [ "$(cat /sys/module/vivid/parameters/input_types 2>/dev/null)" != "2,3" ]; then
    sudo modprobe -r vivid 2>/dev/null || true
    sudo modprobe vivid
fi
sudo udevadm trigger --subsystem-match=video4linux --action=add && sudo udevadm settle
if [ "$WITH_MTX" = --with-mediamtx ]; then sudo systemctl enable --now mediamtx; fi
sudo systemctl enable shcam-vivid-setup.service shcam-feed@cvbs.service shcam-feed@hdmi.service shcam-usbip-uvc.service
sudo systemctl restart shcam-vivid-setup.service
sudo systemctl start shcam-feed@cvbs shcam-feed@hdmi
sudo systemctl restart shcam-usbip-uvc
rm -rf $STAGE
sleep 4
shcam-sig status
REMOTE
