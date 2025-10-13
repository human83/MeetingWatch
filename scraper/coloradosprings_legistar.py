# scraper/coloradosprings_legistar.py
from __future__ import annotations
from datetime import datetime, timedelta
from typing import List, Dict
import requests
import pytz

from .utils import make_meeting, clean_text, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")

API = "https://webapi.legistar.com/v1/coloradosprings/events"

# We only want City Council Meetings and Work Sessions, in the future
WANTED_KEYWORDS = ("council",)   # body name contains this
WANTED_TYPES = ("work session", "meeting")  # meeting type contains one of these

def _is_wanted(body: str, mtg_type: str) -> bool:
    b = (body or "").lower()
    t = (mtg_type or "").lower()
    return any(k in b for k in WANTED_KEYWORDS) and any(k in t for k in WANTED_TYPES)

def _iso_date_mt(dt: datetime) -> str:
    return dt.astimezone(MT).strftime("%Y-%m-%d")

def parse_legistar() -> List[Dict]:
    today = datetime.now(MT).date()
    in_120 = today + timedelta(days=120)

    # OData filter: future window + only events with published titles/dates
    params = {
        "$filter": f"EventDate ge {today.isoformat()} and EventDate le {in_120.isoformat()}",
        "$orderby": "EventDate asc",
        "$top": 200,
    }
    r = requests.get(API, params=params, timeout=30)
    r.raise_for_status()
    items = r.json() or []

    meetings: List[Dict] = []
    for ev in items:
        body = (ev.get("EventBodyName") or "").strip()
        mtg_type = (ev.get("EventMeetingTypeName") or ev.get("EventMeetingType") or ev.get("EventAgendaStatusName") or "").strip()
        if not _is_wanted(body, mtg_type):
            continue

        # date & time
        date_str = (ev.get("EventDate") or "").split("T")[0]  # e.g., 2025-10-27T00:00:00
        if not date_str:
            continue
        # Legistar stores time in EventTime (minutes after midnight) sometimes; fall back to "Time TBD"
        mins = ev.get("EventTime", None)
        if isinstance(mins, int) and 0 <= mins < 24*60:
            h, m = divmod(mins, 60)
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            start_time_local = f"{h12}:{m:02d} {ampm}"
        else:
            start_time_local = "Time TBD"

        # links & location
        agenda_url = (ev.get("EventAgendaFile") or ev.get("EventAgendaUrl") or "").strip() or None
        location = clean_text(ev.get("EventLocation") or "")

        # summarize agenda (best effort)
        summary = []
        if agenda_url and agenda_url.lower().endswith(".pdf"):
            summary = summarize_pdf_if_any(agenda_url) or []

        meetings.append(
            make_meeting(
                city_or_body="Colorado Springs — City Council",
                meeting_type=mtg_type or "City Council Meeting",
                date=date_str,
                start_time_local=start_time_local,
                status="Scheduled",
                location=location or None,
                agenda_url=agenda_url,
                agenda_summary=summary,
                source="https://coloradosprings.legistar.com/Calendar.aspx",
            )
        )

    return meetings
