"""Small parsing helpers shared by source adapters."""

from datetime import date, datetime


def parse_source_date(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()
