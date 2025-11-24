# scraper/coloradosprings_legistar.py
from __future__ import annotations

import os
"""
Colorado Springs Legistar scraper (v1.3.1)
------------------------------------------
- Fetches upcoming events (next 120 days) from Legistar Web API
- Normalizes meeting start time:
    1) Use EventTime if present (string like "6:00 PM" or int minutes-after-midnight)
    2) Fallback: fetch agenda and parse first pages (up to 3) for a time string (no longer requires .pdf in URL)
    3) Else: "Time TBD"
- Summarizes the agenda using utils.summarize_pdf_if_any (OpenAI-backed with graceful fallback)
- Filters boilerplate from bullets; if we filter everything, keep a soft-fallback of a few cleaned lines
- Returns list of meeting dicts shaped by utils.make_meeting
"""
# Bullet limit follows repo-wide setting; falls back to 16 if not set
BULLET_LIMIT = int(os.getenv("PDF_SUMMARY_MAX_BULLETS", "16"))


from datetime import datetime, timedelta
from typing import List, Dict, Optional
import io
import re
import logging

import requests
import pytz

from .utils import make_meeting, clean_text, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
API = "https://webapi.legistar.com/v1/coloradosprings/events"
UA = (
    "MeetingWatch/1.3.1 (+https://human83.github.io/MeetingWatch/; contact: meetingwatch@example.com) "
    "Python-requests"
)

_LOG = logging.getLogger(__name__)

# --- Helpers -----------------------------------------------------------------

# Matches times like "9:00 AM", "9 AM", "12:30 P.M.", "21:05", etc.
_TIME_RE = re.compile(
    r"\b("
    r"(?:[01]?\d|2[0-3]):[0-5]\d(?:\s?[AP]\.?M\.?)?"           # 0-23:MM optionally AM/PM
    r"|(?:[1-9]|1[0-2])(?:\s?:\s?[0-5]\d)?\s?[AP]\.?M\.?"      # 1..12, optional :MM + AM/PM
    r")\b",
    re.IGNORECASE,
)

