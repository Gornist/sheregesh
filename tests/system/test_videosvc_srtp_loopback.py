# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Системные тесты SRTP-конвейеров (ADR-0016, уровень 0): TX -> RX по 127.0.0.1 через selftest-CLI.

Нужен интерпретатор с gi и GStreamer (x264enc, avdec_h264, srtpenc, srtpdec): SHEREGESH_GI_PYTHON или
/opt/homebrew/bin/python3. Иначе тесты пропускаются. Каждый прогон — отдельный процесс с таймаутом.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _gi_python():
    cand = os.environ.get("SHEREGESH_GI_PYTHON") or "/opt/homebrew/bin/python3"
    if not shutil.which(cand) and not Path(cand).exists():
        return None
    code = ("import gi;gi.require_version('Gst','1.0');from gi.repository import Gst;Gst.init(None);"
            "import sys;sys.exit(0 if all(Gst.ElementFactory.find(e) for e in "
            "('x264enc','avdec_h264','srtpenc','srtpdec','rtpbin')) else 1)")
    try:
        r = subprocess.run([cand, "-c", code], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cand if r.returncode == 0 else None


GI_PY = _gi_python()
pytestmark = pytest.mark.skipif(GI_PY is None, reason="нет интерпретатора с gi и GStreamer (SHEREGESH_GI_PYTHON)")


def _run(scenario, *extra):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "software"))
    r = subprocess.run([GI_PY, "-W", "ignore", "-m", "sheregesh.videosvc.pipelines", "selftest", scenario, *extra],
                       capture_output=True, text=True, timeout=60, env=env)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"нет JSON: {r.stdout!r} {r.stderr[-500:]!r}"
    out = json.loads(lines[-1])
    assert "error" not in out, out
    return out


@pytest.mark.parametrize("cipher", ["aes-128-gcm", "aes-128-icm"])
def test_кадры_проходят_по_loopback(cipher):
    r = _run("loopback", f"--cipher={cipher}")
    assert r["frames"] >= 30
    assert r["first_frame_s"] is not None and r["first_frame_s"] <= 5
    assert not r["errors"]
    assert ["rx", "first-frame"] in r["events"]


def test_чужой_ключ_на_rx_ноль_кадров():
    r = _run("wrongkey")
    assert r["frames"] == 0
    assert not r["errors"]


def test_чужой_ssrc_ноль_кадров_и_счётчик():
    r = _run("wrongssrc")
    assert r["frames"] == 0
    assert r["rx"]["unknown_ssrc"] > 0


def test_stop_идемпотентен_и_без_висящих_потоков():
    r = _run("stop")
    assert r["threads_after"] == r["threads_before"]
    assert r["restart_frames"] > 0
    assert r["threads_final"] == r["threads_before"]


def test_потеря_пакетов_ведёт_к_pli_и_новому_ключевому_кадру():
    r = _run("pli")
    assert r["tx"]["pli_received"] >= 2          # первый — при подключении, остальные — после потери
    assert r["rx"]["pli_sent"] >= 2
    assert r["idr_total"] >= 3                    # key-int-max огромный: IDR кодер выдаёт только по запросу
    assert r["rx"]["last_frame_age_s"] is not None and r["rx"]["last_frame_age_s"] < 1.0


@pytest.mark.parametrize("loss,reorder,dup", [(0.01, 0.05, 0.0), (0.03, 0.10, 0.02)])
def test_случайные_потери_перестановка_и_повторы_не_рвут_поток(loss, reorder, dup):
    """Искажения канала (прокси selftest impair): поток не прерывается, на потерю приходит PLI, повторы отбрасывает
    защита от повтора SRTP без ошибок конвейера. Кадров декодер выдаёт столько же (маскирует порчу до IDR по PLI)."""
    r = _run("impair", f"--loss={loss}", f"--reorder={reorder}", f"--dup={dup}", "--seconds=8")
    assert r["proxy"]["dropped"] > 0 and r["proxy"]["reordered"] > 0
    assert r["fps_after_first"] >= 27 and r["max_frame_gap_s"] < 0.5, r
    assert r["rx"]["lost"] > 0 and r["rx"]["pli_sent"] >= 2 and r["tx"]["pli_received"] >= 2
    assert r["rx"]["last_frame_age_s"] is not None and r["rx"]["last_frame_age_s"] < 0.5
    assert not r["errors"]
