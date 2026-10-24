# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Испытание DEV deploy с настоящим systemd (PID 1) в контейнере (S3.1-DEPLOY, уровень L3 — эмуляция, не плата Pi).
# Базовый образ — Debian trixie, в котором УЖЕ есть systemd, python3, python3-yaml, openssh-client и runuser
# (сеть для apt не требуется; на стенде использован локальный образ armbian-debian-trixie). Пакет sheregesh из
# текущего коммита ставится без зависимостей (Qt, GStreamer, cage не нужны): обе службы роли «без роли» —
# эмулятор (sheregesh-remote-emu) и подставной «пульт» — исполняют модуль sheregesh_remote через тот же launcher,
# что в образе, и шлют настоящие READY/WATCHDOG (Type=notify). Проверяется путь
# «пакет → установщик root → current → launcher → READY/падение → откат». polkit проверяется отдельно
# (tests/tools/test_polkit_rule.py), если в базовом образе нет polkitd/dbus.
ARG BASE=debian:trixie
FROM ${BASE}
ENV container=docker
RUN python3 -c "import yaml" && command -v ssh-keygen && command -v runuser && test -x /lib/systemd/systemd
RUN useradd -m -U -s /bin/bash sheregesh-ui
COPY sheregesh_*_all.deb /tmp/
RUN dpkg -i --force-depends /tmp/sheregesh_*_all.deb && rm /tmp/sheregesh_*_all.deb
COPY 50-sheregesh-dev.rules /etc/polkit-1/rules.d/50-sheregesh-dev.rules
COPY allowed_signers /etc/sheregesh/deploy/allowed_signers
COPY console-container.conf /etc/systemd/system/sheregesh-console.service.d/container.conf
COPY emu-container.conf /etc/systemd/system/sheregesh-remote-emu.service.d/container.conf
COPY check.sh bundles/ /opt/check/
RUN chmod 0755 /opt/check/check.sh \
    && systemctl enable sheregesh-console.service sheregesh-remote-emu.service \
    && systemctl mask getty@tty1.service console-getty.service systemd-logind.service
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT []
CMD ["/lib/systemd/systemd"]
