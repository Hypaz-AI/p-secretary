"""Filesystem dell'agente: cartelle di contenuto in lettura, `workspace/` in scrittura.

Costruito sulle primitive già collaudate di file_browser.py (apertura per
dir_fd, O_NOFOLLOW, rifiuto di hardlink e di file protetti): il workspace è
ESATTAMENTE la cartella che la dashboard mostra e permette di scaricare.

* Lettura: `workspace/` e le cartelle di contenuto (email_attachments, tg_files,
  wa_media, invoices, uploads). Mai sessioni, token, credenziali, backup.
* Scrittura: solo dentro `workspace/`, solo file di testo NUOVI o in append;
  niente cancellazione né sovrascrittura (resta all'owner via dashboard).
* `ISTRUZIONI.md` (istruzioni permanenti dell'owner) è riservato: l'agente non
  può scriverlo, perché un'iniezione da un'email lo renderebbe persistente.
"""
from fnmatch import fnmatch
from io import BytesIO
from pathlib import Path
import html.parser
import os
import re
import zipfile
import xml.etree.ElementTree as ET

from secretary.core import config
from secretary.archive import file_browser
from secretary.archive.file_browser import FileError

WORKSPACE = 'workspace'
RESERVED = frozenset({'ISTRUZIONI.md'})
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_FILE_WRITE = 2 * 1024 * 1024
MAX_DEPTH = 5
MAX_FIND = 200
TEXT_SUFFIXES = frozenset('.txt .md .csv .tsv .json .ics .log .yaml .yml .ini .eml .vcf'.split())


class FsError(ValueError):
    """Errore mostrabile al modello (nessun percorso fisico)."""


