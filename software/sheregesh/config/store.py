# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Хранилище ревизий конфигурации (ADR-0004; REQ-CFG-002…009).

Раскладка каталога:
  revisions/<id>/        неизменяемый набор *.yaml + MANIFEST.json (sha256 файлов)
  confirmed/<id>         маркер «ревизия была подтверждена» (для восстановления без state.json)
  quarantine/<id>/       неуспешные и неподтверждённые ревизии (для диагностики)
  state.json             {current, confirmed, pending, snapshots, slots}; state.prev.json — предыдущая копия

Единственная точка переключения набора — атомарная замена state.json (запись во временный файл,
fsync, rename, fsync каталога). Все изменяющие операции ФС идут через FsOps, чтобы тесты могли
имитировать прерывание в любой точке.
"""
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Schema, ValidationIssue

SNAPSHOTS_KEPT = 3          # REQ-CFG-009
KEEP_UNREFERENCED = 10      # сколько неиспользуемых ревизий и карантинных наборов хранить
HEALTH_TIMEOUT_S = 60       # ADR-0004, шаг 6
STATE_FORMAT = 1
BACKUP_FORMAT = 1
_REV = re.compile(r"^r(\d{6})$")


class ConfigError(Exception):
    pass


class NoValidConfiguration(ConfigError):
    """Ни одной целостной подтверждённой ревизии нет — нужен безопасный режим (REQ-REC-001)."""


class BackupError(ConfigError):
    pass


class ConfirmationRequired(ConfigError):
    """Действие изменяет состояние и требует явного подтверждения пользователя (REQ-SYS-009, CFG-008)."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FsOps:
    """Изменяющие операции файловой системы с fsync. Тесты переопределяют их для инжекции сбоев."""

    def write_file(self, path: Path, data: bytes):
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())

    def fsync_dir(self, path: Path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def rename(self, src: Path, dst: Path):
        os.replace(src, dst)

    def mkdir(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)

    def rmtree(self, path: Path):
        shutil.rmtree(path, ignore_errors=True)

    def unlink(self, path: Path):
        path.unlink(missing_ok=True)


@dataclass
class Revision:
    id: str
    files: dict          # имя -> текст YAML
    data: dict           # имя -> разобранные данные
    manifest: dict


@dataclass
class ApplyResult:
    ok: bool
    stage: str                       # validation | health-check | confirmed
    revision: str | None = None      # id новой ревизии (если создавалась)
    active: str | None = None        # id активной ревизии после операции
    issues: list = field(default_factory=list)
    message: str = ""


@dataclass
class RecoveryReport:
    actions: list = field(default_factory=list)   # [(код, описание)]

    def add(self, code, text):
        self.actions.append((code, text))


@dataclass
class BackupInfo:
    path: Path
    device: str
    schema_version: int
    revision: str
    created: str
    files: dict


class ConfigStore:
    def __init__(self, root, schema: Schema, fs: FsOps | None = None, clock=time.time, slot: str | None = None):
        self.root = Path(root)
        self.schema = schema
        self.fs = fs or FsOps()
        self.clock = clock
        self.slot = slot
        self.revisions = self.root / "revisions"
        self.markers = self.root / "confirmed"
        self.quarantine = self.root / "quarantine"
        self.state_path = self.root / "state.json"
        self.prev_path = self.root / "state.prev.json"

    # ---------- служебное ----------
    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "a+") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _now(self):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))

    def _next_id(self):
        nums = [0]
        for d in (self.revisions, self.quarantine):
            if d.exists():
                for p in d.iterdir():
                    m = _REV.match(p.name.removeprefix(".tmp-"))
                    if m:
                        nums.append(int(m.group(1)))
        return f"r{max(nums) + 1:06d}"

    def _atomic_write(self, path: Path, data: bytes):
        tmp = path.with_name(f".tmp-{path.name}")
        self.fs.write_file(tmp, data)
        self.fs.rename(tmp, path)
        self.fs.fsync_dir(path.parent)

    @staticmethod
    def _read_json(path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _valid_state(self, st):
        return (isinstance(st, dict) and st.get("format") == STATE_FORMAT
                and all(k in st for k in ("current", "confirmed", "pending", "snapshots", "slots")))

    def _load_state(self, report: RecoveryReport | None = None):
        st = self._read_json(self.state_path)
        if self._valid_state(st):
            return st
        if self.state_path.exists() and report is not None:
            report.add("CFG-STATE-CORRUPT", "state.json повреждён")
        st = self._read_json(self.prev_path)
        if self._valid_state(st):
            if report is not None:
                report.add("CFG-STATE-PREV", "использована предыдущая копия state.prev.json")
            return st
        return None

    def _write_state(self, st):
        old = self.state_path.read_bytes() if self.state_path.exists() else None
        if old is not None and self._valid_state(self._read_json(self.state_path)):
            self._atomic_write(self.prev_path, old)
        self._atomic_write(self.state_path, json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True).encode())

    def _verify(self, rev_id):
        """Ревизия целостна: манифест читается, набор файлов и хеши совпадают, данные проходят схему."""
        if not rev_id:
            return None
        d = self.revisions / rev_id
        man = self._read_json(d / "MANIFEST.json")
        if not man or man.get("id") != rev_id or not isinstance(man.get("files"), dict):
            return None
        files = {}
        for name, digest in man["files"].items():
            try:
                raw = (d / name).read_bytes()
            except OSError:
                return None
            if _sha(raw) != digest:
                return None
            files[name] = raw.decode("utf-8")
        present = {p.name for p in d.iterdir()} - {"MANIFEST.json"}
        if present != set(man["files"]):
            return None
        data, issues = self.schema.parse(files)
        if issues:
            return None
        return Revision(rev_id, files, data, man)

    def _write_revision(self, files: dict, parent, reason):
        rev_id = self._next_id()
        self.fs.mkdir(self.revisions)
        tmp = self.revisions / f".tmp-{rev_id}"
        self.fs.rmtree(tmp)
        self.fs.mkdir(tmp)
        digests = {}
        for name in sorted(files):
            raw = files[name].encode("utf-8")
            self.fs.write_file(tmp / name, raw)
            digests[name] = _sha(raw)
        manifest = {"id": rev_id, "parent": parent, "created": self._now(), "reason": reason,
                    "device": self.schema.device, "schema_version": self.schema.version, "files": digests}
        self.fs.write_file(tmp / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=1).encode())
        self.fs.fsync_dir(tmp)
        self.fs.rename(tmp, self.revisions / rev_id)
        self.fs.fsync_dir(self.revisions)
        return rev_id

    def _mark_confirmed(self, rev_id):
        self.fs.mkdir(self.markers)
        self.fs.write_file(self.markers / rev_id, b"")
        self.fs.fsync_dir(self.markers)

    def _to_quarantine(self, rev_id):
        src = self.revisions / rev_id
        if src.exists():
            self.fs.mkdir(self.quarantine)
            self.fs.rename(src, self.quarantine / rev_id)
            self.fs.fsync_dir(self.quarantine)
            self.fs.fsync_dir(self.revisions)

    def _rebuild_state(self, report):
        """state.json и копия потеряны: берём новейшую целостную ревизию с маркером подтверждения."""
        ids = sorted((p.name for p in self.markers.iterdir()), reverse=True) if self.markers.exists() else []
        valid = [i for i in ids if self._verify(i)]
        if not valid:
            return None
        report.add("CFG-STATE-REBUILT", f"состояние восстановлено по маркерам подтверждения; активна {valid[0]}")
        return {"format": STATE_FORMAT, "current": valid[0], "confirmed": valid[0], "pending": None,
                "snapshots": list(reversed(valid[1:SNAPSHOTS_KEPT + 1])), "slots": {}}

    # ---------- публичный API ----------
    def is_initialized(self):
        return self.state_path.exists() or self.prev_path.exists() or (self.markers.exists() and any(self.markers.iterdir()))

    def initialize(self, default_files: dict):
        """Первичная установка заводской конфигурации (только при пустом хранилище)."""
        with self._lock():
            if self.is_initialized():
                raise ConfigError("хранилище уже инициализировано")
            issues = self.schema.validate(default_files)
            if issues:
                raise ConfigError("заводская конфигурация не проходит схему: " + "; ".join(map(str, issues)))
            rev = self._write_revision(default_files, None, "factory defaults")
            self._mark_confirmed(rev)
            slots = {self.slot: rev} if self.slot else {}
            self._write_state({"format": STATE_FORMAT, "current": rev, "confirmed": rev, "pending": None,
                               "snapshots": [], "slots": slots})
            return rev

    def recover(self) -> RecoveryReport:
        """Вызывается при каждом старте (REQ-CFG-005, CFG-006, REC-001)."""
        report = RecoveryReport()
        with self._lock():
            if self.revisions.exists():
                for p in self.revisions.iterdir():
                    if p.name.startswith(".tmp-"):
                        self.fs.rmtree(p)
                        report.add("CFG-TMP-REMOVED", f"удалён незавершённый набор {p.name}")
            for d in (self.root,):
                for p in d.glob(".tmp-state*"):
                    self.fs.unlink(p)
            st = self._load_state(report) or self._rebuild_state(report)
            if st is None:
                raise NoValidConfiguration("нет ни одной целостной подтверждённой ревизии")
            changed = False
            if st["pending"]:
                self._to_quarantine(st["pending"])
                report.add("CFG-PENDING-DISCARDED",
                           f"неподтверждённая ревизия {st['pending']} перенесена в карантин и не активирована")
                st["pending"], st["current"], changed = None, st["confirmed"], True
            if not self._verify(st["confirmed"]):
                fallback = next((s for s in reversed(st["snapshots"]) if self._verify(s)), None)
                if fallback is None:
                    rebuilt = self._rebuild_state(report)
                    if rebuilt is None:
                        raise NoValidConfiguration(f"подтверждённая ревизия {st['confirmed']} повреждена, замены нет")
                    fallback = rebuilt["confirmed"]
                report.add("CFG-CONFIRMED-CORRUPT",
                           f"ревизия {st['confirmed']} повреждена; активирован снимок {fallback}")
                st["confirmed"] = st["current"] = fallback
                changed = True
            if st["current"] != st["confirmed"]:
                st["current"], changed = st["confirmed"], True
            if self.slot:
                bound = st["slots"].get(self.slot)
                if bound is None:
                    st["slots"][self.slot], changed = st["confirmed"], True
                elif bound != st["confirmed"] and self._verify(bound):
                    report.add("CFG-SLOT-BINDING",
                               f"слот {self.slot}: активирована связанная ревизия {bound}; {st['confirmed']} сохранена")
                    st["current"] = st["confirmed"] = bound
                    changed = True
            if changed or not self._valid_state(self._read_json(self.state_path)):
                self._write_state(st)
        return report

    def state(self):
        st = self._load_state()
        if st is None:
            raise NoValidConfiguration("состояние не загружено; вызовите recover()")
        return st

    def active(self) -> Revision:
        rev = self._verify(self.state()["current"])
        if rev is None:
            raise NoValidConfiguration("активная ревизия повреждена; вызовите recover()")
        return rev

    def history(self):
        """Ревизии (кроме карантина) с манифестами, от новых к старым."""
        out = []
        if self.revisions.exists():
            for p in sorted(self.revisions.iterdir(), reverse=True):
                if _REV.match(p.name):
                    man = self._read_json(p / "MANIFEST.json") or {}
                    out.append({"id": p.name, "reason": man.get("reason"), "created": man.get("created"),
                                "confirmed": (self.markers / p.name).exists()})
        return out

    def validate(self, files: dict) -> list[ValidationIssue]:
        return self.schema.validate(files)

    def apply(self, files: dict, health_check, reason="user change", timeout_s=HEALTH_TIMEOUT_S) -> ApplyResult:
        """Проверить → записать → pending → health-check → confirmed или откат (REQ-CFG-002…004, 009).

        health_check(revision, services) -> bool; исключение или таймаут считаются провалом.
        """
        issues = self.schema.validate(files)
        if issues:
            return ApplyResult(False, "validation", issues=issues, active=self.state()["current"],
                               message="конфигурация не прошла проверку; активная ревизия не изменена")
        with self._lock():
            st = self.state()
            if st["pending"]:
                raise ConfigError(f"ревизия {st['pending']} уже применяется; выполните recover()")
            base = self._verify(st["confirmed"])
            changed_files = [n for n in files if base is None or base.files.get(n) != files[n]]
            rev_id = self._write_revision(files, st["confirmed"], reason)
            if st["confirmed"]:
                snaps = [s for s in st["snapshots"] if s != st["confirmed"]] + [st["confirmed"]]
                st["snapshots"] = snaps[-SNAPSHOTS_KEPT:]
            st["pending"] = st["current"] = rev_id
            self._write_state(st)

        rev = self._verify(rev_id)
        services = self.schema.services_for(changed_files)
        ok, why = False, ""
        try:
            with ThreadPoolExecutor(max_workers=1) as ex:
                ok = bool(ex.submit(health_check, rev, services).result(timeout=timeout_s))
            why = "" if ok else "health-check вернул отказ"
        except FutureTimeout:
            why = f"health-check не завершился за {timeout_s} с"
        except Exception as e:  # noqa: BLE001 — любой сбой проверки = провал
            why = f"health-check завершился ошибкой: {e}"

        with self._lock():
            st = self.state()
            if ok:
                self._mark_confirmed(rev_id)
                st["confirmed"], st["pending"] = rev_id, None
                if self.slot:
                    st["slots"][self.slot] = rev_id
                self._write_state(st)
                self._gc(st)
                return ApplyResult(True, "confirmed", revision=rev_id, active=rev_id, message="ревизия подтверждена")
            st["current"], st["pending"] = st["confirmed"], None
            self._write_state(st)
            self._to_quarantine(rev_id)
            return ApplyResult(False, "health-check", revision=rev_id, active=st["confirmed"],
                               message=f"{why}; восстановлена ревизия {st['confirmed']}")

    def rollback(self, health_check, timeout_s=HEALTH_TIMEOUT_S) -> ApplyResult:
        """Ручной возврат к предыдущему подтверждённому снимку (новая ревизия с его содержимым)."""
        st = self.state()
        target = next((s for s in reversed(st["snapshots"]) if s != st["confirmed"] and self._verify(s)), None)
        if target is None:
            return ApplyResult(False, "validation", active=st["current"], message="нет снимка для возврата")
        return self.apply(self._verify(target).files, health_check, reason=f"rollback to {target}", timeout_s=timeout_s)

    def _gc(self, st):
        keep = {st["current"], st["confirmed"], *st["snapshots"], *st["slots"].values()} - {None}
        for d in (self.revisions, self.quarantine):
            if not d.exists():
                continue
            extra = sorted((p for p in d.iterdir() if _REV.match(p.name) and p.name not in keep), reverse=True)
            for p in extra[KEEP_UNREFERENCED:]:
                self.fs.rmtree(p)

    # ---------- резервные копии (REQ-CFG-007, CFG-008) ----------
    def backup(self, dest_dir) -> Path:
        """Архив активной подтверждённой ревизии. Секреты в хранилище конфигурации не лежат (ADR-0004)."""
        rev = self._verify(self.state()["confirmed"])
        if rev is None:
            raise NoValidConfiguration("подтверждённая ревизия повреждена")
        info = {"format": BACKUP_FORMAT, "device": self.schema.device, "schema_version": self.schema.version,
                "revision": rev.id, "created": self._now(),
                "files": {n: _sha(t.encode()) for n, t in sorted(rev.files.items())}}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, text in [("BACKUP.json", json.dumps(info, ensure_ascii=False, indent=1))] + \
                              [(f"config/{n}", t) for n, t in sorted(rev.files.items())]:
                raw = text.encode()
                ti = tarfile.TarInfo(name)
                ti.size, ti.mtime, ti.mode = len(raw), int(self.clock()), 0o644
                tar.addfile(ti, io.BytesIO(raw))
        dest = Path(dest_dir) / f"sheregesh-{self.schema.device}-{rev.id}.tar"
        self._atomic_write(dest, buf.getvalue())
        return dest

    def inspect_backup(self, path) -> tuple[BackupInfo, dict]:
        try:
            with tarfile.open(path, mode="r:") as tar:
                members = {m.name: m for m in tar.getmembers()}
                if "BACKUP.json" not in members:
                    raise BackupError("в архиве нет BACKUP.json")
                info = json.loads(tar.extractfile(members["BACKUP.json"]).read())
                files = {}
                for name, digest in info["files"].items():
                    m = members.get(f"config/{name}")
                    if m is None or not m.isfile():
                        raise BackupError(f"в архиве нет файла {name}")
                    raw = tar.extractfile(m).read()
                    if _sha(raw) != digest:
                        raise BackupError(f"контрольная сумма {name} не совпадает")
                    files[name] = raw.decode("utf-8")
                extra = {n for n in members if n != "BACKUP.json" and n.removeprefix("config/") not in info["files"]}
                if extra:
                    raise BackupError(f"посторонние файлы в архиве: {sorted(extra)}")
        except (tarfile.TarError, OSError, ValueError, KeyError, UnicodeDecodeError) as e:
            raise BackupError(f"архив повреждён: {e}") from e
        if info.get("format") != BACKUP_FORMAT:
            raise BackupError(f"неизвестный формат архива {info.get('format')}")
        if info.get("device") != self.schema.device:
            raise BackupError(f"архив для устройства «{info.get('device')}», а это «{self.schema.device}»")
        if info.get("schema_version") != self.schema.version:
            raise BackupError(f"версия схемы архива {info.get('schema_version')} несовместима с {self.schema.version}")
        issues = self.schema.validate(files)
        if issues:
            raise BackupError("содержимое архива не проходит схему: " + "; ".join(map(str, issues)))
        return BackupInfo(Path(path), info["device"], info["schema_version"], info["revision"],
                          info["created"], info["files"]), files

    def restore(self, path, health_check, confirmed=False, timeout_s=HEALTH_TIMEOUT_S) -> ApplyResult:
        info, files = self.inspect_backup(path)
        if not confirmed:
            raise ConfirmationRequired(f"восстановление ревизии {info.revision} из {info.path.name} требует подтверждения")
        return self.apply(files, health_check, reason=f"restore {info.revision} from {info.path.name}",
                          timeout_s=timeout_s)
