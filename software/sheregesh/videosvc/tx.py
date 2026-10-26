# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Служба передатчика видео `sheregesh-video-tx` (ADR-0016, контракт §2–§5): `python3 -m sheregesh.videosvc.tx`.

Владеет источником, кодером, RTP/SRTP. Агент блока — клиент сокета `<runtime>/ctl.sock`. Ключ живёт только в
памяти конвейера между `start` и `stop`.

Главное правило (контракт §5, угроза T2): пара (ключ, SSRC) принадлежит ровно одному запуску конвейера. Любой
перезапуск конвейера tx — это новое поколение; служба сама с тем же ключом не перезапускает: при ошибке, EOS
или таймауте первого кадра поток уходит в `error` с `error: "need-new-gen"` и ждёт нового `start`.
"""
from __future__ import annotations

import hashlib
import logging
import queue
import sys
from collections import deque
from dataclasses import dataclass

from . import media as M
from .ctl import CtlError

log = logging.getLogger("sheregesh.videosvc.tx")

START_TIMEOUT_S = 5.0
STATS_PERIOD_S = 1.0
RETIRED_KEEP = 16


@dataclass
class _Active:
    stream: str
    session: str
    gen: int
    ssrc: int
    fp: bytes                 # отпечаток параметров запуска (для идемпотентности); ключа в нём не восстановить
    token: int
    pipe: object | None
    passthrough: bool
    t_start: float
    alive: bool = True        # конвейер ещё принадлежит этому запуску (starting | streaming)


def _default_factory(cfg, on_event):
    from .pipelines import SrtpTx
    return SrtpTx(cfg, on_event)


class TxService(M.ServiceBase):
    role = "tx"

    def __init__(self, runtime_dir=None, *, factory=None, media_opener=None, **kw):
        super().__init__(runtime_dir, **kw)
        self._factory = factory or _default_factory
        self._media_opener = media_opener or M.open_media
        self._act: dict[str, _Active] = {}
        self._last: dict[str, tuple[str, int]] = {}     # (session, gen) последнего запуска конвейера
        self._retired: deque = deque(maxlen=RETIRED_KEEP)   # вытесненные сеансы: их start — stale
        self._token = 0
        self._last_stats = float("-inf")
        self.state.update("main", transport="srtp")

    # --- команды ---

    def dispatch(self, cmd: str, args: dict) -> dict:
        self._drain()
        if cmd == "start":
            return self._start(args)
        if cmd == "stop":
            return self._stop_cmd(args)
        if cmd == "select-source":
            return self._select_source(args)
        if cmd == "keyframe":
            return self._keyframe(args)
        if cmd == "status":
            M.check_obj(args, set(), what="args")
            return self.state.snapshot()
        if cmd == "catalog":
            raise CtlError("unsupported", "каталог источников остаётся в агенте")
        raise CtlError("unknown-cmd", "неизвестная команда")

    def _known(self, stream: str) -> None:
        if stream not in self.state.snapshot()["streams"]:
            raise CtlError("unknown-stream", "поток не запускался")

    def _start(self, a: dict) -> dict:
        M.check_obj(a, {"stream", "session", "gen", "ssrc", "peer", "srtp", "source", "profile"}, what="args")
        stream, session = M.req_stream(a), M.req_session(a)
        gen, ssrc = M.req_int(a, "gen", 1, 2**32 - 1, "args"), M.req_ssrc(a)
        peer = M.check_obj(a["peer"], {"ip", "port", "rtcp_port"}, {"rtcp_local_port"}, "peer")
        ip = M.req_ip(peer["ip"], "peer.ip")
        port = M.req_int(peer, "port", 1, 65535, "peer")
        rtcp_port = M.req_int(peer, "rtcp_port", 1, 65535, "peer")
        # Порт, где TX слушает RTCP приёмника. По контракту §8 это тот же 7445; на одном хосте (тесты) задаётся явно.
        rtcp_local = M.req_int(peer, "rtcp_local_port", 1, 65535, "peer") if "rtcp_local_port" in peer else rtcp_port
        cipher, key_b64 = M.req_srtp(a["srtp"])
        if not isinstance(a["source"], dict):
            raise M.bad("source: ожидается объект")
        profile = M.profile_args(a["profile"])
        fp = hashlib.sha256(repr((stream, ssrc, ip, port, rtcp_port, rtcp_local, cipher, key_b64,
                                  sorted(a["source"].items(), key=str), sorted(profile.items()))).encode()).digest()

        cur = self._act.get(stream)
        if session in self._retired:
            raise CtlError("stale", "сеанс устарел")
        last = self._last.get(stream)
        if last is not None and last[0] == session:
            if gen < last[1]:
                raise CtlError("stale", "поколение устарело")
            if gen == last[1]:
                if cur is not None and cur.alive and cur.fp == fp:
                    return self._started_result(cur)            # повтор того же start: идемпотентно
                if cur is not None and cur.alive:
                    raise M.bad("то же (session, gen) с другими параметрами")
                raise CtlError("stale", "поколение уже использовано: нужно новое (need-new-gen)")

        # Ключ проверяем ДО вытеснения: неверная команда не должна гасить работающий поток.
        from .pipelines import SrtpParams, TxConfig
        try:
            srtp_params = SrtpParams.from_b64(cipher, key_b64)
        except ValueError:
            raise M.bad("параметры SRTP отклонены") from None
        # Новый запуск вытесняет прежний: устройство одно, ключ прежнего уничтожается.
        if last is not None and last[0] != session:
            self._retired.append(last[0])
        if cur is not None:
            self._teardown(cur, "idle", f"replaced by gen {gen}")
        try:
            med = self._media_opener(a["source"], profile)
        except M.MediaError as e:
            self.state.update(stream, session=session, gen=gen, ssrc=ssrc, peer=ip, error=e.code)
            self.state.transition(stream, "error", e.code)
            raise CtlError("bad-request" if e.code == "bad-request" else
                           ("unsupported" if e.code == "unsupported" else "pipeline"), f"{e.code}: {e.detail}"[:200]
                           ) from None
        try:
            cfg = TxConfig(stream=stream, ssrc=ssrc, peer_ip=ip, peer_port=port, rtcp_local_port=rtcp_local,
                           rtcp_peer_port=rtcp_port, media=med.fragment,
                           srtp=srtp_params)
        except ValueError:
            raise M.bad("параметры конвейера отклонены") from None
        self._token += 1
        act = _Active(stream, session, gen, ssrc, fp, self._token, None, med.passthrough, self.mono())
        token = self._token

        def on_event(kind, detail, _t=token):
            self.events.put((_t, kind, detail))

        self._last[stream] = (session, gen)                  # с этого момента пара (ключ, SSRC) считается использованной
        self._act[stream] = act
        self.state.update(stream, session=session, gen=gen, ssrc=ssrc, peer=ip, error="", frames=0, fps=0.0,
                          bitrate_kbps=int(profile["bitrate"]), source=med.info,
                          rtp={"packets": 0, "lost": 0, "jitter_ms": 0.0, "reordered": 0},
                          rtcp={"pli_sent": 0, "pli_received": 0, "keyframes_requested": 0,
                                "keyframe_unsupported": med.passthrough},
                          last_frame_age_s=None)
        try:
            act.pipe = self._factory(cfg, on_event)
            act.pipe.start()
        except Exception as e:                               # noqa: BLE001
            log.error("tx: конвейер не запущен: %s", type(e).__name__)
            self._teardown(act, "error", "pipeline start failed")
            self.state.update(stream, error="pipeline")
            raise CtlError("pipeline", "конвейер не запущен") from None
        self.state.transition(stream, "starting", "start")
        return self._started_result(act)

    def _started_result(self, act: _Active) -> dict:
        st = self.state.snapshot()["streams"][act.stream]
        return {"stream": act.stream, "state": st["state"], "session": act.session, "gen": act.gen,
                "ssrc": act.ssrc, "passthrough": act.passthrough}

    def _stop_cmd(self, a: dict) -> dict:
        M.check_obj(a, {"stream"}, {"reason", "session", "gen"}, "args")
        stream, reason = M.req_stream(a), M.req_reason(a)
        self._known(stream)
        cur = self._act.get(stream)
        if cur is not None:
            self._teardown(cur, "idle", f"stop: {reason}" if reason else "stop")
        elif self.state.snapshot()["streams"][stream]["state"] != "idle":
            self.state.update(stream, error="")
            self.state.transition(stream, "idle", f"stop: {reason}" if reason else "stop")
        return {"stream": stream, "state": "idle"}

    def _select_source(self, a: dict) -> dict:
        M.check_obj(a, {"stream", "source"}, what="args")
        stream = M.req_stream(a)
        if not isinstance(a["source"], dict):
            raise M.bad("source: ожидается объект")
        self._known(stream)
        cur = self._act.get(stream)
        if cur is not None:
            self._teardown(cur, "idle", "select-source")
        return {"stream": stream, "state": "idle", "need_new_gen": True}

    def _keyframe(self, a: dict) -> dict:
        M.check_obj(a, {"stream"}, what="args")
        stream = M.req_stream(a)
        self._known(stream)
        cur = self._act.get(stream)
        if cur is None or not cur.alive or cur.pipe is None:
            raise CtlError("pipeline", "конвейер не построен")
        if self.state.snapshot()["streams"][stream]["state"] == "starting":
            raise CtlError("busy", "поток запускается")
        ok = bool(cur.pipe.request_keyframe())
        return {"stream": stream, "requested": ok, "keyframe_unsupported": cur.passthrough}

    # --- конвейер и состояния ---

    def _teardown(self, act: _Active, state: str, reason: str, error: str = "") -> None:
        """Остановить конвейер и забыть ключ (ссылка на конфигурацию уходит вместе с объектом конвейера)."""
        pipe, act.pipe, act.alive = act.pipe, None, False
        if pipe is not None:
            try:
                pipe.stop()
            except Exception as e:                           # noqa: BLE001
                log.warning("tx: остановка конвейера: %s", type(e).__name__)
        if self._act.get(act.stream) is act:
            del self._act[act.stream]
        self.state.update(act.stream, error=error)
        self.state.transition(act.stream, state, reason)

    def _fail(self, act: _Active, reason: str) -> None:
        """Сбой конвейера: тот же ключ не используется повторно — ждём нового start (need-new-gen)."""
        log.warning("tx[%s]: %s; нужно новое поколение", act.stream, reason)
        n = self.state.snapshot()["streams"][act.stream]["restarts"] + 1
        self._teardown(act, "error", reason, error="need-new-gen")
        self.state.update(act.stream, restarts=n)

    def _drain(self) -> None:
        while True:
            try:
                token, kind, detail = self.events.get_nowait()
            except queue.Empty:
                return
            act = next((x for x in self._act.values() if x.token == token), None)
            if act is None or not act.alive:
                continue                                     # событие остановленного запуска
            if kind == "first-frame":
                if self.state.snapshot()["streams"][act.stream]["state"] == "starting":
                    self.state.transition(act.stream, "streaming", "encoder output")
            elif kind == "error":
                self._fail(act, f"pipeline error: {str(detail.get('message', ''))[:120]}")
            elif kind == "eos":
                self._fail(act, "pipeline eos")

    def tick(self) -> None:
        self._drain()
        now = self.mono()
        for act in list(self._act.values()):
            st = self.state.snapshot()["streams"][act.stream]["state"]
            if st == "starting" and now - act.t_start > START_TIMEOUT_S:
                self._fail(act, "no encoder output in 5 s")
        if now - self._last_stats >= STATS_PERIOD_S:
            self._last_stats = now
            for act in list(self._act.values()):
                self._refresh(act)

    def _refresh(self, act: _Active) -> None:
        try:
            s = act.pipe.stats() if act.pipe is not None else {}
        except Exception:                                    # noqa: BLE001
            return
        upd: dict = {}
        if isinstance(s.get("packets"), int):
            upd["rtp"] = {"packets": s["packets"]}
        if isinstance(s.get("bitrate_kbps"), (int, float)):
            upd["bitrate_kbps"] = s["bitrate_kbps"]
        rtcp = {k: s[k] for k in ("pli_received", "keyframes_requested") if isinstance(s.get(k), int)}
        if rtcp:
            upd["rtcp"] = rtcp
        if upd:
            self.state.update(act.stream, **upd)

    def shutdown_streams(self) -> None:
        for act in list(self._act.values()):
            self._teardown(act, "idle", "shutdown")

    def status_line(self) -> str:
        parts = [f"{s}={x['state']}" for s, x in self.state.snapshot()["streams"].items()]
        return "tx " + " ".join(parts)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    M.setup_logging()
    svc = TxService(M.parse_runtime_arg(argv), version=_version())
    return svc.serve(install_signals=True)


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("sheregesh")
    except Exception:                                        # noqa: BLE001
        return "dev"


if __name__ == "__main__":
    sys.exit(main())
