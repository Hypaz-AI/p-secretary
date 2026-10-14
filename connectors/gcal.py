"""Connettore Google Calendar: service account o OAuth2 device flow.

Il segretario usa questo modulo per:
  - fissare appuntamenti su richiesta dell'utente: l'AI del dialogo lascia
    in fondo alla risposta un marker @@EVENTO@@, che viene rimosso e
    convertito in un evento reale del calendario;
  - sincronizzare su Google Calendar gli appuntamenti che l'AI rileva
    nelle comunicazioni in arrivo (email, WhatsApp, Telegram);
  - leggere i prossimi eventi (l'agenda "vera") per il contesto dei
    dialoghi, il digest serale e il comando /appuntamenti.

DUE modalità di autenticazione:

1. SERVICE ACCOUNT (consigliata per un server: nessun login, nessun
   consenso, nessuna scadenza). Su Google Cloud Console crea un account
   di servizio, scarica la chiave JSON e salvala come
   data/gcal_service_account.json; poi condividi il tuo calendario con
   l'indirizzo email dell'account di servizio (quello che finisce per
   @<progetto>.iam.gserviceaccount.com), come faresti con una persona.
   L'access token viene richiesto via JWT RS256 firmato con la chiave.

2. OAUTH2 DEVICE FLOW (fallback): login su https://www.google.com/device,
   token in data/gcal_tokens.json rinnovati in automatico. Con l'app in
   modalità Testing il consenso scade dopo 7 giorni.

Nessuna dipendenza esterna: urllib + firma RS256 implementata in puro
Python (hashlib e parsing DER), così setup e servizio funzionano anche
con il Python di sistema, non solo dentro il virtualenv.
"""
import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import flows

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://www.googleapis.com/calendar/v3"
# Il flusso device di Google accetta solo un sottoinsieme di scope: quello
# più mirato 'calendar.events' viene rifiutato con 'invalid_scope'.
# 'calendar' è l'unico scope Calendar con accesso in scrittura supportato.
SCOPES = "https://www.googleapis.com/auth/calendar"

# marker che l'AI del dialogo può lasciare per fissare un appuntamento:
#   @@EVENTO@@ titolo | YYYY-MM-DD HH:MM | luogo | note   (luogo/note opzionali)
_MARKER = re.compile(r"^[ \t]*@@EVENTO@@[ \t]*(.*?)[ \t]*$\n?",
                     re.IGNORECASE | re.MULTILINE)

_DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def token_file() -> Path:
    return config.DATA_DIR / "gcal_tokens.json"


def service_account_file() -> Path:
    """Percorso della chiave JSON dell'account di servizio.

    GCAL_SERVICE_ACCOUNT_FILE nel .env può puntare altrove; il default è
    data/gcal_service_account.json.
    """
    custom = config.get("GCAL_SERVICE_ACCOUNT_FILE")
    if custom:
        return Path(custom).expanduser()
    return config.DATA_DIR / "gcal_service_account.json"


# ------------------------------------------------------- service account ----
def _load_service_account() -> dict:
    """Legge e valida la chiave JSON dell'account di servizio ({} se assente)."""
    path = service_account_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if data.get("type") != "service_account" or not data.get("private_key"):
        return {}
    return data


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ------------------------------------------------ firma RS256 in puro Python ----
# Il service account si autentica con un JWT RS256: servono il parsing DER
# della chiave PEM e la firma PKCS#1 v1.5 con SHA-256. Sono due operazioni
# brevi e ben definite (RFC 8017), quindi le implementiamo qui senza
# dipendenze: nessun pacchetto da installare, funziona con qualsiasi Python.

