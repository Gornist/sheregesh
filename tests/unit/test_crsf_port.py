# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""S8 (ADR-0014, раздел 3.1): порты CRSF — структура termios2 и номера ioctl (без вызова ioctl ядра: фейк), откат
на обычный termios на псевдотерминале, эхо полудуплекса и коллизии на фейковой линии (pty), поиск UART3 по sysfs."""
import errno
import os
import select

import pytest

from sheregesh.crsf import frames as F
from sheregesh.crsf import port as P
from sheregesh_emulators.devices import SimClock


def test_ioctl_numbers_and_constants():
    assert P.TERMIOS2.size == 44
    assert P.TCGETS2 == 0x802C542A and P.TCSETS2 == 0x402C542B
    assert P.L_BOTHER == 0o010000 and P.L_CBAUD == 0o010017 and P.L_CIBAUD == 0x100F0000
    assert P.L_CS8 == 0o60 and P.L_CREAD == 0o200 and P.L_CLOCAL == 0o4000 and P.L_CRTSCTS == 0x80000000
    assert (P.L_VTIME, P.L_VMIN, P.NCCS) == (5, 6, 19)


def cooked():
    """Типичная структура после открытия tty: B38400, эхо, канонический режим, VMIN=1."""
    cc = bytearray(19)
    cc[P.L_VMIN], cc[P.L_VTIME] = 1, 0
    return P.TERMIOS2.pack(P.L_ICRNL | P.L_IXON | P.L_BRKINT, P.L_OPOST | 0o4,
                           0o17 | P.L_CS8 | P.L_CREAD | P.L_HUPCL | P.L_PARENB | P.L_CRTSCTS | (0o17 << 16),
                           P.L_ISIG | P.L_ICANON | P.L_ECHO | P.L_IEXTEN, 0, bytes(cc), 38400, 38400)


def test_termios2_raw_400k_8n1():
    iflag, oflag, cflag, lflag, line, cc, isp, osp = P.TERMIOS2.unpack(P.termios2_raw(cooked(), 400000))
    assert cflag & P.L_CBAUD == P.L_BOTHER and (cflag >> P.L_IBSHIFT) & P.L_CBAUD == P.L_BOTHER
    assert cflag & P.L_CSIZE == P.L_CS8 and cflag & P.L_CREAD and cflag & P.L_CLOCAL
    assert not cflag & (P.L_PARENB | P.L_CSTOPB | P.L_CRTSCTS | P.L_HUPCL)
    assert iflag == 0 and oflag & P.L_OPOST == 0 and lflag == 0
    assert cc[P.L_VMIN] == 0 and cc[P.L_VTIME] == 0 and (isp, osp) == (400000, 400000)


def test_configure_termios2_with_fake_ioctl():
    calls = []

    def fake(fd, req, arg):
        calls.append((fd, req, bytes(arg)))
        return cooked() if req == P.TCGETS2 else arg

    assert P.configure_termios2(7, 400000, ioctl=fake) is True
    assert [c[1] for c in calls] == [P.TCGETS2, P.TCSETS2]
    assert len(calls[0][2]) == 44 and calls[1][2] == P.termios2_raw(cooked(), 400000)

    def unsupported(fd, req, arg):
        raise OSError(errno.ENOTTY, "Inappropriate ioctl for device")

    assert P.configure_termios2(7, 400000, ioctl=unsupported) is False

    def denied(fd, req, arg):
        raise OSError(errno.EPERM, "Operation not permitted")

    with pytest.raises(OSError):
        P.configure_termios2(7, 400000, ioctl=denied)


def test_find_uart3(tmp_path):
    sysfs, dt = tmp_path / "tty", tmp_path / "dt"
    for name, node in (("ttyAMA0", "serial@7e201000"), ("ttyAMA1", "serial@7e201600"), ("ttyS0", "serial@7e215040")):
        (dt / node).mkdir(parents=True)
        (sysfs / name / "device").mkdir(parents=True)
        os.symlink(dt / node, sysfs / name / "device" / "of_node")
    (sysfs / "tty1").mkdir()                                        # виртуальный tty без device
    assert P.find_uart3(str(sysfs)) == "/dev/ttyAMA1"
    os.unlink(sysfs / "ttyAMA1" / "device" / "of_node")
    assert P.find_uart3(str(sysfs)) is None
    assert P.find_uart3(str(tmp_path / "missing")) is None


def read_wait(fd, n, timeout=2.0):
    out = b""
    while len(out) < n:
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            break
        out += os.read(fd, 4096)
    return out


def read_port(port, timeout=2.0):
    """Порт неблокирующий: подождать данных на fd, затем read()."""
    select.select([port.fileno()], [], [], timeout)
    return port.read()


@pytest.fixture
def line():
    """Фейковая линия: ведущая сторона pty — «провод», UartPort открыт на ведомой."""
    master, slave = os.openpty()
    path = os.ttyname(slave)
    clock = SimClock(10.0)
    port = P.UartPort(path, 400000, half_duplex=True, clock=clock)
    yield master, port, clock
    port.close()
    os.close(master)
    os.close(slave)


def test_half_duplex_echo_stripped(line):
    master, port, clock = line
    frame = F.pack_channels([0] * 16, armed=False)
    assert port.write(frame) == len(frame)
    assert read_wait(master, len(frame)) == frame                    # ушло в линию как есть (raw)
    reply = F.timing_frame(20000, 0)
    os.write(master, frame[:10])                                     # эхо приходит частями
    assert read_port(port) == b""
    os.write(master, frame[10:] + reply)
    assert read_port(port) == reply
    assert port.collisions == 0 and port.echo_bytes == len(frame) and port.echo_missing == 0


def test_half_duplex_collision_and_missing_echo(line):
    master, port, clock = line
    frame = F.ping()
    port.write(frame)
    read_wait(master, len(frame))
    garbled = bytes((frame[0], frame[1] ^ 0x40)) + frame[2:]        # модуль говорил одновременно
    reply = F.link_stats_frame()
    os.write(master, garbled[:3])
    assert read_port(port) == b"" and port.collisions == 1
    os.write(master, garbled[3:] + reply)                            # остаток своей передачи — без сравнения
    assert read_port(port) == reply and port.collisions == 1
    port.write(frame)                                                # эха нет (линия не однопроводная)
    read_wait(master, len(frame))
    assert port.read() == b"" and port.echo_missing == 0
    clock.advance(0.1)
    assert port.read() == b"" and port.echo_missing == 1
    os.write(master, reply)
    assert read_port(port) == reply


def test_uart_port_on_pty_falls_back(line):
    master, port, clock = line
    if not os.uname().sysname == "Linux":
        assert port.termios2 is False                                # вне Linux — обычный termios


def test_pty_port_pair():
    port = P.PtyPort()
    fd = os.open(port.path, os.O_RDWR | os.O_NOCTTY)
    try:
        P.configure_termios(fd)
        frame = F.pack_channels([1000] * 16, armed=True)
        port.write(frame)
        assert read_wait(fd, len(frame)) == frame
        os.write(fd, F.ping() + b"\r\n\x03")                         # raw: \r, \n, ^C без преобразований
        assert read_port(port) == F.ping() + b"\r\n\x03"
        assert port.read() == b"" and port.collisions == 0 and not port.half_duplex
    finally:
        os.close(fd)
        port.close()
