"""Host-only, non-destructive repair of retired Docker Chromium singletons.

No Docker socket is exposed to the application. Unknown owners fail closed.
The advisory lock coordinates all supported runtime/setup/launcher writers.
"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess

from secretary.connectors.storage import FileLock


def _docker(*args):
    return subprocess.run(['docker', *args], check=True, capture_output=True,
                          text=True, timeout=15).stdout


def _retired(host, root):
    # An absent ID on a remote daemon says nothing about the local profile.
    if Path('/.dockerenv').exists() or not re.fullmatch(r'[0-9a-f]{12,64}', host):
        return False
    endpoint = os.environ.get('DOCKER_HOST')
    if not endpoint or os.environ.get('DOCKER_CONTEXT'):
        contexts = json.loads(_docker('context', 'inspect'))
        endpoint = contexts[0]['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix://'):
        return False
    ids = _docker('ps', '-aq', '--no-trunc').split()
    containers = json.loads(_docker('inspect', *ids)) if ids else []
    target = root.resolve()
    for container in containers:
        # Treat paused/restarting containers as live too; only an explicitly
        # stopped/dead owner can be retired.
        state = container['State']
        live = state['Running'] or state.get('Paused', False) or state.get('Restarting', False)
        owner = container['Id'].startswith(host) or container['Config']['Hostname'] == host
        if owner and (live or state.get('Status') not in ('exited', 'dead', 'created')):
            return False
        if live:
            for mount in container['Mounts']:
                source = Path(mount['Source']).resolve()
                if source == target or source in target.parents or target in source.parents:
                    return False
    return True


def repair_docker_singletons(root):
    """Return whether repaired; never remove profile data or the owner lock.

    Only call on the Docker host. Missing/ambiguous evidence leaves everything
    intact. Hold the same exclusive inode lock used by runtime and setup.
    """
    root = Path(root)
    if not root.exists() or root.is_symlink():
        return False
    try:
        with FileLock(root / '.owner.lock', blocking=False):
            profile = root / 'session'
            if profile.is_symlink() or not profile.is_dir():
                return False
            lock = profile / 'SingletonLock'
            if not lock.is_symlink():
                return False
            host, sep, pid = os.readlink(lock).rpartition('-')
            if not sep or not pid.isdigit() or host == socket.gethostname():
                return False
            if not _retired(host, root):
                return False
            # Guard orphan Chromium, including a direct host launch outside
            # the application's locking protocol. Unreadable /proc fails shut.
            if not Path('/proc').is_dir():
                return False
            for process in Path('/proc').iterdir():
                if process.name.isdigit():
                    try:
                        command = (process / 'cmdline').read_bytes()
                    except FileNotFoundError:
                        continue
                    if str(profile.resolve()).encode() in command:
                        return False
            entries = [profile / name for name in
                       ('SingletonLock', 'SingletonSocket', 'SingletonCookie')]
            # Chromium's POSIX singleton artifacts are symlinks. Never follow
            # their targets (socket/cookie may point outside the profile).
            snapshots = []
            for entry in entries:
                if entry.is_symlink():
                    stat = entry.lstat()
                    if stat.st_uid != os.getuid():
                        return False
                    snapshots.append((entry, stat))
                elif entry.exists():
                    return False
            for entry, stat in snapshots:
                if entry.lstat() != stat:
                    return False
            for entry, _ in snapshots:
                entry.unlink()
            return True
    except (OSError, ValueError, KeyError, TypeError, IndexError, subprocess.SubprocessError):
        return False