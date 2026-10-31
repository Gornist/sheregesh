# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# Видеоконвейеры SRTP под tc netem (только devbox): Debian trixie, GStreamer с x264/avdec/srtp, iproute2.
FROM debian:trixie-slim
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      python3 python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gstreamer1.0-plugins-base \
      gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav iproute2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