def _normalize_ampm(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\.", "", s, flags=re.IGNORECASE)  # A.M. -> AM
    s = re.sub(r"\s+", " ", s)
    return s.upper()

def _fmt_minutes_after_midnight(m: int) -> Optional[str]:
    if m is None:
        return None
    try:
        m = int(m)
    except Exception:
        return None
    if 0 <= m < 24 * 60:
        h, mm = divmod(m, 60)
        ampm = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{mm:02d} {ampm}"
    return None

def _parse_time_field(raw: Optional[object]) -> Optional[str]:
    """
    Accepts EventTime which can be either a string (e.g., '6:00 PM') or an int (minutes).
    Returns 'H:MM AM/PM' or None.
    """
    if raw is None:
        return None

    # int minutes-after-midnight
    if isinstance(raw, int):
        return _fmt_minutes_after_midnight(raw)

    # numeric string minutes ('1080') – rare, but handle
    if isinstance(raw, str) and raw.isdigit():
        return _fmt_minutes_after_midnight(int(raw))

    # String clock like '6:00 PM' or '18:30'
    if isinstance(raw, str):
        s = raw.strip()
        m = _TIME_RE.search(s)
        if not m:
            return None
        t = _normalize_ampm(m.group(1))
        # If it's 24h (e.g., 18:30) convert to AM/PM
        if re.match(r"^(?:[01]?\d|2[0-3]):[0-5]\d$", t):
            hh, mm = map(int, t.split(":"))
            ampm = "AM" if hh < 12 else "PM"
            h12 = hh % 12 or 12
            return f"{h12}:{mm:02d} {ampm}"
        # If it's '9 PM' add :00
        if re.match(r"^(?:[1-9]|1[0-2])\s?[AP]M$", t):
            return t.replace("AM", ":00 AM").replace("PM", ":00 PM")
        # Already like '6:00 AM'
        if re.match(r"^(?:[1-9]|1[0-2]):[0-5]\d\s?[AP]M$", t):
            t = re.sub(r"\s+", " ", t)
            return t
    return None

def _extract_time_from_pdf_first_pages(pdf_bytes: bytes, *, maxpages: int = 3) -> Optional[str]:
    """Pull text from the first N pages of a PDF and locate a meeting time."""
    try:
        from pdfminer.high_level import extract_text  # lazy import
        txt = extract_text(io.BytesIO(pdf_bytes), maxpages=maxpages) or ""
    except Exception:
        return None

    m = _TIME_RE.search(txt)
    if not m:
        return None
    t = _normalize_ampm(m.group(1))
    if re.match(r"^(?:[1-9]|1[0-2])\s?[AP]M$", t):
        t = t.replace("AM", ":00 AM").replace("PM", ":00 PM")
    return t

def _time_from_agenda_pdf(url: str, session: requests.Session) -> Optional[str]:
    """
    Download agenda and try to extract first-pages time.
    No longer requires '.pdf' in the URL; relies on server response.
    """
    if not url:
        return None
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "application/pdf" not in ctype and ".pdf" not in url.lower():
            return None
        return _extract_time_from_pdf_first_pages(r.content, maxpages=3)
    except Exception:
        return None

def _is_wanted(body: str, mtg_type: str) -> bool:
    return "council" in (body or "").lower() or "council" in (mtg_type or "").lower()

# Boilerplate/headers we don't want as bullets
_DROP_PATTERNS = [
    r"^city of colorado springs\b",
    r"\bcouncil work session\b",
    r"\bwork session meeting agenda\b",
    r"\bagenda\b.*final",
    r"\bagenda (may|subject to)",
    r"channel\s*18|livestream|televised|broadcast",
    r"americans? with disabilities act|ADA\b|auxiliary aid|accessibilit|48 hours before the scheduled event",
    r"\bpublic comment\b",
    r"\bcall to order\b|\broll call\b|\bpledge of allegiance\b|\bapproval of (the )?minutes\b|\badjourn?ment\b",
    r"documents created by third parties may not meet all accessibilit",
    r"participate in this meeting should make the request as soon as",
    r"\bcity hall\b$",
    r"^criteria\.?$",
    # Drop date/time/zip-only crumbs
    r"^\d{5}(?:-\d{4})?$",
    r"^\d{1,2}:\d{2}\s?(?:AM|PM)\s*$",
    r"^(?:[A-Za-z]+,\s*)?(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}\s*$",
    r"^\d{4}-\d{2}-\d{2}$",
]
_DROP_RE = re.compile("|".join(_DROP_PATTERNS), re.IGNORECASE)
def _filter_bullets(bullets: List[str], *, limit: int = BULLET_LIMIT) -> List[str]:
    """
    Keep only newsy, self-contained lines:
    - drop headers/boilerplate (_DROP_RE)
    - drop very short fragments (< 25 chars) unless they contain digits or '$'
    - drop single-word lines
    - de-duplicate
    If everything gets filtered, keep a soft-fallback of up to 6 cleaned lines
    that aren't obvious boilerplate.
    """
    out: List[str] = []
    seen = set()
    for b in bullets or []:
        line = clean_text(b)
        if not line:
            continue
        if _DROP_RE.search(line):
            continue
        words = line.split()
        if len(words) < 3 and not re.search(r"[\d$]", line):
            continue
        if len(line) < 25 and not re.search(r"[\d$]", line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break

    if out:
        return out

    # Soft fallback: keep up to `limit` cleaned non-obvious lines
    soft: List[str] = []
    for b in bullets or []:
        line = clean_text(b)
        if not line:
            continue
        if re.search(r"ADA|auxiliary aid|channel\s*18|broadcast|livestream|televised", line, re.IGNORECASE):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        soft.append(line)
        if len(soft) >= limit:
            break
    return soft

# --- Main --------------------------------------------------------------------

def parse_legistar() -> List[Dict]:
    """
    Fetch events from Legistar, enrich with derived time and agenda summary.
    Returns a list of dicts created by utils.make_meeting.
    """
    today = datetime.now(MT).date()
    in_120 = today + timedelta(days=120)
    
    # Collect from *today* at 00:00 forward (no past days)
    start = today.strftime("%Y-%m-%dT00:00:00")
    end = in_120.strftime("%Y-%m-%dT23:59:59")

    params = {
        "$filter": f"EventDate ge datetime'{start}' and EventDate le datetime'{end}'",
        "$orderby": "EventDate asc",
        "$top": 200,
    }

    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": UA})

    r = session.get(API, params=params, timeout=30)
    r.raise_for_status()
    items = r.json() or []

    _LOG.info("Legistar: fetched %d events (URL: %s)", len(items), getattr(r, "url", API))

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

        date_str = (ev.get("EventDate") or "").split("T")[0]
        if not date_str:
            continue
        # Guardrail: enforce today-and-future only
        try:
            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if event_date < today:
                continue
        except Exception:
            continue

        # 1) EventTime (string '6:00 PM' or int minutes) if present
        start_time_local = _parse_time_field(ev.get("EventTime"))

        # Agenda URL
        agenda_url = (
            (ev.get("EventAgendaFile") or ev.get("EventAgendaUrl") or "").strip() or None
        )

        # 2) Fallback: look in agenda content for a time
        if not start_time_local and agenda_url:
            start_time_local = _time_from_agenda_pdf(agenda_url, session)

        if not start_time_local:
            start_time_local = "Time TBD"

        # Location
        location = clean_text(ev.get("EventLocation") or "") or None

        # Summarize agenda (best effort, handles non-PDF and failures gracefully)
        summary: List[str] = []
        if agenda_url:
            try:
                raw_bullets = summarize_pdf_if_any(agenda_url) or []
                summary = _filter_bullets(raw_bullets, limit=BULLET_LIMIT)
            except Exception as e:
                _LOG.warning("Agenda summary failed for %s: %s", agenda_url, e)

        meetings.append(
            make_meeting(
                city_or_body="Colorado Springs — City Council",
                meeting_type=mtg_type or "City Council Meeting",
                date=date_str,
                start_time_local=start_time_local,
                status="Scheduled",
                location=location,
                agenda_url=agenda_url,
                agenda_summary=summary,
                source="https://coloradosprings.legistar.com/Calendar.aspx",
            )
        )

    return meetings
