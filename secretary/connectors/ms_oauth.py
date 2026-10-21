"""OAuth2 per gli account Microsoft (outlook.com / hotmail / Microsoft 365).

Microsoft ha disattivato l'autenticazione "basic" (indirizzo + password) su
IMAP e SMTP: serve il meccanismo SASL XOAUTH2 con un token OAuth2.

Questo modulo implementa:
  - il flusso "device code" (ideale sul Raspberry: non serve un browser
    locale, basta uno smartphone o un PC qualsiasi per il login);
  - il refresh automatico dei token in scadenza.

I token vengono salvati in data/ms_tokens.json (permessi 600).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from secretary.core import config

# Client ID pubblico usato da Mozilla Thunderbird per IMAP/SMTP su Outlook:
# è pre-registrato da Microsoft, supporta il flusso device code e non richiede
# di creare un'app su Azure. (Se il tenant aziendale lo blocca, si può
# registrare un'app propria e impostarne l'ID in EMAIL{n}_CLIENT_ID.)
DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = (
    "https://outlook.office.com/IMAP.AccessAsUser.All "
    "https://outlook.office.com/SMTP.Send offline_access"
)
TOKEN_FILE = config.DATA_DIR / "ms_tokens.json"


def _load_tokens() -> dict:
    if TOKEN_FILE.exists():
        try:
            return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_tokens(tokens: dict) -> None:
    from secretary.connectors.storage import FileLock, atomic_write
    with FileLock(str(TOKEN_FILE) + '.lock'):
        atomic_write(TOKEN_FILE, json.dumps(tokens, indent=2))


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _client_id(account: dict) -> str:
    return account.get("client_id") or DEFAULT_CLIENT_ID


def _store(address: str, token: dict) -> dict:
    from secretary.connectors.storage import FileLock
    with FileLock(str(TOKEN_FILE) + '.transaction.lock'):
        return _store_locked(address, token)


def _store_locked(address: str, token: dict) -> dict:
    tokens = _load_tokens()
    key = address.lower()
    old = tokens.get(key, {})
    entry = {
        "access_token": token["access_token"],
        # se il server non rimanda un refresh_token nuovo, tieni il vecchio
        "refresh_token": token.get("refresh_token") or old.get("refresh_token", ""),
        "expires_at": time.time() + int(token.get("expires_in", 3600)),
    }
    tokens[key] = entry
    _save_tokens(tokens)
    return entry


def authorize(account: dict, force: bool = False) -> dict:
    """Flusso device-code: stampa le istruzioni e attende il login utente."""
    address = account["address"]
    if not force:
        data = _load_tokens().get(address.lower())
        if data and time.time() < data["expires_at"] - 120:
            print(f"[ms-oauth] token già valido per {address}, salto (usa --force per rifarlo)")
            return data
    flow = _post(f"{AUTHORITY}/oauth2/v2.0/devicecode",
                 {"client_id": _client_id(account), "scope": SCOPES})
    url = flow.get("verification_uri") or "https://microsoft.com/devicelogin"
    print(f"\n👉 Apri  {url}  e inserisci il codice:  {flow['user_code']}")
    print(f"   Poi accedi con:  {address}")
    interval = int(flow.get("interval", 5))
    while True:
        time.sleep(interval)
        try:
            token = _post(f"{AUTHORITY}/oauth2/v2.0/token", {
                "client_id": _client_id(account),
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": flow["device_code"],
            })
            entry = _store(address, token)
            print(f"✅ {address}: autorizzato, token salvato")
            return entry
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode())
            except Exception:  # noqa: BLE001
                raise RuntimeError(f"errore HTTP {exc.code}") from None
            err = body.get("error", "")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            raise RuntimeError(body.get("error_description", err)) from None


def _refresh(account: dict, refresh_token: str) -> dict:
    token = _post(f"{AUTHORITY}/oauth2/v2.0/token", {
        "client_id": _client_id(account),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": SCOPES,
    })
    return _store(account["address"], token)


def get_access_token(account: dict) -> str:
    """Ritorna un access token valido, rinnovandolo se vicino alla scadenza."""
    data = _load_tokens().get(account["address"].lower())
    if not data:
        raise RuntimeError(
            f"nessun token OAuth2 per {account['address']}: "
            "esegui  python3 -m secretary.setup.ms_auth")
    if time.time() > data["expires_at"] - 120:
        if not data.get("refresh_token"):
            raise RuntimeError(
                f"token scaduto per {account['address']}: "
                "riesegui  python3 -m secretary.setup.ms_auth")
        data = _refresh(account, data["refresh_token"])
    return data["access_token"]
