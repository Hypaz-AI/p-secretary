"""Explicit bounded recovery policy and persistent source scan watermarks."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import jobs


def days():
    return max(1, min(90, int(config.get('RECOVERY_DAYS','14'))))


def scan_start(key, now=None):
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    floor = now - timedelta(days=days())
    stored = jobs.cursor('recovery:'+key)
    if stored:
        try:
            # One-day overlap tolerates source date changes and clock skew.
            floor = max(floor, datetime.fromisoformat(stored)-timedelta(days=1))
        except (ValueError, TypeError):
            pass
    return floor


def completed(key, now=None):
    now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    jobs.set_cursor('recovery:'+key,now.isoformat())