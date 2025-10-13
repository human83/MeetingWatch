# scraper/coloradosprings_legistar.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict

import requests
import pytz

from .utils import make_meeting, clean_text, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
API = "https://webapi.legistar.com/v1/coloradosprings/events"


def _is_wanted(body: str, mtg_type: str) -> bool:
    """Loosened filter: any body that contains 'council' (any meeting type)."""
    return "council" in (body or "").lower()


def parse_legistar() -> List[Dict]:
    today = datetime.now(MT).date()
    in_120 = today + timedelta(days=120)

    # OData window, Legistar wants datetime'YYYY-MM-DDTHH:MM:SS'
    start = today.strftime("%Y-%m-%dT00:00:00")
    end = in_120.strftime("%Y-%m-%dT23:59:59")

    params = {
        "$filter": f"EventDate ge datetime'{start}' and EventDate le datetime'{end}'",
        "$orderby": "EventDate asc",
        "$top": 200,
    }

    headers = {"Accept": "application/json"}
    r = requests.get(API, params=params, headers=headers, timeout=30)

    try:
        r.raise_for_status()
    except Exception:
        # Helpful when the OData filter is off
        print("Legistar error:", r.status_code, r.text[:300], "URL:", r.url)
        raise

    items = r.json() or []
    print(f"Legistar: fetched {len(items)} events (URL: {r.url})")

    meetings: List[Dict] = []
    for ev in items:
        body = (ev.get("EventBodyName") or "").strip()
        mtg_type = (
            ev.get("EventMeetingTypeName")
            or ev.get("EventMeetingType")
            or ev.get("EventAgendaStatusName")
            or ""
        ).strip()

        if not _is_wanted(body, mtg_type):
            continue

        # date
        date_str = (ev.get("EventDate") or "").split("T")[0]
        if not date_str:
            continue

        # time (Legistar stores minutes after midnight as an integer)
        mins = ev.get("EventTime", None)
        if isinstance(mins, int) and 0 <= mins < 24 * 60:
            h, m = divmod(mins, 60)
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12 or 12
            start_time_local = f"{h12}:{m:02d} {ampm}"
        else:
            start_time_local = "Time TBD"

        # links & location
        agenda_url = (
            (ev.get("EventAgendaFile") or ev.get("EventAgendaUrl") or "").strip()
            or None
        )
        location = clean_text(ev.get("EventLocation") or "")

        # summarize agenda (best effort)
        summary = []
        if agenda_url and agenda_url.lower().endswith(".pdf"):
            summary = summarize_pdf_if_any(agenda_url) or []

        print("Keeping:", body, mtg_type, ev.get("EventDate"))

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
