# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""PKI лабораторного стенда (S3.1): ключи устройств, запросы, подпись тестовым CA, установка и замена сертификатов.

На устройстве (команда sheregesh-link, пользователь sheregesh-ui):
  sheregesh-link device-id                          серийный номер платы
  sheregesh-link keygen --role console              новый ключ EC P-256 (0600) и CSR в stdout; ключ не покидает плату
  sheregesh-link install < bundle.json              {"cert": PEM, "ca": PEM}: проверка ключа, цепочки и URI, атомарная замена
  sheregesh-link configure --role R --peer-id ID [--peer-host IP]... [--discover auto]
  sheregesh-link status                             срок действия, URI, отпечатки (без ключей)

На машине владельца CA (tools/lab-pki/labpki.py): ca_init, ca_sign. Ключ CA хранится вне репозитория.
Это тестовый CA лабораторного стенда, не продуктивная PKI (ADR-0005 не затрагивается).
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .tls import DEFAULT_CONFIG, DEFAULT_DIR, ROLES, LinkConfig, LinkConfigError, cert_info, cert_uris, device_uri

CERT_DAYS = 365
CA_DAYS = 730


def openssl(*args, input=None):
    r = subprocess.run(["openssl", *args], input=input, capture_output=True, text=isinstance(input, str) or input is None)
    if r.returncode:
        raise RuntimeError(f"openssl {args[0]}: {(r.stderr or '').strip()[-300:]}")
    return r.stdout


def device_id():
    """Серийный номер платы (Raspberry Pi); вне Pi — SHEREGESH_DEVICE_ID."""
    if os.environ.get("SHEREGESH_DEVICE_ID"):
        return os.environ["SHEREGESH_DEVICE_ID"]
    try:
        return Path("/proc/device-tree/serial-number").read_text().strip("\0\n ")
    except OSError:
        raise SystemExit("нет серийного номера платы; задайте SHEREGESH_DEVICE_ID") from None


def keygen(role, dev_id, d: Path) -> str:
    """Новый ключ устройства и CSR (PEM). Прежний ключ сохраняется до установки нового сертификата."""
    if role not in ROLES:
        raise ValueError(role)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = os.umask(0o077)
    try:
        new_key = d / "device.key.new"
        openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(new_key))
        os.chmod(new_key, 0o600)
        csr = openssl("req", "-new", "-key", str(new_key), "-subj", f"/O=Sheregesh lab/CN={role}-{dev_id}",
                      "-addext", f"subjectAltName=URI:{device_uri(role, dev_id)}")
    finally:
        os.umask(old)
    (d / "device.csr").write_text(csr)
    return csr


def _pub(args):
    return openssl(*args)


def install(d: Path, cert_pem: str, ca_pem: str, role=None, dev_id=None) -> dict:
    """Проверить и установить сертификат: ключ соответствует, цепочка до CA, URI — свой. Замена атомарная."""
    with tempfile.TemporaryDirectory() as tmp:
        cert, ca = Path(tmp) / "c.pem", Path(tmp) / "ca.pem"
        cert.write_text(cert_pem)
        ca.write_text(ca_pem)
        key = d / "device.key.new" if (d / "device.key.new").exists() else d / "device.key"
        if not key.exists():
            raise SystemExit("нет ключа устройства: сначала sheregesh-link keygen")
        if _pub(["x509", "-in", str(cert), "-noout", "-pubkey"]) != _pub(["pkey", "-in", str(key), "-pubout"]):
            raise SystemExit("сертификат не соответствует ключу устройства — отказ")
        out = openssl("verify", "-x509_strict", "-CAfile", str(ca), str(cert))
        if "OK" not in out:
            raise SystemExit(f"цепочка не проверена: {out.strip()}")
        uris = cert_uris(cert)
        want = device_uri(role, dev_id) if role and dev_id else None
        if want and want not in uris:
            raise SystemExit(f"в сертификате {uris}, ожидается {want} — отказ")
        for name, src in (("device.crt", cert), ("ca.crt", ca)):
            tmpf = d / (name + ".tmp")
            shutil.copyfile(src, tmpf)
            os.chmod(tmpf, 0o644)
            os.replace(tmpf, d / name)
        if key.name == "device.key.new":
            os.replace(key, d / "device.key")
        return status(d)


def status(d: Path) -> dict:
    out = {"dir": str(d)}
    for name in ("device.crt", "ca.crt"):
        f = d / name
        if f.exists():
            info = cert_info(f)
            out[name] = {"subject": dict(x[0] for x in info.get("subject", ())), "uris": cert_uris(info),
                         "notBefore": info.get("notBefore"), "notAfter": info.get("notAfter"),
                         "sha256": hashlib.sha256(_der(f)).hexdigest()}
    k = d / "device.key"
    out["key"] = {"present": k.exists(), "mode": oct(k.stat().st_mode & 0o777) if k.exists() else None}
    return out


