# scraper/salida_civicclerk.py
from __future__ import annotations

import os
import re
import json
import traceback
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional, Tuple, Iterable, Set

import requests  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
from dateutil import parser as _dtparser  # type: ignore

# Playwright is optional
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

CITY_NAME = "Salida"
PROVIDER = "CivicClerk"

# Base host can be overridden in CI:
SALIDA_BASE_URL = os.getenv("SALIDA_CIVICCLERK_URL", "https://salida.civicclerk.com").rstrip("/")

# You can pass alternates e.g.: SALIDA_CIVICCLERK_ALT_HOSTS="https://salidaco.civicclerk.com,https://cityofsalida.civicclerk.com"
ALT_HOSTS: List[str] = [h.strip().rstrip("/") for h in os.getenv("SALIDA_CIVICCLERK_ALT_HOSTS", "").split(",") if h.strip()]

ENTRY_PATHS = [
    "/", "/Meetings", "/en-US/Meetings", "/en/Meetings", "/en-US", "/en",
]

MAX_TILES = int(os.getenv("CIVICCLERK_MAX_TILES", "120"))
MAX_DISCOVERY_PAGES = int(os.getenv("CIVICCLERK_MAX_DISCOVERY", "20"))

# ------------------ date parsing ------------------

_ORDINAL_RE = re.compile(r'(\d+)(st|nd|rd|th)\b', flags=re.I)

# very tolerant date tokens: "Oct 23, 2025", "Wednesday, Oct 23, 2025 at 6:00 PM", "10/23/2025 6:00 PM"
_FALLBACK_DATE_GUESS = re.compile(
    r"(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s*)?"
    r"([A-Za-z]{3,9}|\d{1,2})[\/\-\s.,]+(\d{1,2})[\/\-\s.,]+(\d{2,4})"
    r"(?:\s*[,@]*\s*\d{1,2}:\d{2}\s*(AM|PM)?)?",
    re.I,
)

def _clean(s: Optional[str]) -> str:
    return " ".join((s or "").split())

def _parse_date(text: str) -> Optional[str]:
    if not text:
        return None
    t = _ORDINAL_RE.sub(r"\1", _clean(text))
    t = re.sub(r"\s+at\s+", " ", t, flags=re.I)
    try:
        return _dtparser.parse(t, fuzzy=True, dayfirst=False).date().isoformat()
    except Exception:
        m = _FALLBACK_DATE_GUESS.search(t)
        if m:
            try:
                return _dtparser.parse(m.group(0), fuzzy=True, dayfirst=False).date().isoformat()
            except Exception:
                return None
        return None

# ------------------ HTML helpers ------------------

LIKELY_TILE_SEL = "[role='link'], a.meeting, .meeting, .tile, .card, article, li"
LIKELY_TIME_CHILDREN = "time[datetime], time, .meeting-date, .date, [data-date], [data-start]"
LIKELY_LINKS = "a[href*='Meeting'], a[href*='meeting'], a[href*='Agenda'], a[href*='agenda'], a[href]"

PRI_WORDS = ("meeting", "agenda", "packet", "council", "board", "commission")

def _same_site(base: str, href: str) -> bool:
    try:
        bu = urlparse(base)
        hu = urlparse(href)
        return (hu.netloc == "" or hu.netloc == bu.netloc) and (hu.scheme in ("", bu.scheme))
    except Exception:
        return True

def _normalize(base: str, href: str) -> str:
    if not href:
        return base
    return urljoin(base + "/", href)

def _extract_text(tag) -> str:
    try:
        return _clean(tag.get_text(" ", strip=True))
    except Exception:
        return ""

def _first_attr(tag, *names) -> Optional[str]:
    for n in names:
        v = tag.get(n)
        if v and str(v).strip():
            return _clean(str(v))
    return None

def _extract_date_text_from_bs4(tag) -> Optional[str]:
    # children first
    for sel in LIKELY_TIME_CHILDREN.split(","):
        sel = sel.strip()
        try:
            found = tag.select_one(sel)
        except Exception:
            found = None
        if found:
            a = _first_attr(found, "datetime", "data-date", "data-start", "aria-label", "title")
            if a:
                return a
            txt = _extract_text(found)
            if txt:
                return txt
    # tile attrs
    a = _first_attr(tag, "aria-label", "title", "data-date", "data-start")
    if a:
        return a
    return None

def _extract_title_from_bs4(tag) -> Optional[str]:
    for sel in ("h1", "h2", "h3", "h4", ".meeting-title", ".title", "a", "[role='link']"):
        try:
            node = tag.select_one(sel)
        except Exception:
            node = None
        if node:
            t = _extract_text(node)
            if t:
                return t
    t = _extract_text(tag)
    return t or None

