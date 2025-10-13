# scraper/coloradosprings_legistar.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict, Optional
import io
import re

import requests
import pytz
from pdfminer.high_level import extract_text

from .utils import make_meeting, clean_text, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
API = "https://webapi.legistar.com/v1/coloradosprings/events"

# --- Helpers -----------------------------------------------------------------

_TIME_RE = re.compile(
    r"\b(\d{1,2}:\d{2}\s?(?:A\.?M\.?|P\.?M\.?)|\d{1,2}\s?(?:A\.?M\.?|P\.?M\.?))\b",
    re.IGNORECASE,
)

def _fmt_minutes_after_midnight(mins: int) -> Optional[str]:
    """Legistar sometimes gives minutes after midnight as an int."""
    try:
        m = int(mins)
    except Exception:
        return None
    if 0 <= m < 24 * 60:
        h, mm = divmod(m, 60)
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{mm:02d} {ampm}"
    return None


def _extract_time_from_pdf_first_page(pdf_bytes: bytes) -> Optional[str]:
    """Pull text from the first page of a PDF and locate a meeting time."""
    try:
        txt = extract_text(io.BytesIO(pdf_bytes), maxpages=1) or ""
    except Exception:
        return None
    m = _TIME_RE.search(txt)
    if not m:
        return None
    # Normalize AM/PM punctuation (e.g., A.M. -> AM)
    t = re.sub(r"\.", "", m.group(1)).upper().replace("  ", " ").strip()
    # Force HH:MM for cases like '9 AM'
    if re.match(r"^\d{1,2}\s?(AM|PM)$", t):
        t = t.replace("AM", ":00 AM").replace("PM", ":00 PM")
    return t


def _time_from_agenda_pdf(url: str, session: requests.Session) -> Optional[str]:
    """Download an agenda PDF and try to extract the time from its first page."""
    if not url or not url.lower().endswith(".pdf"):
        return None
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        return _extract_time_from_pdf_first_page(r.content)
    except Exception:
        return None


def _is_wanted(body: str, mtg_type: str) -> bool:
    """Loosened filter: any body that contains 'council' (any meeting type)."""
    return "council" in (body or "").lower()

# --- Main --------------------------------------------------------------------

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

    session = requests.Session()
    headers = {"Accept": "application/json"}

    r = session.get(API, params=params, headers=headers, timeout=30)
    try:
        r.raise_for_status()
    except Exception:
        # Helpful when the OData filter is off or malformed
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

        # date (YYYY-MM-DD)
        date_str = (ev.get("EventDate") or "").split("T")[0]
        if not date_str:
            continue

        # 1) Try Legistar minutes-after-midnight field
        start_time_local = _fmt_minutes_after_midnight(ev.get("EventTime"))

        # 2) Fallback: look in agenda PDF first page
        agenda_url = (
            (ev.get("EventAgendaFile") or ev.get("EventAgendaUrl") or "").strip() or None
        )
        if not start_time_local and agenda_url:
            start_time_local = _time_from_agenda_pdf(agenda_url, session)

        # 3) Fallback of last resort
        if not start_time_local:
            start_time_local = "Time TBD"

        # links & location
        location = clean_text(ev.get("EventLocation") or "")

        # summarize agenda (best effort)
        summary = []
        if agenda_url and agenda_url.lower().endswith(".pdf"):
            summary = summarize_pdf_if_any(agenda_url) or []

        print("Keeping:", body, mtg_type, ev.get("EventDate"), "→", start_time_local)

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
