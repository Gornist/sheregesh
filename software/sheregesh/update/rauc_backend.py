# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Бэкенд загрузчика RAUC `bootloader=custom` поверх tryboot Pi 4 и разметки image-rota (прототип варианта C
ADR-0009; ADR не утверждён; в образ не входит).

  rauc-tryboot-backend get-primary | set-primary BOOTNAME | get-state BOOTNAME | set-state BOOTNAME good|bad
                       | get-current

Слоты: bootname A ↔ boot_a (p2) + system_a (p4), B ↔ boot_b (p3) + system_b (p5). Источник истины прошивки —
autoboot.txt раздела bootconfig (p1, FAT) в формате rpi-slot-tryboot (slots.render_autoboot):
  [all] tryboot_a_b=1 boot_partition=<основной>   [tryboot] boot_partition=<пробный>
tryboot однократен: флаг живёт до первого сброса, затем прошивка грузит [all].

Соответствие операций RAUC (RAUC — единственный, кто решает; бэкенд только исполняет и не даёт нарушить инварианты):
  get-primary         основной слот = [all] boot_partition (то, что прошивка загрузит при обычной загрузке);
  get-current         слот текущей загрузки = /proc/device-tree/chosen/bootloader/partition;
  set-state X bad     X «плохой»; снимается пробная загрузка X (RAUC делает так перед записью слота и по mark-bad).
                      Основной слот плохим не помечается (иначе нечем грузиться) — отказ;
  set-primary X       X ≠ основного: «пробная загрузка X» (trial) — [all] НЕ меняется, [tryboot]=X; перезагрузку
                      «0 tryboot» выполняет отдельная команда (rauc_hooks tryboot), не RAUC. X = основной: отмена
                      пробной загрузки;
  set-state X good    на пробной загрузке X (флаг tryboot прошивки, загружен X) — СДЕЛАТЬ ОСНОВНЫМ: [all]=X
                      (mark-good по критериям confirm.gates — rauc_health). Вне пробной загрузки X — отказ
                      (подтвердить можно только слот, который реально загрузился);
  get-state X         good — основной (если не помечен bad) или подтверждённый; иначе bad (в т. ч. «ожидает пробы»).
