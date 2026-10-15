#!/usr/bin/env python3
"""Collega WhatsApp al segretario: mostra il QR e salva la sessione.

La sessione viene salvata in data/wa_session e riusata dal servizio.
Al termine imposta WHATSAPP_ENABLED=true nel .env automaticamente.

Uso:
    python3 setup_whatsapp.py
"""
import json
import os
import subprocess
import sys
import time
import shutil
import socket
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from connectors.whatsapp import bridge_path, bridge_env, stop_process
from connectors.storage import FileLock
from setup_ui import guide


def retired_container_owner(host, root):
    """Host-only verification: inspect stopped owner and every running mount.

    A foreign hostname alone or an absent PID is never evidence of staleness.
    Docker inspection is unavailable in the wizard container: fail closed there.
    """
    if Path('/.dockerenv').exists():
        return False
    try:
        ids = subprocess.run(['docker', 'ps', '-aq', '--no-trunc'], check=True,
                             capture_output=True, text=True).stdout.split()
        if not ids:
            return False
        containers = json.loads(subprocess.run(['docker', 'inspect', *ids], check=True,
                                capture_output=True, text=True).stdout)
        owners = [c for c in containers if c['Config']['Hostname'] == host or c['Id'] == host or c['Id'][:12] == host]
        if len(owners) != 1 or owners[0]['State']['Running']:
            return False
        target = root.resolve()
        def shares(container):
            for mount in container.get('Mounts', []):
                source = Path(mount['Source']).resolve()
                if source == target or source in target.parents or target in source.parents:
                    return True
            return False
        if not shares(owners[0]):
            return False
        return not any(c['State']['Running'] and shares(c) for c in containers)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return False


def reset_session():
    """Called with the shared owner lock held; never unlink that lock inode.

    Foreign Chromium owners cannot be disproved from a container PID namespace.
    Refuse rather than interpreting a missing local PID as proof of staleness.
    """
    root = config.DATA_DIR / 'wa_session'
    if root.is_symlink() or root.resolve().parent != config.DATA_DIR.resolve():
        raise ValueError('Percorso sessione non sicuro')
    profile = root / 'session'
    if not Path('/proc').is_dir():
        return False
    if profile.is_symlink():
        raise ValueError('Profilo symlink non consentito')
    singleton = profile / 'SingletonLock'
    if singleton.is_symlink():
        host, sep, pid = os.readlink(singleton).rpartition('-')
        if not sep or not pid.isdigit():
            raise ValueError('Proprietario Chromium non verificabile')
        if host != socket.gethostname():
            if not retired_container_owner(host, root):
                print('Lock Chromium di un altro hostname: proprietà inattiva non verificata.')
                print('Dal wizard Docker il reset non è sicuro: eseguire il setup sull’host con visibilità Docker, a runtime fermo.')
                return False
        elif Path('/proc', pid).exists():
            return False
    # Also guard Chromium processes that survived the Node bridge shutdown.
    try:
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():
                continue
            try:
                command = (process / 'cmdline').read_bytes().replace(b'\0', b' ')
            except FileNotFoundError:
                continue
            if str(profile.resolve()).encode() in command:
                return False
    except PermissionError:
        return False
    if root.stat().st_uid != os.getuid():
        return False
    entries = [p for p in root.iterdir() if p.name != '.owner.lock']
    if any(p.lstat().st_uid != os.getuid() for p in entries):
        return False
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    return True


def _setup(retry=True) -> int:
    guide('whatsapp')

    session_dir = config.DATA_DIR / "wa_session"
    media_dir = config.DATA_DIR / "wa_media"
    session_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    already = any(session_dir.glob("*"))
    if already:
        print("\nℹ️  Esiste già una sessione: se è ancora valida il ponte si")
        print("    collegherà subito senza chiedere il QR.")

    print("\nAvvio del ponte WhatsApp...\n")
    env = bridge_env(auth_only=True)
    chromium = env.get('WA_CHROMIUM_PATH')
    if chromium:
        print(f"ℹ️  Uso Chromium di sistema: {chromium}")
        env["WA_CHROMIUM_PATH"] = chromium
    else:
        print("⚠️  Nessun Chromium di sistema trovato: Puppeteer proverà col suo")
        print("   (su ARM64 fallirà — installa: sudo snap install chromium)")
    try:
        proc = subprocess.Popen(
            ["node", str(bridge_path())],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=str(config.DATA_DIR),
        )
    except FileNotFoundError:
        print("❌ node non trovato: installalo con  sudo apt install nodejs npm")
        return 1

    ready = False
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    print(line)
                    continue
                if event.get("type") == "ready":
                    ready = True
                    break
                # il QR ASCII è stampato dal bridge sulle righe non-JSON
            else:
                print(line)
    except KeyboardInterrupt:
        pass
    finally:
        stop_process(proc)

    if not ready:
        print("\n❌ Ponte non connesso. Possibili cause:")
        print("   - QR non scansionato in tempo (rilancia lo script)")
        print("   - dipendenze mancanti: cd data && npm install")
        print("   - guarda i messaggi d'errore qui sopra")
        if retry:
            print('Puoi azzerare SOLO la sessione locale WhatsApp, non media o altri dati.')
            print('Sul telefono revoca il vecchio dispositivo da Dispositivi collegati; poi collega il nuovo QR.')
            if input('Azzerare la sessione WhatsApp locale? Scrivi RESET per consentire: ').strip() == 'RESET':
                if reset_session():
                    return _setup(retry=False)
                print('Reset rifiutato: profilo attivo o proprietà non verificabile; nessun dato cancellato.')
        return 1

    if proc.returncode != 0:
        print('❌ Arresto del bridge non pulito; configurazione non abilitata.')
        return 1
    config.set_env("WHATSAPP_ENABLED", "true")
    print("\n✅ WhatsApp connesso! Sessione salvata in data/wa_session")
    print("   WHATSAPP_ENABLED=true scritto nel .env")
    print("\nProsegui il wizard; l’avvio dell’app verrà chiesto alla fine.")
    return 0


def main():
    try:
        with FileLock(config.DATA_DIR / 'wa_session/.owner.lock', blocking=False):
            return _setup()
    except (Exception, KeyboardInterrupt):
        print('❌ Sessione occupata o non accessibile; fermare runtime/setup concorrente.')
        return 1


if __name__ == "__main__":
    sys.exit(main())