# DigestInfo DER per SHA-256 (RFC 8017 §9.2, nota 1)
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _der_element(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Legge un elemento TLV DER da offset: (tag, valore, offset successivo)."""
    if offset + 2 > len(data):
        raise ValueError("DER troncato")
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:  # lunghezza estesa (oltre 127 byte)
        n_bytes = length & 0x7F
        if n_bytes == 0 or offset + n_bytes > len(data):
            raise ValueError("lunghezza DER non valida")
        length = int.from_bytes(data[offset:offset + n_bytes], "big")
        offset += n_bytes
    if offset + length > len(data):
        raise ValueError("DER troncato")
    return tag, data[offset:offset + length], offset + length


def _parse_rsa_private_key(pem: str) -> tuple[int, int]:
    """Estrae (n, d) da una chiave PEM in formato PKCS#8 o PKCS#1.

    Le chiavi JSON di Google sono PKCS#8 («BEGIN PRIVATE KEY»); PKCS#1
    («BEGIN RSA PRIVATE KEY») è accettato per completezza.
    """
    body = "".join(line for line in pem.strip().splitlines()
                   if "-----" not in line)
    der = base64.b64decode(body)
    tag, content, _ = _der_element(der, 0)
    if tag != 0x30:
        raise ValueError("chiave PEM malformata")
    if "BEGIN RSA PRIVATE KEY" in pem:
        rsa_seq = content  # PKCS#1: il SEQUENCE è già la RSAPrivateKey
    else:  # PKCS#8: version, AlgorithmIdentifier, OCTET STRING(RSAPrivateKey)
        _, _, offset = _der_element(content, 0)
        _, _, offset = _der_element(content, offset)
        tag, inner, _ = _der_element(content, offset)
        if tag != 0x04:
            raise ValueError("chiave PKCS#8 malformata")
        tag, rsa_seq, _ = _der_element(inner, 0)
        if tag != 0x30:
            raise ValueError("chiave PKCS#8 malformata")
    fields, offset = [], 0
    while offset < len(rsa_seq):
        _, value, offset = _der_element(rsa_seq, offset)
        fields.append(int.from_bytes(value, "big"))
    if len(fields) < 4:
        raise ValueError("RSAPrivateKey incompleto")
    return fields[1], fields[3]  # n, d


def _rsa_sign_sha256(message: bytes, n: int, d: int) -> bytes:
    """Firma RSA PKCS#1 v1.5 con SHA-256 (l'algoritmo RS256 dei JWT)."""
    digest_info = _SHA256_DIGESTINFO + hashlib.sha256(message).digest()
    key_len = (n.bit_length() + 7) // 8
    if key_len < len(digest_info) + 11:
        raise ValueError("chiave RSA troppo corta per firmare")
    padded = (b"\x00\x01" + b"\xff" * (key_len - len(digest_info) - 3)
              + b"\x00" + digest_info)
    signature = pow(int.from_bytes(padded, "big"), d, n)
    return signature.to_bytes(key_len, "big")


# cache in memoria dell'access token del service account (dura 1 ora):
# evita di rifirmare il JWT e rifare lo scambio a ogni chiamata API
_sa_token_cache = {"token": "", "expires_at": 0.0}


def _service_account_token(sa: dict, force_refresh: bool = False) -> str:
    """Access token via JWT RS256 firmato con la chiave dell'account.

    Firma in puro Python (vedi sopra): nessuna dipendenza esterna. Il token
    è cachato in memoria per la sua durata (1 ora): niente da persistere
    né da rinnovare a mano.
    """
    if (not force_refresh and _sa_token_cache["token"]
            and time.time() < _sa_token_cache["expires_at"] - 120):
        return _sa_token_cache["token"]
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iss": sa["client_email"],
        "scope": SCOPES,
        "aud": TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    segments = _b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        _b64url(json.dumps(claims, separators=(",", ":")).encode())
    n, d = _parse_rsa_private_key(sa["private_key"])
    signature = _rsa_sign_sha256(segments.encode(), n, d)
    jwt = segments + "." + _b64url(signature)
    try:
        token = _post(TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": jwt,
        })
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        raise RuntimeError(
            f"scambio JWT rifiutato: {detail or exc}") from None
    _sa_token_cache["token"] = token["access_token"]
    _sa_token_cache["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    return token["access_token"]


# ------------------------------------------------------------------ OAuth2 ----
def _load_tokens() -> dict:
    path = token_file()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_tokens(entry: dict) -> None:
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _post(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    """Estrae error/error_description dal corpo JSON di un errore HTTP."""
    try:
        body = json.loads(exc.read().decode())
        return body.get("error_description") or body.get("error") or ""
    except Exception:  # noqa: BLE001
        return ""


def _client_credentials() -> tuple[str, str]:
    """Credenziali OAuth lette al volo da os.environ.

    config.set_env (usato da setup_gcal.py) aggiorna .env e os.environ
    ma NON gli attributi del modulo config, già valutati all'import:
    leggere config.GCAL_CLIENT_ID qui vedrebbe il valore di prima del
    setup anche a inserimento appena concluso.
    """
    return config.get("GCAL_CLIENT_ID"), config.get("GCAL_CLIENT_SECRET")


def _store(token: dict) -> dict:
    old = _load_tokens()
    entry = {
        "access_token": token["access_token"],
        # Google non rimanda sempre un refresh_token nuovo: tieni il vecchio
        "refresh_token": token.get("refresh_token") or old.get("refresh_token", ""),
        "expires_at": time.time() + int(token.get("expires_in", 3600)),
    }
    if old.get("account"):
        entry["account"] = old["account"]
    _save_tokens(entry)
    return entry


def authorize(force: bool = False) -> dict:
    """Flusso device-code: stampa le istruzioni e attende il login utente."""
    data = _load_tokens()
    if (not force and data.get("refresh_token")
            and time.time() < data.get("expires_at", 0) - 120):
        print("[gcal] token già valido, salto (usa --force per rifare il login)")
        return data
    client_id, client_secret = _client_credentials()
    if not client_id or not client_secret:
        raise RuntimeError(
            "GCAL_CLIENT_ID / GCAL_CLIENT_SECRET mancanti nel .env: "
            "segui i prerequisiti in testa a setup_gcal.py")
    try:
        flow = _post(DEVICE_URL, {"client_id": client_id, "scope": SCOPES})
    except urllib.error.HTTPError as exc:
        detail = _http_error_detail(exc)
        raise RuntimeError(
            f"richiesta del device code rifiutata: {detail or exc}") from None
    url = flow.get("verification_url") or "https://www.google.com/device"
    print(f"\n👉 Apri  {url}  e inserisci il codice:  {flow['user_code']}")
    print("   Poi accedi con l'account Google del tuo calendario")
    interval = int(flow.get("interval", 5))
    deadline = time.time() + int(flow.get("expires_in", 1800))
    while time.time() < deadline:
        time.sleep(interval)
        try:
            token = _post(TOKEN_URL, {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": flow["device_code"],
            })
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
        entry = _store(token)
        print("✅ Google Calendar autorizzato, token salvato")
        return entry
    raise RuntimeError("tempo scaduto per il login: riesegui setup_gcal.py")


def get_access_token(force_refresh: bool = False) -> str:
    """Access token valido per la modalità configurata.

    Service account: JWT firmato al volo e cachato per 1 ora (niente da
    rinnovare a mano, niente che scade). Device flow: token rinnovato se
    vicino alla scadenza.
    """
    sa = _load_service_account()
    if sa:
        return _service_account_token(sa, force_refresh=force_refresh)
    data = _load_tokens()
    if not data:
        raise RuntimeError(
            "Google Calendar non autorizzato: esegui  python3 setup_gcal.py")
    if not force_refresh and time.time() < data.get("expires_at", 0) - 120:
        return data["access_token"]
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            "token Google senza refresh: esegui  python3 setup_gcal.py")
    client_id, client_secret = _client_credentials()
    try:
        token = _post(TOKEN_URL, {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
    except urllib.error.HTTPError as exc:
        # es. consenso in modalità Testing scaduto: invalid_grant
        detail = _http_error_detail(exc)
        raise RuntimeError(
            f"rinnovo del token rifiutato: {detail or exc}") from None
    return _store(token)["access_token"]


def remember_account(email: str) -> None:
    """Annota nel file token l'account collegato (solo informativo)."""
    data = _load_tokens()
    data["account"] = email
    _save_tokens(data)


# ------------------------------------------------------------------- stato ----
def readiness() -> str:
    """Stato locale, senza rete: per banner, log e dashboard."""
    if not config.GCAL_ENABLED:
        return "config_disabled"
    if _load_service_account():
        return "ready"
    client_id, client_secret = _client_credentials()
    if not client_id or not client_secret:
        return "credentials_missing"
    if not token_file().is_file():
        return "token_missing"
    return "ready"


def is_active() -> bool:
    """True se Calendar è configurato, autorizzato e il flusso è acceso."""
    if not config.GCAL_ENABLED or readiness() != "ready":
        return False
    try:
        return flows.enabled("gcal")
    except Exception:  # noqa: BLE001  (DB non raggiungibile -> inattivo)
        return False


def calendar_id() -> str:
    # lettura al volo: setup_gcal.py può cambiare il calendario a runtime
    return (config.get("GCAL_CALENDAR_ID", "primary") or "primary").strip()


def _cal_path() -> str:
    return urllib.parse.quote(calendar_id(), safe="")


# --------------------------------------------------------------- API REST ----
def _api(method: str, path: str, params: dict = None, body: dict = None) -> dict:
    """Chiamata REST v3 con Bearer token; su 401 rinnova e riprova una volta."""
    url = API_ROOT + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in (1, 2):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {get_access_token(force_refresh=(attempt == 2))}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode()
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                continue  # token scaduto: rinnova e riprova
            detail = ""
            try:
                detail = json.loads(exc.read().decode()).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"Google Calendar API {exc.code}: {detail or exc.reason}") from None
    raise RuntimeError("Google Calendar API: tentativi esauriti")


def whoami() -> dict:
    """Info del calendario primario: 'id' è l'email dell'account.

    Solo per la modalità OAuth2: un service account non ha un calendario
    «primary», vede solo quelli condivisi (vedi list_calendars).
    """
    return _api("GET", "/calendars/primary")


def list_calendars() -> list:
    """Calendari visibili all'identità autenticata.

    Per un service account sono esattamente quelli condivisi con il suo
    indirizzo email: la base per scegliere quale usare (il suo
    «primary» non esiste). Ritorna [{'id','summary','access_role'}].
    """
    data = _api("GET", "/users/me/calendarList")
    return [{"id": c.get("id", ""), "summary": c.get("summary", ""),
             "access_role": c.get("accessRole", "")}
            for c in data.get("items", [])]


def create_event(title: str, start: str, location: str = "", notes: str = "",
                 duration_min: int = None, source: str = "",
                 reminder_minutes: int = None) -> dict:
    """Crea un evento sul calendario configurato.

    start: ora locale 'YYYY-MM-DD HH:MM' nel fuso TIMEZONE.
    Ritorna {'ok': True, 'event_id', 'link'} oppure {'ok': False, 'error'}.
    """
    try:
        event = validate_event({"title": title, "dt": start,
                                "duration_min": duration_min,
                                "reminder_minutes": reminder_minutes})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    dt = _parse_local_dt(event["dt"])
    tz = ZoneInfo(config.TIMEZONE)
    local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    end = local + timedelta(minutes=event["duration_min"])
    body = {
        "summary": title or "Appuntamento",
        "start": {"dateTime": local.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": config.TIMEZONE},
        "reminders": {"useDefault": False, "overrides": [
            {"method": "popup", "minutes": event["reminder_minutes"]}]},
    }
    if location:
        body["location"] = location
    description = (notes or "").strip()
    if source:
        description = f"{description}\n\nRegistrato da: {source}".strip()
    if description:
        body["description"] = description
    try:
        data = _api("POST", f"/calendars/{_cal_path()}/events", body=body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "event_id": data.get("id", ""),
            "link": data.get("htmlLink", "")}


def upcoming_events(days: int = 7, max_results: int = 50) -> list:
    """Prossimi eventi del calendario, ordinati per inizio.

    Dict compatibili con db.upcoming_appointments (dt, title, location) più
    'all_day' ed 'event_id'. Solleva RuntimeError se i token mancano o
    l'API fallisce: i chiamanti decidono il fallback sull'agenda DB.
    """
    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    data = _api("GET", f"/calendars/{_cal_path()}/events", params={
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=days)).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(max_results),
    })
    out = []
    for item in data.get("items", []):
        start = item.get("start") or {}
        if start.get("date"):  # evento che dura tutto il giorno
            dt_text, all_day = start["date"], True
        elif start.get("dateTime"):
            dt_text = datetime.fromisoformat(start["dateTime"]).astimezone(
                tz).strftime("%Y-%m-%d %H:%M")
            all_day = False
        else:
            continue
        out.append({
            "dt": dt_text,
            "title": item.get("summary") or "(senza titolo)",
            "location": item.get("location") or "",
            "all_day": all_day,
            "event_id": item.get("id") or "",
        })
    return out


def delete_event(event_id: str) -> dict:
    try:
        _api("DELETE",
             f"/calendars/{_cal_path()}/events/{urllib.parse.quote(event_id, safe='')}")
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------- marker del dialogo ----
def _parse_local_dt(value: str):
    """Parse di 'YYYY-MM-DD HH:MM' (e varianti ISO). None se non valida."""
    if not isinstance(value, str) or not re.search(r"[T ]\d{2}:\d{2}", value):
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def validate_event(event: dict) -> dict:
    """Validate untrusted model output before any DB/API write; normalize local time."""
    if not isinstance(event, dict):
        raise ValueError("appuntamento non valido")
    result = dict(event)
    title = event.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("titolo mancante")
    dt = _parse_local_dt(event.get("dt", event.get("datetime")))
    if dt is None:
        raise ValueError("data non valida o ora mancante (usa YYYY-MM-DD HH:MM)")
    tz = ZoneInfo(config.TIMEZONE)
    local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)
    # Reject nonexistent and ambiguous wall times at DST transitions.
    from datetime import timezone
    if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("ora locale inesistente")
    if dt.tzinfo is None and local.utcoffset() != local.replace(fold=1).utcoffset():
        raise ValueError("ora locale ambigua: specificare il fuso/offset")
    if local <= datetime.now(tz):
        raise ValueError("data/ora già trascorsa")
    result.update(title=title.strip(), dt=local.strftime("%Y-%m-%d %H:%M"))
    for key, default, minimum, maximum in (
            ("duration_min", config.GCAL_EVENT_DURATION, 1, 10080),
            ("reminder_minutes", 30, 0, 40320)):
        value = event.get(key)
        value = default if value is None else value
        if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
            raise ValueError(f"{key}: minuti interi richiesti")
        value = int(value)
        if not minimum <= value <= maximum:
            raise ValueError(f"{key}: intervallo {minimum}-{maximum} minuti")
        result[key] = value
    for key in ("location", "notes"):
        if event.get(key) is not None and not isinstance(event[key], str):
            raise ValueError(f"{key}: testo richiesto")
        result[key] = event.get(key) or ""
    return result


