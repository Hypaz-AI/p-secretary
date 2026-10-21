"""Restricted file storage API. Never follow symlinks or expose physical paths."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import threading

from flask import jsonify, request, send_file
from secretary.core import config, db

MAX_UPLOAD = 20 * 1024 * 1024
CONTENT_DIRS = {'email_attachments', 'wa_media', 'tg_files', 'zips', 'uploads', 'workspace'}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
LOCK = threading.Lock()


class FileError(Exception):
    def __init__(self, message='File non disponibile', status=400):
        self.message, self.status = message, status


def safe_name(name):
    return (isinstance(name, str) and len(name.encode('utf-8')) <= 240
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. -]*', name) is not None
            and name not in {'.', '..'}
            and not re.search(r'credential|secret|session|token|config|bridge|node_modules|state|spool|pending|password|\.pem$|\.key$|\.env', name, re.I))


def parts(path):
    if not isinstance(path, str) or len(path) > 2048:
        raise FileError()
    result = path.split('/') if path else []
    if any(not safe_name(p) for p in result):
        raise FileError()
    return result


def root_path(root):
    if root == 'data':
        return Path(config.DATA_DIR).absolute()
    if root == 'invoices':
        return Path(config.INVOICE_DIR).absolute()
    raise FileError()


@contextmanager
def directory(root, path=''):
    segments = parts(path)
    if root == 'data' and segments and segments[0] not in CONTENT_DIRS:
        raise FileError()
    base = root_path(root)
    fd = os.open('/', DIR_FLAGS)
    try:
        for segment in (*base.parts[1:], *segments):
            child = os.open(segment, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def protected(info):
    legacy = Path.home() / '.config' / 'pi-secretary' / 'dashboard.json'
    for path in (legacy, Path(config.get('DASHBOARD_CREDENTIAL_FILE', str(legacy)))):
        try:
            other = path.stat()
            if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                return True
        except FileNotFoundError:
            pass
    return False


def regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or protected(info):
        raise FileError()
    return info


def listing(root, path):
    entries = []
    with directory(root, path) as fd:
        for name in sorted(os.listdir(fd)):
            if not safe_name(name) or (root == 'data' and not path and name not in CONTENT_DIRS):
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            is_dir = stat.S_ISDIR(info.st_mode)
            if not is_dir and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or protected(info)):
                continue
            if root == 'data' and not path and not is_dir:
                continue
            entries.append({'name': name, 'directory': is_dir, 'size': None if is_dir else info.st_size})
            if len(entries) >= 2000:
                break
    return entries


def upload(name, stream, limit=MAX_UPLOAD):
    if not safe_name(name):
        raise FileError('Nome file non valido')
    with directory('data') as base:
        try:
            os.mkdir('uploads', 0o700, dir_fd=base)
        except FileExistsError:
            pass
    with directory('data', 'uploads') as fd:
        try:
            out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        except FileExistsError:
            raise FileError('File già esistente', 409)
        try:
            with os.fdopen(out, 'wb') as target:
                total = 0
                while True:
                    chunk = stream.read(min(65536, limit - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise FileError('File troppo grande', 413)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            os.unlink(name, dir_fd=fd)
            raise


def delete(root, path):
    segments = parts(path)
    if not segments:
        raise FileError()
    parent = '/'.join(segments[:-1])
    # Validate the entire path, including direct children of DATA_DIR.
    if root == 'data' and segments[0] not in CONTENT_DIRS:
        raise FileError()
    with directory(root, parent) as fd:
        target = os.open(segments[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = regular(target)
            physical = root_path(root).joinpath(*segments)
            try:
                # La connessione e' in autocommit: LOCK TABLE esiste solo dentro una transazione, che resta
                # aperta fino all'unlink (blocca le scritture concorrenti di riferimenti).
                with db._conn() as conn, conn.transaction():
                    conn.execute('LOCK TABLE attachments, invoices, monthly_zips IN SHARE MODE')
                    rows = conn.execute('SELECT path FROM attachments WHERE path IS NOT NULL UNION ALL SELECT path FROM invoices UNION ALL SELECT path FROM monthly_zips').fetchall()
                    for row in rows:
                        value = Path(row[0])
                        candidates = [value] if value.is_absolute() else [Path(config.BASE_DIR) / value, root_path(root) / value]
                        for candidate in candidates:
                            if os.path.realpath(candidate) == str(physical):
                                raise FileError('File referenziato: cancellazione vietata', 409)
                            try:
                                ref = candidate.stat()
                                if (ref.st_dev, ref.st_ino) == (info.st_dev, info.st_ino):
                                    raise FileError('File referenziato: cancellazione vietata', 409)
                            except OSError:
                                pass
                    current = os.stat(segments[-1], dir_fd=fd, follow_symlinks=False)
                    if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 1):
                        raise FileError()
                    os.unlink(segments[-1], dir_fd=fd)
            except FileError:
                raise
            except Exception:
                raise FileError('Verifica database non disponibile', 503) from None
        finally:
            os.close(target)


def register(app):
    @app.errorhandler(FileError)
    def failure(error):
        return jsonify(error=error.message), error.status

    @app.route('/api/files', methods=['GET', 'DELETE'])
    def files():
        try:
            with LOCK:
                root, path = request.args.get('root', 'data'), request.args.get('path', '')
                if request.method == 'DELETE':
                    delete(root, path)
                    return jsonify(ok=True)
                return jsonify(entries=listing(root, path))
        except OSError:
            raise FileError() from None

    @app.get('/api/files/download')
    def download():
        try:
            root, path = request.args.get('root', 'data'), request.args.get('path', '')
            segments = parts(path)
            if not segments or (root == 'data' and segments[0] not in CONTENT_DIRS):
                raise FileError()
            with directory(root, '/'.join(segments[:-1])) as fd:
                handle = os.fdopen(os.open(segments[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), 'rb')
            try:
                info = regular(handle.fileno())
                response = send_file(handle, mimetype='application/octet-stream', as_attachment=True, download_name=segments[-1], conditional=False)
                response.content_length = info.st_size
                response.call_on_close(handle.close)
                return response
            except BaseException:
                handle.close()
                raise
        except OSError:
            raise FileError() from None

    @app.post('/api/files/upload')
    def receive():
        try:
            with LOCK:
                upload(request.args.get('name'), request.stream)
            return jsonify(ok=True), 201
        except OSError:
            raise FileError() from None