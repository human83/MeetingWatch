# -*- coding: utf-8 -*-
"""
Trinidad, CO — City Council Regular Meeting scraper
Scrapes the server-rendered "list" calendar pages and follows each event's
day-view URL (which contains the modal HTML with title/time/description).
Only returns City Council Regular Meetings, today-forward.

Adapter exports:
  - parse_trinidad(months_ahead: int = 3) -> list[dict]
  - collect(months_ahead: int = 3) -> list[dict]
"""

from __future__ import annotations
import os
import re
import json
import logging
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from dateutil import tz

log = logging.getLogger("trinidad")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s [trinidad] %(message)s"))
    log.addHandler(_h)
log.propagate = False
log.setLevel(logging.INFO)

CAL_ID = os.getenv("TRINIDAD_CALENDAR_ID", "845")
BASE = "https://www.trinidad.co.gov/calendar.php"
MT_TZ = tz.gettz("America/Denver")

# --- Helpers ---------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"
})

TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I)

def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def _month_iter(start: date, months_ahead: int):
    y, m = start.year, start.month
    for _ in range(months_ahead):
        yield y, m
        # advance 1 month
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1

def _is_regular_meeting(text: str) -> bool:
    return "city council regular meeting".lower() in (text or "").lower()

def _fetch(url: str, **params) -> BeautifulSoup:
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def _parse_time_range(text: str) -> tuple[str | None, str | None]:
    m = TIME_RANGE_RE.search(text or "")
    if not m:
        return None, None
    return m.group(1), m.group(2)

def _parse_hhmm_ampm_to_24h(d: date, tstr: str) -> datetime | None:
    try:
        dt = datetime.strptime(f"{d.isoformat()} {tstr.upper()}", "%Y-%m-%d %I:%M %p")
        return dt.replace(tzinfo=MT_TZ)
    except Exception:
        return None

def _extract_day_params(href: str) -> tuple[int, int, int]:
    """
    href looks like: calendar.php?view=day&month=11&day=05&year=2025&calendar=&id=845
    """
    q = parse_qs(urlparse(href).query)
    year = int(q.get("year", ["1970"])[0])
    month = int(q.get("month", ["1"])[0])
    day = int(q.get("day", ["1"])[0])
    return year, month, day

# --- Core scraping ---------------------------------------------------------

def _gather_candidates(months_ahead: int = 3) -> list[str]:
    """
    Returns day-view URLs (&id=...) for City Council Regular Meeting items
    discovered from the list view.
    """
    today = datetime.now(MT_TZ).date()
    out: list[str] = []

    for y, m in _month_iter(today.replace(day=1), months_ahead):
        list_params = dict(view="list", month=m, day=1, year=y, calendar=CAL_ID)
        log.info("[trinidad] fetching month: %s?view=list&month=%s&day=1&year=%s&calendar=%s",
                 BASE, m, y, CAL_ID)
        soup = _fetch(BASE, **list_params)

        # list view has <a href="calendar.php?view=day&...&id=###">Title...</a>
        links = soup.select('a[href*="view=day"][href*="id="]')
        if not links:
            month_params = dict(view="month", month=m, day=1, year=y, calendar=CAL_ID)
            mdoc = _fetch(BASE, **month_params)
            for a in mdoc.select("a.fc-day-grid-event"):
                title = _clean_text(a.get_text(" "))
                if not _is_regular_meeting(title):
                    continue
                href = a.get("href") or ""
                if href and "view=day" in href and "id=" in href:
                    links.append(a)
        for a in links:
            title = _clean_text(a.get_text(" ").strip())
            href = a.get("href") or ""
            if not href:
                continue
            if not _is_regular_meeting(title):
                continue

            # Filter to today-forward using the date embedded in the href
            yy, mm, dd = _extract_day_params(href)
            d = date(yy, mm, dd)
            if d < today:
                continue

            out.append(urljoin(BASE, href))

    return out

