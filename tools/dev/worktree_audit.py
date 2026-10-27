#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only worktree inventory. No deletion or unlocking and no 'safe to delete' verdict."""
import argparse
import json
from pathlib import Path
import subprocess


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True)


def inspect(root, base='v2'):
    result = []
    fields = {}
    records = git(root, 'worktree', 'list', '--porcelain', '-z').split('\0')
    for token in records:
        if token:
            key, _, value = token.partition(' ')
            fields[key] = value
            continue
        if not fields:
            continue
        path = Path(fields['worktree'])
        item = {'path': str(path), 'head': fields.get('HEAD'), 'branch': fields.get('branch'),
                'locked': 'locked' in fields, 'lock_reason': fields.get('locked')}
        try:
            status = git(path, 'status', '--porcelain', '-z')
            item.update(dirty=bool(status), status=status.split('\0') if status else [],
                        unique_commits=int(git(path, 'rev-list', '--count', 'HEAD', '--not', base)))
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            item['error'] = str(error)
        result.append(item)
        fields = {}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--base', default='v2')
    args = parser.parse_args(argv)
    try:
        result = inspect(args.root, args.base)
    except (OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(any('error' in item for item in result))


if __name__ == '__main__':
    raise SystemExit(main())
