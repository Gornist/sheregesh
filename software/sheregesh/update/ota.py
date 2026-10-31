# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Агент системного обновления A/B для dev-образа (S3.1-DEPLOY, подготовка OTA; ADR-0006, предложение ADR-0009).

  sheregesh-ota-agent serve      — одно соединение сокета /run/sheregesh-ota.sock (systemd Accept=yes, root)
  sheregesh-ota-agent on-boot    — учёт загрузки (служба при старте): только состояние, без переключений
  sheregesh-ota-client ДЕЙСТВИЕ  — клиент для sheregesh-ui (по SSH): status | install | enable | disable |
                                   tryboot | confirm | abort

Доступ: сокет 0660 root:sheregesh-ui; действия фиксированы, путей и команд извне нет.
  install  — поток «SHRGOTA1» (манифест, подпись ssh-keygen пространства sheregesh-ota, образы boot/system в xz)
             пишется ТОЛЬКО в неактивный слот, проверяется повторным чтением; /var целевого слота пересоздаётся.
             Основной слот, autoboot.txt и перезагрузка не затрагиваются.
  tryboot  — однократная пробная загрузка записанного слота (autoboot [tryboot] + reboot "0 tryboot");
  confirm  — на пробной загрузке: все обязательные критерии confirm.py (слот, версия, persistent, те же ключи и роль,
             службы роли без перезапусков, health PASS, mTLS, видео стенда) → autoboot [all] на новый слот (mark-good);
  abort    — отказ: с пробной загрузки — обычная перезагрузка (прошивка вернёт прежний слот), иначе сброс в IDLE.
  dev-arm / dev-disarm — разовый режим разработчика (REQ-UPD-005/006): разрешает установить ОДИН собственный образ без
             подписи проекта. Взводится только службой самого устройства — GUI пульта (sheregesh-console, после
             подтверждения оператора) или агентом блока (sheregesh-remote-net, по подтверждению на пульте); по SSH —
             отказ. Разрешение живёт DEV_GRANT_S и снимается первой же установкой (успех или отказ); следующий
             неподписанный образ отвергается. Пробная загрузка, подтверждение и откат такого образа — тот же A/B.
