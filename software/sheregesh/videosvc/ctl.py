# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Сокет управления видеослужбы: сервер и клиент (ADR-0016, контракт §3).

Кадрирование: JSON UTF-8, одна строка — одно сообщение, не длиннее 16 КиБ. Сервер пропускает только
клиента с тем же uid (SO_PEERCRED на Linux, LOCAL_PEERCRED на macOS); чужого или неопознанного
закрывает без ответа. Ключ в ответах и журнале маскируется через `session.mask`.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import struct
import sys
import threading

from .session import mask

log = logging.getLogger("sheregesh.videosvc.ctl")

MAX_LINE = 16 * 1024
MAX_REPLY = 1024 * 1024
ERROR_CODES = frozenset({"bad-request", "unknown-cmd", "unknown-stream", "stale", "busy", "pipeline",
                         "unsupported", "forbidden"})


class CtlError(Exception):
    """Ошибка команды с кодом из контракта §3. Обработчик бросает её, клиент получает её же."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"неизвестный код ошибки: {code!r}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class CtlUnavailable(OSError):
    """Служба недоступна: нет сокета, обрыв без ответа (в том числе отказ по uid), бессвязный ответ."""


def peer_uid(sock: socket.socket) -> int:
    """uid процесса на другом конце unix-сокета. Не удалось определить — OSError (соединение отклоняется)."""
    if sys.platform.startswith("linux"):
        data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", data)
        return uid
    if sys.platform == "darwin":
        # struct xucred { u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]; }
        LOCAL_PEERCRED = 0x001
        data = sock.getsockopt(0, LOCAL_PEERCRED, 76)   # уровень SOL_LOCAL = 0
        if len(data) < 8:
            raise OSError("короткий xucred")
        version, uid = struct.unpack_from("II", data)
        if version != 0:
            raise OSError("неизвестная версия xucred")
        return uid
    raise OSError("peer_uid не поддержан на этой платформе")


def _dump(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8") + b"\n"


class CtlServer:
    """Потоковый сервер unix-сокета. `handler(cmd, args) -> dict` (result) или бросает CtlError."""

    def __init__(self, path: str, handler, allowed_uid: int | None = None) -> None:
        self.path = str(path)
        self.handler = handler
        self.allowed_uid = os.getuid() if allowed_uid is None else allowed_uid
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._conns: dict[socket.socket, threading.Thread] = {}
        self._stop = threading.Event()

    # --- жизненный цикл ---

    def start(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)
            try:
                s.bind(self.path)
            except BaseException:
                s.close()
                raise
            finally:
                os.umask(old)
            os.chmod(self.path, 0o600)
            s.listen(16)
            s.settimeout(0.05)
            self._stop.clear()
            self._sock = s
            self._accept_thread = threading.Thread(target=self._accept_loop, args=(s,), daemon=True,
                                                   name="videosvc-ctl-accept")
            self._accept_thread.start()

    def close(self) -> None:
        with self._lock:
            s, self._sock = self._sock, None
            t, self._accept_thread = self._accept_thread, None
            conns, self._conns = dict(self._conns), {}
        if s is None:
            return
        self._stop.set()
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if t is not None:
            t.join(2.0)
        for c, th in conns.items():
            th.join(2.0)
            c.close()
        s.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # --- внутреннее ---

    def _accept_loop(self, s: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = s.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                self._stop.wait(0.05)               # временный сбой accept не должен убивать сервер
                continue
            try:
                uid = peer_uid(conn)
            except OSError:
                log.warning("ctl: uid клиента не определён, соединение закрыто")
                conn.close()
                continue
            if uid != self.allowed_uid:
                log.warning("ctl: чужой uid %s, соединение закрыто", uid)
                conn.close()
                continue
            conn.settimeout(0.3)
            th = threading.Thread(target=self._serve, args=(conn,), daemon=True, name="videosvc-ctl-conn")
            with self._lock:
                if self._sock is None:
                    conn.close()
                    return
                self._conns[conn] = th
            th.start()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while not self._stop.is_set():
                nl = buf.find(b"\n")
                if nl < 0:
                    if len(buf) > MAX_LINE:
                        self._send(conn, self._err(0, "bad-request", "строка длиннее 16 КиБ"))
                        return                       # сбиты границы кадров: соединение закрывается
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        return
                    buf += chunk
                    continue
                line, buf = buf[:nl], buf[nl + 1:]
                if len(line) > MAX_LINE:
                    self._send(conn, self._err(0, "bad-request", "строка длиннее 16 КиБ"))
                    return
                if not self._send(conn, self._dispatch(line)):
                    return
        except OSError:
            pass
        finally:
            with self._lock:
                self._conns.pop(conn, None)
            conn.close()

    @staticmethod
    def _send(conn: socket.socket, reply: dict) -> bool:
        data = _dump(reply)
        try:
            conn.settimeout(2.0)
            conn.sendall(data)
            conn.settimeout(0.3)
            return True
        except OSError:
            return False

    @staticmethod
    def _err(rid: int, code: str, detail: str = "") -> dict:
        return {"v": 1, "id": rid, "ok": False, "error": code, "detail": detail}

    def _dispatch(self, line: bytes) -> dict:
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return self._err(0, "bad-request", "не JSON")
        raw_id = req.get("id") if isinstance(req, dict) else None
        id_ok = isinstance(raw_id, int) and not isinstance(raw_id, bool)
        rid = raw_id if id_ok else 0
        if not isinstance(req, dict) or type(req.get("v")) is not int or req["v"] != 1:
            return self._err(rid, "bad-request", "ожидается объект с v=1")
        cmd, args = req.get("cmd"), req.get("args", {})
        if not id_ok or not isinstance(cmd, str) or not cmd \
                or not isinstance(args, dict) or set(req) - {"v", "id", "cmd", "args"}:
            return self._err(rid, "bad-request", "неверные поля запроса")
        log.debug("ctl: %s %s", cmd, mask(args))
        try:
            result = self.handler(cmd, args)
            if not isinstance(result, dict):
                raise TypeError("результат не объект")
            reply = {"v": 1, "id": rid, "ok": True, "result": mask(result)}
            _dump(reply)                              # проверка сериализуемости
            return reply
        except CtlError as e:
            return self._err(rid, e.code, e.detail)
        except Exception as e:                        # noqa: BLE001 — текст исключения может содержать ключ
            log.error("ctl: сбой обработчика команды %s: %s", cmd, type(e).__name__)
            return self._err(rid, "pipeline", "внутренняя ошибка")


class CtlClient:
    """Клиент сокета управления. Одно соединение, переподключение при обрыве (одна повторная попытка).

    Повтор даёт семантику «хотя бы раз»: команды контракта (start/stop/…) идемпотентны.
    """

    def __init__(self, path: str, timeout: float = 2.0) -> None:
        self.path = str(path)
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 1
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self._buf = None, b""

    def _connect(self) -> socket.socket:
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(self.path)
            except OSError:
                s.close()
                raise
            self._sock = s
        return self._sock

    def _roundtrip(self, data: bytes) -> dict:
        s = self._connect()
        s.sendall(data)
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_REPLY:
                raise CtlUnavailable("слишком длинный ответ")
            chunk = s.recv(65536)
            if not chunk:
                raise CtlUnavailable("соединение закрыто без ответа")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            reply = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CtlUnavailable("ответ не JSON") from None
        if not isinstance(reply, dict):
            raise CtlUnavailable("ответ не объект")
        return reply

    def request(self, cmd: str, args: dict | None = None) -> dict:
        """Вернуть result или бросить CtlError; недоступность службы — CtlUnavailable (OSError)."""
        with self._lock:
            rid, self._next_id = self._next_id, self._next_id + 1
            try:
                data = _dump({"v": 1, "id": rid, "cmd": cmd, "args": args or {}})
            except (TypeError, ValueError):
                raise CtlError("bad-request", "аргументы не сериализуются в JSON") from None
            if len(data) - 1 > MAX_LINE:
                raise CtlError("bad-request", "запрос длиннее 16 КиБ")
            reply = None
            for attempt in (0, 1):
                try:
                    reply = self._roundtrip(data)
                    break
                except (OSError, socket.timeout) as e:
                    self._drop()
                    if attempt or isinstance(e, socket.timeout):
                        if isinstance(e, CtlUnavailable):
                            raise
                        raise CtlUnavailable(str(e)) from None
            if reply.get("id") != rid:
                self._drop()
                raise CtlUnavailable("id ответа не совпал с запросом")
            if reply.get("ok") is True and isinstance(reply.get("result"), dict):
                return reply["result"]
            code = reply.get("error")
            if reply.get("ok") is False and code in ERROR_CODES:
                raise CtlError(code, str(reply.get("detail", "")))
            self._drop()
            raise CtlUnavailable("бессвязный ответ")
