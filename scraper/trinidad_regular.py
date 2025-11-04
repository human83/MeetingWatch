# scraper/trinidad_regular.py
# Trinidad, CO — City Council Regular Meetings (today forward)
# Works without PDFs by reading the event modal on the calendar page.

from __future__ import annotations
import re
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, date, time, timedelta
from typing import Optional, List, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
import pytz

# ---------- Config ----------
BASE = "https://www.trinidad.co.gov"
CAL_PATH = "/calendar.php"
MONTH_URL = f"{BASE}{CAL_PATH}"
MT_TZ = pytz.timezone("America/Denver")

TITLE_OK = re.compile(r"\bcity\s*council\b.*\bregular\b", re.I)
TITLE_EXCLUDE = re.compile(r"\b(work\s*session|special|retreat)\b", re.I)

# Matches "06:00 PM - 07:00 PM" or with en dash
TIME_RANGE_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I
)

HEADERS = {
    "User-Agent": "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"
}

log = logging.getLogger("trinidad_regular")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


# ---------- Data model ----------
@dataclass
class TrinidadMeeting:
    city: str
    title: str
    start_dt: Optional[str]
    end_dt: Optional[str]
    date_str: str
    location: Optional[str]
    agenda_text: Optional[str]
    agenda_bullets: List[str]
    source_url: str
    event_id: Optional[str]


# ---------- Helpers ----------
def _month_pages(start_dt: date, months_ahead: int = 3):
    y, m = start_dt.year, start_dt.month
    for k in range(months_ahead + 1):
        mm = m + k
        yk = y + (mm - 1) // 12
        mk = (mm - 1) % 12 + 1
        yield yk, mk


def _build_month_url(y: int, m: int) -> str:
    qs = dict(view="month", month=m, day=1, year=y, calendar="")
    return f"{MONTH_URL}?{urlencode(qs)}"


def _full(href: str) -> str:
    return urljoin(BASE, href)


def _parse_date_from_href(href: str) -> Optional[date]:
    # calendar.php?view=day&month=11&day=05&year=2025&calendar=&id=845
    try:
        q = parse_qs(urlparse(href).query)
        y = int((q.get("year") or ["0"])[0])
        m = int((q.get("month") or ["0"])[0])
        d = int((q.get("day") or ["0"])[0])
        return date(y, m, d)
    except Exception:
        return None


def _nearest_cell_date(a_tag) -> Optional[date]:
    """Fallback: find the td with data-date around the anchor."""
    cell = a_tag.find_parent(lambda tag: tag.name in ("td", "div") and tag.get("data-date"))
    if cell:
        try:
            return dtparse.parse(cell.get("data-date")).date()
        except Exception:
            return None
    return None


def _extract_modal_bits(html: str) -> Tuple[str, str, str, Optional[str]]:
    """
    Returns (title, time_badge, body_text, location)
    """
    s = BeautifulSoup(html, "html.parser")
    modal = s.select_one("div#event-modal, .event-modal, .modal.fade, .modal")
    if not modal:
        modal = s

    # title
    h = modal.select_one("h1, h2, .event-title")
    title = (h.get_text(" ", strip=True) if h else "").strip()

    # time badge
    badge_str = ""
    # common places for the purple time label
    for sel in [".badge", ".time", ".event-time", ".time-badge", ".event-details-time"]:
        el = modal.select_one(sel)
        if el and TIME_RANGE_RE.search(el.get_text(" ", strip=True)):
            badge_str = el.get_text(" ", strip=True)
            break
    if not badge_str:
        # fallback: search anywhere in modal text
        m = TIME_RANGE_RE.search(modal.get_text(" ", strip=True))
        if m:
            badge_str = m.group(0)

    # body text
    body_el = modal.select_one(".modal-body, .event-details, .event-content, .content")
    body_text = (body_el.get_text("\n", strip=True) if body_el else modal.get_text("\n", strip=True))

    # location (best-effort)
    location = None
    # look for explicit "in City Council Chambers at City Hall..." pattern
    mloc = re.search(
        r"in\s+(.+?),\s*Trinidad,\s*Colorado\b", body_text, re.I
    )
    if mloc:
        location = mloc.group(1).strip()
    else:
        # look for a "Location" label
        lab = modal.find(string=re.compile(r"Location", re.I))
        if lab and lab.parent:
            location = lab.parent.get_text(" ", strip=True)

    return title, badge_str, body_text, location


