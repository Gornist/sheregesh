# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Атомарность записи конфигурации на плате (TEST-CFG-002, L4). Код хранилища — из образа платы.

На плате (root, каталог на той же ФС /var, что рабочее хранилище; рабочее хранилище не трогается):

  PYTHONPATH=/usr/lib/sheregesh/software python3 cfg_atomic.py matrix --dir /var/lib/sheregesh-cfgtest/m
  PYTHONPATH=… python3 cfg_atomic.py loop --dir /var/lib/sheregesh-cfgtest/r    # применяет без конца
  PYTHONPATH=… python3 cfg_atomic.py check --dir /var/lib/sheregesh-cfgtest/r   # после сброса платы

matrix   прерывание в каждой изменяющей операции ФС при apply (чистая остановка и «рваная» запись половины файла):
         после recover активна полная прежняя или полная новая подтверждённая ревизия; невалидный набор не
         становится активным, прежняя ревизия сохраняется.
loop     попеременно применяет два валидных набора A и B (health-check успешен) — плату сбрасывают в любой момент.
check    recover после сброса: pending нет, current == confirmed, активный набор целиком равен A, B или заводскому,
         все ревизии в revisions/ целостны; затем одно apply — хранилище работоспособно.
Итог — JSON в stdout, код 0 при PASS.
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from sheregesh.config import ConfigStore, FsOps, Schema, console_defaults

SCHEMA = Schema.load()
OK = lambda rev, services: True  # noqa: E731


class PowerLoss(BaseException):
    pass


