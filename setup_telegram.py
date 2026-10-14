#!/usr/bin/env python3
"""Configura il bot Telegram del segretario, in modo guidato.

1) Crea il bot con @BotFather e incolla qui il token
2) Scrivi un messaggio al tuo bot
3) Token e chat id vengono scritti automaticamente nel .env

Uso:
    python3 setup_telegram.py
"""
import json
import subprocess
import sys
import time
import urllib.request

import config


def tg_call(token: str, method: str, params: dict = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def warn_if_service_running() -> None:
    """Il bot può leggere i messaggi da un solo processo alla volta."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", "pi-secretary"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "active":
            print("⚠️  Il servizio pi-secretary è ATTIVO.")
            print("    Fermalo prima di continuare, altrimenti i due processi")
            print("    si contendono i messaggi del bot:")
            print("    sudo systemctl stop pi-secretary\n")
            input("Premi INVIO se l'hai fermato (o per continuare comunque) ")
    except Exception:  # noqa: BLE001
        pass  # systemctl non disponibile o servizio inesistente: si prosegue


def main() -> None:
    print("=" * 55)
    print("  Setup bot Telegram — PI-SECRETARY")
    print("=" * 55)
    warn_if_service_running()

    print("\n1. Apri Telegram, cerca @BotFather e scrivi /newbot")
    print("2. Scegli un nome (es. 'Il mio segretario')")
    print("3. Scegli uno username che finisce con 'bot'")
    print("4. BotFather ti darà un TOKEN tipo '123456789:AAE...'")
    token = input("\nIncolla qui il TOKEN: ").strip()

    try:
        me = tg_call(token, "getMe")
        bot_username = me["result"]["username"]
        print(f"\n✅ Token valido! Il tuo bot è @{bot_username}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Token non valido: {exc}")
        sys.exit(1)

    config.set_env("TELEGRAM_BOT_TOKEN", token)
    print("   Token salvato nel .env")

    print(f"\n5. Ora scrivi UN messaggio qualsiasi al bot @{bot_username}")
    print("   da Telegram (es. 'ciao'). Aspetto fino a 120 secondi...")

    chat_id = None
    offset = 0
    deadline = time.time() + 120
    while time.time() < deadline and chat_id is None:
        try:
            updates = tg_call(token, "getUpdates", {"offset": offset, "timeout": 0})
            for u in updates.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message") or u.get("edited_message")
                # solo chat private: in un gruppo il chat id sarebbe sbagliato
                if msg and msg.get("text") and msg["chat"].get("type") == "private":
                    chat_id = msg["chat"]["id"]
                    name = msg["chat"].get("first_name", "")
                    print(f"\n✅ Messaggio ricevuto da {name}! chat_id = {chat_id}")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)

    if chat_id is None:
        print("\n⚠️  Nessun messaggio ricevuto entro 120 secondi.")
        print("    Riprova più tardi o scrivi il chat_id a mano nel .env")
        print("    (lo trovi su https://api.telegram.org/bot<TOKEN>/getUpdates)")
        sys.exit(1)

    config.set_env("TELEGRAM_CHAT_ID", str(chat_id))
    print("   chat_id salvato nel .env")

    try:
        tg_call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🤖 Segretario virtuale: Telegram configurato correttamente!",
        })
        print("\n🎉 Fatto! Ti ho appena mandato un messaggio di test.")
        print("   Riavvia il segretario: sudo systemctl restart pi-secretary")
    except Exception as exc:  # noqa: BLE001
        print(f"\n⚠️  Invio del test fallito: {exc}")


if __name__ == "__main__":
    main()
