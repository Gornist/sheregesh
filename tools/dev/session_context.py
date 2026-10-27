#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Claude SessionStart: short local context, no network, tests or hardware operations."""
import json
from pathlib import Path
import subprocess


def describe(root):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL,
                                       timeout=5).strip()
    try:
        revision, branch = git('rev-parse', '--short', 'HEAD'), git('branch', '--show-current')
        dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
        common = Path(git('rev-parse', '--path-format=absolute', '--git-common-dir'))
        path = common / 'agent-state/leases.json'
        leases = json.loads(path.read_text()).get('leases', {}) if path.exists() else {}
        occupied = ', '.join(sorted(leases))[:240] or 'none'
        text = (f'Sheregesh: branch={branch}, HEAD={revision}, dirty={dirty}, reserved resources={occupied}. '
                'Read AGENTS.md and docs/development/PROJECT-STATE.md; reported board state is historical until checked. '
                'Procedures: docs/development/AGENT-WORKFLOW.md. '
                'Video contract: docs/architecture/VIDEO-SERVICE-CONTRACT.md. '
                'Handoff: docs/development/SESSION-HANDOFF.md.')
    except (OSError, ValueError, subprocess.SubprocessError):
        text = 'Local context unavailable. Read AGENTS.md and docs/development/PROJECT-STATE.md.'
    return {'hookSpecificOutput': {'hookEventName': 'SessionStart', 'additionalContext': text}}


if __name__ == '__main__':
    print(json.dumps(describe(Path(__file__).resolve().parents[2])))
