# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Порты CRSF (ADR-0014, раздел 3.1): UART pi2 (полудуплекс, произвольная скорость) и псевдотерминал эмулятора.

UartPort — os.open + termios2 через fcntl.ioctl (TCGETS2/TCSETS2, BOTHER: 400000 бод не входит в набор Bxxx),
8N1, raw, неблокирующий. Вне Linux и там, где ioctl termios2 не поддерживается (ENOTTY/EINVAL — например,
псевдотерминал), — обычный termios: raw, скорость только из стандартного набора, иначе не трогается.

Полудуплекс (один провод, линия Nomad): приёмник слышит собственную передачу. Отправленные байты ждут в очереди эха и
вычитываются из принятого байт в байт; расхождение — коллизия (модуль говорил одновременно с хостом): счётчик
collisions, байты, пришедшие за время своей передачи, отбрасываются без сравнения. Эхо, не пришедшее за время
передачи плюс запас, — echo_missing (линия собрана не однопроводной или приёмник глух), очередь эха сбрасывается.

Константы termios2 — из заголовков ядра Linux (include/uapi/asm-generic/termbits.h, termbits-common.h, ioctls.h),
общих для aarch64 и x86_64: struct termios2 = 4 × u32 флагов, c_line, c_cc[19], c_ispeed, c_ospeed — 44 байта.
"""
import errno
import fcntl
import os
import select
import struct
import sys
import termios
import time
import tty

# ---------- termios2 (Linux, asm-generic) ----------
NCCS = 19
TERMIOS2 = struct.Struct("=IIIIB19sII")          # 44 байта, без выравнивания (все поля уже выровнены)


def _ioc(direction, kind, nr, size):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


TCGETS2 = _ioc(2, "T", 0x2A, TERMIOS2.size)      # _IOR('T', 0x2A, struct termios2) = 0x802C542A
TCSETS2 = _ioc(1, "T", 0x2B, TERMIOS2.size)      # _IOW('T', 0x2B, struct termios2) = 0x402C542B

# флаги asm-generic (значения Linux, а не платформы, на которой собирается структура)
L_IGNBRK, L_BRKINT, L_IGNPAR, L_PARMRK, L_INPCK, L_ISTRIP = 0o1, 0o2, 0o4, 0o10, 0o20, 0o40
L_INLCR, L_IGNCR, L_ICRNL, L_IUCLC, L_IXON, L_IXANY, L_IXOFF, L_IMAXBEL = (0o100, 0o200, 0o400, 0o1000, 0o2000,
                                                                           0o4000, 0o10000, 0o20000)
L_OPOST = 0o1
L_CBAUD, L_CSIZE, L_CS8, L_CSTOPB, L_CREAD, L_PARENB, L_PARODD = 0o10017, 0o60, 0o60, 0o100, 0o200, 0o400, 0o1000
L_HUPCL, L_CLOCAL, L_BOTHER, L_CIBAUD, L_CRTSCTS = 0o2000, 0o4000, 0o10000, 0o2003600000, 0o20000000000
L_IBSHIFT = 16
L_ISIG, L_ICANON, L_ECHO, L_ECHOE, L_ECHOK, L_ECHONL, L_IEXTEN = 0o1, 0o2, 0o10, 0o20, 0o40, 0o100, 0o100000
L_VTIME, L_VMIN = 5, 6

UART3_OF_NODE = "serial@7e201600"                 # UART3 BCM2711 (dtoverlay=uart3)


def termios2_raw(raw: bytes, baud: int) -> bytes:
    """Структура termios2 (как вернул TCGETS2) → raw 8N1 с произвольной скоростью baud (BOTHER), VMIN=VTIME=0."""
    iflag, oflag, cflag, lflag, line, cc, _isp, _osp = TERMIOS2.unpack(raw)
    iflag &= ~(L_IGNBRK | L_BRKINT | L_IGNPAR | L_PARMRK | L_INPCK | L_ISTRIP | L_INLCR | L_IGNCR | L_ICRNL | L_IUCLC
               | L_IXON | L_IXANY | L_IXOFF | L_IMAXBEL)
    oflag &= ~L_OPOST
    lflag &= ~(L_ISIG | L_ICANON | L_ECHO | L_ECHOE | L_ECHOK | L_ECHONL | L_IEXTEN)
    cflag &= ~(L_CBAUD | L_CIBAUD | L_CSIZE | L_CSTOPB | L_PARENB | L_PARODD | L_HUPCL | L_CRTSCTS)
    cflag |= L_BOTHER | (L_BOTHER << L_IBSHIFT) | L_CS8 | L_CREAD | L_CLOCAL
    cc = bytearray(cc.ljust(NCCS, b"\x00")[:NCCS])
    cc[L_VMIN] = 0
    cc[L_VTIME] = 0
    return TERMIOS2.pack(iflag & 0xFFFFFFFF, oflag & 0xFFFFFFFF, cflag & 0xFFFFFFFF, lflag & 0xFFFFFFFF, line,
                         bytes(cc), int(baud), int(baud))


def configure_termios2(fd: int, baud: int, ioctl=fcntl.ioctl) -> bool:
    """termios2 на fd: True — скорость установлена; False — ioctl не поддерживается (ENOTTY/EINVAL/вне Linux)."""
    if not sys.platform.startswith("linux") and ioctl is fcntl.ioctl:
        return False
    try:
        cur = ioctl(fd, TCGETS2, bytes(TERMIOS2.size))
        ioctl(fd, TCSETS2, termios2_raw(bytes(cur), baud))
    except OSError as e:
        if e.errno in (errno.ENOTTY, errno.EINVAL, errno.ENOSYS):
            return False
        raise
    return True


def configure_termios(fd: int, baud=None) -> None:
    """Обычный termios: raw, 8N1, без управления потоком; скорость — только из стандартного набора Bxxx."""
    tty.setraw(fd, termios.TCSANOW)
    attr = termios.tcgetattr(fd)
    attr[2] = (attr[2] & ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | getattr(termios, "CRTSCTS", 0))
               | termios.CS8 | termios.CREAD | termios.CLOCAL)
    attr[6][termios.VMIN] = 0
    attr[6][termios.VTIME] = 0
    speed = getattr(termios, f"B{int(baud)}", None) if baud else None
    if speed is not None:
        attr[4] = attr[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attr)


def find_uart3(sysfs: str = "/sys/class/tty", dev: str = "/dev"):
    """Устройство tty, чей device/of_node указывает на serial@7e201600 (UART3 BCM2711); нет — None."""
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return None
    for name in names:
        node = os.path.join(sysfs, name, "device", "of_node")
        try:
            target = os.path.realpath(node)
        except OSError:
            continue
        if os.path.exists(node) and os.path.basename(target.rstrip("/")) == UART3_OF_NODE:
            return os.path.join(dev, name)
    return None


# ---------- общее: неблокирующие чтение и запись, эхо ----------
class _FdPort:
    """Чтение/запись неблокирующего fd; при half_duplex — вычитывание эха (см. модуль)."""

    WRITE_WAIT_S = 0.02

    def __init__(self, fd: int, baud: int, half_duplex: bool, clock=time.monotonic):
        self.fd = fd
        self.baud = int(baud)
        self.half_duplex = bool(half_duplex)
        self.clock = clock
        self._echo = bytearray()          # ожидаемое эхо
        self._echo_skip = 0               # байты своей передачи после коллизии: отбросить без сравнения
        self._echo_deadline = 0.0
        self.collisions = self.echo_missing = self.echo_bytes = 0
        self.rx_bytes = self.tx_bytes = self.write_errors = 0

    def fileno(self) -> int:
        return self.fd

    def write(self, data: bytes) -> int:
        """Весь кадр или ничего лишнего: короткая запись дожидается готовности до 20 мс, остаток — write_errors."""
        view = memoryview(bytes(data))
        sent = 0
        deadline = None
        while sent < len(view):
            try:
                n = os.write(self.fd, view[sent:])
            except BlockingIOError:
                n = 0
            sent += n
            if sent < len(view):
                now = time.monotonic()
                deadline = deadline or now + self.WRITE_WAIT_S
                if now >= deadline:
                    self.write_errors += 1
                    break
                select.select([], [self.fd], [], max(0.0, deadline - now))
        self.tx_bytes += sent
        if self.half_duplex and sent:
            self._echo += view[:sent]
            # время передачи (10 бит на байт) плюс запас на задержки драйвера
            self._echo_deadline = self.clock() + (len(self._echo) + self._echo_skip) * 10.0 / self.baud + 0.02
        return sent

    def read(self) -> bytes:
        """Всё принятое к этому моменту (без эха при half_duplex); пусто — нет данных."""
        chunks = []
        while True:
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EIO:          # псевдотерминал: другой конец закрыт
                    break
                raise
            if not data:
                break
            chunks.append(data)
            if len(data) < 4096:
                break
        data = b"".join(chunks)
        self.rx_bytes += len(data)
        if self.half_duplex:
            data = self._strip_echo(data)
        return data

    def _strip_echo(self, data: bytes) -> bytes:
        i = 0
        if self._echo_skip:
            k = min(self._echo_skip, len(data))
            self._echo_skip -= k
            i = k
        while self._echo and i < len(data):
            if data[i] == self._echo[0]:
                del self._echo[0]
                self.echo_bytes += 1
                i += 1
                continue
            # коллизия: оставшиеся байты своей передачи испорчены — отбрасываются без сравнения
            self.collisions += 1
            skip = len(self._echo)
            self._echo.clear()
            k = min(skip, len(data) - i)
            i += k
            self._echo_skip = skip - k
        if (self._echo or self._echo_skip) and i >= len(data) and self.clock() > self._echo_deadline:
            self.echo_missing += 1
            self._echo.clear()
            self._echo_skip = 0
        return data[i:]

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def stats(self) -> dict:
        return {"rx_bytes": self.rx_bytes, "tx_bytes": self.tx_bytes, "collisions": self.collisions,
                "echo_missing": self.echo_missing, "write_errors": self.write_errors}


class UartPort(_FdPort):
    """UART pi2 для линии модуля: path, скорость baud (произвольная через termios2), полудуплекс с эхом."""

    def __init__(self, path: str, baud: int = 400000, half_duplex: bool = True, clock=time.monotonic):
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self.termios2 = configure_termios2(fd, baud)
            if not self.termios2:
                configure_termios(fd, baud)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except BaseException:
            os.close(fd)
            raise
        super().__init__(fd, baud, half_duplex, clock)
        self.path = path


class PtyPort(_FdPort):
    """Псевдотерминал для лабораторного эмулятора: хост — ведущая сторона, path — ведомая (передать эмулятору).

    Ведомая сторона остаётся открытой и у хоста: выход эмулятора не превращает чтение в EIO. Эха нет.
    """

    def __init__(self, clock=time.monotonic):
        master, slave = os.openpty()
        try:
            configure_termios(slave)
            os.set_blocking(master, False)
            self.path = os.ttyname(slave)
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        super().__init__(master, 400000, False, clock)
        self._slave = slave
        self.termios2 = False

    def close(self) -> None:
        super().close()
        if self._slave >= 0:
            os.close(self._slave)
            self._slave = -1
