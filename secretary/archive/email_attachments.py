"""Durable content-addressed email attachment files (filenames stay in the DB)."""
import hashlib
import os
from pathlib import Path
import tempfile

from secretary.core import config


def verify(path, data):
    """Refuse missing/corrupt files before committing a database reference."""
    actual = Path(path).read_bytes()
    if actual != data or hashlib.sha256(actual).hexdigest() != hashlib.sha256(data).hexdigest():
        raise ValueError('Attachment file verification failed')


def persist(data):
    data = bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    directory = config.EMAIL_ATTACHMENTS_DIR.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / digest
    if not path.exists():
        # Hard-link publication is atomic and never overwrites a competing writer.
        fd, temporary = tempfile.mkstemp(prefix='.attachment-', dir=directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            verify(temporary, data)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    verify(path, data)
    # Also sync deduplicated entries: another writer may just have published it.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return str(path)