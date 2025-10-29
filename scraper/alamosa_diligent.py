# scraper/alamosa_diligent.py
from __future__ import annotations

"""
Alamosa Diligent Community scraper (v1.2)
- More tolerant discovery of meeting detail links
- Falls back to clicking visible tiles if hrefs are scarce
- Validates "Regular" in the detail header/title (not list tile)
- Robust date parsing (several formats)
- Today/future filter (America/Denver)
- Deduplication by (date, time, type, pdf)
- CI-friendly Chromium launch flags
- Emits concise debug prints so we can see what's happening in Actions logs
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

# Patterns we consider as "Regular"
REGULAR_PATTERNS = [
    r"\bregular\b",
    r"\bregular meeting\b",
    r"\bcity council regular\b",
]

def _is_regular_text(text: str) -> bool:
    t = text.lower()
    if "city council" not in t:
        return False
    return any(re.search(p, t) for p in REGULAR_PATTERNS)

DATE_FMTS = [
    "%A, %B %d, %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%m/%d/%Y",
    "%Y-%m-%d",
]

def _parse_date_time(detail_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Try to extract date and time from the full detail text.
    Returns (YYYY-MM-DD or None, 'H:MM AM/PM' or 'Time TBD'/None)
    """
    # DATE: look for "Wednesday, October 15, 2025" or similar
    date_iso = None
    # first, exact long day-of-week pattern
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+\w+\s+\d{1,2},\s+\d{4}", detail_text)
    if m:
        raw_date = m.group(0)
        for fmt in DATE_FMTS:
            try:
                d = datetime.strptime(raw_date.strip(), fmt)
                date_iso = d.strftime("%Y-%m-%d")
                break
            except Exception:
                pass
    # fallback: Month D, YYYY anywhere
    if not date_iso:
        m2 = re.search(r"\b(\w+\s+\d{1,2},\s+\d{4})\b", detail_text)
        if m2:
            raw_date = m2.group(1)
            for fmt in DATE_FMTS:
                try:
                    d = datetime.strptime(raw_date.strip(), fmt)
                    date_iso = d.strftime("%Y-%m-%d")
                    break
                except Exception:
                    pass
    # fallback: ISO-like
    if not date_iso:
        m3 = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", detail_text)
        if m3:
            date_iso = m3.group(1)

    # TIME
    time_local = None
    mt = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([AP]\.?M\.?)\b", detail_text, re.I)
    if mt:
        hh = int(mt.group(1))
        mm = int(mt.group(2) or "0")
        ampm = mt.group(3).replace(".", "").upper()
        time_local = f"{(hh if 1 <= hh <= 12 else 7)}:{mm:02d} {ampm}"
    else:
        time_local = "Time TBD"

    return date_iso, time_local

def _extract_pdf_from_detail(page) -> Optional[str]:
    try:
        # prefer links with agenda/packet hints
        for a in page.locator(".dg-portal-detail a, .dgc-meeting-detail a, .meeting-detail a, #content a, #MainContent a, body a").all():
            href = (a.get_attribute("href") or "").strip()
            text = (a.inner_text() or "").strip().lower()
            if href and ".pdf" in href.lower() and ("agenda" in text or "packet" in text):
                return href
        # fallback: any .pdf
        for a in page.locator(".dg-portal-detail a, .dgc-meeting-detail a, .meeting-detail a, #content a, #MainContent a, body a").all():
            href = (a.get_attribute("href") or "").strip()
            if href and ".pdf" in href.lower():
                return href
    except Exception:
        pass
    return None

def _get_detail_text(page) -> str:
    try:
        for sel in [".dg-portal-detail", ".dgc-meeting-detail", ".meeting-detail", "#content", "#MainContent", "body"]:
            if page.locator(sel).count():
                txt = page.locator(sel).inner_text(timeout=1000)
                if txt and len(txt) > 50:
                    return txt
    except Exception:
        pass
    try:
        return page.inner_text("body", timeout=1000)
    except Exception:
        return ""

