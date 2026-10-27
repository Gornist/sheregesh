#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Sheregesh contributors
# SPDX-License-Identifier: GPL-3.0-or-later
"""Advisory stand ownership shared by all worktrees on one POSIX host; never steals expired leases."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import time


class LeaseError(Exception):
    pass


def default_store():
    common = subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], text=True).strip()
    return Path(common) / 'agent-state'


def change(store, action, run_id, resources, ttl=3600, now=None):
    if action not in ('acquire', 'release', 'status'):
        raise LeaseError('unknown action')
    if action != 'status' and not run_id:
        raise LeaseError('run-id is required')
    if action == 'acquire' and (not resources or ttl <= 0):
        raise LeaseError('resources and positive ttl are required')
    now = time.time() if now is None else now
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    with (store / 'lease.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = store / 'leases.json'
        state = json.loads(path.read_text()) if path.exists() else {'leases': {}}
        leases = state['leases']
        if action == 'acquire':
            # Repeating a run-id must not silently extend or steal a live/expired reservation.
            busy = [r for r in resources if r in leases]
            if busy:
                raise LeaseError('resources occupied; inspect status and coordinate release: ' + ', '.join(busy))
            for resource in set(resources):
                leases[resource] = {'run_id': run_id, 'host': socket.gethostname(), 'pid': os.getpid(),
                                    'acquired_at': now, 'expires_at': now + ttl}
        elif action == 'release':
            owned = [r for r, v in leases.items() if v['run_id'] == run_id]
            if not owned:
                raise LeaseError('run-id owns no resources')
            selected = resources or owned
            if any(r not in leases or leases[r]['run_id'] != run_id for r in selected):
                raise LeaseError('cannot release another run or an unknown resource')
            for resource in selected:
                del leases[resource]
        if action != 'status':
            temporary = store / 'leases.tmp'
            with temporary.open('w') as stream:
                json.dump(state, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        return {'leases': {r: dict(v, expired=now >= v['expires_at']) for r, v in leases.items()},
                **({'run_id': run_id} if run_id else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path)
    parser.add_argument('action', choices=['acquire', 'release', 'status'])
    parser.add_argument('--run-id')
    parser.add_argument('--resource', action='append', default=[])
    parser.add_argument('--ttl', type=float, default=3600)
    args = parser.parse_args(argv)
    try:
        result = change(args.store or default_store(), args.action, args.run_id, args.resource, args.ttl)
    except (LeaseError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
