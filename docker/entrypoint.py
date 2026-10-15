"""Fail closed for normal application startup, not for setup/diagnostics."""
import json
import os
from pathlib import Path
import sys

os.umask(0o077)
home = Path(os.environ.get('HOME') or '/var/lib/pi-secretary/home')
try:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    if home.stat().st_uid != os.getuid() or not os.access(home, os.W_OK | os.X_OK):
        raise PermissionError('HOME non scrivibile')
except OSError:
    sys.exit('HOME non disponibile: verificare proprietà e permessi del volume dati.')
os.environ['HOME'] = str(home)
if sys.argv[1:] == ['python', 'main.py']:
    try:
        ready = json.loads(Path('/config/progress.json').read_text()).get('complete') is True
    except (OSError, ValueError):
        ready = False
    if not ready:
        sys.exit('Configurazione incompleta: usare il launcher setup.')
os.execvp(sys.argv[1], sys.argv[1:])