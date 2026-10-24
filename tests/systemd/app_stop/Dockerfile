# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Изолированный systemd (PID 1) для испытания останова пульта (app-stop): Debian trixie, как образ платы.
FROM debian:trixie
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus libpam-systemd login procps python3 python3-pytest \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && useradd -r -m -d /home/sheregesh-ui -s /usr/sbin/nologin sheregesh-ui
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
