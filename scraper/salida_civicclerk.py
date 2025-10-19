# scraper/salida_civicclerk.py
from __future__ import annotations

import os
import re
import json
from datetime import date
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

# ---------- configuration ----------
CITY_NAME = "Salida"
PROVIDER = "CivicClerk"

SALIDA_BASE_URL = os.getenv(
    "SALIDA_CIVICCLERK_URL", "https://salidaco.civicclerk.com"
).rstrip("/")

ALT_HOSTS: List[str] = [
    h.strip().rstrip("/")
    for h in os.getenv("SALIDA_CIVICCLERK_ALT_HOSTS", "").split(",")
    if h.strip()
]

ENTRY_PATHS = ["/", "/Meetings", "/en-US/Meetings", "/en/Meetings", "/en-US", "/en"]

MAX_TILES = int(os.getenv("CIVICCLERK_MAX_TILES", "160"))
MAX_DISCOVERY_PAGES = int(os.getenv("CIVICCLERK_MAX_DISCOVERY", "25"))

SALIDA_DEBUG = os.getenv("SALIDA_DEBUG", "0") == "1"
AGENDA_KEYWORDS = ("agenda", "packet")

# ---------- date helpers ----------

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", flags=re.I)
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


# ---------- HTML helpers ----------

LIKELY_TILE_SEL = "[role='link'], a.meeting, .meeting, .tile, .card, article, li, .Row, .ListItem"
LIKELY_TIME_CHILDREN = "time[datetime], time, .meeting-date, .date, [data-date], [data-start]"
PRI_WORDS = ("meeting", "agenda", "packet", "council", "board", "commission")

_ONCLICK_URL_RE = re.compile(
    r"""(?:location(?:\.href)?|window\.location(?:\.href)?)\s*=\s*(['"])(.+?)\1""",
    re.I,
)


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


def _extract_meeting_href(tag) -> Optional[str]:
    """
    CivicClerk often renders:
      - <a href="#"> with data-href/data-url/data-link
      - <div onclick="location.href='/en/Meetings/Details/...'">
    This extracts the *real* target when present.
    """
    # 1) direct anchor with a usable href
    for a in tag.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        bad = href == "#" or href.lower().startswith("javascript:")
        if bad:
            # try data-* on the anchor
            via_data = a.get("data-href") or a.get("data-url") or a.get("data-link")
            if via_data:
                return via_data.strip()
            # or onclick on the anchor
            onclick = a.get("onclick") or ""
            m = _ONCLICK_URL_RE.search(onclick)
            if m:
                return m.group(2).strip()
            continue
        return href

    # 2) any element with data-* pointing to a url
    data = _first_attr(tag, "data-href", "data-url", "data-link")
    if data:
        return data

    # 3) onclick='location.href="..."'
    onclick = tag.get("onclick") or ""
    m = _ONCLICK_URL_RE.search(onclick)
    if m:
        return m.group(2).strip()

    return None


def _find_agenda_pdf(source_url: str, soup: Optional[BeautifulSoup] = None) -> Optional[str]:
    """Try to find an agenda/packet PDF on a meeting page and return absolute URL or None."""
    try:
        if soup is None:
            soup = _get_soup(source_url)
        if not soup:
            return None

        # Prefer links that look like agendas/packets and end with .pdf
        for a in soup.select("a[href$='.pdf'], a[href*='Agenda' i][href], a[href*='Packet' i][href]"):
            href = (a.get("href") or "").strip()
            text = _extract_text(a).lower()
            h = href.lower()
            if ".pdf" in h and (any(k in h for k in AGENDA_KEYWORDS) or any(k in text for k in AGENDA_KEYWORDS)):
                return _normalize(source_url, href)

        # Fallback: any PDF on the page
        a = soup.select_one("a[href$='.pdf']")
        if a and a.get("href"):
            return _normalize(source_url, a.get("href"))
    except Exception:
        pass
    return None


