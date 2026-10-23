# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Настройка доверия между платами лабораторного стенда (S3.1) с машины владельца тестового CA.

  python3 tools/lab-pki/labpki.py init                                  создать CA стенда в ~/.sheregesh-lab-ca
  python3 tools/lab-pki/labpki.py enroll --host 192.168.1.89 --role console   ключ на плате → подпись → установка
  python3 tools/lab-pki/labpki.py pair --console 192.168.1.89 --remote 192.168.1.169
                                                                        выдать сертификаты обеим и связать их
  python3 tools/lab-pki/labpki.py status --host 192.168.1.89

Как устроено: ключ устройства создаётся на плате (sheregesh-link keygen) и не покидает её; сюда приходит только CSR.
CSR подписывается, если в нём ровно ожидаемый URI «urn:sheregesh:<роль>:<серийный номер платы>». Сертификат и CA
передаются на плату, она сама проверяет соответствие ключу, цепочку и URI (sheregesh-link install). Повторный enroll —
замена сертификата (новый ключ). Отзыв устройства — новый CA и повторный pair обеих плат.

Ключ CA — в ~/.sheregesh-lab-ca (0600), вне репозитория; это тестовый CA стенда, не продуктивная PKI (ADR-0005).
Вход на платы — SSH dev-образа (sheregesh-ui, ключ владельца).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "software"))
from sheregesh.link import pki  # noqa: E402

CA_DIR = Path("~/.sheregesh-lab-ca").expanduser()
PEER = {"console": "remote", "remote": "console"}


def ssh(host, cmd, inp=None):
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"sheregesh-ui@{host}", cmd],
                       input=inp, capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise SystemExit(f"{host}: {cmd.split()[0]} {cmd.split()[1] if len(cmd.split()) > 1 else ''}: "
                         f"{(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


def enroll(host, role):
    if not (CA_DIR / "ca.key").exists():
        raise SystemExit(f"нет CA стенда в {CA_DIR}: python3 tools/lab-pki/labpki.py init")
    dev_id = ssh(host, "sheregesh-link device-id").strip()
    csr = ssh(host, f"sheregesh-link keygen --role {role}")
    cert = pki.ca_sign(CA_DIR, csr, role, dev_id)
    bundle = json.dumps({"cert": cert, "ca": (CA_DIR / "ca.crt").read_text()})
    st = json.loads(ssh(host, f"sheregesh-link install --role {role}", bundle))
    print(f"{host}: {role}:{dev_id} — сертификат до {st['device.crt']['notAfter']}, "
          f"ключ {st['key']['mode']}, URI {st['device.crt']['uris']}")
    return dev_id


def configure(host, role, peer_id, peer_host):
    out = ssh(host, f"sheregesh-link configure --role {role} --peer-id {peer_id} --peer-host {peer_host} --discover auto")
    print(f"{host}: {out.strip()}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    e = sub.add_parser("enroll")
    e.add_argument("--host", required=True)
    e.add_argument("--role", choices=PEER, required=True)
    p = sub.add_parser("pair")
    p.add_argument("--console", required=True, help="адрес платы-пульта (сейчас; доверие — по сертификату)")
    p.add_argument("--remote", required=True, help="адрес платы удалённого блока")
    s = sub.add_parser("status")
    s.add_argument("--host", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "init":
        crt = pki.ca_init(CA_DIR)
        print(f"CA стенда: {crt} (ключ {CA_DIR / 'ca.key'}, вне репозитория)")
    elif a.cmd == "enroll":
        enroll(a.host, a.role)
    elif a.cmd == "pair":
        cid, rid = enroll(a.console, "console"), enroll(a.remote, "remote")
        configure(a.console, "console", f"remote:{rid}", a.remote)
        configure(a.remote, "remote", f"console:{cid}", a.console)
        print("Готово. Применение: перезапуск служб на платах (systemctl restart sheregesh-console / sheregesh-remote-net).")
    elif a.cmd == "status":
        print(ssh(a.host, "sheregesh-link status"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
