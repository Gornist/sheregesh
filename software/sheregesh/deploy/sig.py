# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Подпись происхождения через ssh-keygen -Y (OpenSSH ≥ 8.2; в образе — openssh-client 10).

Закрытый ключ подписи — только на Mac владельца (или в ssh-agent); в образ попадает только открытый ключ
в файле allowed_signers (/etc/sheregesh/deploy/allowed_signers, корень только для чтения). Пространства имён
разделяют назначения: подпись пакета приложения нельзя выдать за подпись системного обновления и наоборот.
"""
import os
import subprocess
import tempfile
from pathlib import Path

IDENTITY = "sheregesh-deploy"
NS_APP = "sheregesh-deploy"            # пакет приложения (DEV deploy)
NS_OTA = "sheregesh-ota"               # манифест системного обновления A/B
NS_OTA_ENABLE = "sheregesh-ota-enable"  # разрешение переключения слотов для конкретной платы
NAMESPACES = (NS_APP, NS_OTA, NS_OTA_ENABLE)
MAX_SIG = 16 * 1024


class SignatureError(Exception):
    pass


def allowed_signers_line(pubkey_text: str) -> str:
    """Строка allowed_signers из открытого ключа (тип и тело; комментарий отбрасывается)."""
    parts = pubkey_text.strip().split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-ed25519", "ecdsa-sha2-", "sk-ssh-ed25519", "ssh-rsa")):
        raise SignatureError("не похоже на открытый ключ OpenSSH")
    if "PRIVATE KEY" in pubkey_text:
        raise SignatureError("передан закрытый ключ")
    return f'{IDENTITY} namespaces="{",".join(NAMESPACES)}" {parts[0]} {parts[1]}\n'


def sign(data: bytes, key: str, namespace: str, ssh_keygen: str = "ssh-keygen") -> bytes:
    """Подпись на Mac: key — закрытый ключ или открытый при ключе в ssh-agent."""
    if namespace not in NAMESPACES:
        raise SignatureError(f"неизвестное пространство имён {namespace}")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "data"
        f.write_bytes(data)
        r = subprocess.run([ssh_keygen, "-q", "-Y", "sign", "-f", str(key), "-n", namespace, str(f)],
                           capture_output=True, text=True)
        sig = f.with_name("data.sig")
        if r.returncode or not sig.exists():
            raise SignatureError(f"ssh-keygen -Y sign: {r.stderr.strip()[-300:]}")
        return sig.read_bytes()


def verify(data: bytes, signature: bytes, signers: Path, namespace: str, ssh_keygen: str = "ssh-keygen") -> str:
    """Проверка на плате. Возвращает строку ssh-keygen об успешной проверке; иначе SignatureError."""
    if namespace not in NAMESPACES:
        raise SignatureError(f"неизвестное пространство имён {namespace}")
    if not signature or len(signature) > MAX_SIG or b"BEGIN SSH SIGNATURE" not in signature:
        raise SignatureError("нет подписи или неверный формат")
    signers = Path(signers)
    if not signers.is_file() or not signers.read_text().strip():
        raise SignatureError(f"нет доверенных ключей: {signers}")
    with tempfile.TemporaryDirectory() as tmp:
        s = Path(tmp) / "sig"
        s.write_bytes(signature)
        os.chmod(s, 0o600)
        r = subprocess.run([ssh_keygen, "-Y", "verify", "-f", str(signers), "-I", IDENTITY, "-n", namespace,
                            "-s", str(s)], input=data, capture_output=True)
    out = (r.stdout + r.stderr).decode(errors="replace").strip()
    if r.returncode != 0:
        raise SignatureError(f"подпись не принята: {out[-300:]}")
    return out
