# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Юнит-тесты сокета управления видеослужбы (videosvc/ctl.py, ADR-0016, контракт §3).

Пути unix-сокетов короткие: на macOS лимит ~104 символа, поэтому временный каталог создаём в /tmp.
"""
import json
import logging
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time

import pytest

from sheregesh.videosvc import ctl as C

CANARY = "SHRG-CANARY-VIDEOKEY-xyz"
CODES = ["bad-request", "unknown-cmd", "unknown-stream", "stale", "busy", "pipeline", "unsupported", "forbidden"]


def echo_handler(cmd, args):
    return {"echo": args}


def _request_bytes(cmd, args, rid=1):
    """Строка запроса в байтах, как её формирует клиент (без перевода строки)."""
    return json.dumps({"v": 1, "id": rid, "cmd": cmd, "args": args},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _padded_args(line_len):
    """args, при которых строка запроса (id=1, cmd='x') имеет ровно line_len байт."""
    base = len(_request_bytes("x", {"p": ""}))
    return {"p": "a" * (line_len - base)}


def _connect(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3.0)
    s.connect(path)
    return s


def _read_line(s):
    """Одна строка ответа без перевода строки; если соединение закрыто раньше, вернуть накопленное (b'' при пустом)."""
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            return buf
        buf += chunk
    return buf.split(b"\n", 1)[0]


def _raw_line(path, payload):
    """Отправить сырые байты по новому соединению и вернуть строку ответа."""
    with _connect(path) as s:
        s.sendall(payload)
        return _read_line(s)


def _raw_call(path, payload):
    return json.loads(_raw_line(path, payload))


# --- фикстуры ---------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_leaked_threads():
    """После теста не должно остаться новых потоков; даём им до 1 с на завершение."""
    before = set(threading.enumerate())
    yield
    deadline = time.monotonic() + 1.0
    extra = []
    while True:
        extra = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not extra or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    assert not extra, f"остались потоки после теста: {[t.name for t in extra]}"


@pytest.fixture
def sock_dir():
    d = tempfile.mkdtemp(prefix="shv", dir="/tmp")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sock_path(sock_dir):
    return os.path.join(sock_dir, "ctl.sock")


@pytest.fixture
def run_server(sock_path):
    """Фабрика серверов: все запущенные здесь серверы закрываются в teardown."""
    servers = []

    def _run(handler=echo_handler, path=None, **kwargs):
        srv = C.CtlServer(path or sock_path, handler, **kwargs)
        servers.append(srv)
        srv.start()
        return srv

    try:
        yield _run
    finally:
        for srv in servers:
            srv.close()


@pytest.fixture
def server(sock_path):
    """Запущенный сервер с эхо-обработчиком; close() всегда вызывается."""
    srv = C.CtlServer(sock_path, echo_handler)
    srv.start()
    try:
        yield srv
    finally:
        srv.close()


# --- 1. запрос/ответ --------------------------------------------------------------------------------------------

def test_request_reply_roundtrip(server, sock_path):
    cli = C.CtlClient(sock_path)
    try:
        assert cli.request("ping", {"a": 1}) == {"echo": {"a": 1}}
        # сырой сокет: id и v должны вернуться как в запросе
        line = _raw_line(sock_path, b'{"v":1,"id":7,"cmd":"x","args":{}}\n')
        reply = json.loads(line)
        assert reply["v"] == 1
        assert reply["id"] == 7
        assert reply["ok"] is True
        assert reply["result"] == {"echo": {}}
    finally:
        cli.close()


# --- 2. коды ошибок ---------------------------------------------------------------------------------------------

def test_error_codes_match_contract():
    assert set(CODES) == C.ERROR_CODES


@pytest.mark.parametrize("code", CODES)
def test_handler_error_code_roundtrip(run_server, sock_path, code):
    detail = f"деталь для {code}"

    def handler(cmd, args):
        raise C.CtlError(code, detail)

    run_server(handler)
    cli = C.CtlClient(sock_path)
    try:
        with pytest.raises(C.CtlError) as ei:
            cli.request("x")
    finally:
        cli.close()
    assert ei.value.code == code
    assert ei.value.detail == detail

    raw = _raw_call(sock_path, _request_bytes("x", {}) + b"\n")
    assert raw == {"v": 1, "id": 1, "ok": False, "error": code, "detail": detail}


def test_ctl_error_with_unknown_code_is_value_error():
    with pytest.raises(ValueError):
        C.CtlError("nonsense")


# --- 3. внутренние сбои не утекают ------------------------------------------------------------------------------

@pytest.mark.parametrize("exc_type", [RuntimeError, ValueError, OSError])
def test_unexpected_exception_is_pipeline_without_leaking(run_server, sock_path, caplog, exc_type):
    def handler(cmd, args):
        raise exc_type(f"сбой с ключом {CANARY}")

    run_server(handler)
    cli = C.CtlClient(sock_path)
    with caplog.at_level(logging.DEBUG):
        try:
            with pytest.raises(C.CtlError) as ei:
                cli.request("x")
        finally:
            cli.close()
        raw_line = _raw_line(sock_path, _request_bytes("x", {}) + b"\n")

    raw = json.loads(raw_line)
    assert raw["ok"] is False and raw["error"] == "pipeline"
    assert ei.value.code == "pipeline"
    assert CANARY.encode() not in raw_line
    assert CANARY not in str(ei.value)
    assert CANARY not in caplog.text
    assert any("сбой обработчика" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "result",
    [["список"], "строка", None, 42, {"s": {1, 2}}, {"b": b"bytes"}],
    ids=["список", "строка", "none", "число", "set-внутри", "bytes-внутри"],
)
def test_bad_handler_result_is_pipeline(run_server, sock_path, result):
    run_server(lambda cmd, args: result)
    raw = _raw_call(sock_path, _request_bytes("x", {}) + b"\n")
    assert raw["ok"] is False
    assert raw["error"] == "pipeline"


def test_key_b64_masked_in_result_on_all_levels(run_server, sock_path):
    secret = "SHRG-SECRET-KEY-b64-test"

    def handler(cmd, args):
        return {"key_b64": secret, "a": {"b": {"key_b64": secret}},
                "lst": [{"key_b64": secret}], "tup": ({"key_b64": secret},)}

    run_server(handler)
    cli = C.CtlClient(sock_path)
    try:
        res = cli.request("x")
    finally:
        cli.close()
    assert res == {"key_b64": "***", "a": {"b": {"key_b64": "***"}},
                   "lst": [{"key_b64": "***"}], "tup": [{"key_b64": "***"}]}
    assert secret.encode() not in _raw_line(sock_path, _request_bytes("x", {}) + b"\n")


def test_key_b64_in_args_not_written_to_log(run_server, sock_path, caplog):
    secret = "SHRG-SECRET-KEY-args"
    run_server(echo_handler)
    cli = C.CtlClient(sock_path)
    with caplog.at_level(logging.DEBUG):
        try:
            cli.request("x", {"key_b64": secret})
        finally:
            cli.close()
    assert "***" in caplog.text          # журнал действительно записан
    assert secret not in caplog.text


# --- 4. некорректные запросы по сырому сокету ------------------------------------------------------------------

BAD_REQUESTS = [
    pytest.param(b"not json\n", id="не-json"),
    pytest.param(b"\xff\xfe\n", id="не-utf8"),
    pytest.param(b"[1,2]\n", id="список"),
    pytest.param(b'{"v":2,"id":1,"cmd":"x","args":{}}\n', id="v2"),
    pytest.param(b'{"v":true,"id":1,"cmd":"x","args":{}}\n', id="v-true"),
    pytest.param(b'{"id":1,"cmd":"x","args":{}}\n', id="нет-v"),
    pytest.param(b'{"v":1,"cmd":"x","args":{}}\n', id="нет-id"),
    pytest.param(b'{"v":1,"id":"7","cmd":"x","args":{}}\n', id="id-строка"),
    pytest.param(b'{"v":1,"id":true,"cmd":"x","args":{}}\n', id="id-bool"),
    pytest.param(b'{"v":1,"id":1,"cmd":"","args":{}}\n', id="cmd-пустой"),
    pytest.param(b'{"v":1,"id":1,"cmd":7,"args":{}}\n', id="cmd-не-строка"),
    pytest.param(b'{"v":1,"id":1,"cmd":"x","args":[1]}\n', id="args-список"),
    pytest.param(b'{"v":1,"id":1,"cmd":"x","args":null}\n', id="args-null"),
    pytest.param(json.dumps({"v": 1, "id": 1, "cmd": "x", "args": {}, "лишнее": 1}).encode("utf-8") + b"\n", id="лишнее-поле"),
]


@pytest.mark.parametrize("payload", BAD_REQUESTS)
def test_bad_request_keeps_connection_alive(server, sock_path, payload):
    with _connect(sock_path) as s:
        s.sendall(payload)
        bad = json.loads(_read_line(s))
        assert bad["ok"] is False
        assert bad["error"] == "bad-request"
        # то же соединение продолжает работать
        s.sendall(_request_bytes("x", {"n": 1}, rid=8) + b"\n")
        good = json.loads(_read_line(s))
    assert good["ok"] is True
    assert good["id"] == 8
    assert good["result"] == {"echo": {"n": 1}}


def test_request_without_args_is_accepted(server, sock_path):
    reply = _raw_call(sock_path, b'{"v":1,"id":3,"cmd":"x"}\n')
    assert reply["ok"] is True
    assert reply["result"] == {"echo": {}}


# --- 5. ограничение строки 16 КиБ -------------------------------------------------------------------------------

@pytest.mark.parametrize("tail", [b"\n", b""], ids=["с-переводом", "без-перевода"])
def test_line_over_16kib_gets_bad_request_and_closes(server, sock_path, tail):
    with _connect(sock_path) as s:
        s.sendall(b"x" * 20000 + tail)
        reply = json.loads(_read_line(s))
        assert reply["ok"] is False
        assert reply["error"] == "bad-request"
        assert s.recv(4096) == b""           # сервер закрыл соединение
    # новое соединение работает
    assert _raw_call(sock_path, _request_bytes("x", {}) + b"\n")["ok"] is True


def test_line_of_exactly_16kib_is_accepted(server, sock_path):
    line = _request_bytes("x", _padded_args(16384))
    assert len(line) == 16384
    with _connect(sock_path) as s:
        s.sendall(line + b"\n")
        reply = json.loads(_read_line(s))
    assert reply["ok"] is True
    assert reply["result"] == {"echo": _padded_args(16384)}


def test_line_of_16kib_plus_one_is_bad_request(server, sock_path):
    line = _request_bytes("x", _padded_args(16385))
    assert len(line) == 16385
    reply = _raw_call(sock_path, line + b"\n")
    assert reply["ok"] is False
    assert reply["error"] == "bad-request"


def test_client_refuses_oversize_request_without_sending(run_server, sock_path):
    calls = []

    def handler(cmd, args):
        calls.append(cmd)
        return {"echo": args}

    run_server(handler)
    cli = C.CtlClient(sock_path)
    try:
        with pytest.raises(C.CtlError) as ei:
            cli.request("x", _padded_args(16385))
        assert ei.value.code == "bad-request"
        assert calls == []                    # на сервер ничего не ушло
        assert cli.request("x", {"n": 2}) == {"echo": {"n": 2}}
    finally:
        cli.close()
    assert calls == ["x"]


# --- 6. проверка uid пира ---------------------------------------------------------------------------------------

def test_foreign_uid_connection_is_closed_without_reply(run_server, sock_path, monkeypatch):
    calls = []

    def handler(cmd, args):
        calls.append(cmd)
        return {}

    monkeypatch.setattr(C, "peer_uid", lambda s: os.getuid() + 1)
    run_server(handler)

    with _connect(sock_path) as s:
        assert s.recv(4096) == b""           # закрыто без ответа, данных не требуем

    cli = C.CtlClient(sock_path)
    try:
        with pytest.raises(C.CtlUnavailable):
            cli.request("x")
    finally:
        cli.close()
    assert calls == []


def test_peer_uid_error_closes_connection(run_server, sock_path, monkeypatch):
    calls = []

    def handler(cmd, args):
        calls.append(cmd)
        return {}

    def boom(sock):
        raise OSError("uid не определён")

    monkeypatch.setattr(C, "peer_uid", boom)
    run_server(handler)

    with _connect(sock_path) as s:
        assert s.recv(4096) == b""
    cli = C.CtlClient(sock_path)
    try:
        with pytest.raises(C.CtlUnavailable):
            cli.request("x")
    finally:
        cli.close()
    assert calls == []


def test_explicit_current_uid_is_allowed(run_server, sock_path):
    run_server(echo_handler, allowed_uid=os.getuid())
    cli = C.CtlClient(sock_path)
    try:
        assert cli.request("x", {"k": 1}) == {"echo": {"k": 1}}
    finally:
        cli.close()


def test_other_allowed_uid_closes_connection(run_server, sock_path):
    calls = []

    def handler(cmd, args):
        calls.append(cmd)
        return {}

    run_server(handler, allowed_uid=os.getuid() + 1)
    cli = C.CtlClient(sock_path)
    try:
        with pytest.raises(C.CtlUnavailable):
            cli.request("x")
    finally:
        cli.close()
    assert calls == []


# --- 7. peer_uid на реальном сокете -----------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform not in ("linux", "darwin"), reason="peer_uid поддержан на Linux и macOS")
def test_peer_uid_on_real_socketpair_matches_current_uid():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert C.peer_uid(a) == os.getuid()
        assert C.peer_uid(b) == os.getuid()
    finally:
        a.close()
        b.close()


# --- 8. права сокета и старый файл ------------------------------------------------------------------------------

def test_socket_file_mode_is_0600(server, sock_path):
    st = os.stat(sock_path)
    assert stat.S_ISSOCK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600


def test_stale_regular_file_is_replaced_on_start(sock_path):
    with open(sock_path, "w", encoding="utf-8") as f:
        f.write("старый файл")
    srv = C.CtlServer(sock_path, echo_handler)
    try:
        srv.start()
        assert stat.S_ISSOCK(os.stat(sock_path).st_mode)
        assert _raw_call(sock_path, _request_bytes("x", {}) + b"\n")["ok"] is True
    finally:
        srv.close()


def test_socket_file_removed_on_close(sock_path):
    srv = C.CtlServer(sock_path, echo_handler)
    srv.start()
    assert os.path.exists(sock_path)
    srv.close()
    assert not os.path.exists(sock_path)


# --- 9. идемпотентность жизненного цикла ------------------------------------------------------------------------

def test_start_and_close_are_idempotent(sock_path):
    srv = C.CtlServer(sock_path, echo_handler)
    srv.close()                              # close без start — no-op
    try:
        srv.start()
        srv.start()                          # повторный start — no-op
        assert os.path.exists(sock_path)
    finally:
        srv.close()
        srv.close()                          # повторный close — no-op
    assert not os.path.exists(sock_path)


def test_restart_after_close_works(run_server, sock_path):
    srv = run_server(echo_handler)
    cli = C.CtlClient(sock_path)
    try:
        assert cli.request("x", {"n": 1}) == {"echo": {"n": 1}}
        srv.close()
        srv.start()
        assert cli.request("x", {"n": 2}) == {"echo": {"n": 2}}
    finally:
        cli.close()


def test_client_unavailable_after_server_close(run_server, sock_path):
    srv = run_server(echo_handler)
    cli = C.CtlClient(sock_path)
    try:
        assert cli.request("x") == {"echo": {}}
        srv.close()
        with pytest.raises(C.CtlUnavailable):
            cli.request("x")
    finally:
        cli.close()


# --- 10. клиент: переподключение и недоступность ----------------------------------------------------------------

def test_client_reconnects_after_server_restart(run_server, sock_path):
    srv = run_server(echo_handler)
    cli = C.CtlClient(sock_path)
    try:
        assert cli.request("x", {"n": 1}) == {"echo": {"n": 1}}
        srv.close()
        run_server(echo_handler)             # новый сервер на том же пути
        assert cli.request("x", {"n": 2}) == {"echo": {"n": 2}}
    finally:
        cli.close()


def test_missing_socket_is_ctl_unavailable(sock_path):
    assert not os.path.exists(sock_path)
    with pytest.raises(C.CtlUnavailable) as ei:
        C.CtlClient(sock_path).request("x")
    assert isinstance(ei.value, OSError)


# --- 11. параллельные клиенты -----------------------------------------------------------------------------------

def test_sixteen_threads_with_own_clients(run_server, sock_path):
    run_server(echo_handler)
    n_threads, n_req = 16, 25
    barrier = threading.Barrier(n_threads)
    errors = []
    done = []

    def worker(tid):
        cli = C.CtlClient(sock_path)
        try:
            barrier.wait(5)
            for i in range(n_req):
                res = cli.request("ping", {"t": tid, "i": i})
                if res != {"echo": {"t": tid, "i": i}}:
                    errors.append((tid, i, res))
                else:
                    done.append((tid, i))
        except BaseException as e:           # noqa: BLE001 — собираем для проверки в основном потоке
            errors.append((tid, repr(e)))
        finally:
            cli.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert errors == []
    assert len(done) == n_threads * n_req


def test_one_shared_client_from_many_threads(run_server, sock_path):
    run_server(echo_handler)
    cli = C.CtlClient(sock_path)
    n_threads, n_req = 8, 25
    errors = []
    count = [0]
    lock = threading.Lock()

    def worker(tid):
        try:
            for i in range(n_req):
                res = cli.request("ping", {"t": tid, "i": i})
                if res != {"echo": {"t": tid, "i": i}}:
                    errors.append((tid, i, res))
                else:
                    with lock:
                        count[0] += 1
        except BaseException as e:           # noqa: BLE001
            errors.append((tid, repr(e)))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        cli.close()
    assert errors == []
    assert count[0] == n_threads * n_req


# --- 12. медленный обработчик не блокирует другого клиента -----------------------------------------------------

def test_slow_handler_does_not_block_other_client(run_server, sock_path):
    started = threading.Event()
    order = []

    def handler(cmd, args):
        if cmd == "slow":
            started.set()
            time.sleep(0.3)
        return {"echo": args}

    run_server(handler)

    def slow_call():
        cli = C.CtlClient(sock_path)
        try:
            cli.request("slow", {})
            order.append("slow")
        finally:
            cli.close()

    t = threading.Thread(target=slow_call)
    t.start()
    try:
        assert started.wait(2)
        cli = C.CtlClient(sock_path)
        try:
            t0 = time.monotonic()
            assert cli.request("fast", {"n": 1}) == {"echo": {"n": 1}}
            elapsed = time.monotonic() - t0
            order.append("fast")
        finally:
            cli.close()
    finally:
        t.join(5)
    assert order == ["fast", "slow"]
    assert elapsed < 0.3


def test_deeply_nested_json_gets_bad_request(server, sock_path):
    """Глубокая вложенность JSON: bad-request, а не обрыв соединения."""
    reply = _raw_call(sock_path, b"[" * 16000 + b"\n")
    assert reply["ok"] is False and reply["error"] == "bad-request"


def test_float_version_is_bad_request(server, sock_path):
    reply = _raw_call(sock_path, b'{"v":1.0,"id":1,"cmd":"x","args":{}}\n')
    assert reply["ok"] is False and reply["error"] == "bad-request"


def test_client_unserializable_args_is_ctl_error(server, sock_path):
    cli = C.CtlClient(sock_path)
    try:
        with pytest.raises(C.CtlError) as ei:
            cli.request("x", {"s": {1, 2}})
        assert ei.value.code == "bad-request"
    finally:
        cli.close()