def _der(f):
    r = subprocess.run(["openssl", "x509", "-in", str(f), "-outform", "DER"], capture_output=True, check=True)
    return r.stdout


def configure(path: Path, role, dev_id, peer_id, peer_hosts, discover, d: Path, port=7443, video_port=7444):
    cfg = {"role": role, "device_id": dev_id, "peer_id": peer_id, "dir": str(d), "port": port,
           "video_port": video_port, "peer_hosts": list(peer_hosts), "discover": discover}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1))
    LinkConfig.load(tmp)                      # проверка до замены: файлы на месте, права ключа, URI совпадает
    os.replace(tmp, path)
    loaded = LinkConfig.load(path)
    for r in ROLES:                           # маркер роли для условий служб systemd (ConditionPathExists)
        marker = path.parent / f"{r}.role"
        if r == role:
            marker.write_text(f"{device_uri(role, dev_id)}\n")
        elif marker.exists():
            marker.unlink()
    return loaded


# ---------------- CA стенда (машина владельца) ----------------
def ca_init(ca_dir: Path, name="Sheregesh LAB TEST CA (not for production)"):
    ca_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key, crt = ca_dir / "ca.key", ca_dir / "ca.crt"
    if key.exists():
        raise SystemExit(f"{key} уже есть; новый CA — только явно, после удаления старого")
    old = os.umask(0o077)
    try:
        openssl("genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(key))
    finally:
        os.umask(old)
    openssl("req", "-x509", "-new", "-key", str(key), "-days", str(CA_DAYS), "-subj", f"/O=Sheregesh lab/CN={name}",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", str(crt))
    return crt


def ca_sign(ca_dir: Path, csr_pem: str, role, dev_id, days=CERT_DAYS) -> str:
    """Подписать CSR устройства: только ожидаемый URI, EKU serverAuth+clientAuth, без прав CA."""
    want = device_uri(role, dev_id)
    text = openssl("req", "-noout", "-text", input=csr_pem)
    if f"URI:{want}" not in text:
        raise SystemExit(f"CSR не содержит {want} — отказ")
    openssl("req", "-noout", "-verify", input=csr_pem)
    with tempfile.TemporaryDirectory() as tmp:
        csr, ext = Path(tmp) / "r.csr", Path(tmp) / "ext.cnf"
        csr.write_text(csr_pem)
        ext.write_text("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
                       "extendedKeyUsage=serverAuth,clientAuth\nsubjectKeyIdentifier=hash\n"
                       f"authorityKeyIdentifier=keyid\nsubjectAltName=URI:{want}\n")
        return openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_dir / "ca.crt"), "-CAkey", str(ca_dir / "ca.key"),
                       "-CAcreateserial", "-CAserial", str(ca_dir / "ca.srl"), "-days", str(days),
                       "-extfile", str(ext), "-sha256")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sheregesh-link", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("device-id")
    k = sub.add_parser("keygen")
    k.add_argument("--role", choices=ROLES, required=True)
    i = sub.add_parser("install")
    i.add_argument("--role", choices=ROLES, required=True)
    c = sub.add_parser("configure")
    c.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    c.add_argument("--role", choices=ROLES, required=True)
    c.add_argument("--peer-id", required=True)
    c.add_argument("--peer-host", action="append", default=[])
    c.add_argument("--discover", default=None)
    sub.add_parser("status")
    a = ap.parse_args(argv)
    d = a.dir.expanduser()
    if a.cmd == "device-id":
        print(device_id())
    elif a.cmd == "keygen":
        sys.stdout.write(keygen(a.role, device_id(), d))
    elif a.cmd == "install":
        bundle = json.loads(sys.stdin.read())
        print(json.dumps(install(d, bundle["cert"], bundle["ca"], a.role, device_id()), ensure_ascii=False, indent=1))
    elif a.cmd == "configure":
        try:
            cfg = configure(a.config.expanduser(), a.role, device_id(), a.peer_id, a.peer_host, a.discover, d)
        except LinkConfigError as e:
            raise SystemExit(f"конфигурация не принята: {e}") from None
        print(f"OK: {cfg.own_uri} → {cfg.peer_uri}, порт {cfg.port}, адреса {list(cfg.peer_hosts)}, поиск {cfg.discover}")
    elif a.cmd == "status":
        print(json.dumps(status(d), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
