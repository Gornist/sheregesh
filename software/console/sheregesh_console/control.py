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

MAX_REQUEST = 4096
WAIT_MAX_S = 60.0
CALL_TIMEOUT_S = 5.0        # поток интерфейса не ответил за это время — «busy», а не зависшая команда


def server_path(env=None):
    """Путь сокета пульта: SHEREGESH_CONSOLE_CONTROL или $XDG_RUNTIME_DIR/sheregesh-console.sock; "" — канала нет
    (запуск без каталога выполнения, например разработка без systemd)."""
    env = os.environ if env is None else env
    if env.get("SHEREGESH_CONSOLE_CONTROL"):
        return env["SHEREGESH_CONSOLE_CONTROL"]
    return os.path.join(env["XDG_RUNTIME_DIR"], videoselect.CONSOLE_SOCKET) if env.get("XDG_RUNTIME_DIR") else ""


class ControlServer(QObject):
    _call = Signal(object)          # (fn, box, event): из потока соединения — в поток интерфейса

    def __init__(self, backend, path, parent=None):
        super().__init__(parent)
        self.backend, self.path = backend, str(path)
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
        if not isinstance(req, dict) or req.get("cmd") not in ("list", "select"):
            return self._fail("bad-request", "cmd: list | select")
        if not self._busy.acquire(blocking=False):
            return self._fail("busy", "another request is in progress")
        try:
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