def parse_alamosa() -> List[Dict]:
    items: List[Dict] = []
    seen: Set[Tuple[str, str, str, str]] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="MeetingWatch/Alamosa-Diligent/1.2")
        page = context.new_page()

        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            print("[alamosa] Timeout loading portal")
        except Exception as e:
            print(f"[alamosa] Error loading portal: {e}")

        # Collect candidate hrefs by looking for MeetingDetail/MeetingInformation links and text mentioning City Council
        anchors = page.locator("a:visible").all()
        candidate_hrefs = []
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            txt = (a.inner_text() or "").strip()
            if not href:
                continue
            if ("MeetingDetail" in href or "MeetingInformation" in href) and ("City" in txt or "Council" in txt or "Regular" in txt):
                candidate_hrefs.append(href)

        # Dedup
        candidate_hrefs = list(dict.fromkeys(candidate_hrefs))
        print(f"[alamosa] Found candidate hrefs: {len(candidate_hrefs)}")

        # If none, try clicking list tiles generically
        if not candidate_hrefs:
            tiles = page.locator("li, .k-listview-item, .dgc-meeting, .meeting, a").all()
            print(f"[alamosa] No hrefs; falling back to scanning {len(tiles)} tiles")
            # click up to first 15 tiles to look for valid details
            for idx, t in enumerate(tiles[:15]):
                try:
                    t.click(timeout=1000)
                    page.wait_for_timeout(400)
                    # may have navigated; capture URL for source
                    candidate_hrefs.append(page.url)
                    page.go_back(timeout=2000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            candidate_hrefs = list(dict.fromkeys(candidate_hrefs))
            print(f"[alamosa] After tile-scan, candidates: {len(candidate_hrefs)}")

        today_mt = datetime.now(MT).date()
        accepted = 0
        visited = 0

        for href in candidate_hrefs:
            visited += 1
            # Navigate to detail
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # try to resolve relative
                try:
                    base = PORTAL_URL.rsplit("/", 1)[0]
                    page.goto(base + "/" + href.lstrip("/"), wait_until="domcontentloaded", timeout=45000)
                except Exception:
                    print(f"[alamosa] Skip: cannot open {href}")
                    continue

            detail_text = _get_detail_text(page)
            if not detail_text or "City Council" not in detail_text:
                # Not a City Council page
                continue

            # Check it's a Regular meeting
            if not _is_regular_text(detail_text):
                # Not Regular (could be work session/special/board)
                continue

            date_iso, time_local = _parse_date_time(detail_text)
            if not date_iso:
                print("[alamosa] Skip: could not parse date")
                continue

            try:
                meeting_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
                if meeting_date < today_mt:
                    # older than today
                    continue
            except Exception:
                continue

            location = None
            mloc = re.search(r"(Council Chambers.*?300 Hunt Avenue.*?Alamosa.*)", detail_text, re.I)
            if mloc:
                location = mloc.group(1).strip()
            else:
                m2 = re.search(r"300 Hunt Avenue.*Alamosa.*", detail_text, re.I)
                if m2:
                    location = m2.group(0).strip()

            pdf_url = _extract_pdf_from_detail(page)

            key = (date_iso, time_local or "", "City Council Regular Meeting", (pdf_url or ""))
            if key in seen:
                continue
            seen.add(key)

            agenda_summary = summarize_pdf_if_any(pdf_url)

            item = make_meeting(
                city_or_body="City of Alamosa",
                meeting_type="City Council Regular Meeting",
                date=date_iso,
                start_time_local=time_local or "Time TBD",
                status="Scheduled" if pdf_url else "Scheduled (no agenda yet)",
                location=location,
                agenda_url=pdf_url,
                agenda_summary=agenda_summary,
                source=href if href.startswith("http") else PORTAL_URL,
            )
            items.append(item)
            accepted += 1

        print(f"[alamosa] Visited {visited} candidates; accepted {accepted} items")

        context.close()
        browser.close()

    return items
