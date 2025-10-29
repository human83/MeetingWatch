# scraper/alamosa_diligent.py
from __future__ import annotations

import re
import time
import logging
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple, Set
from urllib.parse import urljoin

import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .utils import make_meeting, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
_LOG = logging.getLogger(__name__)

# Calendar landing page that lists Alamosa City Council meetings
PORTAL_URL = "https://cityofalamosa.diligent.community/Portal/MeetingInformation.aspx?Org=Cal&Id=107"

# Allow only these meeting types
ALLOW_TYPES = (
    "CITY COUNCIL REGULAR MEETING",
    "CITY COUNCIL SPECIAL MEETING",
)

# Explicit exclusions (safety)
EXCLUDE_TYPES = (
    "WORK SESSION",
    "WORKSHOP",
    "PLANNING COMMISSION",
    "BOARD",
    "AUTHORITY",
)

# ---- helpers -----------------------------------------------------------------

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _parse_title_and_date(page_text: str) -> Tuple[Optional[str], Optional[date]]:
    """
    The detail page shows a large header like:
      CITY COUNCIL SPECIAL MEETING - OCT 29 2025
    Return a normalized type and a datetime.date if found.
    """
    txt = _norm_space(page_text).upper()
    # quick guard against schedule tiles text (we only want detail pages)
    if "SCHEDULE OF MEETINGS" in txt and "TODAY" in txt and "CITY COUNCIL" in txt and "-" not in txt:
        # this is a tile list, not the detail header
        return None, None

    # prefer patterns "CITY COUNCIL SPECIAL MEETING - OCT 29 2025"
    m = re.search(r"(CITY COUNCIL [A-Z ]+?)\s*-\s*([A-Z]{3}\s+\d{1,2}\s+\d{4})", txt)
    if m:
        kind = m.group(1).strip()
        when = m.group(2).strip()
        try:
            d = datetime.strptime(when, "%b %d %Y").date()
            return kind, d
        except ValueError:
            pass

    # fallback: separate "CITY COUNCIL X MEETING" and later a date token
    m2 = re.search(r"(CITY COUNCIL [A-Z ]+?MEETING)[^A-Z0-9]{0,40}([A-Z]{3}\s+\d{1,2}\s+\d{4})", txt)
    if m2:
        kind = m2.group(1).strip()
        when = m2.group(2).strip()
        try:
            d = datetime.strptime(when, "%b %d %Y").date()
            return kind, d
        except ValueError:
            pass

    return None, None

def _parse_time_and_location(info_text: str) -> Tuple[Optional[str], Optional[str]]:
    t = None
    loc = None
    txt = _norm_space(info_text)
    m = re.search(r"\bTime:\s*([0-9]{1,2}:[0-9]{2}\s*[AP]M)\b", txt, re.I)
    if m:
        t = m.group(1).upper()
    m = re.search(r"\bLocation:\s*([^|]+?)(?:\s{2,}|$)", txt, re.I)
    if m:
        loc = m.group(1).strip()
    return t, loc

def _is_allowed(kind: str) -> bool:
    k = (kind or "").upper()
    if not any(k.startswith(a) or a in k for a in ALLOW_TYPES):
        return False
    if any(ex in k for ex in EXCLUDE_TYPES):
        return False
    return True

def _today_denver() -> date:
    return datetime.now(MT).date()

def _abs(base: str, href: str) -> str:
    return urljoin(base, href)

# ---- main entry ---------------------------------------------------------------

def parse_alamosa(headless: bool = True) -> List[Dict]:
    """
    Scrape the Alamosa Diligent portal for today/future City Council
    Regular or Special meetings. Returns a list of meeting dicts created
    with make_meeting().
    """
    items: List[Dict] = []
    seen: Set[Tuple[str, str, str]] = set()  # (date, time, pdf_url)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )
        context = browser.new_context(locale="en-US", timezone_id="America/Denver")
        page = context.new_page()

        _LOG.info("[alamosa] goto %s", PORTAL_URL)
        page.goto(PORTAL_URL, wait_until="load", timeout=60_000)

        # Collect candidate links to specific meeting pages.
        candidates: List[str] = []

        # Primary selector: sidebar "Today/Upcoming" lists
        try:
            links = page.locator("a.list-link[href*='MeetingInformation.aspx?Org=Cal&Id=']").all()
            for a in links:
                href = a.get_attribute("href")
                if href:
                    candidates.append(_abs(PORTAL_URL, href))
        except PWTimeout:
            pass

        # Fallback: click visible "UPCOMING MEETINGS" tiles and harvest their links
        if not candidates:
            print("[alamosa] No hrefs; falling back to scanning tiles")
            tiles = page.locator("div.item-content.list .list-link, div.item-content.list a.list-link").all()
            for t in tiles:
                href = t.get_attribute("href")
                if href:
                    candidates.append(_abs(PORTAL_URL, href))

        # Dedup candidates while preserving order
        seen_href: Set[str] = set()
        uniq: List[str] = []
        for h in candidates:
            if h not in seen_href:
                seen_href.add(h)
                uniq.append(h)

        print(f"[alamosa] Found candidate hrefs: {len(uniq)}")
        visited = accepted = 0

        for href in uniq:
            visited += 1
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=60_000)
            except PWTimeout:
                print(f"[alamosa] Skip: timeout loading {href}")
                continue

            # Read the whole visible content text to find title + date
            try:
                header_text = page.locator("main, body").inner_text(timeout=10_000)
            except Exception:
                try:
                    header_text = page.locator("#content, #container").inner_text(timeout=5_000)
                except Exception:
                    header_text = page.inner_text("body")

            kind, day = _parse_title_and_date(header_text)
            if not kind or not day:
                print(f"[alamosa] Skip: could not parse date (header={header_text[:80]!r}…)")
                continue

            if not _is_allowed(kind):
                print(f"[alamosa] Skip: filtered out by type: {kind}")
                continue

            # Extract time/location
            time_str, location = _parse_time_and_location(header_text)

            # Today/future filter
            if day < _today_denver():
                print(f"[alamosa] Skip past date: {day.isoformat()}")
                continue

            # Agenda PDF link
            pdf_url = None
            try:
                # The portal uses a link with id document-cover-pdf to the single-file agenda
                doc = page.locator("a#document-cover-pdf, a[id*='document'][href*='document/']").first
                if doc and doc.count() > 0:
                    h = doc.get_attribute("href")
                    if h:
                        pdf_url = _abs(href, h)
            except Exception:
                pass

            # If no explicit agenda link on this tab, also scan for "Agenda" link text to a document
            if not pdf_url:
                try:
                    link = page.locator("a:has-text('Agenda')").first
                    if link and link.count() > 0:
                        h = link.get_attribute("href")
                        if h:
                            pdf_url = _abs(href, h)
                except Exception:
                    pass

            # Summarize if we have a PDF
            agenda_summary = None
            if pdf_url:
                agenda_summary = summarize_pdf_if_any(pdf_url)

            # Build meeting dict
            city = "Alamosa — City Council"
            title = kind.title()
            date_str = day.isoformat()
            # prefer the actual time, otherwise blank
            t_str = time_str or ""

            dedup_key = (date_str, t_str, pdf_url or "")
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            item = make_meeting(
                city=city,
                title=title,
                date=date_str,
                time=t_str,
                location=location or "",
                agenda_url=pdf_url,
                agenda_summary=agenda_summary,
                source=href,
            )
            items.append(item)
            accepted += 1

        print(f"[alamosa] Visited {visited} candidates; accepted {accepted} items")

        context.close()
        browser.close()

    return items
