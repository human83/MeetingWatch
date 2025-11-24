# scraper/alamosa_diligent.py
from __future__ import annotations

import os
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import re
from typing import List, Dict, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .utils import make_meeting

PORTAL_URL = "https://cityofalamosa.diligent.community/Portal/MeetingInformation.aspx?Org=Cal&Id=115"
ALAMOSA_TZ = "America/Denver"

def _today_denver():
    return datetime.now(ZoneInfo(ALAMOSA_TZ)).date()

# Header text on the detail pane looks like:
#   "CITY COUNCIL SPECIAL MEETING - OCT 29 2025"
WANTED_TYPES = ("CITY COUNCIL REGULAR MEETING", "CITY COUNCIL SPECIAL MEETING")


def _today_denver_str() -> str:
    return _today_denver().isoformat()


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_main_detail(html: str) -> Optional[Dict]:
    """
    Parse the currently-visible detail pane on the MeetingInformation page.
    Captures: type, date, time, location, agenda pdf url (if present).
    """
    soup = BeautifulSoup(html, "html.parser")

    # The big header above Time/Location
    h = soup.find(lambda tag: tag.name in ("h1", "h2", "h3")
                           and "CITY COUNCIL" in tag.get_text(strip=True).upper())
    if not h:
        return None

    header = _norm_space(h.get_text(" ", strip=True)).upper()

    mtg_type = None
    for t in WANTED_TYPES:
        if header.startswith(t):
            mtg_type = t.title()  # pretty case
            break
    if not mtg_type:
        return None  # not City Council Regular/Special

    # Date appears after the hyphen, e.g., "- Oct 29 2025"
    m = re.search(r"-\s+([A-Za-z]{3}\s+\d{1,2}\s+\d{4})\s*$", header, re.I)
    if not m:
        return None  # couldn't read a date → skip

    try:
        date_obj = datetime.strptime(m.group(1), "%b %d %Y").date()
    except ValueError:
        return None  # bad date → skip

    # Hard guard: today & future only (Denver)
    if date_obj < _today_denver():
        return None

    date_iso = date_obj.isoformat()

    # Time and Location blocks
    time_text = None
    t_lbl = soup.find(string=re.compile(r"^\s*Time:", re.I))
    if t_lbl and t_lbl.parent:
        val = t_lbl.parent.find_next(text=re.compile(r".+"))
        time_text = _norm_space(val if isinstance(val, str) else val.get_text(" ", strip=True))

    location = None
    l_lbl = soup.find(string=re.compile(r"^\s*Location:", re.I))
    if l_lbl and l_lbl.parent:
        val = l_lbl.parent.find_next(text=re.compile(r".+"))
        location = _norm_space(val if isinstance(val, str) else val.get_text(" ", strip=True))

    # Agenda PDF link (if posted)
    a_pdf = soup.find("a", id="document-cover-pdf")
    pdf_url = None
    if a_pdf and a_pdf.get("href"):
        href = a_pdf["href"]
        pdf_url = ("https://cityofalamosa.diligent.community" + href) if href.startswith("/") else href

    title = "City Council " + ("Regular Meeting" if "REGULAR" in mtg_type.upper() else "Special Meeting")
    meeting = make_meeting(
        city_or_body="Alamosa",
        meeting_type=title,
        date=date_iso,
        start_time_local=time_text,
        status="Scheduled",
        location=location,
        agenda_url=pdf_url,
        agenda_summary=[],
        source=PORTAL_URL,
    )
    meeting["agenda_text_url"] = None
    meeting["tags"] = ["City Council"]
    return meeting


def _parse_upcoming_sidebar(html: str) -> List[Dict]:
    """
    Parse the 'UPCOMING MEETINGS' list for future City Council Regular/Special dates.
    These rarely expose time or PDFs; we still record them so they appear in meetings.json.
    """
    items: List[Dict] = []
    soup = BeautifulSoup(html, "html.parser")

    header = soup.find(lambda t: t.name in ("div", "h3", "h4")
                              and "UPCOMING MEETINGS" in t.get_text(strip=True).upper())
    if not header:
        return items

    container = header.find_parent() or soup
    for li in container.find_all("li"):
        text = _norm_space(li.get_text(" ", strip=True))
        up = text.upper()
        if "CITY COUNCIL" in up and ("REGULAR" in up or "SPECIAL" in up):
            # Expect tail like " - Nov 05 2025" (allow any case and trailing spaces)
            m = re.search(r"-\s+([A-Za-z]{3}\s+\d{1,2}\s+\d{4})\s*$", text, re.I)
            dt_obj = None
            if m:
                try:
                    dt_obj = datetime.strptime(m.group(1), "%b %d %Y").date()
                except Exception:
                    pass

            # today/future only (Denver)
            if dt_obj and dt_obj >= _today_denver():
                title = "City Council Regular Meeting" if "REGULAR" in up else "City Council Special Meeting"
                meeting = make_meeting(
                    city_or_body="Alamosa",
                    meeting_type=title,
                    date=dt_obj.isoformat(),
                    start_time_local=None,
                    status="Scheduled",
                    location=None,
                    agenda_url=None,
                    agenda_summary=[],
                    source=PORTAL_URL,
                )
                meeting["agenda_text_url"] = None
                meeting["tags"] = ["City Council"]
                items.append(meeting)
    return items


def parse_alamosa() -> List[Dict]:
    """
    Entry point. Uses Playwright to render the page, then:
      1) Scrapes the currently-selected detail (today's/next) for full info & PDF.
      2) Scrapes 'Upcoming Meetings' for future City Council Regular/Special dates.
    """
    print(f"[alamosa] starting; url: {PORTAL_URL}")
    items: List[Dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
        ])
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(20000)

        page.goto(PORTAL_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(500)  # let the DOM settle

        html = page.content()

        # Visible detail (should be today's/next City Council meeting)
        detail_item = _parse_main_detail(html)
        if detail_item:
            items.append(detail_item)

        # Future dates from the sidebar
        items.extend(_parse_upcoming_sidebar(html))

        browser.close()

    # Deduplicate by (date, title). Prefer entries with more detail (pdf/time/location).
    def score(d: Dict) -> int:
        return int(bool(d.get("agenda_url"))) + int(bool(d.get("time"))) + int(bool(d.get("location")))

    dedup: Dict[tuple, Dict] = {}
    for it in items:
        key = ((it.get("date") or ""), _norm_space(it.get("title", "")).lower())
        best = dedup.get(key)
        if best is None or score(it) > score(best):
            dedup[key] = it

    items = list(dedup.values())
    items.sort(key=lambda d: (d.get("date") or "9999-12-31", d.get("title") or ""))

    print(f"[alamosa] produced {len(items)} item(s) (today/future only; council regular/special)")
    return items
