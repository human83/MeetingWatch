# scraper/alamosa_diligent.py
from __future__ import annotations

"""
Alamosa Diligent Community scraper (v0.9)
- Navigates the Diligent Community calendar page via Playwright
- Picks City Council Regular Meetings (upcoming + today)
- Pulls Agenda/Packet PDF link from the meeting detail pane
- Summarizes the agenda PDF using utils.summarize_pdf_if_any
- Returns normalized meeting dicts via utils.make_meeting
"""

import os
import re
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional

import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .utils import make_meeting, clean_text, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
_LOG = logging.getLogger(__name__)

# You gave this portal URL (Org=Cal, Id=107)
PORTAL_URL = "https://cityofalamosa.diligent.community/Portal/MeetingInformation.aspx?Org=Cal&Id=107"

# Strings we consider as "regular meeting"
MEETING_NAME_PATTERNS = [
    r"\bcity council regular meeting\b",
    r"\bregular city council meeting\b",
]

def _looks_like_regular(title: str) -> bool:
    t = title.lower()
    return any(re.search(p, t) for p in MEETING_NAME_PATTERNS)

def _parse_date_time(raw_date: str, raw_time: Optional[str]) -> (str, str):
    """
    Convert scraped date/time strings into YYYY-MM-DD and 'H:MM AM/PM' (local).
    If we fail to parse, return ('YYYY-MM-DD', 'Time TBD').
    """
    date_iso = "Unknown"
    time_local = "Time TBD"

    # Many Diligent calendars render as 'Wednesday, October 15, 2025' etc.
    try:
        d = datetime.strptime(raw_date.strip(), "%A, %B %d, %Y")
        date_iso = d.strftime("%Y-%m-%d")
    except Exception:
        # Try a looser parse: e.g., 'Oct 15, 2025'
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
            try:
                d = datetime.strptime(raw_date.strip(), fmt)
                date_iso = d.strftime("%Y-%m-%d")
                break
            except Exception:
                pass

    if raw_time:
        # Normalize '7:00 PM', '7 PM', etc.
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([AP]\.?M\.?)", raw_time, re.I)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2) or "0")
            ampm = m.group(3).replace(".", "").upper()
            time_local = f"{(hh if 1 <= hh <= 12 else 7)}:{mm:02d} {ampm}"

    return date_iso, time_local

def _extract_pdf_links(texts: List[str]) -> Optional[str]:
    # First Packet, then Agenda if present
    for t in texts:
        if re.search(r"\b(packet|agenda packet)\b", t, re.I) and t.lower().endswith(".pdf"):
            return t
    for t in texts:
        if "agenda" in t.lower() and t.lower().endswith(".pdf"):
            return t
    # Last-resort: any PDF on the page
    for t in texts:
        if t.lower().endswith(".pdf"):
            return t
    return None

def parse_alamosa() -> List[Dict]:
    items: List[Dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="MeetingWatch/Alamosa-Diligent/0.9")
        page = context.new_page()

        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            _LOG.warning("Timeout loading %s", PORTAL_URL)
        except Exception as e:
            _LOG.warning("Error loading %s: %s", PORTAL_URL, e)

        # The Diligent calendar typically renders meeting tiles/cards.
        # We'll scan visible cards, open details, and gather fields.

        # Grab all meeting tiles/cards text + links
        # NOTE: selectors may evolve; we keep this tolerant and text-first.
        tiles = page.locator("a, div, li, .meeting, .k-listview-item, .dgc-meeting").all()

        for i, tile in enumerate(tiles):
            try:
                title = tile.inner_text(timeout=1000).strip()
            except Exception:
                continue
            if not title:
                continue

            if not _looks_like_regular(title):
                continue

            # Try to click to open details (often opens a detail pane or navigates)
            try:
                tile.click(timeout=2000)
            except Exception:
                pass

            page.wait_for_timeout(500)  # let any Ajax detail load

            # Scrape the newly visible detail area (fallback: whole page text)
            detail_text = ""
            try:
                # common detail containers
                candidates = [
                    ".dg-portal-detail", ".dgc-meeting-detail", ".meeting-detail",
                    "#content", "#MainContent", "body"
                ]
                for sel in candidates:
                    if page.locator(sel).count():
                        detail_text = page.locator(sel).inner_text(timeout=1000)
                        if detail_text and len(detail_text) > 50:
                            break
            except Exception:
                pass

            if not detail_text:
                try:
                    detail_text = page.inner_text("body", timeout=1000)
                except Exception:
                    detail_text = ""

            # Collect all hrefs we can see (possible PDF links)
            hrefs: List[str] = []
            try:
                for a in page.locator("a").all():
                    try:
                        href = a.get_attribute("href") or ""
                        if href:
                            hrefs.append(href)
                    except Exception:
                        pass
            except Exception:
                pass

            # Parse date/time from the visible strings
            # Heuristics: look for lines like 'Wednesday, October 15, 2025' and '7:00 PM'
            raw_date = None
            raw_time = None
            for line in detail_text.splitlines():
                line_stripped = line.strip()
                if not raw_date and re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+\w+\s+\d{1,2},\s+\d{4}", line_stripped):
                    raw_date = line_stripped
                if not raw_time and re.search(r"\b\d{1,2}(:\d{2})?\s*[AP]\.?M\.?\b", line_stripped, re.I):
                    raw_time = line_stripped

            date_iso, time_local = _parse_date_time(raw_date or "", raw_time)

            # Location: look for ‘Council Chambers’ or an address line
            location = None
            mloc = re.search(r"(Council Chambers.*?300 Hunt Avenue.*?Alamosa.*)", detail_text, re.I)
            if mloc:
                location = mloc.group(1).strip()
            else:
                m2 = re.search(r"300 Hunt Avenue.*Alamosa.*", detail_text, re.I)
                if m2:
                    location = m2.group(0).strip()

            # Find agenda or packet PDF
            pdf_url = _extract_pdf_links(hrefs)

            # Summarize PDF (can be None if absent)
            agenda_summary = summarize_pdf_if_any(pdf_url)

            item = make_meeting(
                city_or_body="City of Alamosa",
                meeting_type="City Council Regular Meeting",
                date=date_iso if date_iso != "Unknown" else datetime.now(MT).strftime("%Y-%m-%d"),
                start_time_local=time_local,
                status="Scheduled" if pdf_url else "Scheduled (no agenda yet)",
                location=location,
                agenda_url=pdf_url,
                agenda_summary=agenda_summary,
                source=PORTAL_URL,
            )
            items.append(item)

        context.close()
        browser.close()

    return items
