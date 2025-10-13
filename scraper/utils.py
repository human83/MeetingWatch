import re, time, json, math, pytz
from datetime import datetime
from dateutil import parser as dateparser
from typing import Optional

MT_TZ = pytz.timezone("America/Denver")

def now_mt():
    return datetime.now(MT_TZ)

def to_mt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return MT_TZ.localize(dt)
    return dt.astimezone(MT_TZ)

def is_future(dt: datetime) -> bool:
    return to_mt(dt) > now_mt()

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def make_meeting(
    city_or_body: str,
    meeting_type: str,
    date: str,
    start_time_local: str,
    location: Optional[str],
    agenda_url: Optional[str],
    status: str,
    agenda_summary,
    notable_items=None,
    presenters_or_sponsors=None,
    documents=None,
    source_url: Optional[str] = None
):
    return {
        "city_or_body": city_or_body,
        "meeting_type": meeting_type,
        "date": date,
        "start_time_local": start_time_local,
        "location": location,
        "agenda_url": agenda_url,
        "status": status,
        "agenda_summary": agenda_summary,
        "notable_items": notable_items or [],
        "presenters_or_sponsors": presenters_or_sponsors or [],
        "documents": documents or [],
        "source_url": source_url
    }