def _extract_event(day_url: str) -> dict | None:
    """
    Parse the day view (with &id=...) to pull title, time range, body text.
    """
    soup = _fetch(day_url)

    # Title e.g., <h2 id="modal-event-title">City Council Regular Meeting</h2>
    title_tag = soup.select_one("#modal-event-title")
    title = _clean_text(title_tag.get_text()) if title_tag else ""

    # Time ribbon area: e.g., "06:00 PM - 07:00 PM"
    header = soup.select_one(".modal-event-header") or soup.select_one(".modal-body")
    header_text = _clean_text(header.get_text(" ")) if header else ""
    start_s, end_s = _parse_time_range(header_text)

    # Agenda / description body
    desc = soup.select_one("#modal-event-description") or soup.select_one(".modal-event-body")
    agenda_text = _clean_text(desc.get_text("\n")) if desc else ""

    # Location (best-effort) — sometimes embedded in the body lines
    loc = ""
    for line in (agenda_text or "").splitlines():
        l = line.strip()
        if not l:
            continue
        if "Chambers" in l or "City Hall" in l or "Animas" in l or "Street" in l:
            loc = l
            break

    # Date comes from URL params (most reliable)
    yy, mm, dd = _extract_day_params(day_url)
    event_date = date(yy, mm, dd)

    # Build start_dt if possible
    start_dt = None
    if start_s:
        start_dt = _parse_hhmm_ampm_to_24h(event_date, start_s)
    if start_dt is None:
        # Fallback 6:00 PM local if time not parsed (calendar often uses 6–7 PM)
        start_dt = datetime(event_date.year, event_date.month, event_date.day, 18, 0, tzinfo=MT_TZ)

    # Build result
    ev_id = parse_qs(urlparse(day_url).query).get("id", [""])[0]
    result = {
        "city": "Trinidad",
        "title": "City Council Regular Meeting",
        "start_dt": start_dt.isoformat(),
        "end_dt": None,  # can be added if we want to parse end_s
        "date_str": start_dt.astimezone(MT_TZ).strftime("%A, %B %-d, %Y"),
        "location": loc or "City Council Chambers, City Hall (Trinidad, CO)",
        "agenda_text": agenda_text,
        "agenda_bullets": [],  # summarize step will fill if/when we ever get PDFs/text
        "source_url_detail_url": day_url,
        "event_id": ev_id,
    }
    return result

def collect(months_ahead: int = 3) -> list[dict]:
    candidates = _gather_candidates(months_ahead=months_ahead)
    log.info("[trinidad] candidates seen: %d", len(candidates))

    meetings: list[dict] = []
    for url in candidates:
        try:
            mtg = _extract_event(url)
            if mtg:
                meetings.append(mtg)
        except Exception as e:
            log.warning("[trinidad] failed to parse %s: %r", url, e)

    # Final safety filter: today-forward
    out: list[dict] = []
    today = datetime.now(MT_TZ).date()
    for m in meetings:
        try:
            d = datetime.fromisoformat(m["start_dt"]).astimezone(MT_TZ).date()
            if d >= today:
                out.append(m)
        except Exception:
            continue

    log.info("[trinidad] accepted %d City Council Regular meeting(s)", len(out))
    return out

# --- Adapter for your pipeline --------------------------------------------

__all__ = ["parse_trinidad", "collect"]

def parse_trinidad(months_ahead: int = 3) -> list[dict]:
    """
    Adapter for scraper.main: return a list[dict] of Trinidad meetings
    from today forward. `months_ahead` is kept for parity with other parsers.
    """
    return collect(months_ahead=months_ahead)

if __name__ == "__main__":
    import sys
    months = int(os.getenv("TRINIDAD_MONTHS_AHEAD", "3"))
    print(json.dumps(parse_trinidad(months_ahead=months), indent=2, ensure_ascii=False))
