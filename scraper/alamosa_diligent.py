# scraper/alamosa_diligent.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple
from playwright.sync_api import sync_playwright, TimeoutError  # type: ignore
from datetime import datetime, date
import pytz
import re

PORTAL_BASE = "https://cityofalamosa.diligent.community"
PORTAL_URL = f"{PORTAL_BASE}/Portal/"
DENVER_TZ = pytz.timezone("America/Denver")

ALLOW_TYPES = (
    "city council regular meeting",
    "city council special meeting",
)

EXCLUDE_PHRASES = (
    "work session",   # exclude work sessions explicitly
)


@dataclass
class MeetingItem:
    city: str
    title: str
    date: Optional[str]  # YYYY-MM-DD
    time: Optional[str]  # e.g., '6:30 PM'
    location: Optional[str]
    agenda_url: Optional[str]
    agenda_text_url: Optional[str]
    source: str


def _abs_url(href: Optional[str]) -> Optional[str]:
    if not href:
        return None
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"{PORTAL_BASE}{href}"
    return f"{PORTAL_URL}{href}"


def _looks_allowed(meeting_text: str) -> bool:
    t = meeting_text.strip().lower()
    if any(x in t for x in EXCLUDE_PHRASES):
        return False
    return any(x in t for x in ALLOW_TYPES)


def _parse_date_from_text(text: str) -> Optional[str]:
    """
    Try a few patterns:
      - 'Wednesday, October 29, 2025'
      - 'Oct 29 2025' or 'October 29 2025'
      - 'OCT 29 2025' (uppercased in header tab)
    Return ISO date or None.
    """
    # Long day-of-week form
    m = re.search(
        r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}",
        text,
        re.IGNORECASE,
    )
    if m:
        raw = m.group(0)
        for fmt in ("%A, %B %d, %Y", "%A %B %d, %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except Exception:
                pass

    # Month name/abbrev
    m = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}",
        text,
        re.IGNORECASE,
    )
    if m:
        raw = m.group(0).replace(",", "")
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except Exception:
                pass

    return None


def _parse_time_from_text(text: str) -> Optional[str]:
    """
    Return something like '6:30 PM' or '6:00 PM'.
    """
    m = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", text, re.IGNORECASE)
    if m:
        return m.group(1).upper().replace("  ", " ")

    m = re.search(r"(\d{1,2}\s*[AP]M)", text, re.IGNORECASE)
    if m:
        hh_ampm = m.group(1).upper().replace(" ", "")
        # normalize to HH:MM AM/PM
        hh = re.match(r"(\d{1,2})([AP]M)", hh_ampm)
        if hh:
            return f"{hh.group(1)}:00 {hh.group(2)}"
    return None


def _today_iso_denver() -> str:
    return datetime.now(DENVER_TZ).date().isoformat()


def parse_alamosa(headless: bool = True) -> List[dict]:
    """
    Scrape Alamosa Diligent portal for City Council Regular/Special meetings
    (today and future only). Returns a list of dicts.
    """
    items: List[MeetingItem] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ],
        )
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1800})
        page = context.new_page()

        print("[alamosa] Navigating to portal…")
        page.goto(PORTAL_URL, timeout=60_000)
        page.wait_for_load_state("domcontentloaded")

        # Collect candidate meeting links from "Today's Meetings" and "Upcoming Meetings".
        candidates = set()

        for sel in [
            "div#ctl00_TodaysMeetings a.list-link",
            "div#ctl00_UpcomingMeetings a.list-link",
            # fallback: any list-link on page (we'll filter by text)
            "a.list-link",
        ]:
            for a in page.query_selector_all(sel):
                text = (a.inner_text() or "").strip()
                if not _looks_allowed(text):
                    continue
                href = _abs_url(a.get_attribute("href"))
                if href:
                    candidates.add(href)

        print(f"[alamosa] Found candidate hrefs: {len(candidates)}")

        visited = 0
        accepted = 0
        today_iso = _today_iso_denver()

        for href in sorted(candidates):
            visited += 1
            dp = context.new_page()
            try:
                dp.goto(href, timeout=60_000)
                dp.wait_for_load_state("domcontentloaded")
            except TimeoutError:
                print(f"[alamosa] Timeout opening {href}")
                dp.close()
                continue

            # Header text (contains something like "CITY COUNCIL SPECIAL MEETING - OCT 29 2025")
            header_text = ""
            h2 = dp.query_selector("h2") or dp.query_selector("h1")
            if h2:
                header_text = (h2.inner_text() or "").strip()

            # Extract date, time, location from labeled fields under the header.
            page_text = (dp.inner_text("body") or "")
            date_iso = _parse_date_from_text(page_text) or _parse_date_from_text(header_text)

            time_el = dp.locator("xpath=//*[normalize-space()='Time:']/following-sibling::*[1]").first
            time_text = time_el.inner_text().strip() if (time_el and time_el.count() > 0) else ""
            time_out = _parse_time_from_text(time_text or page_text)

            loc_el = dp.locator("xpath=//*[normalize-space()='Location:']/following-sibling::*[1]").first
            location = loc_el.inner_text().strip() if (loc_el and loc_el.count() > 0) else None

            # Find the Agenda PDF link
            pdf_link = (
                dp.query_selector("a#document-cover-pdf")
                or dp.query_selector("a.meeting-document-type-pdf-link")
                or dp.query_selector("a:has-text('Agenda')")
            )
            pdf_url = _abs_url(pdf_link.get_attribute("href")) if pdf_link else None

            # Decide title based on the header (prefer to classify here rather than list tile)
            title_lc = header_text.lower()
            if "regular" in title_lc:
                title = "City Council Regular Meeting"
            elif "special" in title_lc:
                title = "City Council Special Meeting"
            else:
                # if header is ambiguous, fall back to allowed types from any text
                title = "City Council Regular Meeting" if "regular" in page_text.lower() else (
                    "City Council Special Meeting" if "special" in page_text.lower() else "City Council Meeting"
                )

            # Keep only Regular/Special
            if title.lower() not in ALLOW_TYPES:
                dp.close()
                continue

            # Date filter: today + future only (Denver time)
            if date_iso and date_iso < today_iso:
                dp.close()
                continue

            items.append(
                MeetingItem(
                    city="Alamosa — City Council",
                    title=title,
                    date=date_iso,
                    time=time_out,
                    location=location,
                    agenda_url=pdf_url,
                    agenda_text_url=None,
                    source=href,
                )
            )
            accepted += 1
            dp.close()

        print(f"[alamosa] Visited {visited} candidates; accepted {accepted} items")

        context.close()
        browser.close()

    return [asdict(i) for i in items]


# Optional quick test
if __name__ == "__main__":
    for i in parse_alamosa(headless=False):
        print(i)
