# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подключить устройство USB/IP через vhci-hcd без пакета usbip (root; модуль vhci-hcd загружается сам).

  python3 usbip_attach.py list HOST [--port 3240]          устройства на сервере
  python3 usbip_attach.py attach HOST BUSID [--port 3240]  подключить → печатает порт vhci
  python3 usbip_attach.py detach PORT                      отключить («выдернуть»)
  python3 usbip_attach.py status                           порты vhci

Делает то же, что `usbip attach`: OP_REQ_IMPORT по TCP, затем передаёт сокет ядру записью
«порт sockfd devid скорость» в /sys/devices/platform/vhci_hcd.0/attach.
"""
import argparse
import socket
import struct
import subprocess
import sys
from pathlib import Path

USBIP_VERSION = 0x0111
OP_REQ_DEVLIST, OP_REQ_IMPORT = 0x8005, 0x8003
VHCI = Path("/sys/devices/platform")
SPEED_HUB = {1: "hs", 2: "hs", 3: "hs", 5: "ss", 6: "ss"}     # low/full/high → порты hs; super(+) → ss
VDEV_ST_NULL = 4


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("сервер закрыл соединение")
        buf += chunk
    return buf


def parse_devinfo(raw):
    busid = raw[256:288].rstrip(b"\0").decode()
    busnum, devnum, speed, vid, pid = struct.unpack_from(">IIIHH", raw, 288)
    return {"busid": busid, "busnum": busnum, "devnum": devnum, "speed": speed, "vid": vid, "pid": pid}


def devlist(host, port):
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(struct.pack(">HHI", USBIP_VERSION, OP_REQ_DEVLIST, 0))
        _v, _code, status, ndev = struct.unpack(">HHII", recv_exact(s, 12))
        if status != 0:
            raise SystemExit(f"сервер вернул ошибку списка (status {status})")
        out = []
        for _ in range(ndev):
            raw = recv_exact(s, 312)
            recv_exact(s, 4 * raw[311])         # по 4 байта на интерфейс (класс, подкласс, протокол, выравнивание)
            out.append(parse_devinfo(raw))
        return out


def vhci_ports():
    """[(controller_dir, hub, port, status)] по всем vhci_hcd.N/status*."""
    return [(p["ctl"], p["hub"], p["port"], p["sta"]) for p in vhci_port_table()]


def vhci_port_table():
    """Строки status*: hub port sta spd dev sockfd local_busid → [{ctl, hub, port, sta, devid, local_busid}].
    devid — busnum<<16 | devnum, переданные при attach (у свободного порта 0)."""
    ports = []
    for ctl in sorted(VHCI.glob("vhci_hcd.*")):
        for st in sorted(ctl.glob("status*")):
            for line in st.read_text().splitlines()[1:]:
                f = line.split()
                if len(f) >= 3:
                    ports.append({"ctl": ctl, "hub": f[0], "port": int(f[1]), "sta": int(f[2]),
                                  "devid": int(f[4], 16) if len(f) > 4 else 0,
                                  "local_busid": f[6] if len(f) > 6 else ""})
    return ports


def port_info(vport):
    return next((p for p in vhci_port_table() if p["port"] == vport), None)


def ensure_vhci():
    """Каталог контроллера vhci_hcd.0 (модуль загружается при необходимости)."""
    if not any(VHCI.glob("vhci_hcd.*")):
        subprocess.run(["modprobe", "vhci-hcd"], check=True)
    return sorted(VHCI.glob("vhci_hcd.*"))[0]


def import_device(host, port, busid, expect=None):
    """OP_REQ_IMPORT и передача сокета ядру. expect="vvvv:pppp" — отказ ДО attach, если сервер заявил другое
    устройство. → {vport, devid, vid, pid, speed}."""
    ensure_vhci()
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(struct.pack(">HHI", USBIP_VERSION, OP_REQ_IMPORT, 0) + busid.encode().ljust(32, b"\0"))
    _v, _code, status = struct.unpack(">HHI", recv_exact(s, 8))
    if status != 0:
        raise SystemExit(f"сервер отказал в импорте {busid} (status {status}): занят или нет такого")
    info = parse_devinfo(recv_exact(s, 312))
    claimed = f"{info['vid']:04x}:{info['pid']:04x}"
    if expect and claimed != expect:
        s.close()
        raise SystemExit(f"сервер предлагает {claimed} вместо {expect} в {busid} — не подключаю")
    hub = SPEED_HUB.get(info["speed"], "hs")
    free = [(ctl, p) for ctl, h, p, st in vhci_ports() if h == hub and st == VDEV_ST_NULL]
    if not free:
        raise SystemExit(f"нет свободного порта vhci ({hub})")
    ctl, vport = free[0]
    s.settimeout(None)
    devid = info["busnum"] << 16 | info["devnum"]
    (ctl / "attach").write_text(f"{vport} {s.fileno()} {devid} {info['speed']}")
    s.close()                                   # ядро держит свою ссылку на сокет
    return {"vport": vport, "devid": devid, "vid": info["vid"], "pid": info["pid"], "speed": info["speed"]}


def attach(host, port, busid, expect=None):
    d = import_device(host, port, busid, expect)
    print(f"attached {busid} {d['vid']:04x}:{d['pid']:04x} -> vhci port {d['vport']}")
    return d["vport"]


def detach(vport):
    for ctl, _h, p, st in vhci_ports():
        if p == vport:
            if st == VDEV_ST_NULL:
                raise SystemExit(f"порт {vport} свободен")
            (ctl / "detach").write_text(str(vport))
            print(f"detached port {vport}")
            return
    raise SystemExit(f"нет порта vhci {vport}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "attach"):
        c = sub.add_parser(name)
        c.add_argument("host")
        if name == "attach":
            c.add_argument("busid")
        c.add_argument("--port", type=int, default=3240)
    sub.add_parser("detach").add_argument("vport", type=int)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for d in devlist(a.host, a.port):
            print(f"{d['busid']}  {d['vid']:04x}:{d['pid']:04x}  speed {d['speed']}")
    elif a.cmd == "attach":
        attach(a.host, a.port, a.busid)
    elif a.cmd == "detach":
        detach(a.vport)
    else:
        for ctl, hub, p, st in vhci_ports():
            print(f"{ctl.name} {hub} port {p}: {'free' if st == VDEV_ST_NULL else f'used (status {st})'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
