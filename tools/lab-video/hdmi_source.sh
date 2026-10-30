#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
#
# HDMI-источник стенда на devbox: тестовая картинка (hdmi_testpattern.py) на выходе GTX 1650 в сеансе GNOME
# пользователя devbox — юнит systemd --user, без sudo; GNOME не гасит экран, пока картинка идёт (gnome-session-inhibit,
# настройки не меняются).
#   tools/lab-video/hdmi_source.sh start|stop|status [ХОСТ]   (хост по умолчанию devbox)
# Конвертер MS2130 сообщает видеокарте «монитор подключён» только пока pi2 его захватывает: источник блока должен
# быть HDMI-захватом (выбор источника с пульта), иначе выход GTX отключён и картинку некуда выводить.
set -euo pipefail
CMD=${1:?start|stop|status}; HOST=${2:-devbox}
UNIT=sheregesh-hdmi-pattern
HERE=$(cd "$(dirname "$0")" && pwd)
ENVS="--setenv=WAYLAND_DISPLAY=wayland-0 --setenv=XDG_RUNTIME_DIR=/run/user/\$(id -u)"
case "$CMD" in
  start)
    ssh "$HOST" "mkdir -p ~/lab-video"
    scp -q "$HERE/hdmi_testpattern.py" "$HOST:lab-video/hdmi_testpattern.py"
    ssh "$HOST" "systemctl --user stop $UNIT 2>/dev/null; systemctl --user reset-failed $UNIT 2>/dev/null; \
      systemd-run --user --unit $UNIT $ENVS gnome-session-inhibit --inhibit idle --reason 'стенд: HDMI-источник' \
      python3 \$HOME/lab-video/hdmi_testpattern.py && sleep 3 && systemctl --user is-active $UNIT" ;;
  stop)   ssh "$HOST" "systemctl --user stop $UNIT; systemctl --user reset-failed $UNIT 2>/dev/null; echo остановлен" ;;
  status) ssh "$HOST" "systemctl --user is-active $UNIT; journalctl --user -u $UNIT -n 5 --no-pager -o cat" ;;
  *) echo "start|stop|status" >&2; exit 2 ;;
esac
