# scraper/alamosa_diligent.py
from __future__ import annotations

import os
import re
import logging
from datetime import datetime, date
from typing import List, Dict, Optional, Tuple, Set
from urllib.parse import urljoin

import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .utils import make_meeting, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
_LOG = logging.getLogger(__name__)
_DBG = os.getenv("DEBUG", "").strip() not in ("", "0", "false", "False")

# Landing page that lists City Council meetings (tabs/links lead to dated pages)
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

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _today_denver() -> date:
    return datetime.now(MT).date()

def _is_allowed(kind: str) -> bool:
    k = (kind or "").upper()
    if not any(k.startswith(a) or a in k for a in ALLOW_TYPES):
        return False
    if any(ex in k for ex in EXCLUDE_TYPES):
        return False
    return True

def _parse_title_and_date(page_text: str) -> Tuple[Optional[str], Optional[date]]:
    """
    The detail page shows a header like:
      CITY COUNCIL SPECIAL MEETING - OCT 29 2025
    """
    txt = _norm_space(page_text).upper()

    # Prefer "TYPE - MON DD YYYY"
    m = re.search(r"(CITY COUNCIL [A-Z ]+?)\s*-\s*([A-Z]{3}\s+\d{1,2}\s+\d{4})", txt)
    if m:
        kind = m.group(1).strip()
        when = m.group(2).strip()
        try:
            d = datetime.strptime(when, "%b %d %Y").date()
            return kind, d
        except ValueError:
            pass

    # Fallback: separated type then date later in the page
    m2 = re.search(r"(CITY COUNCIL [A-Z ]+?MEETING)[^A-Z0-9]{0,60}([A-Z]{3}\s+\d{1,2}\s+\d{4})", txt)
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

def _abs(base: str, href: str) -> str:
    return urljoin(base, href)

def _collect_meeting_links(page) -> List[str]:
    """
    Gather candidate links to specific meeting detail pages from multiple spots.
    """
    candidates: List[str] = []

    # 1) Anything that obviously navigates to a specific meeting detail:
    sel_any_meeting = "a[href*='Portal/MeetingInformation.aspx?Org=Cal&Id=']"
    try:
        page.wait_for_selector(sel_any_meeting, timeout=20_000)
    except Exception:
        pass
    for a in page.locator(sel_any_meeting).all():
        href = a.get_attribute("href")
        if href:
            candidates.append(href)

    # 2) “Today’s Meetings” / “Upcoming Meetings” sidebars use .list-link
    for a in page.locator("a.list-link").all():
        href = a.get_attribute("href")
        if href and "MeetingInformation.aspx?Org=Cal&Id=" in href:
            candidates.append(href)

    # 3) Sometimes tiles use div.item-content.list .list-link
    for a in page.locator("div.item-content.list a.list-link").all():
        href = a.get_attribute("href")
        if href and "MeetingInformation.aspx?Org=Cal&Id=" in href:
            candidates.append(href)

    # De-dup & absolutize
    seen: Set[str] = set()
    uniq: List[str] = []
    for h in candidates:
        abs_h = _abs(PORTAL_URL, h)
        if abs_h not in seen:
            seen.add(abs_h)
            uniq.append(abs_h)

    if _DBG:
        print(f"[alamosa] collected raw links: {len(candidates)} -> unique: {len(uniq)}")
        for u in uniq[:12]:
            print("   ", u)

    return uniq

def parse_alamosa(headless: bool = True) -> List[Dict]:
    """
    Scrape Alamosa Diligent for today/future City Council Regular/Special meetings.
    """
    items: List[Dict] = []
    seen_keys: Set[Tuple[str, str, str]] = set()

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

        # Let the portal finish hydrating client-side content.
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        links = _collect_meeting_links(page)
        if not links:
            # One more try after a short delay, some portals hydrate slowly
            page.wait_for_timeout(1500)
            links = _collect_meeting_links(page)

        if _DBG:
            print(f"[alamosa] Found candidate hrefs: {len(links)}")

        visited = accepted = 0
        for href in links:
            visited += 1
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
            except PWTimeout:
                if _DBG:
                    print(f"[alamosa] timeout loading {href}")
                continue

            # Extract all visible text once for parsing
            try:
                txt = page.locator("main").inner_text(timeout=10_000)
            except Exception:
                try:
                    txt = page.locator("#content, #container").inner_text(timeout=5_000)
                except Exception:
                    txt = page.inner_text("body")

            kind, day = _parse_title_and_date(txt)
            if not kind or not day:
                if _DBG:
                    head = _norm_space(txt)[:120]
                    print(f"[alamosa] Skip: could not parse header/date @ {href} :: {head!r}")
                continue

            if not _is_allowed(kind):
                if _DBG:
                    print(f"[alamosa] Skip by type: {kind}")
                continue

            if day < _today_denver():
                if _DBG:
                    print(f"[alamosa] Skip past date: {day.isoformat()}")
                continue

            time_str, location = _parse_time_and_location(txt)

            # Agenda PDF link
            pdf_url = None
            try:
                btn = page.locator("a#document-cover-pdf, a[id*='document'][href*='/document/']").first
                if btn and btn.count() > 0:
                    h = btn.get_attribute("href")
                    if h:
                        pdf_url = _abs(href, h)
            except Exception:
                pass

            if not pdf_url:
                try:
                    a = page.locator("a:has-text('Agenda')").first
                    if a and a.count() > 0:
                        h = a.get_attribute("href")
                        if h:
                            pdf_url = _abs(href, h)
                except Exception:
                    pass

            agenda_summary = summarize_pdf_if_any(pdf_url) if pdf_url else None

            city = "Alamosa — City Council"
            title = kind.title()
            date_str = day.isoformat()
            t_str = time_str or ""

            key = (date_str, t_str, pdf_url or "")
            if key in seen_keys:
                continue
            seen_keys.add(key)

            items.append(
                make_meeting(
                    city=city,
                    title=title,
                    date=date_str,
                    time=t_str,
                    location=location or "",
                    agenda_url=pdf_url,
                    agenda_summary=agenda_summary,
                    source=href,
                )
            )
            accepted += 1

        if _DBG:
            print(f"[alamosa] Visited {visited} candidates; accepted {accepted} items")

        context.close()
        browser.close()

    return items
