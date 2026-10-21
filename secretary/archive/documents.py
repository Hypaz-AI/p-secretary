"""Bounded, non-executing invoice text extraction. No ZIP/P7M unpacking or OCR."""
from io import BytesIO
from pathlib import Path
import xml.etree.ElementTree as ET

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 16000


def extract(data, filename):
    data = bytes(data)
    if len(data) > MAX_BYTES:
        raise ValueError('Document exceeds extraction limit')
    suffix = Path(filename or '').suffix.lower()
    if suffix == '.xml':
        # Reject DTD/entity declarations even in UTF-16/32 encodings.
        probe = data.replace(b'\x00', b'').upper()
        if b'<!DOCTYPE' in probe or b'<!ENTITY' in probe:
            raise ValueError('XML declarations forbidden')
        root = ET.fromstring(data)
        return ' '.join(text.strip() for text in root.itertext() if text.strip())[:MAX_TEXT]
    if suffix == '.pdf' and data.startswith(b'%PDF-'):
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            return ''
        # Content stream and page caps bound common decompression/text cases.
        result = ''
        for page in reader.pages[:10]:
            contents = page.get_contents()
            if contents and len(contents.get_data()) > MAX_BYTES:
                break
            result += (page.extract_text() or '')[:MAX_TEXT-len(result)]
            if len(result) >= MAX_TEXT:
                break
        return result
    return ''


def attachment_text(attachments):
    result = ''
    for attachment in attachments[:10]:
        try:
            data = attachment.get('data')
            if data is None and attachment.get('path'):
                path = Path(attachment['path'])
                if path.stat().st_size > MAX_BYTES:
                    continue
                data = path.read_bytes()
            if data is not None:
                result += '\n'+extract(data, attachment.get('filename',''))
        except Exception:
            # Corrupt/unavailable optional parser must not block message analysis.
            continue
        if len(result) >= MAX_TEXT:
            break
    return result[:MAX_TEXT]