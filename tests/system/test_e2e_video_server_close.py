# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""VideoServer.close(): остановленный видеосервер блока не принимает новых видеосеансов.

Дефект найден E2E-тестом в CI (ubuntu-24.04): на Linux close() слушающего сокета из другого потока не прерывает
accept(); сокет оставался в LISTEN, следующее подключение пульта принималось и обслуживалось «остановленным»
сервером (видео шло, drop() нового сервера на том же порту его не видел). На macOS accept прерывается сразу,
поэтому проверка содержательна на Linux (CI, платы).
"""
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

from procs import wait_for
from sheregesh import videolink
from sheregesh.link import pki

pytestmark = pytest.mark.skipif(not shutil.which("openssl"), reason="нет openssl")
FAKE = [sys.executable, str(Path(__file__).with_name("fake_rtp_sender.py")), "30"]


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("vclose")
    pki.ca_init(root / "ca")

    def device(name, role, did, peer):
        d = root / name
        pki.install(d, pki.ca_sign(root / "ca", pki.keygen(role, did, d), role, did), (root / "ca/ca.crt").read_text(),
                    role, did)
        return pki.configure(root / f"{name}.json", role, did, peer, ["127.0.0.1"], None, d)
    return {"remote": device("remote", "remote", "PI2", "console:PI1"),
            "console": device("console", "console", "PI1", "remote:PI2")}


def accept_threads():
    return [t for t in threading.enumerate() if t.name == "video-accept" and t.is_alive()]


def test_closed_server_accepts_no_new_session(lab):
    before = len(accept_threads())
    srv = videolink.VideoServer(lab["remote"], "h264", lambda a: None, lambda m: None, bind="127.0.0.1", port=0,
                                command=FAKE).start()
    assert len(accept_threads()) == before + 1
    srv.close()
    assert wait_for(lambda: len(accept_threads()) == before, 3), "поток accept не завершился после close()"
    c = videolink.VideoClient(lab["console"], "127.0.0.1", srv.port)
    r, w = os.pipe()
    why = c.run(w)                                          # подключение отклонено или сразу закрыто
    os.close(r)
    os.close(w)
    assert c.stats()["frames"] == 0 and c.info is None, why
    assert srv.stats()["sessions"] == 0 and not srv.active
