# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Служба приёмника видео `sheregesh-video-rx` (ADR-0016, контракт §2–§5, §8): `python3 -m sheregesh.videosvc.rx`.

Приём UDP, SRTP, джиттер-буфер, декодер, раздача кадров через unixfdsink. GUI пульта — клиент сокета
`<runtime>/ctl.sock`. В отличие от tx, сбой конвейера rx лечится внутри службы с тем же ключом и поколением
(пакеты приёмника ключевым потоком не повторяются): откат 1, 2, 4, 8, 16, 30 с; если и перезапуск № 6 упал — `stuck`.
Таблица nftables проверяется при `start` и раз в секунду; нет таблицы — поток в `error` (`firewall`), конвейер остановлен.
"""
from __future__ import annotations

import hashlib
import logging
import os
import queue
import shlex
import subprocess
import sys
from dataclasses import dataclass

from . import media as M
from .ctl import CtlError

log = logging.getLogger("sheregesh.videosvc.rx")

ENV_NFT_CHECK = "SHEREGESH_VIDEO_NFT_CHECK"
NFT_UNIT = "sheregesh-video-nft.service"
BACKOFF_S = (1, 2, 4, 8, 16, 30)
START_TIMEOUT_S = 5.0
DEGRADED_AFTER_S = 1.0
PLI_PERIOD_S = 1.0
PLI_MAX = 5
STATS_PERIOD_S = 1.0
NFT_PERIOD_S = 1.0


def default_nft_check():
    """Проверка таблицы nftables: None — отключена (SHEREGESH_VIDEO_NFT_CHECK=0), иначе функция () -> bool."""
    spec = os.environ.get(ENV_NFT_CHECK)
    if spec is not None and spec.strip() == "0":
        return None
    cmd = shlex.split(spec) if spec else ["systemctl", "is-active", NFT_UNIT]

    def check() -> bool:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and (spec is not None or r.stdout.strip() == "active")
    return check


def _default_factory(cfg, on_event):
    from .pipelines import SrtpRx
    return SrtpRx(cfg, on_event)


@dataclass
class _Active:
    stream: str
    session: str
    gen: int
    ssrc: int
    fp: bytes
    cfg: object | None          # RxConfig с ключом: только в памяти, до stop
    pipe: object | None = None
    token: int = 0
    t_state: float = 0.0        # когда вошли в текущее состояние
    frames_base: int = 0        # кадры прежних запусков конвейера
    frames_mark: int = 0        # кадры при входе в degraded
    pli_n: int = 0
    t_pli: float = 0.0
    fails: int = 0              # подряд идущие сбои без достигнутого streaming
    next_restart: float | None = None
    blocked: bool = False       # остановлен из-за nftables
    stuck: bool = False
    alive: bool = True


class RxService(M.ServiceBase):
    role = "rx"

    def __init__(self, runtime_dir=None, *, factory=None, nft_check="env", decoder=None, sink=None, **kw):
        super().__init__(runtime_dir, **kw)
        self._factory = factory or _default_factory
        self._nft = default_nft_check() if nft_check == "env" else nft_check
        self._decoder, self._sink = decoder, sink
        self._act: dict[str, _Active] = {}
        self._last: dict[str, tuple[str, int]] = {}
        self._token = 0
        self._offset: float | None = None
        self._last_stats = float("-inf")
        self._last_nft = float("-inf")
        self.state.update("main", transport="srtp")

    # --- команды ---

    def dispatch(self, cmd: str, args: dict) -> dict:
        self._drain()
        if cmd == "start":
            return self._start(args)
        if cmd == "stop":
            return self._stop_cmd(args)
        if cmd == "status":
            M.check_obj(args, set(), what="args")
            return self.state.snapshot()
        if cmd == "set-clock-offset":
            M.check_obj(args, {"offset_s"}, what="args")
            self._offset = M.finite_float(args["offset_s"], "offset_s")
            for act in self._act.values():
                self._apply_offset(act)
            return {"offset_s": self._offset}
        raise CtlError("unknown-cmd", "неизвестная команда")

    def _start(self, a: dict) -> dict:
        M.check_obj(a, {"stream", "session", "gen", "ssrc", "peer", "local", "srtp", "codec"}, what="args")
        stream, session = M.req_stream(a), M.req_session(a)
        gen, ssrc = M.req_int(a, "gen", 1, 2**32 - 1, "args"), M.req_ssrc(a)
        peer = M.check_obj(a["peer"], {"ip"}, what="peer")
        ip = M.req_ip(peer["ip"], "peer.ip")
        local = M.check_obj(a["local"], {"port", "rtcp_port"}, {"rtcp_peer_port"}, "local")
        port = M.req_int(local, "port", 1, 65535, "local")
        rtcp_port = M.req_int(local, "rtcp_port", 1, 65535, "local")
        # Порт, куда RX шлёт RTCP (PLI, RR) на передатчике: по контракту §8 тот же 7445; на одном хосте задаётся явно.
        rtcp_peer = M.req_int(local, "rtcp_peer_port", 1, 65535, "local") if "rtcp_peer_port" in local else rtcp_port
        if a["codec"] != "h264":
            raise CtlError("unsupported", "codec: поддержан h264")
        cipher, key_b64 = M.req_srtp(a["srtp"])
        fp = hashlib.sha256(repr((stream, ssrc, ip, port, rtcp_port, rtcp_peer, cipher, key_b64)).encode()).digest()

        cur = self._act.get(stream)
        last = self._last.get(stream)
        if last is not None and last[0] == session:
            if gen < last[1]:
                raise CtlError("stale", "поколение устарело")
            if gen == last[1]:
                if cur is not None and cur.alive and cur.fp == fp:
                    return self._result(cur)
                if cur is not None and cur.alive:
                    raise M.bad("то же (session, gen) с другими параметрами")
                raise CtlError("stale", "поколение уже остановлено")
        if self._nft is not None and not self._nft():
            if cur is not None:
                self._teardown(cur, "idle", "replaced")
            self.state.update(stream, session=session, gen=gen, ssrc=ssrc, peer=ip, error="firewall")
            self.state.transition(stream, "error", "nftables table is not active")
            raise CtlError("pipeline", "firewall")
        from .pipelines import RxConfig, SrtpParams
        try:
            dec = self._decoder or M.decoder_name()
            sink = self._sink or M.rx_sink_fragment(self.runtime_dir, stream)
        except M.MediaError as e:
            raise CtlError("unsupported", e.detail[:200]) from None
        try:
            cfg = RxConfig(stream=stream, ssrc=ssrc, peer_ip=ip, local_port=port, rtcp_local_port=rtcp_port,
                           rtcp_peer_port=rtcp_peer, decoder=dec, sink=sink,
                           srtp=SrtpParams.from_b64(cipher, key_b64))
        except ValueError:
            raise M.bad("параметры SRTP или конвейера отклонены") from None
        if cur is not None:
            self._teardown(cur, "idle", f"replaced by gen {gen}")
        self._token += 1
        act = _Active(stream, session, gen, ssrc, fp, cfg, t_state=self.mono())
        self._remove_frames_socket(stream)
        self.state.update(stream, session=session, gen=gen, ssrc=ssrc, peer=ip, error="", frames=0, fps=0.0,
                          restarts=0, last_frame_age_s=None,
                          rtp={"packets": 0, "lost": 0, "jitter_ms": 0.0, "reordered": 0},
                          srtp={"auth_fail": 0, "replay_fail": 0, "unknown_ssrc": 0},
                          rtcp={"pli_sent": 0, "pli_received": 0, "keyframes_requested": 0,
                                "keyframe_unsupported": False},
                          latency_ms={"p50": None, "p95": None, "max": None, "samples": 0})
        try:
            self._launch(act)
        except Exception as e:                               # noqa: BLE001
            log.error("rx: конвейер не запущен: %s", type(e).__name__)
            self._teardown(act, "error", "pipeline start failed", error="pipeline")
            raise CtlError("pipeline", "конвейер не запущен") from None
        self._act[stream] = act
        self._last[stream] = (session, gen)
        self.state.transition(stream, "starting", "start")
        return self._result(act)

    def _result(self, act: _Active) -> dict:
        st = self.state.snapshot()["streams"][act.stream]
        return {"stream": act.stream, "state": st["state"], "session": act.session, "gen": act.gen,
                "ssrc": act.ssrc}

    def _stop_cmd(self, a: dict) -> dict:
        M.check_obj(a, {"stream"}, {"reason", "session", "gen"}, "args")
        stream, reason = M.req_stream(a), M.req_reason(a)
        if stream not in self.state.snapshot()["streams"]:
            raise CtlError("unknown-stream", "поток не запускался")
        cur = self._act.get(stream)
        if cur is not None:
            self._teardown(cur, "idle", f"stop: {reason}" if reason else "stop")
        elif self.state.snapshot()["streams"][stream]["state"] != "idle":
            self.state.update(stream, error="")
            self.state.transition(stream, "idle", f"stop: {reason}" if reason else "stop")
        return {"stream": stream, "state": "idle"}

    # --- конвейер ---

    def _remove_frames_socket(self, stream: str) -> None:
        try:
            os.unlink(os.path.join(self.runtime_dir, f"frames-{stream}.sock"))
        except OSError:
            pass

    def _launch(self, act: _Active) -> None:
        """Построить и запустить конвейер по сохранённой конфигурации (ключ и поколение те же)."""
        self._token += 1
        act.token = self._token

        def on_event(kind, detail, _t=act.token):
            self.events.put((_t, kind, detail))

        pipe = self._factory(act.cfg, on_event)
        act.pipe = pipe
        self._apply_offset(act)
        pipe.start()
        act.t_state = self.mono()
        act.next_restart = None

    def _apply_offset(self, act: _Active) -> None:
        if act.pipe is not None and self._offset is not None and hasattr(act.pipe, "set_clock_offset"):
            act.pipe.set_clock_offset(self._offset)

    def _stop_pipe(self, act: _Active) -> None:
        pipe, act.pipe = act.pipe, None
        if pipe is not None:
            act.frames_base += int(getattr(pipe, "frames", 0) or 0)
            try:
                pipe.stop()
            except Exception as e:                           # noqa: BLE001
                log.warning("rx: остановка конвейера: %s", type(e).__name__)

    def _teardown(self, act: _Active, state: str, reason: str, error: str = "") -> None:
        """Остановить и забыть конфигурацию с ключом."""
        self._stop_pipe(act)
        act.cfg, act.alive = None, False
        if self._act.get(act.stream) is act:
            del self._act[act.stream]
        self._remove_frames_socket(act.stream)
        self.state.update(act.stream, error=error)
        self.state.transition(act.stream, state, reason)

    def _set(self, act: _Active, state: str, reason: str) -> None:
        act.t_state = self.mono()
        self.state.transition(act.stream, state, reason)

    def _fail(self, act: _Active, reason: str) -> None:
        """Сбой конвейера: перезапуск внутри службы с откатом; после шестого неудачного перезапуска — stuck."""
        log.warning("rx[%s]: %s", act.stream, reason)
        self._stop_pipe(act)
        act.fails += 1
        if act.fails > len(BACKOFF_S):
            act.stuck, act.next_restart = True, None
            self.state.update(act.stream, error="stuck")
            self._set(act, "stuck", f"{reason}; restart {len(BACKOFF_S)} failed")
            return
        delay = BACKOFF_S[act.fails - 1]
        act.next_restart = self.mono() + delay
        self.state.update(act.stream, error="pipeline")
        self._set(act, "error", f"{reason}; restart in {delay} s")

    def _restart(self, act: _Active) -> None:
        n = self.state.snapshot()["streams"][act.stream]["restarts"] + 1
        self.state.update(act.stream, restarts=n, error="")
        try:
            self._launch(act)
        except Exception as e:                               # noqa: BLE001
            self._fail(act, f"restart failed: {type(e).__name__}")
            return
        self._set(act, "starting", "restart")

    def _drain(self) -> None:
        while True:
            try:
                token, kind, detail = self.events.get_nowait()
            except queue.Empty:
                return
            act = next((x for x in self._act.values() if x.token == token), None)
            if act is None or not act.alive or act.pipe is None:
                continue
            if kind == "error":
                self._fail(act, f"pipeline error: {str(detail.get('message', ''))[:120]}")
            elif kind == "eos":
                self._fail(act, "pipeline eos")

    # --- цикл ---

    def tick(self) -> None:
        self._drain()
        now = self.mono()
        if self._nft is not None and now - self._last_nft >= NFT_PERIOD_S:
            self._last_nft = now
            ok = self._nft()
            for act in list(self._act.values()):
                self._firewall(act, ok)
        for act in list(self._act.values()):
            self._step(act, now)
        if now - self._last_stats >= STATS_PERIOD_S:
            self._last_stats = now
            for act in list(self._act.values()):
                self._refresh(act, now)

    def _firewall(self, act: _Active, ok: bool) -> None:
        if not ok and not act.blocked:
            act.blocked = True
            self._stop_pipe(act)
            act.next_restart = None
            self.state.update(act.stream, error="firewall")
            self._set(act, "error", "nftables table is not active")
        elif ok and act.blocked:
            act.blocked = False
            self._restart(act)

    def _step(self, act: _Active, now: float) -> None:
        st = self.state.snapshot()["streams"][act.stream]["state"]
        pipe = act.pipe
        if act.blocked or act.stuck:
            return
        if st == "error":
            if act.next_restart is not None and now >= act.next_restart:
                self._restart(act)
            return
        if pipe is None:
            return
        frames = int(getattr(pipe, "frames", 0) or 0)
        age = now - pipe.last_frame if getattr(pipe, "last_frame", 0) else None
        if st == "starting":
            if frames > 0:
                act.fails = 0
                self._set(act, "streaming", "first frame")
            elif now - act.t_state > START_TIMEOUT_S:
                self._fail(act, "no frames in 5 s")
        elif st == "streaming":
            if age is None or age > DEGRADED_AFTER_S:
                act.frames_mark = frames
                self._set(act, "degraded", f"no frames > {DEGRADED_AFTER_S:g} s")
                self._pli(act, now, first=True)
                self._set(act, "recovering", "PLI sent")
        elif st == "recovering":
            if frames > act.frames_mark and age is not None and age <= DEGRADED_AFTER_S:
                self._set(act, "streaming", "keyframe received")
            elif now - act.t_pli >= PLI_PERIOD_S:
                if act.pli_n < PLI_MAX:
                    self._pli(act, now)
                else:
                    self._set(act, "degraded", f"no keyframe after {PLI_MAX} PLI")
        elif st == "degraded":
            if frames > act.frames_mark:
                self._set(act, "streaming", "frames resumed")
            else:
                self._pli(act, now, first=True)
                self._set(act, "recovering", "PLI sent")

    def _pli(self, act: _Active, now: float, first: bool = False) -> None:
        act.pli_n = 1 if first else act.pli_n + 1
        act.t_pli = now
        req = getattr(act.pipe, "request_keyframe", None)
        if req is not None:
            try:
                req()
            except Exception:                                # noqa: BLE001
                pass

    def _refresh(self, act: _Active, now: float) -> None:
        pipe = act.pipe
        if pipe is None:
            return
        try:
            s = pipe.stats()
        except Exception:                                    # noqa: BLE001
            return
        upd: dict = {"frames": act.frames_base + int(s.get("frames") or 0)}
        for src, dst in (("fps", "fps"), ("bitrate_kbps", "bitrate_kbps"), ("last_frame_age_s", "last_frame_age_s")):
            if isinstance(s.get(src), (int, float)):
                upd[dst] = s[src]
        rtp = {k: s[v] for k, v in (("packets", "packets"), ("lost", "lost")) if isinstance(s.get(v), int)}
        if isinstance(s.get("jitter_ms"), (int, float)):
            rtp["jitter_ms"] = s["jitter_ms"]
        if rtp:
            upd["rtp"] = rtp
        if isinstance(s.get("unknown_ssrc"), int):
            upd["srtp"] = {"unknown_ssrc": s["unknown_ssrc"]}
        if isinstance(s.get("pli_sent"), int):
            upd["rtcp"] = {"pli_sent": s["pli_sent"]}
        lat = s.get("latency_ms")
        if isinstance(lat, dict):
            upd["latency_ms"] = {k: lat.get(k) for k in ("p50", "p95", "max", "samples")}
        try:
            self.state.update(act.stream, **upd)
        except ValueError as e:
            log.warning("rx: статистика отклонена схемой: %s", e)

    def shutdown_streams(self) -> None:
        for act in list(self._act.values()):
            self._teardown(act, "idle", "shutdown")

    def status_line(self) -> str:
        parts = [f"{s}={x['state']}" for s, x in self.state.snapshot()["streams"].items()]
        return "rx " + " ".join(parts)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    M.setup_logging()
    svc = RxService(M.parse_runtime_arg(argv), version=_version())
    return svc.serve(install_signals=True)


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("sheregesh")
    except Exception:                                        # noqa: BLE001
        return "dev"


if __name__ == "__main__":
    sys.exit(main())
