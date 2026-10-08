"""Versioned publication calendars. Historical return alignment is deliberately separate."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import DataError

CATALOG = Path(__file__).resolve().parents[2] / 'references/publication-calendars.json'
MAX_PUBLICATION_DAYS = 7


def load_calendars(path: Path = CATALOG) -> dict:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
        if data['schema_version'] != 1 or not data['version']:
            raise ValueError('unsupported version')
        for key in ('cn_nav', 'nasdaq', 'safe_fx'):
            cal = data['calendars'][key]
            ZoneInfo(cal['timezone'])
            for year, spec in cal['years'].items():
                days = spec['closed_dates']
                if not spec['source_url'].startswith('https://') or len(days) != len(set(days)):
                    raise ValueError('missing source or duplicate closure')
                if any(date.fromisoformat(d).year != int(year) for d in days):
                    raise ValueError('calendar year mismatch')
                date.fromisoformat(spec['verified_on'])
        for notice in data.get('nav_suspensions', []):
            if notice['scope'] != 'nav_publication' or not notice['source_url'].startswith('https://'):
                raise ValueError('only explicit NAV-publication notices may override calendars')
            if not notice['evidence_text'] or not notice['code'].isdigit() or len(notice['code']) != 6:
                raise ValueError('invalid NAV suspension evidence')
            if date.fromisoformat(notice['start']) > date.fromisoformat(notice['end']):
                raise ValueError('invalid NAV suspension interval')
            date.fromisoformat(notice['published_date'])
        data['fingerprint'] = hashlib.sha256(raw).hexdigest()
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DataError(f'Invalid publication calendar: {exc}') from exc


def freshness(observed: date, as_of: date, calendar_id: str = 'cn_nav', *,
              code: str | None = None, now: datetime | None = None, catalog: dict | None = None) -> dict:
    catalog = catalog or load_calendars()
    cal = catalog['calendars'][calendar_id]
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise DataError('Freshness evaluation requires a timezone-aware timestamp')
    # A source-local day counts only once it has ended. Historical as-of dates include that day.
    completed = min(as_of, now.astimezone(ZoneInfo(cal['timezone'])).date() - timedelta(days=1))
    if observed > as_of:
        raise DataError('Future observation cannot be used for freshness')
    sources = set()
    for year in range(observed.year, as_of.year + 1):
        spec = cal['years'].get(str(year))
        if spec is None:
            raise DataError(f'Publication calendar {calendar_id} does not cover {year}')
        sources.add(spec['source_url'])
    suspensions = [n for n in catalog.get('nav_suspensions', [])
                   if calendar_id == 'cn_nav' and n['code'] == code
                   and n['published_date'] <= as_of.isoformat()]
    lag = 0
    day = observed + timedelta(days=1)
    while day <= completed:
        spec = cal['years'][str(day.year)]
        notices = [n for n in suspensions if n['start'] <= day.isoformat() <= n['end']]
        sources.update(n['source_url'] for n in notices)
        if day.weekday() < 5 and day.isoformat() not in spec['closed_dates'] and not notices:
            lag += 1
        day += timedelta(days=1)
    calendar_lag = (as_of - observed).days
    return {
        'schema_version': 1, 'calendar_id': calendar_id,
        'calendar_version': catalog['version'], 'calendar_fingerprint': catalog['fingerprint'],
        'observed_date': observed.isoformat(), 'as_of': as_of.isoformat(),
        'evaluated_at': now.isoformat(timespec='seconds'), 'completed_through': completed.isoformat(),
        'calendar_lag_days': calendar_lag, 'publication_lag_days': lag,
        'max_publication_lag_days': MAX_PUBLICATION_DAYS,
        'status': 'stale' if lag > MAX_PUBLICATION_DAYS else ('calendar_adjusted' if calendar_lag > 7 else 'fresh'),
        'source_urls': sorted(sources),
    }


def require_fresh(observed: date, as_of: date, calendar_id: str = 'cn_nav', **kwargs) -> dict:
    result = freshness(observed, as_of, calendar_id, **kwargs)
    if result['status'] == 'stale':
        raise DataError(f"{kwargs.get('code') or calendar_id}: latest {observed}, "
                        f"{result['publication_lag_days']} publication workdays old (maximum 7)")
    return result


def validate_freshness(value: dict, observed: date, as_of: date, calendar_id: str, *, code=None) -> None:
    try:
        expected = require_fresh(observed, as_of, calendar_id, code=code,
                                 now=datetime.fromisoformat(value['evaluated_at']))
        if expected != value:
            raise DataError('Freshness evidence disagrees with calendar calculation')
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f'Invalid freshness evidence: {exc}') from exc


def freshness_text(value: dict) -> str:
    return (f"数据截至 {value['observed_date']}；落后 {value['calendar_lag_days']} 个自然日 / "
            f"{value['publication_lag_days']} 个发布工作日（上限 7）"
            + ('；按发布日历仍在时效内' if value['status'] == 'calendar_adjusted' else ''))


def revalidate_nav_suspensions(client, as_of: date):
    """Explicit, reviewed notice evidence must still match its source in this run."""
    catalog = load_calendars()
    for notice in catalog.get("nav_suspensions", []):
        if notice["published_date"] > as_of.isoformat() or notice["start"] > as_of.isoformat():
            continue
        expected = notice.get("source_sha256")
        if not expected or hashlib.sha256(client.get_bytes(notice["source_url"])).hexdigest() != expected:
            raise DataError(f"NAV publication notice changed or lacks verified content: {notice['code']}")
