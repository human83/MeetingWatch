# scraper/salida_civicclerk.py
from __future__ import annotations

import os
import re
import sys
import time
import json
import traceback
from typing import List, Dict, Optional, Tuple

from bs4 import BeautifulSoup  # type: ignore
import requests  # type: ignore

# Playwright is optional at import time; we handle absence at runtime
try:
    from playwright.sync_api import sync_playwright, Page  # type: ignore
except Exception:  # pragma: no cover
    sync_playwright = None
    Page = object  # type: ignore

# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

CITY_NAME = "Salida"
PROVIDER = "CivicClerk"

# This URL works for many CivicClerk instances; adjust if your site differs.
# If you need to override in CI, set SALIDA_CIVICCLERK_URL in the workflow env.
SALIDA_BASE_URL = os.getenv(
    "SALIDA_CIVICCLERK_URL",
    "https://salida.civicclerk.com"
)

# Common entry points to try (some tenants use /Meetings, others use /Meetings?*, etc.)
ENTRY_PATHS = [
    "/en-US/Meetings",
    "/Meetings",
    "/",
]

# Cap how many tiles/links we’ll scan so we don’t waste cycles
MAX_TILES = int(os.getenv("CIVICCLERK_MAX_TILES", "80"))

# ---------------------------------------------------------------------
# Date parsing (tolerant)
# ---------------------------------------------------------------------

from dateutil import parser as _dtparser  # type: ignore

_ORDINAL_RE = re.compile(r'(\d+)(st|nd|rd|th)\b', flags=re.I)

def _parse_date(text: str) -> Optional[str]:
    """
    Best-effort parser for CivicClerk-style date strings.
    Accepts:
      - "October 23, 2025"
      - "Oct 23, 2025, 6:00 PM"
      - "Wed, Oct 23, 2025 at 6:00 PM"
      - "10/23/2025 6:00 PM"
      - "2025-10-23T18:00:00-06:00", etc.
    Returns ISO date (YYYY-MM-DD) or None.
    """
    if not text:
        return None

    t = " ".join(str(text).strip().split())
    t = _ORDINAL_RE.sub(r"\1", t)
    # Make the very common " at " neutral for parsing
    t = re.sub(r"\s+at\s+", " ", t, flags=re.I)

    try:
        dt = _dtparser.parse(t, fuzzy=True, dayfirst=False)
        return dt.date().isoformat()
    except Exception:
        # Last-ditch: strip an hh:mm (AM/PM) bit and try again
        t2 = re.split(r'\b\d{1,2}:\d{2}\s*(AM|PM)?\b', t, maxsplit=1, flags=re.I)[0]
        try:
            dt = _dtparser.parse(t2, fuzzy=True, dayfirst=False)
            return dt.date().isoformat()
        except Exception:
            return None

# ---------------------------------------------------------------------
# Tile/date extractors
# ---------------------------------------------------------------------

def _clean(s: Optional[str]) -> str:
    return " ".join((s or "").split())

def _extract_date_text_from_tile(page_or_elem) -> Optional[str]:
    """
    Given a Playwright Locator/ElementHandle or a bs4 tag, try common places
    CivicClerk stores its date/time.
    """
    # Playwright path
    try:
        # prefer machine-readable attrs first on obvious nodes
        for sel in ["time[datetime]", "time", ".meeting-date", ".date", "[data-date]", "[data-start]"]:
            try:
                loc = page_or_elem.locator(sel).first
                if getattr(loc, "count", lambda: 0)():
                    for attr in ("datetime", "data-date", "data-start", "aria-label", "title"):
                        try:
                            val = loc.get_attribute(attr)
                            if val and val.strip():
                                return _clean(val)
                        except Exception:
                            pass
                    txt = loc.text_content()
                    if txt and txt.strip():
                        return _clean(txt)
            except Exception:
                pass

        # sometimes the card itself carries labels
        for attr in ("aria-label", "title", "data-date", "data-start"):
            try:
                val = page_or_elem.get_attribute(attr)
                if val and val.strip():
                    return _clean(val)
            except Exception:
                pass
    except AttributeError:
        # bs4 path
        tag = page_or_elem
        # Within children
        for sel in ["time", ".meeting-date", ".date", "[data-date]", "[data-start]"]:
            try:
                found = tag.select_one(sel)
                if found:
                    # attributes first
                    for attr in ("datetime", "data-date", "data-start", "aria-label", "title"):
                        val = found.get(attr)
                        if val and str(val).strip():
                            return _clean(str(val))
                    # then text
                    txt = found.get_text(" ", strip=True)
                    if txt:
                        return _clean(txt)
            except Exception:
                pass

        # attributes on the tile itself
        for attr in ("aria-label", "title", "data-date", "data-start"):
            val = tag.get(attr)
            if val and str(val).strip():
                return _clean(str(val))

    return None

def _extract_title_from_tile(page_or_elem) -> Optional[str]:
    try:
        # Playwright
        for sel in ["h3", "h4", ".meeting-title", ".title", "a", "[role='link']"]:
            try:
                loc = page_or_elem.locator(sel).first
                if getattr(loc, "count", lambda: 0)():
                    txt = loc.text_content()
                    if txt and txt.strip():
                        return _clean(txt)
            except Exception:
                pass
        # Fallback to overall text
        try:
            txt = page_or_elem.text_content()
            if txt and txt.strip():
                return _clean(txt)
        except Exception:
            pass
    except AttributeError:
        # bs4
        tag = page_or_elem
        for sel in ["h3", "h4", ".meeting-title", ".title", "a", "[role='link']"]:
            found = tag.select_one(sel)
            if found:
                txt = found.get_text(" ", strip=True)
                if txt:
                    return _clean(txt)
        txt = tag.get_text(" ", strip=True)
        if txt:
            return _clean(txt)

    return None

