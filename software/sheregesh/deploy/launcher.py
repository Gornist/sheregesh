# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Запуск приложения из проверенного выпуска DEV deploy или из заводского /usr/lib/sheregesh (S3.1-DEPLOY).

  /usr/bin/python3 -I /usr/lib/sheregesh/software/sheregesh/deploy/launcher.py МОДУЛЬ [аргументы…]

Обёртки /usr/bin/sheregesh-console и /usr/bin/sheregesh-remote-emu вызывают этот файл из /usr (корень только
для чтения); он сам из выпуска не загружается. Выбор при каждом старте службы:
  • /var/lib/sheregesh-deploy/current → releases/<id>, каталог принадлежит root, RELEASE.json на месте,
    SHA-256 всех файлов совпадают с манифестом, посторонних файлов и ссылок нет → код из выпуска;
  • переключение не подтверждено установщиком до перезагрузки (pending с другим boot_id) → прежний выпуск;
  • иначе (нет current, битый выпуск, ошибка чтения) → заводской код. Причина пишется одной строкой в stderr.
Затем exec /usr/bin/python3 -s -m МОДУЛЬ (тот же PID — важно для Type=notify). -s отключает пользовательские
site-packages: код службы определяется только выбранным каталогом. Только стандартная библиотека.
"""
import hashlib
import json
import os
import re
import stat
import sys

FACTORY = "/usr/lib/sheregesh"
DEPLOY_DIR = "/var/lib/sheregesh-deploy"
PYTHON = "/usr/bin/python3"
# Совпадает с sheregesh.deploy.bundle.PYTHON_PATHS и PYTHONPATH обёрток build_deb (проверяют тесты).
PYTHON_PATHS = ("software", "software/emulators", "software/console", "software/remote")
MODULES = ("sheregesh_console", "sheregesh_remote")      # только службы приложения; системные — всегда из /usr
_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+~_-]{0,79}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_@+-][A-Za-z0-9._@+-]*(/[A-Za-z0-9_@+-][A-Za-z0-9._@+-]*)*$")


class Rejected(Exception):
    pass


def boot_id(path="/proc/sys/kernel/random/boot_id"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _sha256(path):
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Rejected(f"не обычный файл: {path}")
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_release(rel, require_root=True):
    """Проверка каталога выпуска; возвращает манифест или Rejected с причиной."""
    st = os.lstat(rel)
    if not stat.S_ISDIR(st.st_mode):
        raise Rejected("каталог выпуска — не каталог")
    if require_root and (st.st_uid != 0 or st.st_mode & 0o022):
        raise Rejected("каталог выпуска не принадлежит root или доступен на запись")
    try:
        with open(os.path.join(rel, "RELEASE.json"), "rb") as f:
            manifest = json.loads(f.read())
    except (OSError, ValueError) as e:
        raise Rejected(f"нет или битый RELEASE.json: {e}") from None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if manifest.get("kind") != "sheregesh-app-release" or not isinstance(files, dict) or not files:
        raise Rejected("RELEASE.json не описывает выпуск приложения")
    for path, e in files.items():
        if not _PATH_RE.match(path):
            raise Rejected(f"недопустимый путь в манифесте: {path!r}")
        try:
            if _sha256(os.path.join(rel, path)) != e.get("sha256"):
                raise Rejected(f"SHA-256 не совпал: {path}")
        except OSError as err:
            raise Rejected(f"нет файла {path}: {err.strerror}") from None
    for dirpath, dirnames, filenames in os.walk(rel):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            relp = os.path.relpath(full, rel)
            if stat.S_ISLNK(st.st_mode):
                raise Rejected(f"символическая ссылка в выпуске: {relp}")
            if require_root and (st.st_uid != 0 or st.st_mode & 0o022):
                raise Rejected(f"файл не принадлежит root или доступен на запись: {relp}")
            # Байт-код установщик не создаёт; непроверенный .pyc Python предпочёл бы исходнику — отказ.
            if stat.S_ISREG(st.st_mode) and relp not in files and relp != "RELEASE.json":
                raise Rejected(f"посторонний файл в выпуске: {relp}")
    return manifest


def _release_dir(deploy_dir, rid):
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise Rejected(f"недопустимый id выпуска {rid!r}")
    return os.path.join(deploy_dir, "releases", rid)


def select(deploy_dir=DEPLOY_DIR, factory=FACTORY, current_boot=None, require_root=True):
    """→ (каталог кода, id выпуска или None, пояснение)."""
    cur = os.path.join(deploy_dir, "current")
    try:
        if not os.path.islink(cur):
            return factory, None, "заводской код (выпуск не установлен)"
        target = os.readlink(cur)
        if not target.startswith("releases/") or "/" in target[len("releases/"):]:
            raise Rejected(f"current указывает не на releases/: {target!r}")
        rid = target[len("releases/"):]
        try:
            with open(os.path.join(deploy_dir, "state.json")) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        pending = state.get("pending") if isinstance(state, dict) else None
        current_boot = boot_id() if current_boot is None else current_boot
        if isinstance(pending, dict) and pending.get("release") == rid and pending.get("boot_id") != current_boot:
            prev = pending.get("previous")
            if not prev:
                return factory, None, f"выпуск {rid} не подтверждён до перезагрузки — заводской код"
            verify_release(_release_dir(deploy_dir, prev), require_root)
            return _release_dir(deploy_dir, prev), prev, f"выпуск {rid} не подтверждён до перезагрузки — прежний {prev}"
        rel = _release_dir(deploy_dir, rid)
        verify_release(rel, require_root)
        return rel, rid, f"выпуск {rid}"
    except (Rejected, OSError) as e:
        return factory, None, f"заводской код: выпуск отвергнут ({e})"


def exec_args(module, root, args):
    if module not in MODULES:
        raise SystemExit(f"sheregesh-launcher: модуль {module!r} не запускается через выпуск")
    env = dict(os.environ)
    env["PYTHONPATH"] = ":".join(os.path.join(root, p) for p in PYTHON_PATHS)
    env["SHEREGESH_CODE_ROOT"] = root
    return [PYTHON, "-s", "-m", module, *args], env


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("использование: launcher.py МОДУЛЬ [аргументы…]")
    module, args = argv[0], argv[1:]
    root, rid, why = select()
    cmd, env = exec_args(module, root, args)
    if rid:
        env["SHEREGESH_RELEASE"] = rid
    print(f"sheregesh-launcher: {module}: {why}", file=sys.stderr, flush=True)
    os.execve(PYTHON, cmd, env)


if __name__ == "__main__":
    main()
