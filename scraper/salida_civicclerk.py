# scraper/salida_civicclerk.py
# Fixed Playwright usage + adds expected `parse_salida` entrypoint for scraper.main

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable, List, Optional, Union

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Page,
    Browser,
)

__all__ = ["Meeting", "process_meetings", "parse_salida"]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Meeting:
    id: int
    url: str
    title: Optional[str] = None
    date: Optional[str] = None  # ISO date string
    location: Optional[str] = None
    status: Optional[str] = None
    agenda_url: Optional[str] = None
    minutes_url: Optional[str] = None
    packet_url: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%B %d, %Y",   # October 18, 2025
    "%b %d, %Y",   # Oct 18, 2025
    "%m/%d/%Y",
    "%Y-%m-%d",
    "%m-%d-%Y",
]

def _clean_text(txt: str | None) -> str:
    if not txt:
        return ""
    return " ".join(txt.split())

def _parse_date(text: str) -> Optional[str]:
    """Try multiple formats and return ISO date string (YYYY-MM-DD) or None."""
    t = _clean_text(text)
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(t, fmt)
            return dt.date().isoformat()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Core scraping
# ---------------------------------------------------------------------------

def _scrape_civicclerk_meeting(page: Page, m: Meeting, nav_timeout_ms: int = 30000) -> Meeting:
    """
    Navigate to a CivicClerk meeting page and extract common fields.

    NOTE: CivicClerk sites are heavily templatized but have local tweaks.
    If your instance needs custom selectors, adjust the CSS/XPath below.
    """
    page.set_default_timeout(nav_timeout_ms)

    page.goto(m.url, wait_until="domcontentloaded")

    # Title
    try:
        # Common title locations (adjust as needed for your tenant)
        title = page.locator("h1, h2, .title, .meeting-title").first.text_content()
        m.title = _clean_text(title) or m.title
    except Exception:
        pass

    # Date
    date_candidates = [
        "text=/\\d{1,2}\\/\\d{1,2}\\/\\d{2,4}/",   # 10/18/2025
        "text=/\\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\\b.*\\d{4}/i",
        ".meeting-date",
        ".date",
        "[data-testid='meeting-date']",
    ]
    found_date_text = None
    for sel in date_candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                txt = loc.text_content()
                if txt:
                    found_date_text = _clean_text(txt)
                    break
        except Exception:
            continue

    # Sometimes the date is in a label/value pair:
    if not found_date_text:
        try:
            label = page.locator("text=/Date/i").first
            if label.count():
                val = label.locator("xpath=following::*[1]").first.text_content()
                found_date_text = _clean_text(val)
        except Exception:
            pass

    if found_date_text:
        parsed = _parse_date(found_date_text)
        if parsed:
            m.date = parsed
        else:
            m.notes = (m.notes or "") + (" | could not parse date: " + found_date_text)

    # Location (best-effort)
    try:
        loc_candidates = [
            ".meeting-location",
            ".location",
            "[data-testid='meeting-location']",
            "text=/Location/i >> xpath=following::*[1]",
        ]
        for sel in loc_candidates:
            loc = page.locator(sel).first
            if loc.count():
                txt = _clean_text(loc.text_content())
                if txt and len(txt) > 2:
                    m.location = txt
                    break
    except Exception:
        pass

    # Links: agenda, minutes, packet
    link_map = {
        "agenda_url": ["text=/\\bAgenda\\b/i", "a.agenda-link"],
        "minutes_url": ["text=/\\bMinutes\\b/i", "a.minutes-link"],
        "packet_url": ["text=/\\bPacket\\b/i", "text=/\\bAgenda Packet\\b/i", "a.packet-link"],
    }

    for field, selectors in link_map.items():
        if getattr(m, field, None):
            continue
        for sel in selectors:
            try:
                anchor = page.locator(sel).first
                if anchor.count():
                    href = anchor.get_attribute("href")
                    if href and href.strip():
                        setattr(m, field, href)
                        break
            except Exception:
                continue

    # Status (if present)
    try:
        status_candidates = [".status", ".meeting-status", "[data-testid='meeting-status']"]
        for sel in status_candidates:
            st = page.locator(sel).first
            if st.count():
                txt = _clean_text(st.text_content())
                if txt:
                    m.status = txt
                    break
    except Exception:
        pass

    return m