def parse_markers(text: str) -> tuple[str, list]:
    """Estrae i marker @@EVENTO@@ lasciati dall'AI nella risposta del dialogo.

    Ogni riga-marker diventa {'title','dt','location','notes'} — oppure
    {'error': ...} se incompleta o con data non valida. Ritorna
    (testo senza le righe-marker, lista di eventi).
    """
    if not _MARKER.search(text):
        return text, []
    events = []

    def _grab(match):
        payload = match.group(1).strip()
        parts = [p.strip() for p in payload.split("|")]
        extras = parts[4:]
        parts = parts[:4]
        parts += [""] * (4 - len(parts))
        title, dt, location, notes = parts
        event = {"title": title, "dt": dt, "location": location, "notes": notes}
        for key, value in zip(("duration_min", "reminder_minutes"), extras):
            if value:
                event[key] = value
        if len(extras) > 2:
            event["error"] = "troppi campi nel marker"
        if not title:
            event["error"] = "titolo mancante"
        elif not dt:
            event["error"] = "data/ora mancante (formato YYYY-MM-DD HH:MM)"
        elif _parse_local_dt(dt) is None:
            event["error"] = f"data non valida: '{dt}' (usa YYYY-MM-DD HH:MM)"
        events.append(event)
        return ""

    clean = _MARKER.sub(_grab, text).strip()
    return clean, events
