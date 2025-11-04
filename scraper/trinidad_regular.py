# scraper/trinidad_regular.py
import re
import sys
import json
import time
import html
import pytz
import math
import logging
import calendar
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse

MT_TZ = pytz.timezone("America/Denver")
BASE = "https://www.trinidad.co.gov"
MONTH_URL = f"{BASE}/calendar.php"

TITLE_OK = re.compile(r"\bcity\s+council\s+regular\s+meeting\b", re.I)
TITLE_EXCLUDE = re.compile(r"\b(work\s*session|worksession|special|retreat)\b", re.I)

HEADERS = {
    "User-Agent": "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"
}

log = logging.getLogger("trinidad_regular")
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

@dataclass
class TrinidadMeeting:
    city: str
    title: str
    start_dt: str
    end_dt: str | None
    date_str: str
    location: str | None
    agenda_text: str | None
    agenda_bullets: list[str]
    source_url: str
    event_id: int

def _month_pages(start_dt: date, months_ahead: int = 3):
    """Yield (y, m) tuples from start month through months_ahead inclusive."""
    y, m = start_dt.year, start_dt.month
    for k in range(months_ahead + 1):
        mm = m + k
        yk = y + (mm - 1) // 12
        mk = (mm - 1) % 12 + 1
        yield yk, mk

def _build_month_url(y: int, m: int):
    # Month view with day=1 anchor to keep layout predictable
    qs = dict(view="month", month=m, day=1, year=y, calendar="")
    return f"{MONTH_URL}?{urlencode(qs)}"

def _extract_event_ids(month_html: str):
    """Return list of (event_id, anchor_text, anchor_href) matching title filters."""
    soup = BeautifulSoup(month_html, "html.parser")
    out = []
    for a in soup.select("a.fc-day-grid-event, a.fc-list-item"):
        title_el = a.select_one(".fc-title")
        if not title_el:
            # some CivicEngage themes put title directly as text
            title_txt = a.get_text(" ", strip=True)
        else:
            title_txt = title_el.get_text(" ", strip=True)

        if not title_txt:
            continue

        if not TITLE_OK.search(title_txt):
            continue
        if TITLE_EXCLUDE.search(title_txt):
            continue

        href = a.get("href") or ""
        # If not present, the page usually rewrites its own URL when clicked; try data-id pattern too
        m = re.search(r"[?&]id=(\d+)", href)
        if not m:
            # Try to find a sibling script or data attribute – some themes store data-id on parent container
            data = a.get("data-id") or a.get("data-event-id") or ""
            if data and data.isdigit():
                ev_id = int(data)
            else:
                # As a last resort, ignore (will be rare)
                continue
        else:
            ev_id = int(m.group(1))

        # Normalize href to include id
        if m:
            full = urljoin(MONTH_URL, href)
        else:
            # Build a canonical day view URL with id for the modal
            full = f"{MONTH_URL}?view=day&month=1&day=1&year=1900&id={ev_id}"

        out.append((ev_id, title_txt.strip(), full))
    return out

def _fetch_modal(ev_id: int, any_month_url: str, s: requests.Session):
    """
    Request the same calendar page but with ?id=<ev_id>.
    Many CivicEngage themes render the modal server-side when id is present.
    """
    # Replace/add id in the provided month URL to keep context consistent
    parts = list(urlparse(any_month_url))
    qs = parse_qs(parts[4])
    qs["id"] = [str(ev_id)]
    parts[4] = urlencode({k: v[-1] for k, v in qs.items()})
    detail_url = urlunparse(parts)

    r = s.get(detail_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return detail_url, r.text

def _parse_modal(detail_html: str):
    """
    Return dict with start_dt, end_dt, location, body_text.
    We read the visible modal block.
    """
    soup = BeautifulSoup(detail_html, "html.parser")
    # Many CivicEngage themes use a container with class 'modal fade' and role 'dialog'
    modal = soup.select_one('div[id*="event-modal"], div.modal.fade#event-modal, div.event-modal, div.modal.fade')
    if not modal:
        # Fall back: the page may inline the event text somewhere else; grab by common card styles
        modal = soup

    # Header/title may repeat; we’ll parse time/location/body from the content column.
    # Time range appears near the top in a label-like element.
    time_label = None
    for sel in [
        ".modal .time", ".event-details-time", ".event-details .badge", ".modal .badge",
        ".modal .event-time", ".event-time", ".time-badge"
    ]:
        el = modal.select_one(sel)
        if el:
            time_label = el.get_text(" ", strip=True)
            break
    if not time_label:
        # The screenshot shows the purple label "06:00 PM - 07:00 PM"
        # It may be just the first strong/span before the H2; grab any HH:MM AM - HH:MM PM
        m = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)\s*[–-]\s*(\d{1,2}:\d{2}\s*[AP]M)", modal.get_text(" ", strip=True))
        time_label = m.group(0) if m else None

    # Location: look for "City Council Chambers at City Hall, 135 N. Animas Street, ..." pattern
    text_all = modal.get_text("\n", strip=True)
    loc = None
    loc_match = re.search(r"in\s+(.+?),\s*Trinidad,\s*Colorado\b", text_all, re.I)
    if loc_match:
        loc = loc_match.group(1).strip()
    else:
        # fallback: a 'Location' label block
        lab = modal.find(string=re.compile(r"Location", re.I))
        if lab and lab.parent:
            loc = lab.parent.get_text(" ", strip=True)

    # Pull the main body text (after the H1/H2) – avoid nav/footer
    body_candidates = []
    for sel in [".modal-body", ".event-details", ".event-content", ".modal .content"]:
        for el in modal.select(sel):
            body_candidates.append(el.get_text("\n", strip=True))
    body_text = max(body_candidates, key=len) if body_candidates else text_all

    # Extract a calendar date if present; otherwise we’ll fill from month/day circle text
    # The body often starts with “The Regular Meeting ... will be held on Wednesday, November 5, 2025, at 6:00 P.M.”
    dt = None
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}",
        body_text,
        re.I,
    )
    if m:
        try:
            dt = dtparse.parse(m.group(0)).date()
        except Exception:
            dt = None

    # Parse time(s)
    start_dt = end_dt = None
    if dt and time_label:
        tm = re.findall(r"(\d{1,2}:\d{2}\s*[AP]M)", time_label, re.I)
        if tm:
            start_dt = MT_TZ.localize(datetime.combine(dt, dtparse.parse(tm[0]).time()))
            if len(tm) >= 2:
                end_dt = MT_TZ.localize(datetime.combine(dt, dtparse.parse(tm[1]).time()))

    return {
        "start_dt": start_dt.isoformat() if start_dt else None,
        "end_dt": end_dt.isoformat() if end_dt else None,
        "location": loc,
        "body_text": body_text,
    }

