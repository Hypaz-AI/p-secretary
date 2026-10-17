"""Global quiet hours and secretary mode helpers.

Pure functions, no DB/imports of heavy modules: usable from pipeline, digest
and secretary_runtime alike. Emergency notifications (urgent or high severity)
are NEVER deferred; ordinary ones are postponed to the end of the quiet band.

SECRETARY_MODE gates third-party sends only: observe keeps the secretary
proposal-only (drafts, reviews, owner decisions), active additionally allows
explicitly approved external effects (email dispatch, calendar writes).
Owner replies and emergency notifications are not affected by the mode.
"""
from datetime import datetime, timedelta

import config


def is_quiet(clock: datetime) -> bool:
    """True se l'ora locale rientra nella fascia silenziosa globale."""
    local = clock.astimezone(config._tz())
    start, end = config.QUIET_HOURS_START, config.QUIET_HOURS_END
    if start == end:
        return False
    if start < end:
        return start <= local.hour < end
    return local.hour >= start or local.hour < end  # fascia a cavallo di mezzanotte


def defer_until(clock: datetime) -> datetime:
    """Primo istante UTC fuori dalla fascia silenziosa, >= clock."""
    if not is_quiet(clock):
        return clock
    local = clock.astimezone(config._tz())
    target = local.replace(hour=config.QUIET_HOURS_END, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(clock.tzinfo)


def is_emergency(payload: dict) -> bool:
    """Emergenze vere scavalcano sempre le quiet hours, mai la policy dati."""
    return bool(payload.get('urgent')) or payload.get('severity') == 'alta'


def notification_ready(payload: dict, clock: datetime) -> bool:
    """True se la notifica può partire ora; le emergenze passano sempre."""
    return not is_quiet(clock) or is_emergency(payload)


def sends_allowed() -> bool:
    """True solo in modalità active: invii a terzi esplicitamente approvati."""
    return config.SECRETARY_MODE == 'active'