def _extract_href_from_tile(page_or_elem) -> Optional[str]:
    """Find a likely meeting detail link."""
    try:
        # Playwright
        for sel in ["a[href*='Meeting']", "a[href*='meeting']", "a[href*='Agenda']", "a[href]"]:
            try:
                loc = page_or_elem.locator(sel).first
                if getattr(loc, "count", lambda: 0)():
                    href = loc.get_attribute("href")
                    if href and href.strip():
                        return href
            except Exception:
                pass
        return None
    except AttributeError:
        # bs4
        tag = page_or_elem
        for a in tag.select("a[href]"):
            href = a.get("href") or ""
            if any(k in href for k in ("Meeting", "meeting", "Agenda", "agenda")):
                return href
        a = tag.select_one("a[href]")
        if a:
            return a.get("href")
        return None

# ---------------------------------------------------------------------
# HTTP fallback (for light pages that don’t require JS)
# ---------------------------------------------------------------------

def _requests_candidates(url: str) -> List[Dict]:
    out: List[Dict] = []
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        tiles = soup.select("[role='link'], a.meeting, .meeting, .tile, .card")
        if not tiles:
            tiles = soup.select("a, article, li")

        for tag in tiles[:MAX_TILES]:
            href = _extract_href_from_tile(tag)
            date_text = _extract_date_text_from_tile(tag) or ""
            parsed_date = _parse_date(date_text)
            if not parsed_date:
                print(f"[salida] Skip: could not parse date from: {date_text!r}")
                continue

            title = _extract_title_from_tile(tag) or "Meeting"
            # Normalize absolute URL
            full_url = href if href and href.startswith("http") else (SALIDA_BASE_URL.rstrip("/") + "/" + (href or "").lstrip("/"))
            out.append({
                "city": CITY_NAME,
                "provider": PROVIDER,
                "title": title,
                "date": parsed_date,
                "url": full_url,
                "source": url,
            })
    except Exception as e:
        print(f"[salida] requests fallback failed: {e}")
        traceback.print_exc()

    return out

# ---------------------------------------------------------------------
# Main scrape
# ---------------------------------------------------------------------

def _playwright_candidates(entry_url: str) -> List[Dict]:
    out: List[Dict] = []
    if sync_playwright is None:  # Playwright not available
        return out

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(20000)

            print(f"[salida] Navigating to {entry_url}")
            page.goto(entry_url, wait_until="domcontentloaded")

            # Try to find anchor links that look like meeting detail links
            hrefs = set()
            for sel in [
                "a[href*='Meeting']",
                "a[href*='meeting']",
                "a[href*='Agenda']",
                "a[href*='agenda']",
            ]:
                try:
                    for a in page.locator(sel).all()[:MAX_TILES]:
                        href = a.get_attribute("href")
                        if href:
                            hrefs.add(href)
                except Exception:
                    pass

            if hrefs:
                print(f"[salida] Found candidate hrefs: {len(hrefs)}")
                # Build minimal records; dates may come from the card or the target
                tiles = page.locator("[role='link'], a.meeting, .meeting, .tile, .card")
            else:
                print(f"[salida] No hrefs; falling back to scanning tiles")
                tiles = page.locator("[role='link'], a.meeting, .meeting, .tile, .card")
                if tiles.count() == 0:
                    tiles = page.locator("a, article, li")

            n_tiles = min(tiles.count(), MAX_TILES)
            print(f"[salida] After tile-scan, candidates: {n_tiles}")

            for i in range(n_tiles):
                tile = tiles.nth(i)
                date_text = _extract_date_text_from_tile(tile) or ""
                parsed_date = _parse_date(date_text)
                if not parsed_date:
                    print(f"[salida] Skip: could not parse date from: {date_text!r}")
                    continue

                title = _extract_title_from_tile(tile) or "Meeting"
                href = _extract_href_from_tile(tile) or ""
                full_url = href if href.startswith("http") else (SALIDA_BASE_URL.rstrip("/") + "/" + href.lstrip("/"))

                out.append({
                    "city": CITY_NAME,
                    "provider": PROVIDER,
                    "title": title,
                    "date": parsed_date,
                    "url": full_url,
                    "source": entry_url,
                })

        finally:
            browser.close()

    return out

# ---------------------------------------------------------------------
# Public API expected by scraper.main
# ---------------------------------------------------------------------

def parse_salida() -> List[Dict]:
    """
    Scrape upcoming/visible meetings for Salida (CivicClerk) and return
    a list of dicts with keys:
      city, provider, title, date (YYYY-MM-DD), url, source
    """
    # Try multiple known entry points until we get results
    tried_urls: List[str] = []
    results: List[Dict] = []

    for path in ENTRY_PATHS:
        entry = SALIDA_BASE_URL.rstrip("/") + path
        tried_urls.append(entry)

        # Prefer Playwright (CivicClerk is often JS-rendered)
        items = _playwright_candidates(entry)
        if not items:
            # fallback to requests/bs4 (in case the page is simple)
            items = _requests_candidates(entry)

        if items:
            results.extend(items)
            break

    # De-dup by (date, title, url)
    seen: set[Tuple[str, str, str]] = set()
    unique: List[Dict] = []
    for m in results:
        key = (m.get("date", ""), m.get("title", ""), m.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique.append(m)

    print(f"[salida] Visited {len(tried_urls)} entry url(s); accepted {len(unique)} items")
    return unique


# Manual test (optional): python -m scraper.salida_civicclerk
if __name__ == "__main__":  # pragma: no cover
    data = parse_salida()
    print(json.dumps(data, indent=2))
