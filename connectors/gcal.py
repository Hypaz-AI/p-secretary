"""Connettore Google Calendar: esclusivamente service account IAM.

Il segretario usa questo modulo per:
  - fissare appuntamenti su richiesta dell'utente: l'AI del dialogo lascia
    in fondo alla risposta un marker @@EVENTO@@, che viene rimosso e
    convertito in un evento reale del calendario;
  - sincronizzare su Google Calendar gli appuntamenti che l'AI rileva
    nelle comunicazioni in arrivo (email, WhatsApp, Telegram);
  - leggere i prossimi eventi (l'agenda "vera") per il contesto dei
    dialoghi, il digest serale e il comando /appuntamenti.

SERVICE ACCOUNT (nessun login, nessun
   consenso, nessuna scadenza). Su Google Cloud Console crea un account
   di servizio, scarica la chiave JSON e salvala come
   data/gcal_service_account.json; poi condividi il tuo calendario con
   l'indirizzo email dell'account di servizio (quello che finisce per
   @<progetto>.iam.gserviceaccount.com), come faresti con una persona.
   L'access token viene richiesto via JWT RS256 firmato con la chiave.

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

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://www.googleapis.com/calendar/v3"
# Accesso limitato ai calendari condivisi tramite ACL.
SCOPES = "https://www.googleapis.com/auth/calendar"

_DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


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
    try:
        validate_service_account(data)
    except ValueError:
        return {}
    return data


def validate_service_account(data):
    """Validazione locale; gli errori non contengono mai il JSON privato."""
    try:
        if not isinstance(data, dict) or data.get('type') != 'service_account':
            raise ValueError
        for key in ('client_email', 'private_key', 'project_id', 'private_key_id'):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError
        if not re.fullmatch(r'[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.iam\.gserviceaccount\.com', data['client_email']):
            raise ValueError
        n, d = _parse_rsa_private_key(data['private_key'])
        if n.bit_length() < 1024 or d <= 1:
            raise ValueError
    except Exception:
        raise ValueError('JSON service account IAM non valido; controllare tipo, campi e chiave RSA.') from None
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
        # Non ripetere payload remoti che potrebbero includere l'assertion.
        raise RuntimeError(f'scambio JWT rifiutato (HTTP {exc.code})') from None
    _sa_token_cache["token"] = token["access_token"]
    _sa_token_cache["expires_at"] = time.time() + int(token.get("expires_in", 3600))
    return token["access_token"]


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


def get_access_token(force_refresh: bool = False) -> str:
    """Solo JWT service account; vecchi token utente non vengono letti."""
    sa = _load_service_account()
    if not sa:
        raise RuntimeError('Chiave service account mancante/non valida: esegui setup_gcal.py')
    return _service_account_token(sa, force_refresh=force_refresh)


# ------------------------------------------------------------------- stato ----
def readiness() -> str:
    """Stato locale, senza rete: per banner, log e dashboard."""
    if not config.GCAL_ENABLED:
        return "config_disabled"
    if not _load_service_account():
        return "credentials_missing"
    if config.get('GCAL_CALENDAR_ID').strip().lower() in ('', 'primary'):
        return 'calendar_missing'
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
    calendar = config.get('GCAL_CALENDAR_ID').strip()
    if calendar.lower() in ('', 'primary'):
        raise ValueError('Configurare l’ID esplicito del calendario condiviso, non primary')
    return calendar


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
    """Info del calendario condiviso configurato, mai del calendario primary."""
    return _api("GET", "/calendars/" + _cal_path())


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
                 reminder_minutes: int = None, event_id: str = None) -> dict:
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
    if event_id:
        body['id'] = event_id
    try:
        data = _api("POST", f"/calendars/{_cal_path()}/events", body=body)
    except Exception as exc:  # noqa: BLE001
        if event_id and 'API 409:' in str(exc):
            existing = _api('GET', f'/calendars/{_cal_path()}/events/{event_id}')
            if existing.get('id') == event_id and existing.get('status') != 'cancelled':
                return {'ok': True, 'event_id': event_id}
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
        if 'API 404:' in str(exc) or 'API 410:' in str(exc):
            return {'ok': True}
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
