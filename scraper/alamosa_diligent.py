
# scraper/alamosa_diligent.py
from __future__ import annotations

"""
Alamosa Diligent Community scraper (v1.1)
Fixes:
- Narrow selectors to avoid grabbing generic tiles that caused duplicates
- Navigate into each meeting detail and collect PDF links from the detail only
- Accept PDF links that contain ".pdf" anywhere in the URL (even with querystrings)
- Deduplicate by (date, time, meeting_type, pdf_url)
- Keep only today/future (America/Denver)
- Optionally launch with --no-sandbox for CI
"""

import re
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Set

import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .utils import make_meeting, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
_LOG = logging.getLogger(__name__)

PORTAL_URL = "https://cityofalamosa.diligent.community/Portal/MeetingInformation.aspx?Org=Cal&Id=107"

MEETING_NAME_PATTERNS = [
    r"\bcity council regular meeting\b",
    r"\bregular city council meeting\b",
]

def _looks_like_regular(title: str) -> bool:
    t = title.lower()
    return any(re.search(p, t) for p in MEETING_NAME_PATTERNS)

def _parse_date_time(raw_date: str, raw_time: Optional[str]) -> Tuple[str, str]:
    date_iso = "Unknown"
    time_local = "Time TBD"

    try:
        d = datetime.strptime(raw_date.strip(), "%A, %B %d, %Y")
        date_iso = d.strftime("%Y-%m-%d")
    except Exception:
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
            try:
                d = datetime.strptime(raw_date.strip(), fmt)
                date_iso = d.strftime("%Y-%m-%d")
                break
            except Exception:
                pass

    if raw_time:
        m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([AP]\.?M\.?)", raw_time, re.I)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2) or "0")
            ampm = m.group(3).replace(".", "").upper()
            time_local = f"{(hh if 1 <= hh <= 12 else 7)}:{mm:02d} {ampm}"

    return date_iso, time_local

def _extract_pdf_from_detail(page) -> Optional[str]:
    # Search links within detail container for PDFs
    # Look for text hints first, then any .pdf
    try:
        for a in page.locator(".dg-portal-detail a, .dgc-meeting-detail a, .meeting-detail a, #content a, #MainContent a, body a").all():
            href = (a.get_attribute("href") or "").strip()
            text = (a.inner_text() or "").strip().lower()
            if not href:
                continue
            href_l = href.lower()
            if (("packet" in text or "agenda" in text) and ".pdf" in href_l):
                return href
        # fallback: any .pdf in detail
        for a in page.locator(".dg-portal-detail a, .dgc-meeting-detail a, .meeting-detail a, #content a, #MainContent a, body a").all():
            href = (a.get_attribute("href") or "").strip()
            if href and ".pdf" in href.lower():
                return href
    except Exception:
        pass
    return None

def _scrape_detail_fields(page) -> Tuple[str, str, Optional[str], str]:
    """
    Returns: date_iso, time_local, location, full_detail_text
    """
    detail_text = ""
    try:
        candidates = [".dg-portal-detail", ".dgc-meeting-detail", ".meeting-detail", "#content", "#MainContent", "body"]
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

    raw_date = None
    raw_time = None
    for line in detail_text.splitlines():
        s = line.strip()
        if not raw_date and re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+\w+\s+\d{1,2},\s+\d{4}", s):
            raw_date = s
        if not raw_time and re.search(r"\b\d{1,2}(:\d{2})?\s*[AP]\.?M\.?\b", s, re.I):
            raw_time = s
    date_iso, time_local = _parse_date_time(raw_date or "", raw_time)

    location = None
    mloc = re.search(r"(Council Chambers.*?300 Hunt Avenue.*?Alamosa.*)", detail_text, re.I)
    if mloc:
        location = mloc.group(1).strip()
    else:
        m2 = re.search(r"300 Hunt Avenue.*Alamosa.*", detail_text, re.I)
        if m2:
            location = m2.group(0).strip()

    return date_iso, time_local, location, detail_text

def parse_alamosa() -> List[Dict]:
    items: List[Dict] = []
    seen: Set[Tuple[str, str, str, str]] = set()  # (date_iso, time_local, meeting_type, pdf_url or '')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="MeetingWatch/Alamosa-Diligent/1.1")
        page = context.new_page()

        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            _LOG.warning("Timeout loading %s", PORTAL_URL)
        except Exception as e:
            _LOG.warning("Error loading %s: %s", PORTAL_URL, e)

        # Find likely meeting links on the list page.
        # Prefer anchors with text that looks like Regular City Council Meeting.
        meeting_links = page.locator("a:visible").all()
        candidate_hrefs = []
        for a in meeting_links:
            try:
                text = (a.inner_text(timeout=500) or "").strip()
            except Exception:
                text = ""
            if not text:
                continue
            if _looks_like_regular(text):
                href = a.get_attribute("href") or ""
                if href:
                    candidate_hrefs.append(href)

        # Deduplicate hrefs
        candidate_hrefs = list(dict.fromkeys(candidate_hrefs))

        # If we somehow found none (text mismatch), fall back to any visible meeting detail links on the page
        if not candidate_hrefs:
            for a in meeting_links:
                href = a.get_attribute("href") or ""
                if "MeetingDetail" in href or "MeetingInformation" in href:
                    candidate_hrefs.append(href)
            candidate_hrefs = list(dict.fromkeys(candidate_hrefs))

        today_mt = datetime.now(MT).date()

        for href in candidate_hrefs:
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # If relative URL, join with base
                try:
                    page.goto(page.url.rstrip("/") + "/" + href.lstrip("/"), wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    continue

            # Pull fields from detail
            date_iso, time_local, location, detail_text = _scrape_detail_fields(page)

            # Filter by date (today/future only)
            try:
                meeting_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
                if meeting_date < today_mt:
                    # older than today
                    continue
            except Exception:
                # cannot parse date -> skip
                continue

            pdf_url = _extract_pdf_from_detail(page)

            meeting_type = "City Council Regular Meeting"
            key = (date_iso, time_local, meeting_type, pdf_url or "")
            if key in seen:
                continue
            seen.add(key)

            agenda_summary = summarize_pdf_if_any(pdf_url)

            item = make_meeting(
                city_or_body="City of Alamosa",
                meeting_type=meeting_type,
                date=date_iso,
                start_time_local=time_local,
                status="Scheduled" if pdf_url else "Scheduled (no agenda yet)",
                location=location,
                agenda_url=pdf_url,
                agenda_summary=agenda_summary,
                source=href if href.startswith("http") else PORTAL_URL,
            )
            items.append(item)

        context.close()
        browser.close()

    return items