def _wrap(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except FileError as exc:
        raise FsError(exc.message) from None
    except (OSError, UnicodeError):
        raise FsError('File o cartella non disponibile') from None


NAME_HELP = ('Nome non ammesso: usa solo lettere ASCII, numeri, spazio, punto, trattino e underscore '
             '(niente accenti), iniziando con una lettera o un numero, e senza le parole riservate '
             'credential, secret, session, token, config, state, password')


def _segments(path):
    if not isinstance(path, str) or len(path) > 400 or path.startswith('/'):
        raise FsError('Percorso non valido: usa un percorso relativo, es. workspace/report.md')
    result = path.strip('/').split('/') if path.strip('/') else []
    for name in result:
        if not file_browser.safe_name(name):
            raise FsError(NAME_HELP)
    return result


def _route(segments):
    """(root, percorso nella radice): le fatture vivono in INVOICE_DIR, il resto in DATA_DIR."""
    if segments and segments[0] == 'invoices':
        return 'invoices', segments[1:]
    if segments and segments[0] not in file_browser.CONTENT_DIRS:
        raise FsError('Cartella non consentita')
    return 'data', segments


def quota_bytes():
    try:
        return max(1, int(config.get('AGENT_WORKSPACE_QUOTA_MB', '200'))) * 1024 * 1024
    except ValueError:
        return 200 * 1024 * 1024


def listing(path=''):
    """Elenco di una cartella consentita ('' = radice con le cartelle disponibili)."""
    segments = _segments(path)
    _wrap(_ensure_workspace)
    if not segments:
        entries = _wrap(file_browser.listing, 'data', '')
        if Path(config.INVOICE_DIR).is_dir():
            entries.append({'name': 'invoices', 'directory': True, 'size': None})
        return entries
    root, rest = _route(segments)
    return _wrap(file_browser.listing, root, '/'.join(rest))


def read_bytes(path, limit=MAX_READ_BYTES):
    """Legge un file regolare consentito. Ritorna (bytes, troncato)."""
    segments = _segments(path)
    if not segments:
        raise FsError('Indica un file')
    root, rest = _route(segments)
    if not rest:
        raise FsError('Indica un file, non una cartella')

    def run():
        with file_browser.directory(root, '/'.join(rest[:-1])) as fd:
            handle = os.open(rest[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        try:
            file_browser.regular(handle)
            with os.fdopen(handle, 'rb', closefd=False) as stream:
                data = stream.read(limit + 1)
        finally:
            os.close(handle)
        return data[:limit], len(data) > limit
    return _wrap(run)


def read_text(path, *, offset=0, max_chars=20000):
    """Testo di un file: estrae da PDF/DOCX/XLSX/HTML, decodifica gli altri."""
    max_chars = max(200, min(int(max_chars), 60000))
    offset = max(0, int(offset))
    data, cut = read_bytes(path)
    name = path.rsplit('/', 1)[-1]
    text = extract_text(data, name)
    window = text[offset:offset + max_chars]
    return dict(path=path, size=len(data), chars=len(text), offset=offset, text=window,
                truncated=cut or offset + max_chars < len(text))


def find(pattern, path=''):
    """Cerca per nome (glob, senza distinguere maiuscole) sotto una cartella consentita."""
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 80:
        raise FsError('Pattern non valido (es. *.pdf, *fattura*)')
    pattern = pattern.strip().lower()
    base = _segments(path)
    if base:
        _route(base)
    results = []
    queue = [(base, 0)] if base else [([name], 0) for name in sorted(file_browser.CONTENT_DIRS | {'invoices'})]
    while queue and len(results) < MAX_FIND:
        current, depth = queue.pop(0)
        try:
            root, rest = _route(current)
            entries = _wrap(file_browser.listing, root, '/'.join(rest))
        except FsError:
            continue
        for entry in entries:
            relative = '/'.join(current + [entry['name']])
            if entry['directory']:
                if depth < MAX_DEPTH:
                    queue.append((current + [entry['name']], depth + 1))
            elif fnmatch(entry['name'].lower(), pattern):
                results.append(dict(path=relative, size=entry['size']))
                if len(results) >= MAX_FIND:
                    break
    return results


def _workspace_size():
    """Byte totali del workspace (solo conteggio per la quota, non un controllo di sicurezza)."""
    total = 0
    for root, _, files in os.walk(Path(config.DATA_DIR).absolute() / WORKSPACE, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _ensure_workspace():
    with file_browser.directory('data') as fd:
        try:
            os.mkdir(WORKSPACE, 0o700, dir_fd=fd)
        except FileExistsError:
            pass


def _check_target(segments):
    if len(segments) < 2 or segments[0] != WORKSPACE:
        raise FsError(f'Si può scrivere solo dentro {WORKSPACE}/ (es. {WORKSPACE}/report.md)')
    if len(segments) - 1 > MAX_DEPTH:
        raise FsError('Cartelle troppo annidate')
    if len(segments) == 2 and segments[1] in RESERVED:
        raise FsError('File riservato: modificabile solo dal proprietario')


def mkdir(path):
    segments = _segments(path)
    if not segments or segments[0] != WORKSPACE or len(segments) - 1 > MAX_DEPTH:
        raise FsError(f'Si possono creare cartelle solo dentro {WORKSPACE}/')

    def run():
        _ensure_workspace()
        for index in range(2, len(segments) + 1):
            with file_browser.directory('data', '/'.join(segments[:index - 1])) as fd:
                try:
                    os.mkdir(segments[index - 1], 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
        return '/'.join(segments)
    return dict(path=_wrap(run))


def write(path, content, mode='create'):
    """Crea o accoda un file di testo nel workspace. Ritorna il percorso e i byte."""
    if mode not in ('create', 'append'):
        raise FsError("mode deve essere 'create' (nuovo file) o 'append'")
    if not isinstance(content, str):
        raise FsError('Il contenuto deve essere testo')
    data = content.encode('utf-8')
    if len(data) > MAX_FILE_WRITE:
        raise FsError('Contenuto oltre 2 MB: dividilo in più file')
    segments = _segments(path)
    _check_target(segments)
    if _workspace_size() + len(data) > quota_bytes():
        raise FsError('Quota del workspace esaurita: chiedi al proprietario di fare spazio')

    def run():
        _ensure_workspace()
        with file_browser.directory('data', '/'.join(segments[:-1])) as fd:
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            flags |= (os.O_CREAT | os.O_EXCL) if mode == 'create' else (os.O_APPEND | os.O_CREAT)
            try:
                handle = os.open(segments[-1], flags, 0o600, dir_fd=fd)
            except FileExistsError:
                raise FsError('Il file esiste già: usa mode=append oppure un altro nome (es. report_v2.md)') from None
            try:
                info = os.fstat(handle)
                if info.st_nlink != 1 or file_browser.protected(info):
                    raise FsError('File non scrivibile')
                with os.fdopen(handle, 'ab', closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(handle)
        return '/'.join(segments)
    return dict(path=_wrap(run), bytes=len(data), mode=mode)


# ------------------------------------------------------------ estrazione testo
class _Html(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in ('p', 'br', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _zip_xml(data, member, limit=20 * 1024 * 1024):
    with zipfile.ZipFile(BytesIO(data)) as archive:
        info = archive.getinfo(member)
        if info.file_size > limit:
            raise ValueError('Contenuto compresso troppo grande')
        return archive.read(member)


def _strip_ns(tag):
    return tag.rsplit('}', 1)[-1]


def _docx(data):
    root = ET.fromstring(_zip_xml(data, 'word/document.xml'))
    paragraphs = []
    for para in root.iter():
        if _strip_ns(para.tag) == 'p':
            paragraphs.append(''.join(t.text or '' for t in para.iter() if _strip_ns(t.tag) == 't'))
    return '\n'.join(paragraphs)


def _xlsx(data):
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
    shared = []
    if 'xl/sharedStrings.xml' in names:
        root = ET.fromstring(_zip_xml(data, 'xl/sharedStrings.xml'))
        shared = [''.join(t.text or '' for t in si.iter() if _strip_ns(t.tag) == 't')
                  for si in root if _strip_ns(si.tag) == 'si']
    lines = []
    for name in sorted(n for n in names if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n))[:8]:
        lines.append(f'## {name.rsplit("/", 1)[-1]}')
        root = ET.fromstring(_zip_xml(data, name))
        for row in root.iter():
            if _strip_ns(row.tag) != 'row':
                continue
            cells = []
            for cell in row:
                value = next((c.text or '' for c in cell if _strip_ns(c.tag) == 'v'), '')
                if cell.get('t') == 's' and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                cells.append(value)
            lines.append('\t'.join(cells))
            if len(lines) > 5000:
                return '\n'.join(lines)
    return '\n'.join(lines)


def extract_text(data, filename, *, max_chars=400000):
    """Testo leggibile da un file, senza eseguire nulla e con limiti rigidi."""
    suffix = Path(filename or '').suffix.lower()
    try:
        if data.startswith(b'%PDF-') or suffix == '.pdf':
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(data), strict=False)
            if reader.is_encrypted:
                return '[PDF protetto da password: contenuto non leggibile]'
            parts = []
            for page in reader.pages[:60]:
                parts.append(page.extract_text() or '')
                if sum(map(len, parts)) > max_chars:
                    break
            text = '\n\n'.join(parts)
            return text or '[PDF senza testo estraibile: probabilmente una scansione]'
        if suffix == '.docx':
            return _docx(data)[:max_chars]
        if suffix == '.xlsx':
            return _xlsx(data)[:max_chars]
        if suffix in ('.html', '.htm'):
            parser = _Html()
            parser.feed(data.decode('utf-8', 'replace'))
            return re.sub(r'\n{3,}', '\n\n', ''.join(parser.parts))[:max_chars]
        if suffix == '.xml':
            probe = data.replace(b'\x00', b'').upper()
            if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
                return '[XML con dichiarazioni DTD rifiutato per sicurezza]'
            return ' '.join(t.strip() for t in ET.fromstring(data).itertext() if t.strip())[:max_chars]
        sample = data[:4096]
        if suffix in TEXT_SUFFIXES or b'\x00' not in sample:
            return data.decode('utf-8', 'replace')[:max_chars]
    except Exception:
        return '[Formato non leggibile o danneggiato]'
    return f'[File binario {suffix or "senza estensione"}: {len(data)} byte, nessun testo estraibile]'