def process_meetings(
    meetings: Iterable[Union[Meeting, dict]],
    *,
    headless: bool = True,
    nav_timeout_ms: int = 30000,
    reuse_single_page: bool = True,
) -> List[Meeting]:
    """
    Preferred Playwright pattern with robust error logging.
    """
    results: List[Meeting] = []

    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            page = context.new_page() if reuse_single_page else None

            for m in meetings:
                try:
                    if not isinstance(m, Meeting):
                        m = Meeting(**m)

                    if reuse_single_page and page is not None:
                        updated = _scrape_civicclerk_meeting(page, m, nav_timeout_ms=nav_timeout_ms)
                    else:
                        pg = context.new_page()
                        try:
                            updated = _scrape_civicclerk_meeting(pg, m, nav_timeout_ms=nav_timeout_ms)
                        finally:
                            pg.close()

                    results.append(updated)

                except PlaywrightTimeoutError as te:
                    print(f"Salida: timeout for meeting {getattr(m,'id',None)}: {te!r}")
                    traceback.print_exc()
                except Exception as e:
                    print(f"Salida: error for meeting {getattr(m,'id',None)}: {e!r}")
                    traceback.print_exc()
                finally:
                    time.sleep(0.25)

        finally:
            try:
                context.close()
            except Exception:
                pass
            browser.close()

    return results


# ---------------------------------------------------------------------------
# Public entrypoint expected by scraper.main
# ---------------------------------------------------------------------------

def parse_salida(
    items: Optional[Union[str, Iterable[Union[Meeting, dict]]]] = None,
    *,
    headless: bool = True,
    nav_timeout_ms: int = 30000,
    reuse_single_page: bool = True,
) -> List[dict]:
    """
    Flexible wrapper used by scraper.main.

    Accepts either:
      • an iterable of Meeting-like dicts (each with at least {id, url}), or
      • a string path to a JSON file that contains such a list.

    Returns a list of plain dicts (safe for JSON serialization).
    """
    # Allow main.py to call us with a path or an iterable
    if isinstance(items, str):
        with open(items, "r", encoding="utf-8") as f:
            payload = json.load(f)
        meetings_iter = payload
    elif items is None:
        meetings_iter = []  # nothing to do, but keep call site happy
    else:
        meetings_iter = items

    results = process_meetings(
        meetings_iter,
        headless=headless,
        nav_timeout_ms=nav_timeout_ms,
        reuse_single_page=reuse_single_page,
    )
    return [asdict(m) for m in results]


# ---------------------------------------------------------------------------
# CLI convenience (optional)
# ---------------------------------------------------------------------------

def _load_meetings_from_json(path: str) -> List[Meeting]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    meetings: List[Meeting] = []
    for item in raw:
        if isinstance(item, dict):
            meetings.append(Meeting(**item))
        else:
            raise ValueError(f"Invalid meeting item in {path}: {item!r}")
    return meetings


def _save_meetings_to_json(path: str, meetings: List[Meeting]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(m) for m in meetings], f, ensure_ascii=False, indent=2)


def main(argv: List[str]) -> int:
    """
    Usage:
      python -m scraper.salida_civicclerk input_meetings.json output_meetings.json
    Where input JSON is a list of objects with at least { "id": int, "url": str }.
    """
    if len(argv) < 3:
        print("Usage: python -m scraper.salida_civicclerk <input.json> <output.json>")
        return 2

    inp, outp = argv[1], argv[2]
    try:
        meetings = _load_meetings_from_json(inp)
    except Exception as e:
        print(f"Salida: failed to load meetings from {inp}: {e!r}")
        traceback.print_exc()
        return 1

    results = process_meetings(meetings, headless=True, nav_timeout_ms=30000, reuse_single_page=True)

    try:
        _save_meetings_to_json(outp, results)
        print(f"Salida: wrote {len(results)} meetings to {outp}")
    except Exception as e:
        print(f"Salida: failed to write output {outp}: {e!r}")
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
