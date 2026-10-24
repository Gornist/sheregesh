# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S3.3: видеовход удалённого блока (--video-source uvc). Без устройства блок работает и сообщает причину, видео не
объявляет и устройство не ищет без явного выбора. Настоящее устройство (L4) здесь не проверяется."""
import json
import socket

from procs import Agent, wait_for
from sheregesh import PROTOCOL_VERSION
from sheregesh.link.codec import encode


def test_agent_without_uvc_device_keeps_running_and_reports_reason(tmp_path):
    # Корни sysfs/dev в процессе не подменить: на машине CI устройств UVC нет (на macOS нет и /sys).
    a = Agent("--video", "--video-source", "uvc", "--video-device", "ffff:fffe")
    try:
        line = a.proc.stdout.readline().strip()
        assert line.startswith("VIDEO unavailable: UVC device ffff:fffe not found"), (line, a.banner)
        with socket.create_connection(("127.0.0.1", a.port), timeout=5) as s:
            s.sendall(encode(PROTOCOL_VERSION, "hello", 1, 0.0, {"role": "console", "proto": PROTOCOL_VERSION,
                                                                  "software": "t", "device_id": "t"}))
            buf = b""

            def status():
                nonlocal buf
                try:
                    buf += s.recv(65536)
                except socket.timeout:
                    pass
                for raw in buf.split(b"\n"):
                    if b'"status"' in raw:
                        return json.loads(raw)
            msg = wait_for(status, 5)
        assert msg and msg["body"]["video"] == {"enabled": False, "active": False, "codec": None, "port": 5600}
        assert a.proc.poll() is None
    finally:
        a.stop()
