# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Локальный канал управления работающим пультом (S5 GUI-02): выбор видеоисточника блока из командной строки
(sheregesh.link.videoselect) без второго сеанса управления.

Блок держит один сеанс управления: новое подключение заменяет прежнее (sheregesh_remote.agent.handle). Собственный
PeerClient команды вытеснял сеанс пульта, пульт переподключался и вытеснял команду раньше отправки video-select
(стенд S4: not-connected — link lost before sending). Здесь команда передаёт запрос пульту, а пульт выполняет тот же
Backend.selectVideoSource(id, True), что кнопка экрана «Видео», в своём сеансе; итог — по status.video.sources блока.

Канал — unix-сокет в каталоге выполнения пользователя пульта ($XDG_RUNTIME_DIR службы sheregesh-console —
/run/sheregesh-ui, RuntimeDirectoryMode=0700), сокет 0600; на Linux дополнительно сверяется uid собеседника
(SO_PEERCRED). Сетевого доступа нет. Протокол: одна строка JSON запроса, одна строка JSON ответа:
  {"cmd": "list"}                                   → {"result": "ok", "detail", "active", "source", "sources"}
  {"cmd": "select", "id": "rtsp:0", "wait": 15}     → то же; result: ok | unknown | unavailable | unsupported |
                                                      no-link | busy | timeout | failed | bad-request
id проверяется по списку источников блока (status.video.sources); вызовы бэкенда — только в потоке интерфейса.

Внешний модуль блока (S6, ADR-0012, 9.3) — те же слоты ModuleView с confirmed=True, что кнопки экрана «Модуль»:
  {"cmd": "module", "op": "status"}                                  → {"result": "ok", "detail": "", "module": M}
  {"cmd": "module", "op": "reinit"|"reset", "wait": 15}              → {"result": код, "detail": текст, "module": M}
  {"cmd": "module", "op": "lab", "args": {"scenario": "detach"}, "wait": 15}
  {"cmd": "module", "op": "configure", "args": {"enabled": false}, "wait": 30}
  {"cmd": "module", "op": "configure", "args": {"backend": "crsf-pty-lab"}, "wait": 30}   (S8, ADR-0014: lab-emulator |
                                                                       crsf-pty-lab | crsf-uart — ModuleView.setBackend)
M = ModuleView.snapshot(); result: ok | коды блока (disabled, forbidden, not-running, invalid, not-saved, error, …) |
no-link | unsupported | timeout | busy | bad-request | failed.

Стики и кнопки → блок (S7, ADR-0013, 4.2) — тот же слот SticksView.setEnabled, что переключатель экрана «Управление»:
  {"cmd": "sticks", "op": "status"}                     → {"result": "ok", "detail": "", "sticks": S}
  {"cmd": "sticks", "op": "enable", "confirmed": true}  → {"result": код, "detail": текст, "sticks": S}
  {"cmd": "sticks", "op": "disable"}                    → то же (выключение — без подтверждения)
S = SticksView.snapshot(); result: ok | not-confirmed (enable без "confirmed": true) | no-device | no-link | old-remote |
bad-request | busy | failed. Включение из локального канала — только с явным флагом confirmed (для QA, ST-03).

Интерфейс (Minimal, испытания GUI-12 и снимки на плате):
  {"cmd": "ui", "op": "state"}                          → {"result": "ok", "detail": "", "ui": P}
  {"cmd": "ui", "op": "open", "section": "video"}       → то же (video | control | module | diagnostics | system | "")
  {"cmd": "ui", "op": "grab"}                           → {"result": "ok", "detail": путь PNG рядом с сокетом, "ui": P}