Состояние бэкенда — /persistent/sheregesh-rauc/boot-state.json (общий раздел, переживает переключение).
Изменения слотов (set-primary, set-state good) проверяются охранником guard: при незавершённом обновлении
dev-агента sheregesh-ota — отказ.
"""
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import guard as guard_mod
from . import slots
from .model import MAX_BOOT_ATTEMPTS, UpdateError

BOOTNAMES = ("A", "B")
CONF_ENV = "SHEREGESH_RAUC_CONF"


@dataclass
class Config:
    """Пути и команды прототипа. На плате — значения по умолчанию; испытания задают JSON в $SHEREGESH_RAUC_CONF."""
    paths: slots.Paths = field(default_factory=slots.Paths)
    state_dir: Path = Path("/persistent/sheregesh-rauc")
    rauc_system_conf: Path = Path("/etc/rauc/system.conf")
    boot_id: Path = Path("/proc/sys/kernel/random/boot_id")
    parts: dict = field(default_factory=lambda: {"A": 2, "B": 3})
    root: Path = Path("/")
    rauc_cmd: tuple = ("rauc",)
    reboot_cmd: tuple = ("systemctl", "reboot")
    tryboot_cmd: tuple = ("systemctl", "reboot", "--reboot-argument=0 tryboot")
    health_timeout: int = 600
    lab_video: bool = True
    max_attempts: int = MAX_BOOT_ATTEMPTS

    def guard(self):
        return guard_mod.Guard(guard_mod.GuardPaths.under(self.paths.persistent, self.rauc_system_conf, self.boot_id))

    @classmethod
    def load(cls, path=None):
        path = path or os.environ.get(CONF_ENV)
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text())
        kw = {k: (Path(v) if k in ("state_dir", "rauc_system_conf", "boot_id", "root") else
                  tuple(v) if k.endswith("_cmd") else v) for k, v in raw.items() if k != "paths"}
        return cls(paths=slots.Paths(**raw.get("paths", {})), **kw)


class BackendError(Exception):
    pass


def _be32(path):
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    return int.from_bytes(raw[:4], "big") if len(raw) >= 4 else None


class TrybootBackend:
    def __init__(self, cfg: Config | None = None, guard=None):
        self.cfg = cfg or Config()
        self.guard = guard or self.cfg.guard()
        self.part = dict(self.cfg.parts)
        self.name = {v: k for k, v in self.part.items()}
        if sorted(self.part) != list(BOOTNAMES) or len(self.name) != 2:
            raise BackendError(f"неверное соответствие слотов разделам: {self.part}")

    # ---------- факты ----------
    @property
    def autoboot(self):
        return Path(self.cfg.paths.bootfs) / "autoboot.txt"

    @property
    def state_file(self):
        return Path(self.cfg.state_dir) / "boot-state.json"

    def _bootname(self, x):
        x = str(x).upper()
        if x not in self.part:
            raise BackendError(f"неизвестный слот {x!r}; ожидается A или B")
        return x

    def other(self, x):
        return "B" if self._bootname(x) == "A" else "A"

    def committed(self):
        try:
            default, _ = slots.autoboot_parts(self.autoboot.read_text())
        except (OSError, UpdateError) as e:
            raise BackendError(f"autoboot.txt: {e}") from None
        if default not in self.name:
            raise BackendError(f"autoboot.txt: [all] boot_partition={default} — не слот A/B")
        return self.name[default]

    def booted(self):
        """→ (bootname, пробная_загрузка). Номер раздела — от прошивки (device-tree), не от «/»."""
        part = _be32(Path(self.cfg.paths.chosen) / "partition")
        if part not in self.name:
            raise BackendError(f"прошивка загрузила раздел {part} — не boot_a/boot_b")
        return self.name[part], bool(_be32(Path(self.cfg.paths.chosen) / "tryboot"))

    def load(self):
        try:
            st = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            st = {}
        except ValueError as e:
            raise BackendError(f"{self.state_file}: повреждён ({e})") from None
        st.setdefault("slots", {})
        st.setdefault("trial", None)
        return st

    def save(self, st):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        slots.write_atomic(self.state_file, json.dumps(st, ensure_ascii=False, sort_keys=True) + "\n")

    @contextmanager
    def locked(self):
        Path(self.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(self.cfg.state_dir) / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _render(self, committed, trial):
        slots.write_atomic(self.autoboot, slots.render_autoboot(self.part[committed], self.part[trial]))

    def _allowed(self, action):
        ok, why = self.guard.check(guard_mod.RAUC, action)
        if not ok:
            raise BackendError(why)

    # ---------- операции RAUC ----------
    def get_primary(self):
        return self.committed()

    def get_current(self):
        return self.booted()[0]

    def get_state(self, x):
        x = self._bootname(x)
        st = self.load()["slots"].get(x)
        if x == self.committed() and st != "bad":
            return "good"
        return "good" if st == "good" else "bad"

    def set_primary(self, x):
        x = self._bootname(x)
        with self.locked():
            committed, st = self.committed(), self.load()
            if x == committed:
                st["trial"] = None
                self.save(st)
                return f"пробная загрузка отменена; основной {x}"
            self._allowed("set-primary")
            booted, trying = self.booted()
            if booted != committed or trying:
                raise BackendError(f"пробную загрузку можно назначить только с основного слота {committed} "
                                   f"(загружен {booted}, tryboot={trying})")
            st["trial"] = {"slot": x, "attempts": 0}
            st["slots"][x] = "pending"
            self.save(st)
            self._render(committed, x)
            return f"слот {x}: пробная загрузка назначена; основной остаётся {committed}"

    def set_state(self, x, value):
        x = self._bootname(x)
        if value not in ("good", "bad"):
            raise BackendError(f"состояние {value!r}: ожидается good или bad")
        with self.locked():
            committed, st = self.committed(), self.load()
            trial = st["trial"] or {}
            if value == "bad":
                if x == committed:
                    raise BackendError(f"основной слот {x} нельзя пометить плохим: второй слот не подтверждён")
                st["slots"][x] = "bad"
                if trial.get("slot") == x:
                    st["trial"] = None
                self.save(st)
                return f"слот {x} помечен плохим; основной {committed}"
            if x == committed or (st["slots"].get(x) == "good" and trial.get("slot") != x):
                st["slots"][x] = "good"
                self.save(st)
                return f"слот {x} уже основной/подтверждён"
            self._allowed("mark-good")
            booted, trying = self.booted()
            if trial.get("slot") != x or booted != x or not trying:
                raise BackendError(f"подтвердить можно только слот текущей пробной загрузки: {x} не загружен "
                                   f"пробно (загружен {booted}, tryboot={trying}, назначен {trial.get('slot')})")
            # Сначала autoboot.txt (атомарно, с проверкой чтением), затем состояние: сбой между ними
            # исправляет on_boot (пробный слот уже основной → подтверждён).
            self._render(x, self.other(x))
            st["slots"][x] = "good"
            if st["slots"].get(committed) != "bad":
                st["slots"][committed] = "good"      # прежний основной работал — остаётся запасным «good»
            st["trial"] = None
            st["committed_from"] = committed
            self.save(st)
            return f"слот {x} подтверждён и стал основным (был {committed})"

    # ---------- вне интерфейса RAUC (rauc_hooks) ----------
    def status(self):
        st = self.load()
        out = {"committed": None, "booted": None, "tryboot": None, "trial": st["trial"], "slots": st["slots"]}
        try:
            out["committed"] = self.committed()
            out["booted"], out["tryboot"] = self.booted()
            out["state"] = {x: self.get_state(x) for x in BOOTNAMES}
        except BackendError as e:
            out["error"] = str(e)
        return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        b = TrybootBackend(Config.load())
        cmd, args = (argv[0], argv[1:]) if argv else ("", [])
        if cmd == "get-primary" and not args:
            print(b.get_primary())
        elif cmd == "get-current" and not args:
            print(b.get_current())
        elif cmd == "get-state" and len(args) == 1:
            print(b.get_state(args[0]))
        elif cmd == "set-primary" and len(args) == 1:
            print(b.set_primary(args[0]), file=sys.stderr)
        elif cmd == "set-state" and len(args) == 2:
            print(b.set_state(*args), file=sys.stderr)
        elif cmd == "status" and not args:
            print(json.dumps(b.status(), ensure_ascii=False))
        else:
            print("использование: get-primary | set-primary SLOT | get-state SLOT | set-state SLOT good|bad | "
                  "get-current | status", file=sys.stderr)
            return 2
        return 0
    except (BackendError, OSError) as e:
        print(f"rauc-tryboot-backend {' '.join(argv)}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