def _scan_tiles_bs4(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    out: List[Dict] = []
    tiles = soup.select(LIKELY_TILE_SEL)
    if not tiles:
        tiles = soup.select("a, article, li, div")

    for tag in tiles[:MAX_TILES]:
        # find the real target url (handle data-* and onclick)
        raw_target = _extract_meeting_href(tag)
        if not raw_target:
            continue

        full = _normalize(source_url, raw_target)
        # ignore if it resolves back to the same page root
        if full.rstrip("/") in (source_url.rstrip("/"), urljoin(source_url + "/", "#").rstrip("/")):
            continue

        dt_text = _extract_date_text_from_bs4(tag) or _extract_text(tag)
        iso = _parse_date(dt_text)
        if not iso:
            # fallback: scan <time> elsewhere
            for tm in soup.select("time[datetime], time"):
                iso = _parse_date(_first_attr(tm, "datetime") or _extract_text(tm) or "")
                if iso:
                    break
        if not iso:
            if SALIDA_DEBUG:
                print(f"[salida] Skip tile (no date): {full}")
            continue

        # Skip past dates (keep today and future)
        try:
            if iso < date.today().isoformat():
                if SALIDA_DEBUG:
                    print(f"[salida] Skip past date {iso} for {full}")
                continue
        except Exception:
            pass

        title = _extract_title_from_bs4(tag) or "Meeting"

        # Prefer a direct agenda/packet PDF when possible
        final_url = full
        try:
            if not final_url.lower().endswith(".pdf"):
                pdf = _find_agenda_pdf(final_url)
                if pdf:
                    if SALIDA_DEBUG:
                        print(f"[salida] Resolved agenda PDF: {final_url} -> {pdf}")
                    final_url = pdf
        except Exception:
            pass

        out.append(
            {
                "city": CITY_NAME,
                "provider": PROVIDER,
                "title": title,
                "date": iso,
                "url": final_url,
                "source": source_url,
            }
        )

    return out


# ---------- requests path ----------

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

    # Discovery: collect promising links, visit a few, scan again
    links: List[str] = []
    for a in soup.select("a[href], [onclick], [data-href], [data-url], [data-link]"):
        href = (a.get("href") or "").strip()
        text = _extract_text(a)
        data = a.get("data-href") or a.get("data-url") or a.get("data-link")
        onclick = a.get("onclick") or ""
        target = None
        if href and href != "#" and not href.lower().startswith("javascript:"):
            target = href
        elif data:
            target = data
        else:
            m = _ONCLICK_URL_RE.search(onclick)
            if m:
                target = m.group(2)
        if not target:
            continue
        h = (target or "").lower()
        t = text.lower()
        if any(w in h for w in PRI_WORDS) or any(w in t for w in PRI_WORDS):
            full = _normalize(url, target)
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


# ---------- playwright path ----------

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
            for sel in ["main", "#root", "[role='main']", ".meetings", ".agenda", ".List"]:
                try:
                    page.locator(sel).first.wait_for(timeout=2000)
                    break
                except Exception:
                    pass

            # Collect promising links for discovery (including onclick/data-* cases)
            candidates: List[str] = []
            anchors = page.locator("a, [onclick], [data-href], [data-url], [data-link]").all()
            for a in anchors:
                try:
                    href = (a.get_attribute("href") or "").strip()
                    text = (a.text_content() or "").strip()
                    data = a.get_attribute("data-href") or a.get_attribute("data-url") or a.get_attribute("data-link")
                    onclick = a.get_attribute("onclick") or ""
                    target = None
                    if href and href != "#" and not href.lower().startswith("javascript:"):
                        target = href
                    elif data:
                        target = data
                    else:
                        m = _ONCLICK_URL_RE.search(onclick or "")
                        if m:
                            target = m.group(2)
                    if not target:
                        continue
                    h = (target or "").lower()
                    t = text.lower()
                    if any(w in h for w in PRI_WORDS) or any(w in t for w in PRI_WORDS):
                        full = _normalize(entry_url, target)
                        if _same_site(entry_url, full):
                            candidates.append(full)
                except Exception:
                    pass

            # Scan current page
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


# ---------- public API ----------

def _hosts_to_try() -> Iterable[str]:
    tried = [SALIDA_BASE_URL] + ALT_HOSTS
    seen: Set[str] = set()
    for h in tried:
        if h and h not in seen:
            seen.add(h)
            yield h


# --- replace your current parse_salida() with this version ---

def parse_salida() -> List[Dict]:
    tried_urls: List[str] = []
    results: List[Dict] = []

    # Crawl a few likely entry points on a couple of hostnames
    for host in _hosts_to_try():
        for path in ENTRY_PATHS:
            entry = (host + path).rstrip("/")
            tried_urls.append(entry)

            # Prefer your Playwright path if it returns anything
            items = _playwright_candidates(entry)
            if not items:
                items = _requests_candidates(entry)

            if items:
                results.extend(items)
            if results:
                break
        if results:
            break

    # --- de-dup based on (date, title, url) like your original ---
    seen: Set[Tuple[str, str, str]] = set()
    unique: List[Dict] = []
    for m in results:
        key = (m.get("date", ""), m.get("title", ""), m.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique.append(m)

    # ---------- NEW: enrich each meeting with a direct agenda/packet PDF ----------
    for m in unique:
        # If we already have a direct PDF URL as "url", mirror it to agenda_url
        u = (m.get("url") or "").strip()
        if u.lower().endswith(".pdf"):
            m["agenda_url"] = u
            continue

        # Otherwise try to find the agenda/packet PDF on the event's files page.
        # Use "url" first, else fall back to source.
        source_for_files = u or (m.get("source") or "").strip()
        agenda = find_agenda_pdf(source_for_files)
        if agenda:
            m["agenda_url"] = agenda

    print(f"[salida] Visited {len(tried_urls)} entry url(s); accepted {len(unique)} items")
    return unique


def find_agenda_pdf(source_url: str, soup: Optional[BeautifulSoup] = None) -> Optional[str]:
    """
    Try to find an agenda/packet PDF on a CivicClerk *event files* page and
    return an absolute URL or None.

    Heuristics:
      - Prefer links with text/aria-label/title containing 'Agenda' or 'Packet'.
      - Exclude obvious 'Minutes' links.
      - Prefer URLs that end in .pdf.
      - If multiple candidates, choose the one that looks most like an agenda/packet.
    """
    # If caller accidentally passed a non-event page, try to reach the files tab.
    # e.g., /event/519  -> /event/519/files
    m = re.search(r"(/event/\d+)(/files)?/?$", source_url)
    if m and not m.group(2):
        source_url = urljoin(source_url, m.group(1) + "/files")

    try:
        if soup is None:
            r = requests.get(source_url, timeout=30)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

    # Collect all <a> tags with hrefs; CivicClerk also uses data-file-ext
    links: List[Tuple[str, str]] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        text = " ".join([
            a.get_text(" ", strip=True) or "",
            a.get("aria-label", "") or "",
            a.get("title", "") or "",
            a.get("data-file-name", "") or "",
        ]).strip()
        links.append((href, text))

    if not links:
        return None

    # Score links by how much they look like agendas/packets
    def score(href: str, text: str) -> int:
        t = f"{href} {text}".lower()

        # Base score if it's a PDF
        s = 10 if ".pdf" in href.lower() or href.lower().endswith(".pdf") else 0

        # Strong positive signals
        if "agenda" in t: s += 25
        if "packet" in t: s += 20
        if "board packet" in t or "meeting packet" in t or "council packet" in t: s += 5
        if "regular meeting" in t or "work session" in t: s += 3

        # Negative signals
        if "minutes" in t: s -= 30
        if "video" in t or "livestream" in t: s -= 15

        # Prefer shorter query/link clutter a bit less
        if "?" not in href: s += 2

        return s

    # Rank candidates
    scored: List[Tuple[int, str]] = []
    base = source_url
    for href, text in links:
        absu = urljoin(base, href)
        scored.append((score(href, text), absu))

    scored.sort(reverse=True, key=lambda x: x[0])

    # Top candidate must be a positive score and should end with .pdf ideally
    for s, u in scored:
        if s <= 0:
            break
        if u.lower().endswith(".pdf"):
            return u

    # If nothing ended in .pdf but we have a positive candidate, return the best one
    return scored[0][1] if scored and scored[0][0] > 0 else None



if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(parse_salida(), indent=2))