tryboot и confirm ВЫКЛЮЧЕНЫ, пока на плату не внесено разрешение, подписанное владельцем для серийного номера
этой платы (enable; пространство sheregesh-ota-enable), — кроме образа, записанного в разовом режиме разработчика. Автоматического подтверждения нет: confirm — только
по явной команде. Модель состояний и инварианты — model.py.
"""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..deploy import sig
from . import confirm, slots
from .model import STATES, UpdateError, Updater, UpdateState

MAGIC = b"SHRGOTA1\n"
FORMAT = 1
KIND = "sheregesh-system-update"
LAYOUT = "image-rota-gpt-ab-1"
PARTS = ("boot", "system")
MAX_MANIFEST = 64 * 1024
SOCKET = "/run/sheregesh-ota.sock"
ARM_UNITS = ("sheregesh-console.service", "sheregesh-remote-net.service")   # кто может взвести режим разработчика
DEV_GRANT_S = 15 * 60
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def device_compatible(dev_mode: bool) -> str:
    """dev-образ принимает только dev-обновления (в строгом образе нет SSH: такое обновление отрезало бы доступ)."""
    return "sheregesh-lab-pi4-dev-1" if dev_mode else "sheregesh-lab-pi4-1"


class PayloadError(Exception):
    pass


def validate_manifest(m, compatible):
    if not isinstance(m, dict) or m.get("format") != FORMAT or m.get("kind") != KIND:
        raise PayloadError("неизвестный формат манифеста обновления")
    if m.get("layout") != LAYOUT:
        raise PayloadError(f"другая разметка: {m.get('layout')!r}, ожидается {LAYOUT}")
    if m.get("compatible") != compatible:
        raise PayloadError(f"несовместимо: compatible={m.get('compatible')!r}, плата {compatible!r}")
    if not isinstance(m.get("version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", m["version"]):
        raise PayloadError("версия должна быть вида N.N.N")
    imgs = m.get("images")
    if not isinstance(imgs, list) or [i.get("part") for i in imgs if isinstance(i, dict)] != list(PARTS):
        raise PayloadError("ожидаются образы boot и system в этом порядке")
    for i in imgs:
        if (i.get("compression") != "xz" or not isinstance(i.get("size"), int) or i["size"] <= 0
                or not isinstance(i.get("compressed_size"), int) or i["compressed_size"] <= 0
                or not _HEX64.match(str(i.get("sha256"))) or not _HEX64.match(str(i.get("compressed_sha256")))):
            raise PayloadError(f"неверная запись образа {i.get('part')}")
    return m


def _read_exact(stream, n):
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise PayloadError("поток оборвался")
        buf += chunk
    return buf


def _read_block(stream, limit):
    line = b""
    while not line.endswith(b"\n"):
        c = stream.read(1)
        if not c or len(line) > 8:
            raise PayloadError("неверный заголовок блока")
        line += c
    n = int(line) if line[:-1].isdigit() else -1
    if not 0 < n <= limit:
        raise PayloadError("неверная длина блока")
    return _read_exact(stream, n)


class Payload:
    """Поток обновления: подпись манифеста проверяется ДО чтения образов; образы — далее из stream."""

    def __init__(self, stream, signers, compatible, allow_unsigned=False):
        """allow_unsigned — разовый режим разработчика: подпись проекта не обязательна (dev — образ без неё)."""
        if _read_exact(stream, len(MAGIC)) != MAGIC:
            raise PayloadError("это не поток обновления SHRGOTA1")
        self.raw_manifest = _read_block(stream, MAX_MANIFEST)
        signature = _read_block(stream, sig.MAX_SIG)
        self.dev = False
        try:
            self.signer = sig.verify(self.raw_manifest, signature, signers, sig.NS_OTA)
        except sig.SignatureError:
            if not allow_unsigned:
                raise
            self.signer, self.dev = None, True
        try:
            self.manifest = validate_manifest(json.loads(self.raw_manifest), compatible)
        except ValueError as e:
            raise PayloadError(f"манифест не JSON: {e}") from None
        self.stream = stream


def write_payload(out, manifest_bytes, signature, image_files):
    out.write(MAGIC)
    for block in (manifest_bytes, signature):
        out.write(b"%d\n" % len(block))
        out.write(block)
    for path in image_files:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                out.write(chunk)


class Policy:
    """Разрешение переключать слоты: подписанный владельцем токен для серийного номера платы."""

    def __init__(self, state_dir: Path, signers: Path, serial: str):
        self.dir, self.signers, self.serial = Path(state_dir), Path(signers), serial

    def allowed(self):
        try:
            token = (self.dir / "enable.json").read_bytes()
            signature = (self.dir / "enable.json.sig").read_bytes()
        except OSError:
            return False, "переключение слотов не разрешено (нет подписанного разрешения для этой платы)"
        try:
            self._check(token, signature)
        except (sig.SignatureError, PayloadError) as e:
            return False, f"разрешение недействительно: {e}"
        return True, f"разрешено для платы {self.serial}"

    def _check(self, token: bytes, signature: bytes):
        sig.verify(token, signature, self.signers, sig.NS_OTA_ENABLE)
        try:
            t = json.loads(token)
        except ValueError:
            raise PayloadError("разрешение не JSON") from None
        if t.get("kind") != "sheregesh-ota-enable" or t.get("switching") is not True:
            raise PayloadError("это не разрешение переключения")
        if not self.serial or t.get("serial") != self.serial:
            raise PayloadError(f"разрешение для платы {t.get('serial')!r}, а это {self.serial!r}")

    def enable(self, token: bytes, signature: bytes):
        self._check(token, signature)
        self.dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name, data in (("enable.json", token), ("enable.json.sig", signature)):
            tmp = self.dir / (name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, self.dir / name)

    def disable(self):
        for name in ("enable.json", "enable.json.sig"):
            try:
                (self.dir / name).unlink()
            except FileNotFoundError:
                pass


class DevGrant:
    """Разовое разрешение режима разработчика: файл в state_dir с временем взвода, сроком и службой-инициатором."""

    def __init__(self, state_dir: Path, clock=None):
        import time
        self.path, self.clock = Path(state_dir) / "dev-grant.json", clock or time.time

    def arm(self, by: str):
        now = self.clock()
        info = {"armed": round(now, 3), "expires": round(now + DEV_GRANT_S, 3), "by": by}
        self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(info))
        os.replace(tmp, self.path)
        return info

    def current(self):
        """Действующее разрешение или None (нет, просрочено, повреждено)."""
        try:
            info = json.loads(self.path.read_text())
            return info if self.clock() < float(info["expires"]) else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def consume(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def caller_unit(sock) -> str:
    """Служба systemd процесса на другом конце сокета (SO_PEERCRED → /proc/PID/cgroup); «» — не определена."""
    import struct
    try:
        pid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, getattr(socket, "SO_PEERCRED", 17),
                                                  struct.calcsize("3i")))[0]
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except (OSError, ValueError, struct.error):
        return ""
    path = text.strip().splitlines()[-1].rpartition(":")[2] if text.strip() else ""
    return next((p for p in reversed(path.split("/")) if p.endswith(".service")), "")


@dataclass
class AgentConfig:
    paths: slots.Paths = field(default_factory=slots.Paths)
    state_dir: Path = Path("/persistent/sheregesh-ota")     # общий для слотов: переживает переключение
    signers: Path = Path("/etc/sheregesh/deploy/allowed_signers")
    build_json: Path = Path("/usr/share/sheregesh/build.json")
    serial: Path = Path("/proc/device-tree/serial-number")
    dev_mode: Path = Path("/etc/sheregesh/DEV-MODE")
    health_cmd: tuple = ("/usr/bin/sheregesh-health", "--json")
    reboot_cmd: tuple = ("systemctl", "reboot")
    tryboot_cmd: tuple = ("systemctl", "reboot", "--reboot-argument=0 tryboot")
    root: Path = Path("/")                                  # для отпечатков expect_from_board (испытания — каталог)
    probe: object = None                                    # confirm.Probe; испытания подставляют свой
    lab_video: bool = True                                  # стенд S3.1-VIDEO: видео — обязательный критерий


class Agent:
    def __init__(self, cfg: AgentConfig | None = None, detect=slots.detect, run=subprocess.run, seeder=None):
        self.cfg = cfg or AgentConfig()
        self.detect, self.run, self.seeder = detect, run, seeder

    # ---------- факты платы ----------
    def serial(self):
        try:
            return Path(self.cfg.serial).read_bytes().rstrip(b"\0").decode().strip()
        except OSError:
            return ""

    def build(self):
        try:
            return json.loads(Path(self.cfg.build_json).read_text())
        except (OSError, ValueError):
            return {}

    def compatible(self):
        return device_compatible(Path(self.cfg.dev_mode).exists())

    def policy(self):
        return Policy(self.cfg.state_dir, self.cfg.signers, self.serial())

    def dev_grant(self):
        return DevGrant(self.cfg.state_dir)

    def updater(self, layout, backend=None):
        state_path = Path(self.cfg.state_dir) / "state.json"
        Path(self.cfg.state_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
        state = None if state_path.exists() else UpdateState(active=layout.active.name.upper(),
                                                              version_active=self.build().get("ota_version", "0.0.0"))
        u = Updater(backend or slots.BlockSlots(layout, self.cfg.paths), state_path, state,
                    compatible=self.compatible())
        if state is not None:
            u.save()
        return u

    @staticmethod
    def consistent(u, layout):
        booted = layout.active.name.upper()
        if u.s.state in ("TRYBOOT", "HEALTH_PENDING"):
            return booted in (u.s.active, u.s.target)
        return u.s.state == "INSTALLING" or booted == u.s.active

    def status(self):
        out = {"serial": self.serial(), "compatible": self.compatible(), "build": self.build()}
        try:
            layout = self.detect(self.cfg.paths)
            out["slots"] = {"active": layout.active.name.upper(), "other": layout.other.name.upper(),
                            "active_boot": layout.active.boot, "active_system": layout.active.system,
                            "other_boot": layout.other.boot, "other_system": layout.other.system,
                            "tryboot_now": layout.tryboot}
            u = self.updater(layout)
            out["state"] = vars(u.s)
            out["consistent"] = self.consistent(u, layout)
        except UpdateError as e:
            out["slots_error"] = str(e)
        try:
            text = (Path(self.cfg.paths.bootfs) / "autoboot.txt").read_text()
            out["autoboot"] = dict(zip(("default_part", "tryboot_part"), slots.autoboot_parts(text)))
        except (OSError, UpdateError) as e:
            out["autoboot_error"] = str(e)
        out["switching"] = self.policy().allowed()[1]
        out["dev_mode"] = self.dev_grant().current()
        return out

    # ---------- действия ----------
    def handle(self, request, stream, emit, caller=""):
        """caller — служба systemd клиента сокета (caller_unit): режим разработчика взводит только служба устройства."""
        action = request.get("action")
        if action == "status":
            return {"result": "OK", **self.status()}
        if action not in ("install", "enable", "disable", "tryboot", "confirm", "abort", "dev-arm", "dev-disarm"):
            return {"result": "REJECTED", "reason": f"неизвестное действие {action!r}"}
        if action == "dev-arm":
            if caller not in ARM_UNITS:
                return {"result": "REFUSED", "reason": f"режим разработчика включается только на устройстве "
                                                       f"(GUI пульта или по подтверждению на пульте), не {caller or 'извне'}"}
            info = self.dev_grant().arm(caller)
            return {"result": "OK", "reason": f"разрешена установка одного собственного образа до "
                                              f"{int(info['expires'] - info['armed'])} с", "dev_mode": info}
        if action == "dev-disarm":
            self.dev_grant().consume()
            return {"result": "OK", "reason": "режим разработчика выключен"}
        if action == "disable":
            self.policy().disable()
            return {"result": "OK", "reason": "переключение слотов запрещено"}
        if action == "enable":
            try:
                self.policy().enable(str(request.get("token", "")).encode(), str(request.get("sig", "")).encode())
            except (sig.SignatureError, PayloadError) as e:
                return {"result": "REJECTED", "reason": str(e)}
            return {"result": "OK", "reason": f"переключение слотов разрешено для платы {self.serial()}"}
        layout = self.detect(self.cfg.paths)                 # UpdateError → отказ без изменений
        u = self.updater(layout)
        if u.s.state == "INSTALLING":                        # блокировка у нас: прежний агент завершился посреди записи
            u._go("FAILED", "запись прервана (агент завершён); основной слот не изменён")
        if not self.consistent(u, layout):
            if action != "abort":
                return {"result": "REJECTED", "reason": f"состояние {u.s.state}/{u.s.active} не согласовано со слотом "
                                                        f"{layout.active.name.upper()}; выполните abort"}
        if action == "install":
            return self._install(layout, u, stream, emit)
        if action == "abort":
            return self._abort(layout, u)
        allowed, why = self.policy().allowed()
        if not allowed and not (u.s.dev_image and u.s.state in ("INSTALLED", "TRYBOOT", "HEALTH_PENDING")):
            return {"result": "REFUSED", "reason": why}
        if action == "tryboot":
            return self._tryboot(layout, u)
        return self._confirm(layout, u)

    def _install(self, layout, u, stream, emit):
        if u.s.state in ("TRYBOOT", "HEALTH_PENDING", "INSTALLING"):
            return {"result": "REJECTED", "reason": f"идёт обновление: {u.s.state}; сначала abort"}
        grant = self.dev_grant()
        armed = grant.current() is not None
        if armed:
            grant.consume()                          # одна установка в режиме разработчика — успешная или нет
        try:
            payload = Payload(stream, self.cfg.signers, self.compatible(), allow_unsigned=armed)
        except (PayloadError, sig.SignatureError) as e:
            return {"result": "REJECTED", "reason": str(e)}
        m = payload.manifest
        emit({"msg": f"{'собственный образ без подписи проекта (режим разработчика)' if payload.dev else 'подпись принята'}; "
                     f"версия {m['version']} (коммит {str(m.get('commit'))[:12]}) → слот {layout.other.name.upper()}"})
        if u.s.state in ("VERIFIED", "INSTALLED"):
            u.reset(layout.active.name.upper(), u.s.version_active, "замена ранее записанного обновления")
        u.b = slots.BlockSlots(layout, self.cfg.paths, payload, seeder=self.seeder,
                               progress=lambda part, w, t: emit({"msg": f"{part}: {w * 100 // t}%"}))
        u.verify({"version": m["version"], "compatible": m["compatible"], "signature_ok": True})
        u.s.dev_image = payload.dev
        if u.s.state == "VERIFIED":                          # отпечатки рабочего слота для критериев подтверждения
            u.s.expect = confirm.expect_from_board(self.cfg.root)
            u.save()
        if u.s.state == "FAILED":
            return {"result": "FAILED", "reason": u.s.reason, "state": vars(u.s)}
        try:
            u.install()
        except UpdateError as e:
            u._go("FAILED", str(e))
        if u.s.state != "INSTALLED":
            return {"result": "FAILED", "reason": u.s.reason, "state": vars(u.s)}
        return {"result": "OK", "reason": f"слот {u.s.target} записан и проверен; основной слот не изменён",
                "state": vars(u.s)}

    def _tryboot(self, layout, u):
        u.b = slots.BlockSlots(layout, self.cfg.paths)
        if u.s.state in ("INSTALLED", "TRYBOOT") and u.s.target and u.s.target.lower() == layout.other.name:
            # Конфигурация пульта — с текущего (рабочего, самого свежего) слота непосредственно перед пробной
            # загрузкой: изменения после установки не теряются; старая копия новую не перезаписывает (S3.2-OTA).
            seeder = self.seeder or slots.VarSeeder(self.cfg.paths)
            seeder.carry(seeder.target_var(layout, layout.other.name))
        if u.s.state == "INSTALLED":
            u.request_tryboot()
        elif u.s.state == "TRYBOOT":                         # повтор после health FAIL (UPD-004: не больше 2 попыток)
            u.b.set_tryboot(u.s.target)
        else:
            return {"result": "REJECTED", "reason": f"пробная загрузка невозможна в состоянии {u.s.state}"}
        self.run(list(self.cfg.tryboot_cmd), capture_output=True)
        return {"result": "OK", "reason": f"перезагрузка в слот {u.s.target} однократно (tryboot)", "state": vars(u.s)}

    def _confirm(self, layout, u):
        if u.s.state != "HEALTH_PENDING" or not layout.tryboot or layout.active.name.upper() != u.s.target:
            return {"result": "REJECTED", "reason": f"подтверждать можно только на пробной загрузке нового слота "
                                                    f"(состояние {u.s.state}, tryboot={layout.tryboot})"}
        probe = self.cfg.probe or confirm.Probe(self.cfg.root, self.run)
        ok, checks = confirm.gates(layout, u.s, probe, lab_video=self.cfg.lab_video)
        failed = [c["id"] for c in checks if c["mandatory"] and c["status"] != "PASS"]
        u.b = slots.BlockSlots(layout, self.cfg.paths)
        u.on_health("PASS" if ok else "FAIL", layout.active.name.upper())
        res = "OK" if u.s.state == "COMMITTED" else "FAILED"
        return {"result": res, "reason": f"критерии подтверждения: {'все PASS' if ok else 'не пройдены ' + ', '.join(failed)}; "
                                         f"{u.s.reason or u.s.state}", "checks": checks, "state": vars(u.s)}

    def committed_part(self):
        return slots.autoboot_parts((Path(self.cfg.paths.bootfs) / "autoboot.txt").read_text())[0]

    def _abort(self, layout, u):
        booted = layout.active.name.upper()
        if layout.active.boot_part != self.committed_part():
            # Идёт пробная загрузка неподтверждённого слота: обычная перезагрузка вернёт основной ([all]).
            self.run(list(self.cfg.reboot_cmd), capture_output=True)
            return {"result": "OK", "reason": "обычная перезагрузка: прошивка вернёт основной слот"}
        if u.s.state in ("TRYBOOT", "HEALTH_PENDING"):
            u.abort_trial("отказ оператора от пробной загрузки")
        u.reset(booted, u.s.version_active if booted == u.s.active else self.build().get("ota_version", "0.0.0"))
        return {"result": "OK", "reason": "состояние сброшено; слоты не изменены", "state": vars(u.s)}

    def on_boot(self):
        layout = self.detect(self.cfg.paths)
        u = self.updater(layout)
        before = u.s.state
        if before in ("TRYBOOT", "HEALTH_PENDING", "INSTALLING"):
            u.on_boot(layout.active.name.upper())
        return {"result": "OK", "slot": layout.active.name.upper(), "tryboot_now": layout.tryboot,
                "state": f"{before} → {u.s.state}"}


# ---------------------------------------------------------------- сокет


def serve(agent, rfile, wfile, caller=""):
    def emit(obj):
        try:
            wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            wfile.flush()
        except OSError:
            pass                                     # клиент отключился — работа доводится до конца
    line = rfile.readline(MAX_MANIFEST)
    try:
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("не объект")
    except ValueError as e:
        emit({"result": "REJECTED", "reason": f"неверный запрос: {e}"})
        return 2
    Path(agent.cfg.state_dir).mkdir(mode=0o755, parents=True, exist_ok=True)
    with open(Path(agent.cfg.state_dir) / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            emit({"result": "BUSY", "reason": "агент занят другим действием"})
            return 1
        try:
            result = agent.handle(request, rfile, emit, caller=caller)
        except (UpdateError, OSError) as e:
            result = {"result": "FAILED", "reason": str(e)}
    emit(result)
    return 0 if result.get("result") == "OK" else 1


def client(action, stdin, stdout, path=SOCKET):
    """Клиент от имени sheregesh-ui: запрос, затем для install — поток из stdin; ответы — строками JSON."""
    request = {"action": action}
    if action == "enable":
        request.update(json.loads(stdin.read(MAX_MANIFEST)))
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    with s:
        s.sendall((json.dumps(request) + "\n").encode())
        if action == "install":
            for chunk in iter(lambda: stdin.read(1 << 20), b""):
                s.sendall(chunk)
        s.shutdown(socket.SHUT_WR)
        last = {}
        with s.makefile("rb") as f:
            for line in f:
                stdout.write(line.decode(errors="replace"))
                stdout.flush()
                try:
                    last = json.loads(line)
                except ValueError:
                    pass
    return 0 if last.get("result") == "OK" else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve"]:                    # systemd Accept=yes: соединение — stdin/stdout
        with socket.socket(fileno=os.dup(0)) as conn:
            caller = caller_unit(conn)
        return serve(Agent(), sys.stdin.buffer, sys.stdout.buffer, caller=caller)
    if argv[:1] == ["on-boot"]:
        return on_boot_main(Agent())
    print("использование: sheregesh-ota-agent serve | on-boot", file=sys.stderr)
    return 2


def on_boot_main(agent):
    """Учёт загрузки. Ошибка определения слотов — сбой юнита только если идёт обновление (её нельзя скрыть);
    без незавершённого обновления — предупреждение в журнале (иначе sheregesh-health видел бы failed-юнит)."""
    try:
        print(json.dumps(agent.on_boot(), ensure_ascii=False))
        return 0
    except (UpdateError, OSError) as e:
        try:
            pending = json.loads((Path(agent.cfg.state_dir) / "state.json").read_text()).get("state")
        except (OSError, ValueError):
            pending = None
        busy = pending in ("INSTALLING", "TRYBOOT", "HEALTH_PENDING")
        print(f"sheregesh-ota: слоты не определены ({e}); состояние {pending or 'нет'}", file=sys.stderr)
        return 1 if busy else 0


def client_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    actions = ("status", "install", "enable", "disable", "tryboot", "confirm", "abort", "dev-arm", "dev-disarm")
    if len(argv) != 1 or argv[0] not in actions:
        print(f"использование: sheregesh-ota-client {{{'|'.join(actions)}}}", file=sys.stderr)
        return 2
    try:
        return client(argv[0], sys.stdin.buffer if argv[0] == "install" else sys.stdin, sys.stdout)
    except OSError as e:
        print(f"sheregesh-ota-client: агент недоступен ({e}); OTA есть только в dev-образе S3.1-DEPLOY",
              file=sys.stderr)
        return 1


assert set(STATES) >= {"IDLE", "INSTALLED", "COMMITTED"}


if __name__ == "__main__":
    sys.exit(main())
