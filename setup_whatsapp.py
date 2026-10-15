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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from connectors.whatsapp import bridge_path, bridge_env, stop_process
from connectors.storage import FileLock
from setup_ui import guide


def _setup() -> None:
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
        return 1

    if proc.returncode != 0:
        print('❌ Arresto del bridge non pulito; configurazione non abilitata.')
        return 1
    config.set_env("WHATSAPP_ENABLED", "true")
    print("\n✅ WhatsApp connesso! Sessione salvata in data/wa_session")
    print("   WHATSAPP_ENABLED=true scritto nel .env")
    print("\nRicaricare con il launcher up (Docker) o riavviare il servizio host.")
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
