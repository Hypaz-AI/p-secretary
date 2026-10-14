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
from connectors.whatsapp import NODE_BRIDGE, _find_system_chromium


def main() -> None:
    print("=" * 55)
    print("  Setup WhatsApp — PI-SECRETARY")
    print("=" * 55)

    session_dir = config.DATA_DIR / "wa_session"
    media_dir = config.DATA_DIR / "wa_media"
    session_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    bridge_path = config.DATA_DIR / "wa_bridge.js"
    bridge_path.write_text(NODE_BRIDGE, encoding="utf-8")

    already = any(session_dir.glob("*"))
    if already:
        print("\nℹ️  Esiste già una sessione: se è ancora valida il ponte si")
        print("    collegherà subito senza chiedere il QR.")

    print("\nAvvio del ponte WhatsApp...\n")
    env = dict(os.environ,
               WA_SESSION_DIR=str(session_dir),
               WA_MEDIA_DIR=str(media_dir))
    chromium = _find_system_chromium()
    if chromium:
        print(f"ℹ️  Uso Chromium di sistema: {chromium}")
        env["WA_CHROMIUM_PATH"] = chromium
    else:
        print("⚠️  Nessun Chromium di sistema trovato: Puppeteer proverà col suo")
        print("   (su ARM64 fallirà — installa: sudo snap install chromium)")
    try:
        proc = subprocess.Popen(
            ["node", str(bridge_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, cwd=str(config.DATA_DIR),
        )
    except FileNotFoundError:
        print("❌ node non trovato: installalo con  sudo apt install nodejs npm")
        sys.exit(1)

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
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if not ready:
        print("\n❌ Ponte non connesso. Possibili cause:")
        print("   - QR non scansionato in tempo (rilancia lo script)")
        print("   - dipendenze mancanti: cd data && npm install")
        print("   - guarda i messaggi d'errore qui sopra")
        sys.exit(1)

    # dà un attimo a Node per flushare la sessione su disco
    time.sleep(3)
    config.set_env("WHATSAPP_ENABLED", "true")
    print("\n✅ WhatsApp connesso! Sessione salvata in data/wa_session")
    print("   WHATSAPP_ENABLED=true scritto nel .env")
    print("\nRiavvia il segretario: sudo systemctl restart pi-secretary")


if __name__ == "__main__":
    main()