class CrashFs(FsOps):
    """Сбой на изменяющей операции № crash_at; torn — на диск попадает половина данных файла."""

    def __init__(self, crash_at=None, torn=False):
        self.n, self.crash_at, self.torn = 0, crash_at, torn

    def _tick(self):
        self.n += 1
        return self.n == self.crash_at

    def write_file(self, path, data):
        if self._tick():
            if self.torn:
                Path(path).write_bytes(data[: len(data) // 2])
            raise PowerLoss()
        super().write_file(path, data)

    def fsync_dir(self, path):
        if self._tick():
            raise PowerLoss()
        super().fsync_dir(path)

    def rename(self, src, dst):
        if self._tick():
            raise PowerLoss()
        super().rename(src, dst)

    def mkdir(self, path):
        if self._tick():
            raise PowerLoss()
        super().mkdir(path)

    def rmtree(self, path):
        if self._tick():
            raise PowerLoss()
        super().rmtree(path)

    def unlink(self, path):
        if self._tick():
            raise PowerLoss()
        super().unlink(path)


def changed(**overrides):
    f = console_defaults()
    for key, value in overrides.items():
        name, fld = key.split("__")
        lines = [ln for ln in f[f"{name}.yaml"].splitlines() if not ln.startswith(f"{fld}:")]
        f[f"{name}.yaml"] = "\n".join(lines + [f"{fld}: {value}"]) + "\n"
    return f


SET_A = changed(system__language="en", diagnostics__temp_warning_c=60)
SET_B = changed(system__language="ru", system__screen_timeout_s=42, diagnostics__temp_warning_c=70)
INVALID = changed(system__screen_timeout_s="'не число'")


def _fresh(d):
    shutil.rmtree(d, ignore_errors=True)
    s = ConfigStore(d, SCHEMA)
    s.initialize(console_defaults())
    s.recover()
    return s


def _which(files):
    return {json.dumps(SET_A, sort_keys=True): "A", json.dumps(SET_B, sort_keys=True): "B",
            json.dumps(console_defaults(), sort_keys=True): "factory"}.get(json.dumps(files, sort_keys=True))


def _integrity(s):
    """Все ревизии в revisions/ целостны (частично записанный набор туда не попадает)."""
    bad = [p.name for p in s.revisions.iterdir() if not p.name.startswith(".") and s._verify(p.name) is None]
    tmp = [p.name for p in s.revisions.iterdir() if p.name.startswith(".tmp-")]
    return bad, tmp


def matrix(root):
    root = Path(root)
    base = _fresh(root / "base")
    out = {"cases": [], "ok": True}
    for torn in (False, True):
        cnt = root / "count"
        shutil.rmtree(cnt, ignore_errors=True)
        shutil.copytree(base.root, cnt)
        fs = CrashFs()
        ConfigStore(cnt, SCHEMA, fs=fs).apply(SET_A, OK)
        total, res = fs.n, {"old": 0, "new": 0, "bad": []}
        for k in range(1, total + 1):
            d = root / f"crash-{k}"
            shutil.rmtree(d, ignore_errors=True)
            shutil.copytree(base.root, d)
            try:
                ConfigStore(d, SCHEMA, fs=CrashFs(crash_at=k, torn=torn)).apply(SET_A, OK)
                res["bad"].append(f"k={k}: сбой не случился")
            except PowerLoss:
                pass
            s = ConfigStore(d, SCHEMA)
            s.recover()
            st, rev = s.state(), s.active()
            kind = _which(rev.files)
            if st["pending"] is not None or st["current"] != st["confirmed"]:
                res["bad"].append(f"k={k}: pending={st['pending']} current={st['current']}")
            elif kind == "factory":
                res["old"] += 1
            elif kind == "A" and (s.markers / rev.id).exists():
                res["new"] += 1
            else:
                res["bad"].append(f"k={k}: активен смешанный или неподтверждённый набор {rev.id}")
            if not s.apply(SET_B, OK).ok:
                res["bad"].append(f"k={k}: apply после восстановления не прошёл")
            shutil.rmtree(d)
        case = {"case": "torn-write" if torn else "clean-stop", "operations": total, **res,
                "ok": not res["bad"] and res["old"] + res["new"] == total and res["old"] > 0 and res["new"] > 0}
        out["cases"].append(case)
    s = _fresh(root / "invalid")
    before = s.active()
    r = s.apply(INVALID, OK)
    after = s.active()
    inv = {"case": "invalid", "stage": r.stage, "issues": [str(i) for i in r.issues][:3],
           "active_unchanged": after.id == before.id and after.files == before.files,
           "revisions": [h["id"] for h in s.history()]}
    inv["ok"] = not r.ok and r.stage == "validation" and inv["active_unchanged"] and inv["revisions"] == [before.id]
    out["cases"].append(inv)
    s = _fresh(root / "prev")
    s.apply(SET_A, OK)
    first = s.active().id
    seen = {}

    def check_prev(rev, services):
        st = s.state()
        seen.update(pending=st["pending"], confirmed=st["confirmed"], prev_intact=s._verify(first) is not None)
        return True

    s.apply(SET_B, check_prev)
    prev = {"case": "previous-kept", **seen, "snapshots": s.state()["snapshots"]}
    prev["ok"] = (seen.get("confirmed") == first and seen.get("prev_intact") is True
                  and first in prev["snapshots"])
    out["cases"].append(prev)
    shutil.rmtree(root, ignore_errors=True)
    out["ok"] = all(c["ok"] for c in out["cases"])
    return out


def loop(root):
    root = Path(root)
    if not ConfigStore(root, SCHEMA).is_initialized():
        _fresh(root)
    s = ConfigStore(root, SCHEMA)
    s.recover()
    n = 0
    while True:
        s.apply(SET_A if n % 2 == 0 else SET_B, OK)
        n += 1


def check(root):
    s = ConfigStore(Path(root), SCHEMA)
    report = s.recover()
    st, rev = s.state(), s.active()
    bad, tmp = _integrity(s)
    kind = _which(rev.files)
    out = {"active": rev.id, "set": kind, "pending": st["pending"], "confirmed": st["confirmed"],
           "actions": report.actions, "corrupt_revisions": bad, "tmp_left": tmp,
           "quarantine": sorted(p.name for p in s.quarantine.iterdir()) if s.quarantine.exists() else []}
    out["apply_after"] = s.apply(SET_A if kind != "A" else SET_B, OK).ok
    out["ok"] = (kind is not None and st["pending"] is None and st["current"] == st["confirmed"]
                 and (s.markers / rev.id).exists() and not bad and not tmp and out["apply_after"])
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["matrix", "loop", "check"])
    p.add_argument("--dir", required=True)
    a = p.parse_args()
    if a.cmd == "loop":
        loop(a.dir)
    t = time.monotonic()
    r = matrix(a.dir) if a.cmd == "matrix" else check(a.dir)
    r["elapsed_s"] = round(time.monotonic() - t, 2)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
