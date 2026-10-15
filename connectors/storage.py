"""Local advisory locks and private, atomic file replacement (no dependencies)."""
import os
import tempfile
from pathlib import Path


class FileLock:
    def __init__(self, path, blocking=True):
        self.path = Path(path)
        self.blocking = blocking
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.file = os.fdopen(fd, 'r+b')
        try:
            os.chmod(self.path, 0o600)
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(fd, msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except BaseException:
            self.file.close()
            self.file = None
            raise
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
            self.file = None


def atomic_write(path, text):
    """Caller holds the transaction lock when updating existing content."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            os.chmod(name, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def bot_lock_path(token):
    import config
    import hashlib
    identity = token.split(':')[0]
    return config.DATA_DIR / 'locks' / ('bot-' + hashlib.sha256(identity.encode()).hexdigest() + '.lock')