P = Ui.perf(): макет, отрисованные кадры за 5 с, открытый раздел, время открытия раздела. Снимок пишется только
в каталог сокета (ui-grab.png), путь из запроса не принимается; без дисплея (окно не на экране) — unsupported.
"""
import json
import os
import socket
import stat
import struct
import threading
import time

from PySide6.QtCore import QObject, Qt, Signal, Slot

from sheregesh.link import videoselect

from .module import BACKENDS, CONFIGURE_KEYS

MAX_REQUEST = 4096
WAIT_MAX_S = 60.0
CALL_TIMEOUT_S = 5.0        # поток интерфейса не ответил за это время — «busy», а не зависшая команда
MODULE_OPS = ("status", "reinit", "reset", "lab", "configure")
MODULE_WAIT_S = {"configure": 30.0}         # по умолчанию 15 с
STICKS_OPS = ("status", "enable", "disable")


def server_path(env=None):
    """Путь сокета пульта: SHEREGESH_CONSOLE_CONTROL или $XDG_RUNTIME_DIR/sheregesh-console.sock; "" — канала нет
    (запуск без каталога выполнения, например разработка без systemd)."""
    env = os.environ if env is None else env
    if env.get("SHEREGESH_CONSOLE_CONTROL"):
        return env["SHEREGESH_CONSOLE_CONTROL"]
    return os.path.join(env["XDG_RUNTIME_DIR"], videoselect.CONSOLE_SOCKET) if env.get("XDG_RUNTIME_DIR") else ""


class ControlServer(QObject):
    _call = Signal(object)          # (fn, box, event): из потока соединения — в поток интерфейса

    def __init__(self, backend, path, parent=None, ui=None):
        super().__init__(parent)
        self.backend, self.path, self.ui = backend, str(path), ui
        self._sock = None
        self._busy = threading.Lock()   # один запрос за раз: выбор источника и так один (videoSelection)
        self._call.connect(self._run_call, Qt.QueuedConnection)

    # ---------- жизненный цикл ----------
    def start(self):
        d = os.path.dirname(os.path.abspath(self.path))
        st = os.stat(d)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise PermissionError(f"{d}: каталог канала должен принадлежать пользователю пульта и иметь права 0700")
        try:
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)                 # сокет прежнего процесса (перезапуск службы)
        except FileNotFoundError:
            pass
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        s.listen(4)
        self._sock = s
        threading.Thread(target=self._accept, args=(s,), name="console-control", daemon=True).start()
        return self

    def stop(self):
        s, self._sock = self._sock, None
        if s is None:
            return
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept(self, s):
        while self._sock is s:
            try:
                conn, _ = s.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), name="console-control-req", daemon=True).start()

    # ---------- запрос ----------
    @staticmethod
    def _peer_ok(conn):
        if not hasattr(socket, "SO_PEERCRED"):
            return True                              # не Linux: защищает каталог 0700 и сокет 0600
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                              struct.calcsize("3i")))
        return uid == os.getuid()

    def _serve(self, conn):
        with conn:
            try:
                conn.settimeout(CALL_TIMEOUT_S)
                if not self._peer_ok(conn):
                    return
                buf = b""
                while b"\n" not in buf and len(buf) <= MAX_REQUEST:
                    chunk = conn.recv(MAX_REQUEST + 1)
                    if not chunk:
                        break
                    buf += chunk
                reply = self.handle(buf) if len(buf) <= MAX_REQUEST else self._fail("bad-request", "request too long")
                conn.settimeout(None)
                conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
            except (OSError, ValueError):
                pass

    @staticmethod
    def _fail(code, detail):
        return {"result": code, "detail": detail}

    def handle(self, raw):
        """Запрос (байты строки JSON) → ответ. Вызывается из потока соединения."""
        try:
            req = json.loads(raw.decode("utf-8").strip() or "null")
        except (UnicodeDecodeError, ValueError):
            return self._fail("bad-request", "not a JSON line")
        if not isinstance(req, dict) or req.get("cmd") not in ("list", "select", "module", "sticks", "ui"):
            return self._fail("bad-request", "cmd: list | select | module | sticks | ui")
        if not self._busy.acquire(blocking=False):
            return self._fail("busy", "another request is in progress")
        try:
            if req["cmd"] == "module":
                return self._module(req)
            if req["cmd"] == "sticks":
                return self._sticks(req)
            if req["cmd"] == "ui":
                return self._ui(req)
            snap = self._invoke(self._snapshot)
            if req["cmd"] == "list":
                return self._reply("ok", "", snap)
            sid, wait = req.get("id"), req.get("wait", 15.0)
            if not isinstance(sid, str) or not videoselect.VIDEO_SOURCE_ID.fullmatch(sid):
                return self._reply("bad-request", "id: string from status.video.sources", snap)
            if not isinstance(wait, (int, float)) or isinstance(wait, bool):
                return self._reply("bad-request", "wait: seconds", snap)
            if sid not in [s["id"] for s in snap["sources"]]:
                return self._reply("unknown", f"unknown video source id {sid!r}", snap)
            return self._select(sid, min(max(float(wait), 0.0), WAIT_MAX_S))
        except TimeoutError as e:
            return self._fail("busy", str(e))
        except Exception as e:                       # noqa: BLE001 — команда получает причину, а не обрыв
            return self._fail("failed", f"{type(e).__name__}: {e}"[:200])
        finally:
            self._busy.release()

    def _select(self, sid, wait):
        accepted = self._invoke(lambda: self.backend.selectVideoSource(sid, True))
        end = time.monotonic() + wait
        while True:
            snap = self._invoke(self._snapshot)
            sel = snap["selection"]
            if sel["state"] == "error" and sel["id"] == sid:
                return self._reply(sel["error"] or "failed", sel["detail"], snap)
            if not accepted and sel["state"] == "switching":
                return self._reply("busy", f"switching to {sel['id']} is in progress", snap)
            if sid in snap["active"] and sel["state"] != "switching":
                return self._reply("ok", "", snap)
            if time.monotonic() >= end:
                return self._reply("timeout", f"{sid} not active after {wait:g} s", snap)
            time.sleep(0.1)

    # ---------- внешний модуль (S6) ----------
    def _module(self, req):
        mv = self.backend.module
        snap = lambda: self._invoke(mv.snapshot)          # noqa: E731
        op, args = req.get("op"), req.get("args", {})
        wait = req.get("wait", MODULE_WAIT_S.get(op, 15.0))
        if op not in MODULE_OPS:
            return self._module_reply("bad-request", "op: " + " | ".join(MODULE_OPS), snap())
        if not isinstance(args, dict) or not isinstance(wait, (int, float)) or isinstance(wait, bool):
            return self._module_reply("bad-request", "args: object, wait: seconds", snap())
        if op == "status":
            return self._module_reply("ok", "", snap())
        if op in ("reinit", "reset") and args:
            return self._module_reply("bad-request", f"{op}: no args", snap())
        if op == "lab" and (set(args) != {"scenario"} or not isinstance(args["scenario"], str)):
            return self._module_reply("bad-request", "lab: args {\"scenario\": name}", snap())
        if op == "configure" and (not args or any(k not in CONFIGURE_KEYS or type(v) is not CONFIGURE_KEYS[k]
                                                  for k, v in args.items())
                                  or "backend" in args and args["backend"] not in BACKENDS):
            return self._module_reply("bad-request", "configure: args subset of " + ", ".join(CONFIGURE_KEYS)
                                      + "; backend: " + " | ".join(BACKENDS), snap())
        call = {"reinit": lambda: mv.requestReinit(True), "reset": lambda: mv.requestReset(True),
                "lab": lambda: mv.runLabScenario(args.get("scenario", ""), True),
                "configure": lambda: mv.configure(args, True)}[op]
        before = self._invoke(lambda: mv.command)
        accepted = self._invoke(call)
        cmd = self._invoke(lambda: mv.command)
        if not accepted:
            if cmd["state"] == "sending":
                return self._module_reply("busy", f"command {cmd['op']} is in progress", snap())
            if cmd["state"] == "error" and cmd["id"] != before["id"]:
                return self._module_reply(cmd["result"] or "failed", cmd["detail"], snap())
            return self._module_reply("failed", "command not accepted", snap())
        cid, end = cmd["id"], time.monotonic() + min(max(float(wait), 0.0), WAIT_MAX_S)
        while True:
            cmd = self._invoke(lambda: mv.command)
            if cmd["id"] != cid:
                return self._module_reply("busy", "another command replaced this one", snap())
            if cmd["state"] != "sending":
                return self._module_reply(cmd["result"] or ("ok" if cmd["state"] == "done" else "failed"),
                                          cmd["detail"], snap())
            if time.monotonic() >= end:
                return self._module_reply("timeout", f"no result after {wait:g} s", snap())
            time.sleep(0.1)

    # ---------- стики и кнопки → блок (S7) ----------
    def _sticks(self, req):
        sv = self.backend.sticks
        snap = lambda: self._invoke(sv.snapshot)          # noqa: E731
        op, confirmed = req.get("op"), req.get("confirmed", False)
        if op not in STICKS_OPS:
            return self._sticks_reply("bad-request", "op: " + " | ".join(STICKS_OPS), snap())
        if not isinstance(confirmed, bool) or set(req) - {"cmd", "op", "confirmed"}:
            return self._sticks_reply("bad-request", "fields: op, confirmed (bool)", snap())
        if op == "status":
            return self._sticks_reply("ok", "", snap())
        if op == "enable" and confirmed is not True:
            return self._sticks_reply("not-confirmed", "enable requires \"confirmed\": true", snap())
        ok = self._invoke(lambda: sv.setEnabled(op == "enable", confirmed))
        code = "ok" if ok else (self._invoke(lambda: sv.result) or "failed")
        detail = {"no-device": "no control device", "no-link": "no link to the remote unit",
                  "old-remote": "remote unit does not support rc (protocol < 1.4)"}.get(code, "")
        return self._sticks_reply(code, detail, snap())

    @staticmethod
    def _sticks_reply(code, detail, sticks):
        return {"result": code, "detail": detail, "sticks": sticks}

    @staticmethod
    def _module_reply(code, detail, module):
        return {"result": code, "detail": detail, "module": module}

    @staticmethod
    def _reply(code, detail, snap):
        return {"result": code, "detail": detail, "active": snap["active"], "source": snap["source"],
                "sources": snap["sources"], "display": snap["display"]}

    def _snapshot(self):
        b = self.backend
        tel = getattr(b, "_remote_video_tel", None) or {}
        return {"sources": [dict(s) for s in b.videoSources], "active": [s["id"] for s in b.videoSources if s["active"]],
                "source": tel.get("source"), "selection": dict(b.videoSelection), "display": b.videoSource["display"]}

    # ---------- вызов в потоке интерфейса ----------
    SECTIONS = ("video", "control", "module", "diagnostics", "system", "")

    def _ui(self, req):
        ui, op = self.ui, req.get("op")
        if ui is None:
            return self._fail("unsupported", "no interface in this process")
        if op == "state":
            return {"result": "ok", "detail": "", "ui": self._invoke(ui.perf)}
        if op == "open":
            sec = req.get("section")
            if ui.layout != "minimal" or sec not in self.SECTIONS:
                return self._fail("bad-request", "section: video | control | module | diagnostics | system | \"\" "
                                                 "(layout minimal)")

            def go():                                 # как нажатие кнопки меню: замер открытия — тем же полем
                w = ui.window
                w.setProperty("details", "")
                if sec and w.property("section") != sec:
                    w.setProperty("openLoaded", False)
                    w.setProperty("openStartedMs", time.time() * 1000.0)
                w.setProperty("section", sec)
                return ui.perf()
            return {"result": "ok", "detail": "", "ui": self._invoke(go)}
        if op == "grab":
            out = os.path.join(os.path.dirname(os.path.abspath(self.path)), "ui-grab.png")
            # Окно не на экране (нет дисплея — cage без выхода): grabWindow ждёт кадр, которого не будет, и поток
            # интерфейса встаёт до сторожевого таймера systemd. Снимок — только с отображаемого окна.
            if not self._invoke(ui.window.isExposed):
                return self._fail("unsupported", "window is not exposed (no display output)")

            def grab():
                ui.grab(out)
                return ui.perf()
            return {"result": "ok", "detail": out, "ui": self._invoke(grab)}
        return self._fail("bad-request", "op: state | open | grab")

    def _invoke(self, fn):
        box, done = {}, threading.Event()
        self._call.emit((fn, box, done))
        if not done.wait(CALL_TIMEOUT_S):
            raise TimeoutError("console GUI thread did not respond")
        if "error" in box:
            raise box["error"]
        return box["value"]

    @Slot(object)
    def _run_call(self, item):
        fn, box, done = item
        try:
            box["value"] = fn()
        except Exception as e:                       # noqa: BLE001 — ошибка уходит в ответ, а не в цикл Qt
            box["error"] = e
        finally:
            done.set()
