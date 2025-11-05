# trinidad_regular.py
from __future__ import annotations

import os
import re
import json
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from dateutil import tz
from dateutil.parser import parse as dtparse
from datetime import datetime, timedelta

BASE = "https://www.trinidad.co.gov/"
CAL_URL_TMPL = "https://www.trinidad.co.gov/calendar.php?view=month&month={month}&day=1&year={year}&calendar="

MT_TZ = tz.gettz("America/Denver")
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "MeetingWatch (Trinidad scraper)",
        "Accept": "text/html,application/xhtml+xml",
    }
)

TITLE_OK = re.compile(r"\bCity Council Regular\b", re.I)
TITLE_EXCLUDE = re.compile(r"\bWork\s*Session\b", re.I)

@dataclass
class Meeting:
    title: str
    start_dt: Optional[str]
    end_dt: Optional[str]
    location: str
    date_str: str
    agenda_url: Optional[str]
    agenda_text_url: Optional[str]
    source_url_detail_url: Optional[str]
    agenda_text: str
    agenda_bullets: List[str]
    event_id: Optional[str]

def _log(msg: str) -> None:
    print(msg, flush=True)

def month_iter(today_local: datetime, months_ahead: int):
    y = today_local.year
    m = today_local.month
    for i in range(months_ahead):
        mm = ((m - 1 + i) % 12) + 1
        yy = y + ((m - 1 + i) // 12)
        yield yy, mm

def fetch_html(url: str) -> str:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def parse_modal_day_view(url: str) -> dict:
    """Fetch the day view with &id=… and parse the modal content."""
    html = fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")

    # The modal content lives in a div that becomes visible via JS.
    # We can just grab the first .event-modal block.
    modal = soup.find("div", attrs={"aria-label": re.compile(r"Event Modal", re.I)})
    if not modal:
        # Fallback: search by class keyword
        modal = soup.find("div", class_=re.compile(r"event-modal|modal", re.I))

    out = {"time_range": None, "title": None, "body": "", "location": ""}

    if modal:
        # Time range is shown in a colored banner like "06:00 PM - 07:00 PM"
        banner = modal.find(text=re.compile(r"\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M", re.I))
        if banner:
            out["time_range"] = banner.strip()

        # Title is a <h2> or similar
        h = modal.find(["h2", "h3"])
        if h:
            out["title"] = h.get_text(strip=True)

        # Body text (agenda-esque notes)
        body_container = modal.find("div", class_=re.compile(r"modal-body|content", re.I))
        if not body_container:
            # the markup often places body text in the same container as title
            body_container = modal

        paragraphs = []
        for p in body_container.find_all(["p", "li"]):
            txt = p.get_text(" ", strip=True)
            if txt:
                paragraphs.append(txt)
        out["body"] = "\n".join(paragraphs).strip()

        # Location: look for an address line near title/body
        loc = None
        for p in body_container.find_all("p"):
            t = p.get_text(" ", strip=True)
            if "City Hall" in t or "Animas" in t or "Trinidad, Colorado" in t:
                loc = t
                break
        out["location"] = loc or ""
    return out

def parse_time_range(date_str_local: str, time_range: Optional[str]) -> (Optional[str], Optional[str], str):
    """
    Convert the date (YYYY-MM-DD) + time range like '06:00 PM - 07:00 PM' into
    ISO timestamps in MT, plus a nice display date string.
    """
    display = ""
    if not date_str_local:
        return None, None, display

    try:
        base = datetime.fromisoformat(date_str_local).replace(tzinfo=MT_TZ)
    except Exception:
        # fallback: try robust parse
        base = dtparse(date_str_local).replace(tzinfo=MT_TZ)

    display = base.strftime("%A, %B %-d, %Y")

    if not time_range:
        return None, None, display

    m = re.match(r"\s*(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)\s*$", time_range, re.I)
    if not m:
        return None, None, display

    start_s, end_s = m.group(1), m.group(2)

    def _combine(tstr: str) -> datetime:
        # e.g., '06:00 PM'
        t = dtparse(tstr).time()
        return datetime(base.year, base.month, base.day, t.hour, t.minute, tzinfo=MT_TZ)

    start_dt = _combine(start_s)
    end_dt = _combine(end_s)
    return start_dt.isoformat(), end_dt.isoformat(), display

def collect(months_ahead: int = 3) -> List[dict]:
    """
    Crawl present and future months, pick City Council Regular meetings,
    follow each event's href (&id=XYZ) to pull modal details.
    """
    today_local = datetime.now(tz=MT_TZ).date()
    meetings: List[dict] = []

    seen_candidates = 0
    accepted = 0

    for yy, mm in month_iter(datetime.now(tz=MT_TZ), months_ahead):
        month_url = CAL_URL_TMPL.format(month=mm, year=yy)
        _log(f"INFO: [trinidad] fetching month: {month_url}")
        html = fetch_html(month_url)
        soup = BeautifulSoup(html, "html.parser")

        # Each event is an <a class="fc-day-grid-event …"> inside a <td data-date="YYYY-MM-DD">
        for a in soup.select("a.fc-day-grid-event"):
            title_node = a.select_one(".fc-title")
            title_text = title_node.get_text(" ", strip=True) if title_node else a.get_text(" ", strip=True)
            if not title_text:
                continue
            if not TITLE_OK.search(title_text) or TITLE_EXCLUDE.search(title_text):
                continue

            # Climb to the parent day cell to get the date
            day_td = a.find_parent(["td", "div"], attrs={"data-date": True})
            date_local = None
            if day_td and day_td.has_attr("data-date"):
                date_local = day_td["data-date"]  # 'YYYY-MM-DD'

            href = a.get("href") or ""
            detail_url = urljoin(BASE, href)

            # Pull modal/day view to get time/location/body text
            modal = {}
            if detail_url and "id=" in detail_url:
                modal = parse_modal_day_view(detail_url)

            start_iso, end_iso, display_date = parse_time_range(date_local or "", modal.get("time_range"))

            # Final safety: require date and today/future
            try:
                if date_local:
                    d = datetime.fromisoformat(date_local).date()
                else:
                    d = None
            except Exception:
                d = None

            seen_candidates += 1
            if not d or d < today_local:
                continue

            # Build meeting entry
            mtg = {
                "city": "Trinidad",
                "title": "City Council Regular Meeting",
                "start_dt": start_iso,
                "end_dt": end_iso,
                "date_str": display_date or (d.strftime("%A, %B %-d, %Y") if d else ""),
                "location": (modal.get("location") or "City Council Chambers at City Hall, Trinidad, CO").strip(),
                "agenda_url": None,                # no PDF posted in advance
                "agenda_text_url": detail_url,     # we’ll treat the day view as the text source
                "body_text": modal.get("body", "").strip(),
                "agenda_bullets": [],              # summarizer will fill if body_text exists
                "source_url_detail_url": detail_url,
                "event_id": parse_qs(urlparse(detail_url).query).get("id", [None])[0],
            }
            meetings.append(mtg)
            accepted += 1

    _log(f"INFO: [trinidad] candidates seen: {seen_candidates}; accepted {accepted} City Council Regular meeting(s)")
    # Final safety: only keep items today forward (defensive if parsing missed)
    out = []
    for m in meetings:
        try:
            if m["start_dt"]:
                d = dtparse(m["start_dt"]).astimezone(MT_TZ).date()
            else:
                # fall back to date_str if start time is unknown
                d = dtparse(m["date_str"]).date()
        except Exception:
            d = None
        if d and d >= today_local:
            out.append(m)
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
    import os, json
    months = int(os.getenv("TRINIDAD_MONTHS_AHEAD", "3"))
    print(json.dumps(parse_trinidad(months_ahead=months), indent=2, ensure_ascii=False))