def _scan_tiles_bs4(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    out: List[Dict] = []
    tiles = soup.select(LIKELY_TILE_SEL)
    if not tiles:
        tiles = soup.select("a, article, li, div")
    for tag in tiles[:MAX_TILES]:
        # try to find a link first
        href_tag = None
        for a in tag.select("a[href]"):
            href = a.get("href") or ""
            if any(k in href.lower() for k in ("meeting", "agenda", "packet")):
                href_tag = a
                break
        if not href_tag:
            href_tag = tag.select_one("a[href]")
        href = href_tag.get("href") if href_tag else None
        if not href:
            continue
        full = _normalize(source_url, href)

        dt_text = _extract_date_text_from_bs4(tag) or _extract_text(tag)
        iso = _parse_date(dt_text)
        if not iso:
            # fall back: look for a nearby time element anywhere on page
            times = soup.select("time[datetime], time")
            for tm in times:
                iso = _parse_date(_first_attr(tm, "datetime") or _extract_text(tm) or "")
                if iso:
                    break
        if not iso:
            # Still nothing; skip but log once per page
            # (stdout is fine in CI)
            print(f"[salida] Skip tile (no date): {full}")
            continue

        title = _extract_title_from_bs4(tag) or "Meeting"
        out.append({
            "city": CITY_NAME,
            "provider": PROVIDER,
            "title": title,
            "date": iso,
            "url": full,
            "source": source_url,
        })
    return out

# ------------------ requests path ------------------

def _get_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "MeetingWatch/1.0"})
        if r.status_code >= 400:
            print(f"[salida] HTTP {r.status_code} on {url}")
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"[salida] requests error {url}: {e}")
        return None

def _requests_candidates(url: str) -> List[Dict]:
    soup = _get_soup(url)
    if not soup:
        return []
    out = _scan_tiles_bs4(soup, url)
    if out:
        return out

    # If no tiles, do discovery on this page: collect promising links, visit a few, scan again
    links: List[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        text = _extract_text(a)
        h = href.lower()
        t = text.lower()
        if any(w in h for w in PRI_WORDS) or any(w in t for w in PRI_WORDS):
            full = _normalize(url, href)
            if _same_site(url, full):
                links.append(full)

    dedup: List[str] = []
    seen: Set[str] = set()
    for l in links:
        if l not in seen:
            seen.add(l)
            dedup.append(l)

    results: List[Dict] = []
    for target in dedup[:MAX_DISCOVERY_PAGES]:
        sub = _get_soup(target)
        if not sub:
            continue
        results.extend(_scan_tiles_bs4(sub, target))
        if results:
            break  # first success is enough

    return results

# ------------------ playwright path ------------------

def _playwright_candidates(entry_url: str) -> List[Dict]:
    out: List[Dict] = []
    if sync_playwright is None:
        return out
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(25000)

            print(f"[salida] Navigating to {entry_url}")
            page.goto(entry_url, wait_until="networkidle")

            # Try common SPA containers before scanning
            for sel in [
                "main",
                "#root",
                "[role='main']",
                ".meetings",
                ".agenda",
            ]:
                try:
                    page.locator(sel).first.wait_for(timeout=2000)
                    break
                except Exception:
                    pass

            # Collect promising onsite links for discovery
            candidates: List[str] = []
            anchors = page.locator("a[href]").all()
            for a in anchors:
                try:
                    href = a.get_attribute("href") or ""
                    text = (a.text_content() or "").strip()
                    h = href.lower()
                    t = text.lower()
                    if any(w in h for w in PRI_WORDS) or any(w in t for w in PRI_WORDS):
                        full = _normalize(entry_url, href)
                        if _same_site(entry_url, full):
                            candidates.append(full)
                except Exception:
                    pass

            # Also scan current page for tiles
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            out.extend(_scan_tiles_bs4(soup, entry_url))

            # If none, walk a few candidate links and repeat
            seen: Set[str] = set()
            for target in candidates[:MAX_DISCOVERY_PAGES]:
                if target in seen:
                    continue
                seen.add(target)
                try:
                    page.goto(target, wait_until="networkidle")
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    sub = _scan_tiles_bs4(soup, target)
                    if sub:
                        out.extend(sub)
                        break
                except Exception:
                    pass
        finally:
            browser.close()
    return out

# ------------------ public API ------------------

def _hosts_to_try() -> Iterable[str]:
    tried = [SALIDA_BASE_URL] + ALT_HOSTS
    # Remove duplicates while preserving order
    seen: Set[str] = set()
    for h in tried:
        if h and h not in seen:
            seen.add(h)
            yield h

def parse_salida() -> List[Dict]:
    tried_urls: List[str] = []
    results: List[Dict] = []

    for host in _hosts_to_try():
        for path in ENTRY_PATHS:
            entry = (host + path).rstrip("/")
            tried_urls.append(entry)

            items = _playwright_candidates(entry)
            if not items:
                items = _requests_candidates(entry)

            if items:
                results.extend(items)
                break
        if results:
            break

    # de-dup
    seen: Set[Tuple[str, str, str]] = set()
    unique: List[Dict] = []
    for m in results:
        key = (m.get("date", ""), m.get("title", ""), m.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique.append(m)

    print(f"[salida] Visited {len(tried_urls)} entry url(s); accepted {len(unique)} items")
    return unique

if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(parse_salida(), indent=2))