def _bulletize(text: str) -> list[str]:
    # Split on numbered list patterns and common separators
    chunks = re.split(r"(?:\n|\r|\u2022|•|–|-|\u2013|\u2014)\s*", text)
    bullets = []
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        # collapse doubles and remove “The Regular Meeting … will be held …” boilerplate
        if re.search(r"regular meeting.*will be held", c, re.I):
            continue
        # keep short, clean lines
        bullets.append(re.sub(r"\s+", " ", c))
    # de-dupe while keeping order
    seen = set()
    out = []
    for b in bullets:
        if b.lower() in seen:
            continue
        seen.add(b.lower())
        out.append(b)
    # trim to something sane for your site (adjust if you have a global cap)
    return out[:12]

def collect(months_ahead: int = 3) -> list[dict]:
    today = datetime.now(MT_TZ).date()
    meetings: list[TrinidadMeeting] = []
    with requests.Session() as s:
        for y, m in _month_pages(today, months_ahead=months_ahead):
            url = _build_month_url(y, m)
            log.info(f"Fetching month: {url}")
            r = s.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()

            for ev_id, title, href in _extract_event_ids(r.text):
                try:
                    detail_url, detail_html = _fetch_modal(ev_id, url, s)
                    parsed = _parse_modal(detail_html)

                    # If date still unknown, infer from month grid cell (use the id page content date circle if present)
                    start_iso = parsed["start_dt"]
                    # Skip past events
                    if start_iso:
                        start_d = dtparse.isoparse(start_iso).astimezone(MT_TZ).date()
                        if start_d < today:
                            continue

                    bullets = _bulletize(parsed["body_text"] or "")
                    mtg = TrinidadMeeting(
                        city="Trinidad, CO",
                        title="City Council Regular Meeting",
                        start_dt=parsed["start_dt"],
                        end_dt=parsed["end_dt"],
                        date_str=(dtparse.isoparse(parsed["start_dt"]).astimezone(MT_TZ).strftime("%A, %B %-d, %Y")
                                  if parsed["start_dt"] else ""),
                        location=parsed["location"],
                        agenda_text=parsed["body_text"],
                        agenda_bullets=bullets,
                        source_url=detail_url,
                        event_id=ev_id,
                    )
                    meetings.append(mtg)
                except Exception as e:
                    log.warning(f"Failed parsing event {ev_id}: {e}")

    # Final safety filter: only from today forward
    out = []
    for m in meetings:
        if m.start_dt:
            d = dtparse.isoparse(m.start_dt).astimezone(MT_TZ).date()
            if d >= today:
                out.append(asdict(m))
    return out

# --- Adapter for your pipeline (ADD THIS NEAR THE BOTTOM) ---

__all__ = ["parse_trinidad", "collect"]

def parse_trinidad(months_ahead: int = 3):
    """
    Adapter for scraper.main: return a list[dict] of Trinidad meetings
    from today forward. 'months_ahead' is kept for parity with other parsers.
    """
    return collect(months_ahead=months_ahead)

if __name__ == "__main__":
    import json, os
    months = int(os.getenv("TRINIDAD_MONTHS_AHEAD", "3"))
    print(json.dumps(parse_trinidad(months_ahead=months), indent=2, ensure_ascii=False))