def _build_dt(dt_day: date, time_badge: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    m = TIME_RANGE_RE.search(time_badge or "")
    if m:
        try:
            st = dtparse.parse(m.group(1)).time()
            et = dtparse.parse(m.group(2)).time()
            return (
                MT_TZ.localize(datetime.combine(dt_day, st)),
                MT_TZ.localize(datetime.combine(dt_day, et)),
            )
        except Exception:
            pass
    # fallback: 6–7 PM if site omits the badge (seen occasionally)
    try:
        st = time(18, 0)
        et = time(19, 0)
        return (
            MT_TZ.localize(datetime.combine(dt_day, st)),
            MT_TZ.localize(datetime.combine(dt_day, et)),
        )
    except Exception:
        return None, None


def _bulletize(text: str) -> List[str]:
    # Loose split on numbers/dashes/bullets/newlines
    chunks = re.split(r"(?:\n|\r|^|\u2022|•|–|-|\u2013|\u2014|\s\d+[.)])\s*", text)
    bullets = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        if re.search(r"regular meeting.*will be held", c, re.I):
            continue
        bullets.append(re.sub(r"\s+", " ", c))
    # de-dup
    seen = set()
    out = []
    for b in bullets:
        k = b.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(b)
    return out[:12]


# ---------- Collector ----------
def collect(months_ahead: int = 3) -> List[dict]:
    today = datetime.now(MT_TZ).date()
    meetings: List[TrinidadMeeting] = []

    with requests.Session() as s:
        s.headers.update(HEADERS)
        accepted = 0

        for y, m in _month_pages(today, months_ahead=months_ahead):
            month_url = _build_month_url(y, m)
            log.info(f"[trinidad] fetching month: {month_url}")
            r = s.get(month_url, timeout=30)
            r.raise_for_status()
            doc = BeautifulSoup(r.text, "html.parser")

            # FullCalendar anchors used in month/list views
            anchors = doc.select(
                "a.fc-day-grid-event, a.fc-list-item, a.fc-h-event, .fc-content a"
            )
            for a in anchors:
                # title on cell
                title_el = a.select_one(".fc-title") or a
                raw_title = title_el.get_text(" ", strip=True) if title_el else ""
                if not raw_title:
                    continue

                # include regular meetings only, exclude work sessions etc
                if not TITLE_OK.search(raw_title) or TITLE_EXCLUDE.search(raw_title):
                    continue

                href = a.get("href") or ""
                if not href:
                    # sometimes a clickable area is wrapped differently
                    continue
                href = _full(href)

                # date from link (preferred) or from the cell container
                dt_day = _parse_date_from_href(href) or _nearest_cell_date(a)
                if not dt_day:
                    continue

                # fetch the modal by visiting the day/id URL
                rr = s.get(href, timeout=30)
                rr.raise_for_status()

                modal_title, time_badge, body_text, location = _extract_modal_bits(rr.text)
                start_dt, end_dt = _build_dt(dt_day, time_badge)
                if not start_dt:
                    continue

                # future-only
                if start_dt.date() < today:
                    continue

                # event id from href, if present
                q = parse_qs(urlparse(href).query)
                ev_id = (q.get("id") or [None])[0]

                mtg = TrinidadMeeting(
                    city="Trinidad, CO",
                    title="City Council Regular Meeting",
                    start_dt=start_dt.isoformat(),
                    end_dt=end_dt.isoformat() if end_dt else None,
                    date_str=start_dt.astimezone(MT_TZ).strftime("%A, %B %-d, %Y"),
                    location=location,
                    agenda_text=body_text,
                    agenda_bullets=_bulletize(body_text or ""),
                    source_url=href,
                    event_id=ev_id,
                )
                meetings.append(mtg)
                accepted += 1

        log.info(f"[trinidad] accepted {accepted} City Council Regular meeting(s)")

    # Final safety filter (redundant but harmless)
    out = []
    for m in meetings:
        if m.start_dt:
            d = dtparse.isoparse(m.start_dt).astimezone(MT_TZ).date()
            if d >= today:
                out.append(asdict(m))
    return out


# --- Adapter for your pipeline (kept the same) ---
__all__ = ["parse_trinidad", "collect"]

def parse_trinidad(months_ahead: int = 3):
    """
    Adapter for scraper.main: return a list[dict] of Trinidad meetings
    from today forward. 'months_ahead' is kept for parity with other parsers.
    """
    return collect(months_ahead=months_ahead)


if __name__ == "__main__":
    import os
    months = int(os.getenv("TRINIDAD_MONTHS_AHEAD", "3"))
    print(json.dumps(parse_trinidad(months_ahead=months), indent=2, ensure_ascii=False))
