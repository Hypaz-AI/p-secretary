"""Autorità del proprietario sulla dashboard.

`DashboardOwner` è un'asserzione costruita SOLO dal server, dopo aver
autenticato il proprietario configurato e verificato il token CSRF. Non va mai
deserializzata da JSON del browser né da output di un modello.
"""
from dataclasses import dataclass

import config


class Unauthorized(PermissionError):
    """Manca l'autenticazione o la verifica CSRF del proprietario configurato."""


class Conflict(ValueError):
    """Versione cambiata, ritentativo modificato o stato superato: ricaricare."""


@dataclass(frozen=True)
class DashboardOwner:
    owner_id: str
    authenticated: bool
    csrf_verified: bool


def positive(value, name='id'):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError('Invalid ' + name)
    return value


def require(caller):
    """Solo il proprietario configurato, autenticato e con CSRF verificato."""
    if (type(caller) is not DashboardOwner or caller.authenticated is not True
            or caller.csrf_verified is not True or type(caller.owner_id) is not str
            or not caller.owner_id.isdecimal() or int(caller.owner_id) <= 0
            or caller.owner_id != str(config.TELEGRAM_CHAT_ID or '').strip()):
        raise Unauthorized('Authenticated configured dashboard owner + CSRF required')
    return caller


def current():
    """Costruito dentro le sole rotte protette: mai da input del chiamante."""
    owner = str(config.TELEGRAM_CHAT_ID or '').strip()
    if not owner.isdecimal() or int(owner) <= 0:
        raise PermissionError('Configured owner required')
    return DashboardOwner(owner_id=owner, authenticated=True, csrf_verified=True)
