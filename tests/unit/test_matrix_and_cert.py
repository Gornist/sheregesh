# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Кэш матрицы совместимости и разбор сертификата без приватного API ssl (ревью 2026-11-12)."""
import os
import shutil
import ssl
import subprocess

import pytest

from sheregesh import protocol
from sheregesh.link import tls


def test_matrix_is_parsed_once_and_reloaded_on_change(tmp_path, monkeypatch):
    path = tmp_path / "matrix.yaml"
    shutil.copy(protocol.MATRIX_PATH, path)
    calls = []
    real = protocol.yaml.safe_load
    monkeypatch.setattr(protocol.yaml, "safe_load", lambda text: (calls.append(1), real(text))[1])
    first = protocol.load_matrix(path)
    assert protocol.load_matrix(path) is first and len(calls) == 1
    path.write_text(path.read_text() + "\n# изменено\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert protocol.load_matrix(path) == first and len(calls) == 2


@pytest.mark.skipif(shutil.which("openssl") is None, reason="нет openssl")
def test_cert_info_matches_ssl_peercert_shape(tmp_path):
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-keyout", str(key), "-out", str(crt), "-days", "3", "-nodes",
                    "-subj", "/CN=pult, test/O=Sheregesh lab",
                    "-addext", "subjectAltName=URI:urn:sheregesh:device:console,IP:192.168.1.94"],
                   check=True, capture_output=True)
    info = tls.cert_info(crt)
    assert info["subject"] == ((("commonName", "pult, test"),), (("organizationName", "Sheregesh lab"),))
    assert info["subjectAltName"] == (("URI", "urn:sheregesh:device:console"), ("IP Address", "192.168.1.94"))
    assert ssl.cert_time_to_seconds(info["notAfter"]) > ssl.cert_time_to_seconds(info["notBefore"])
    assert tls.cert_uris(crt) == ["urn:sheregesh:device:console"]
    (tmp_path / "bad.pem").write_text("not a cert")
    with pytest.raises(ValueError):
        tls.cert_info(tmp_path / "bad.pem")


def test_lab_address_is_rfc1918_ula_link_local_only():
    """Ревью 2026-11-12: ip.is_private пропускал CGNAT и служебные диапазоны, шире заявленного."""
    for ok in ("10.1.2.3", "172.31.0.1", "192.168.1.94", "127.0.0.1", "::1", "fd00::5", "fe80::1%eth0",
               "169.254.3.4", "::ffff:192.168.1.94"):
        assert tls.is_lab_address(ok), ok
    for bad in ("100.64.0.1", "198.18.0.1", "192.0.2.1", "203.0.113.5", "8.8.8.8", "172.32.0.1", "2001:db8::1",
                "::ffff:8.8.8.8", "not-an-ip"):
        assert not tls.is_lab_address(bad), bad
