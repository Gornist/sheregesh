#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""ТЕСТОВЫЕ ключи и сертификаты подписи бандлов RAUC (прототип варианта C ADR-0009) — только во временный каталог.

  python3 tools/rauc/testpki.py OUTDIR

Создаёт (EC P-256, срок 90 дней):
  ca.cert.pem / ca.key.pem          — тестовый корневой CA → keyring.pem (= ca.cert.pem) для system.conf;
  signing.cert.pem / signing.key.pem — подпись бандлов (extendedKeyUsage=codeSigning), выдан тестовым CA;
  nocodesign.cert.pem / .key.pem     — выдан тем же CA, но без codeSigning (должен отвергаться check-purpose);
  foreign-ca.* / foreign.cert.pem / foreign.key.pem — посторонний CA и его подписант (должен отвергаться).
Ключи владельца (~/.ssh/sheregesh-deploy) не используются. Ничего из OUTDIR не коммитится: каталог вне репозитория
или удаляется после испытания. Ключи и CA для выпуска — решение владельца по ADR-0005 (HSM/PKCS#11, CRL).
"""
import subprocess
import sys
from pathlib import Path

LEAF_EXT = ("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
            "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n")


def _ssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def ca(out: Path, name: str, cn: str):
    _ssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-days", "90",
         "-keyout", f"{name}.key.pem", "-out", f"{name}.cert.pem", "-subj", f"/O=Sheregesh TEST/CN={cn}",
         "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=out)


def leaf(out: Path, name: str, issuer: str, cn: str, codesign=True):
    ext = out / f"{name}.ext"
    ext.write_text(LEAF_EXT + ("extendedKeyUsage=critical,codeSigning\n" if codesign else
                               "extendedKeyUsage=critical,emailProtection\n"))
    _ssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", f"{name}.key.pem",
         "-out", f"{name}.csr", "-subj", f"/O=Sheregesh TEST/CN={cn}", cwd=out)
    _ssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{issuer}.cert.pem", "-CAkey", f"{issuer}.key.pem",
         "-CAcreateserial", "-days", "90", "-out", f"{name}.cert.pem", "-extfile", ext.name, cwd=out)
    (out / f"{name}.csr").unlink()
    ext.unlink()


def make(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ca(out, "ca", "Sheregesh Lab RAUC TEST CA")
    leaf(out, "signing", "ca", "sheregesh-lab-pi4-dev-1 bundle signer (TEST)")
    leaf(out, "nocodesign", "ca", "not a code signer (TEST)", codesign=False)
    ca(out, "foreign-ca", "Foreign TEST CA")
    leaf(out, "foreign", "foreign-ca", "foreign signer (TEST)")
    (out / "keyring.pem").write_bytes((out / "ca.cert.pem").read_bytes())
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(make(sys.argv[1]))
