# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent workflow tools: isolated resources and observable command failures."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    path = ROOT / 'tools/dev' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LeaseTests(unittest.TestCase):
    def test_racing_acquisitions_have_one_owner_and_cannot_release_foreign_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = ROOT / 'tools/dev/stand_lease.py'
            args = [sys.executable, str(script), '--store', tmp, 'acquire', '--resource', 'fake-pi1']
            processes = [subprocess.Popen(args + ['--run-id', str(i)], stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True) for i in range(2)]
            results = [(p.communicate(), p.returncode) for p in processes]
            self.assertEqual(sorted(rc for _, rc in results), [0, 1])
            owner = next(json.loads(out)['run_id'] for (out, _), rc in results if rc == 0)
            wrong = subprocess.run([sys.executable, str(script), '--store', tmp, 'release',
                                    '--run-id', 'foreign'], capture_output=True, text=True)
            self.assertEqual(wrong.returncode, 1)
            status = subprocess.run([sys.executable, str(script), '--store', tmp, 'status'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(status.stdout)['leases']['fake-pi1']['run_id'], owner)

    def test_expired_lease_still_blocks_and_acquisition_is_all_or_nothing(self):
        lease = load('stand_lease')
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp)
            lease.change(store, 'acquire', 'first', ['pi1'], ttl=1, now=0)
            with self.assertRaises(lease.LeaseError):
                lease.change(store, 'acquire', 'second', ['pi2', 'pi1'], ttl=60, now=100)
            state = lease.change(store, 'status', None, [], now=100)
            self.assertNotIn('pi2', state['leases'])
            self.assertTrue(state['leases']['pi1']['expired'])
            lease.change(store, 'release', 'first', [], now=100)
            self.assertEqual(lease.change(store, 'status', None, [], now=100)['leases'], {})


class AuditTests(unittest.TestCase):
    def test_audit_reports_untracked_and_unique_commits_without_changes(self):
        audit = load('worktree_audit')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
            git('init', '-q', '-b', 'v2')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            (root / 'base').write_text('base')
            git('add', 'base'); git('commit', '-qm', 'base')
            git('checkout', '-qb', 'task')
            (root / 'base').write_text('new')
            git('commit', '-qam', 'change')
            (root / 'untracked file').write_text('keep')
            before = git('status', '--porcelain')
            result = audit.inspect(root, 'v2')[0]
            self.assertTrue(result['dirty'])
            self.assertEqual(result['unique_commits'], 1)
            self.assertEqual(before, git('status', '--porcelain'))
            self.assertEqual((root / 'untracked file').read_text(), 'keep')


class VerifyTests(unittest.TestCase):
    def test_runner_reports_failure_timeout_and_missing_program(self):
        verify = load('verify')
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            bad = verify.run_command([sys.executable, '-c', 'raise SystemExit(3)'], ROOT, out / 'bad.log', 5)
            self.assertEqual((bad['result'], bad['exit_code']), ('FAIL', 3))
            slow = verify.run_command([sys.executable, '-c', 'import time; time.sleep(30)'], ROOT,
                                      out / 'slow.log', 0.1)
            self.assertEqual(slow['result'], 'ERROR')
            absent = verify.run_command([str(out / 'absent')], ROOT, out / 'missing.log', 5)
            self.assertEqual(absent['result'], 'ERROR')

    def test_requirements_profile_excludes_release_gate(self):
        verify = load('verify')
        commands = verify.profiles(sys.executable)['requirements']
        self.assertEqual([c[-1] for c in commands], ['validate', '--check'])
        self.assertFalse(any('gate' in c for c in commands))

    def test_snapshot_changes_on_untracked_source_edit(self):
        verify = load('verify')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'source.py'
            source.write_text('a')
            before = verify.snapshot(root)
            source.write_text('b')
            after = verify.snapshot(root)
            self.assertNotEqual(before['fingerprint'], after['fingerprint'])
            self.assertTrue(after['dirty'])


class ContextTests(unittest.TestCase):
    def test_context_is_short_local_and_claude_hook_output(self):
        context = load('session_context')
        result = context.describe(ROOT)
        self.assertLess(len(json.dumps(result)), 2400)
        text = result['hookSpecificOutput']['additionalContext']
        self.assertIn('PROJECT-STATE.md', text)
        self.assertIn('dirty', text)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'SessionStart')


class SkipEvidenceTests(unittest.TestCase):
    def test_skip_reason_is_structured_and_not_complete_pass(self):
        verify = load('verify')
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'junit.xml'
            report.write_text('<testsuites><testsuite tests="2"><testcase name="pass"/>'
                              '<testcase name="srtp"><skipped message="GStreamer unavailable"/>'
                              '</testcase></testsuite></testsuites>')
            result = verify.pytest_outcomes(report)
            self.assertEqual(result['skipped'], [{'test': 'srtp', 'reason': 'GStreamer unavailable'}])


if __name__ == '__main__':
    unittest.main()
