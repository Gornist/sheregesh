# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Пакет приложения для DEV deploy: детерминированный tar.xz «manifest.json + files/…» и его строгий разбор.

Пакет содержит только код приложения (те же каталоги, что .deb в /usr/lib/sheregesh) и переводы .qm.
Подпись — отдельный файл (sig.py, пространство имён sheregesh-deploy) поверх байтов архива целиком;
SHA-256 каждого файла в манифесте — вторая, независимая проверка целостности при распаковке и при каждом
запуске службы (launcher). Разбор на плате не доверяет архиву: только обычные файлы, только разрешённые
каталоги, без «..», ссылок и устройств, с ограничениями размера; лишние или недостающие файлы — отказ.
"""
import hashlib
import io
import json
import re
import tarfile

FORMAT = 1
KIND = "sheregesh-app-release"
# Совместимость с системной частью образа (Python 3.13, PySide6 6.8, формат установщика). Меняется, когда новый
# выпуск приложения перестаёт работать на старом образе: тогда старый образ отвергает такой пакет.
COMPATIBLE = "sheregesh-lab-pi4-app-1"
# Каталоги кода внутри выпуска (те же, что /usr/lib/sheregesh в .deb, см. tools/image/build_deb.py SOURCES).
ALLOWED_PREFIXES = ("software/sheregesh/", "software/emulators/sheregesh_emulators/",
                    "software/console/sheregesh_console/", "software/remote/sheregesh_remote/",
                    "tools/probe/", "tools/dev/")
PYTHON_PATHS = ("software", "software/emulators", "software/console", "software/remote")
MANIFEST = "manifest.json"
MAX_BUNDLE = 32 * 2**20
MAX_FILES = 2000
MAX_FILE = 8 * 2**20
MAX_TOTAL = 64 * 2**20
_PATH_RE = re.compile(r"^[A-Za-z0-9_@+-][A-Za-z0-9._@+-]*(/[A-Za-z0-9_@+-][A-Za-z0-9._@+-]*)*$")
_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+~_-]{0,79}$")
_HEX_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class BundleError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def release_id(version: str) -> str:
    """Имя каталога выпуска: версия с заменой недопустимых символов; проверяется тем же правилом на плате."""
    rid = re.sub(r"[^0-9A-Za-z.+~_-]", "_", version)[:80]
    if not _ID_RE.match(rid):
        raise BundleError(f"недопустимая версия {version!r}")
    return rid


def check_path(path: str) -> str:
    if (len(path) > 200 or not _PATH_RE.match(path) or not path.startswith(ALLOWED_PREFIXES)
            or "__pycache__" in path.split("/") or path.endswith((".pyc", ".pyo"))):
        raise BundleError(f"недопустимый путь в пакете: {path!r}")
    return path


def validate_manifest(m) -> dict:
    if not isinstance(m, dict):
        raise BundleError("манифест — не объект")
    if m.get("format") != FORMAT or m.get("kind") != KIND:
        raise BundleError(f"неизвестный формат пакета: {m.get('kind')!r} v{m.get('format')!r}")
    if m.get("compatible") != COMPATIBLE:
        raise BundleError(f"несовместимый пакет: compatible={m.get('compatible')!r}, ожидается {COMPATIBLE!r}")
    if not isinstance(m.get("version"), str) or m.get("id") != release_id(m["version"]):
        raise BundleError("версия или id выпуска не согласованы")
    if not isinstance(m.get("commit"), str) or not _HEX_RE.match(m["commit"]):
        raise BundleError("нет коммита Git в манифесте")
    files = m.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise BundleError("список файлов пуст или слишком велик")
    total = 0
    for path, e in files.items():
        check_path(path)
        if (not isinstance(e, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(e.get("sha256", "")))
                or not isinstance(e.get("size"), int) or not 0 <= e["size"] <= MAX_FILE or e.get("mode") not in (0o644, 0o755)):
            raise BundleError(f"неверная запись манифеста: {path}")
        total += e["size"]
    if total > MAX_TOTAL:
        raise BundleError("пакет слишком велик после распаковки")
    for p in PYTHON_PATHS:                          # без пакетов приложения выпуск бессмысленен
        if not any(f.startswith(p + "/") for f in files):
            raise BundleError(f"в пакете нет каталога {p}")
    return m


def build_bundle(files: dict, version: str, commit: str, commit_time: int, dirty: bool = False,
                 extra: dict | None = None) -> bytes:
    """files: путь → (байты, режим 0o644|0o755). Детерминированно: время — время коммита, root:root, сортировка."""
    entries = {}
    for path in sorted(files):
        data, mode = files[path]
        check_path(path)
        entries[path] = {"sha256": sha256(data), "size": len(data), "mode": 0o755 if mode & 0o111 else 0o644}
    manifest = {"format": FORMAT, "kind": KIND, "compatible": COMPATIBLE, "version": version, "id": release_id(version),
                "commit": commit, "commit_time": int(commit_time), "dirty": bool(dirty), **(extra or {}),
                "files": entries}
    validate_manifest(manifest)
    raw = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz", format=tarfile.USTAR_FORMAT, preset=6) as tar:
        def add(name, data, mode):
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), mode, int(commit_time)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            tar.addfile(ti, io.BytesIO(data))
        add(MANIFEST, raw, 0o644)
        for path in sorted(files):
            add("files/" + path, files[path][0], entries[path]["mode"])
    data = buf.getvalue()
    if len(data) > MAX_BUNDLE:
        raise BundleError(f"пакет {len(data)} байт больше предела {MAX_BUNDLE}")
    return data


def parse_bundle(data: bytes):
    """Разбор недоверенного архива. Возвращает (манифест, {путь: байты}); любое отклонение — BundleError."""
    if len(data) > MAX_BUNDLE:
        raise BundleError("пакет слишком велик")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:xz")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise BundleError(f"не tar.xz: {e}") from None
    manifest, got, total, count = None, {}, 0, 0
    try:
        with tar:
            for member in tar:
                count += 1
                if count > MAX_FILES + 1:
                    raise BundleError("слишком много элементов в архиве")
                if not member.isreg():
                    raise BundleError(f"в архиве не только обычные файлы: {member.name!r}")
                if member.size > max(MAX_FILE, 1 << 20) or total + member.size > MAX_TOTAL:
                    raise BundleError(f"слишком большой элемент: {member.name!r}")
                total += member.size
                body = tar.extractfile(member).read()
                if manifest is None:
                    if member.name != MANIFEST:
                        raise BundleError("первым элементом должен быть manifest.json")
                    try:
                        manifest = validate_manifest(json.loads(body))
                    except ValueError as e:
                        raise BundleError(f"манифест не JSON: {e}") from None
                    continue
                if not member.name.startswith("files/"):
                    raise BundleError(f"посторонний элемент: {member.name!r}")
                path = check_path(member.name[len("files/"):])
                if path in got:
                    raise BundleError(f"повтор файла: {path}")
                e = manifest["files"].get(path)
                if e is None:
                    raise BundleError(f"файла нет в манифесте: {path}")
                if len(body) != e["size"] or sha256(body) != e["sha256"]:
                    raise BundleError(f"SHA-256 не совпал: {path}")
                got[path] = body
    except (tarfile.TarError, EOFError, OSError, ValueError) as e:
        raise BundleError(f"повреждённый архив: {e}") from None
    if manifest is None:
        raise BundleError("пустой архив")
    missing = sorted(set(manifest["files"]) - set(got))
    if missing:
        raise BundleError(f"не хватает файлов: {missing[:3]}")
    return manifest, got
