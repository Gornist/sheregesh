#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit local verification profiles; JSON evidence never changes requirement status."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def profiles(python):
    test = [python, '-m', 'pytest', '-q']
    return {
        'requirements': [[python, '-I', 'tools/reqs/reqgen.py', 'validate'],
                         [python, '-I', 'tools/reqs/reqgen.py', 'generate', '--check']],
        'deploy': [test + ['tests/unit/test_deploy.py', 'tests/system/test_launcher.py',
                           'tests/unit/test_check_pi_deploy.py', 'tests/tools/test_deploy_cli.py']],
        'video': [test + ['tests/unit/test_videosvc_pipelines.py', 'tests/unit/test_videosvc_media.py',
                          'tests/unit/test_videosvc_session.py', 'tests/unit/test_videosvc_state.py',
                          'tests/unit/test_videosvc_ctl.py', 'tests/unit/test_videosvc_tx.py',
                          'tests/unit/test_videosvc_rx.py', 'tests/system/test_videosvc_daemons.py',
                          'tests/system/test_videosvc_srtp_loopback.py']],
        'gui': [test + ['tests/gui']],
        'control': [test + ['tests/unit/test_crsf_frames.py', 'tests/unit/test_crsf_output.py',
                            'tests/unit/test_control_receiver.py', 'tests/unit/test_control_wire.py',
                            'tests/system/test_crsf_chain.py']],
        'full': [[python, '-I', 'tools/reqs/reqgen.py', 'validate'],
                 [python, '-I', 'tools/reqs/reqgen.py', 'generate', '--check'],
                 [python, '-m', 'unittest', 'discover', '-s', 'tests/tools', '-p', 'test_*.py'],
                 test, [python, '-I', 'tools/security/scan.py', 'secrets']],
    }


def snapshot(root):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)
    try:
        commit = git('rev-parse', 'HEAD').decode().strip()
    except subprocess.CalledProcessError:
        commit = None
    status = git('status', '--porcelain', '-z')
    digest = hashlib.sha256()
    # Hash tracked content plus untracked source roots, not unrelated research disk images.
    tracked = set(git('ls-files', '-z').split(b'\0')) - {b''}
    untracked = set(git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0')) - {b''}
    roots = {'software', 'tools', 'tests', 'docs', 'os', 'hardware', '.github'}
    top_suffixes = {'.py', '.md', '.toml', '.json', '.yaml', '.yml', '.sh'}
    def source(raw):
        path = Path(os.fsdecode(raw))
        return (path.parts[0] in roots or
                path.as_posix().startswith(('.claude/skills/', '.claude/agents/')) or
                (len(path.parts) == 1 and (path.suffix in top_suffixes or path.name in {'Makefile', 'justfile'})))
    selected = {raw for raw in untracked if source(raw)}
    paths = tracked | selected
    for raw in sorted(paths - {b''}):
        digest.update(raw + b'\0')
        path = Path(root) / os.fsdecode(raw)
        if path.is_symlink():
            digest.update(b'link\0' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(str(path.stat().st_mode & 0o777).encode() + b'\0')
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        else:
            digest.update(b'missing\0')
    return {'commit': commit, 'dirty': bool(status), 'fingerprint': digest.hexdigest(),
            'untracked_excluded': len(untracked - selected)}


def pytest_outcomes(path):
    root = ET.parse(path).getroot()
    skipped = [{"test": case.get("name"), "reason": skip.get("message") or skip.text or "unspecified"}
               for case in root.iter("testcase") for skip in case.findall("skipped")]
    return {"skipped": skipped, "testcases": sum(1 for _ in root.iter("testcase"))}


def run_command(command, root, log, timeout):
    started = time.monotonic()
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QUICK_BACKEND='software')
    junit = Path(log).with_suffix('.xml') if command[1:3] == ['-m', 'pytest'] else None
    if junit:
        junit.unlink(missing_ok=True)
        command = [*command, '-ra', f'--junitxml={junit}']
    result = {'command': command, 'log': str(log), 'exit_code': None}
    with Path(log).open('w') as output:
        try:
            process = subprocess.Popen(command, cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                rc = process.wait(timeout=timeout)
                result.update(exit_code=rc, result='PASS' if rc == 0 else 'FAIL')
            except subprocess.TimeoutExpired:
                # Kill the whole command group, including test children, before reporting completion.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                result.update(result='ERROR', reason='timeout')
        except OSError as error:
            output.write(str(error))
            result.update(result='ERROR', reason='command unavailable')
    if junit and result['result'] == 'PASS':
        try:
            result['pytest'] = pytest_outcomes(junit)
            if result['pytest']['skipped']:
                result['result'] = 'INCOMPLETE'
        except (OSError, ET.ParseError):
            result.update(result='ERROR', reason='pytest evidence missing or malformed')
    result['seconds'] = round(time.monotonic() - started, 3)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=list(profiles(sys.executable)))
    parser.add_argument('--python', default=sys.executable, help='Python with dependencies; default is this interpreter')
    parser.add_argument('--out', type=Path, help='Report directory (must be ignored by git)')
    parser.add_argument('--timeout', type=float, default=1800, help='Per-command timeout in seconds')
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    out = (args.out or ROOT / 'reports/verify' / run_id).resolve()
    # Reports within the repo must be ignored, otherwise writing them changes the source fingerprint.
    try:
        relative = out.relative_to(ROOT)
    except ValueError:
        relative = None
    if relative is not None:
        check = subprocess.run(['git', 'check-ignore', '-q', str(relative / 'summary.json')], cwd=ROOT)
        if check.returncode != 0:
            parser.error('--out inside repo must be gitignored')
    out.mkdir(parents=True, exist_ok=True)
    before = snapshot(ROOT)
    report = {'run_id': run_id, 'profile': args.profile, 'source_before': before,
              'environment': {'platform': platform.platform(), 'runner_python': sys.version,
                              'command_python': args.python},
              'level': 'L2/L3', 'note': 'Host checks only; pytest SKIP is not hardware evidence.', 'checks': []}
    for i, command in enumerate(profiles(args.python)[args.profile]):
        item = run_command(command, ROOT, out / f'{i:02d}.log', args.timeout)
        report['checks'].append(item)
        print(f"{item['result']}: {' '.join(command)}", flush=True)
    report['source_after'] = snapshot(ROOT)
    report['source_changed'] = report['source_after'] != before
    report['result'] = ('ERROR' if report['source_changed'] or any(c['result'] == 'ERROR' for c in report['checks'])
                        else 'FAIL' if any(c['result'] == 'FAIL' for c in report['checks'])
                        else 'INCOMPLETE' if any(c['result'] == 'INCOMPLETE' for c in report['checks']) else 'PASS')
    (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(out / 'summary.json')
    return 0 if report['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
