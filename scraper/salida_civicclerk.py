# scraper/salida_civicclerk.py
from __future__ import annotations

import os
import re
import html
from datetime import date
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Optional, Tuple, Iterable, Set

import requests  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
from dateutil import parser as _dtparser  # type: ignore

# Playwright is optional (used to render JS-driven CivicClerk pages)
try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

# ---------- configuration ----------
CITY_NAME = "Salida"
PROVIDER = "CivicClerk"

# Default to the PORTAL host (UI). We’ll derive the API host from it.
SALIDA_BASE_URL = os.getenv(
    "SALIDA_CIVICCLERK_URL", "https://salidaco.portal.civicclerk.com"
).rstrip("/")

ALT_HOSTS: List[str] = [
    h.strip().rstrip("/")
    for h in os.getenv("SALIDA_CIVICCLERK_ALT_HOSTS", "").split(",")
    if h.strip()
]

# Reasonable entry paths for CivicClerk portals
ENTRY_PATHS = ["/", "/Meetings", "/en-US/Meetings", "/en/Meetings", "/en-US", "/en"]

MAX_TILES = int(os.getenv("CIVICCLERK_MAX_TILES", "160"))
MAX_DISCOVERY_PAGES = int(os.getenv("CIVICCLERK_MAX_DISCOVERY", "25"))

SALIDA_DEBUG = os.getenv("SALIDA_DEBUG", "0") == "1"
AGENDA_KEYWORDS = ("agenda", "packet")

UA = {"User-Agent": "MeetingWatch/1.0 (+https://github.com/human83/MeetingWatch)"}

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

_ONCLICK_URL_RE = re.compile(r"""(?:location\.href\s*=\s*|window\.open\()\s*['"]([^'"]+)['"]""", re.I)

def _get_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, timeout=30, headers=UA)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def _same_site(a: str, b: str) -> bool:
    try:
        ha, hb = urlparse(a).hostname or "", urlparse(b).hostname or ""
        return ha.split(":")[0].endswith("civicclerk.com") and hb.split(":")[0].endswith("civicclerk.com")
    except Exception:
        return False

def _normalize(base: str, href: str) -> str:
    return urljoin(base, (href or "").strip())

def _extract_text(tag) -> str:
    t = tag.get_text(" ", strip=True) if getattr(tag, "get_text", None) else ""
    aria = (tag.get("aria-label") or "") if getattr(tag, "get", None) else ""
    title = (tag.get("title") or "") if getattr(tag, "get", None) else ""
    return " ".join([t, aria, title]).strip()

def _extract_title_from_bs4(tag) -> str:
    t = _extract_text(tag)
    # Shorten over-verbose tiles
    return (t[:150] + "…") if len(t) > 150 else t

def _scan_tiles_bs4(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    items: List[Dict] = []
    tiles = soup.select(LIKELY_TILE_SEL)[:MAX_TILES]
    for tag in tiles:
        # peel a candidate URL
        href = (getattr(tag, "get", lambda *_: None)("href") or "").strip()
        if not href:
            onclick = getattr(tag, "get", lambda *_: None)("onclick") or ""
            m = _ONCLICK_URL_RE.search(onclick)
            if m:
                href = m.group(1)
        if not href:
            continue

        full = _normalize(source_url, href)
        if not _same_site(source_url, full):
            continue

        # try to find a date text within the tile
        iso = None
        for csel in [LIKELY_TIME_CHILDREN]:
            for c in tag.select(csel):
                dtxt = _extract_text(c)
                iso = _parse_date(dtxt)
                if iso:
                    break
            if iso:
                break

        title = _extract_title_from_bs4(tag) or "Meeting"

        items.append(
            {
                "city": CITY_NAME,
                "provider": PROVIDER,
                "title": title,
                "date": iso or "",
                "url": full,
                "source": source_url,
            }
        )
    return items

# ---------- requests path (fallback discovery) ----------
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
                target = m.group(1)
        if not target:
            continue
        h = (target or "").lower()
        t = (text or "").lower()
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

# ---------- playwright path (preferred discovery on SPA/JS pages) ----------
def _playwright_candidates(entry_url: str) -> List[Dict]:
    out: List[Dict] = []
    if sync_playwright is None:
        return out

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(25000)

            if SALIDA_DEBUG:
                print(f"[salida] Navigating to {entry_url}")
            page.goto(entry_url, wait_until="networkidle")

            # Scan current page
            html_doc = page.content()
            soup = BeautifulSoup(html_doc, "html.parser")
            out.extend(_scan_tiles_bs4(soup, entry_url))

            # If none, walk a few candidate links and repeat
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
                            target = m.group(1)
                    if not target:
                        continue
                    h = (target or "").lower()
                    t = (text or "").lower()
                    if any(w in h for w in PRI_WORDS) or any(w in t for w in PRI_WORDS):
                        full = _normalize(entry_url, target)
                        if _same_site(entry_url, full):
                            candidates.append(full)
                except Exception:
                    pass

            seen: Set[str] = set()
            for target in candidates[:MAX_DISCOVERY_PAGES]:
                if target in seen:
                    continue
                seen.add(target)
                try:
                    page.goto(target, wait_until="networkidle")
                    html_doc = page.content()
                    soup = BeautifulSoup(html_doc, "html.parser")
                    sub = _scan_tiles_bs4(soup, target)
                    if sub:
                        out.extend(sub)
                        break
                except Exception:
                    pass
        finally:
            browser.close()
    return out

# ---------- CivicClerk API helpers ----------
_SUB_RE = re.compile(r"^([a-z0-9-]+)(?:\.portal)?\.civicclerk\.com$", re.I)

def _api_base_from_portal(url_or_host: str) -> str:
    host = urlparse(url_or_host).hostname or url_or_host
    m = _SUB_RE.search(host or "")
    sub = m.group(1) if m else "salidaco"
    return f"https://{sub}.api.civicclerk.com"

_FILEID_FROM_HREF = re.compile(r"/files/(?:agenda|packet)/(\d+)", re.I)

def _choose_best_file(label: str) -> int:
    """
    Simple numeric weight: higher is better.
      - Prefer 'Agenda Packet' > 'Agenda'
      - Avoid 'Minutes'
    """
    t = (label or "").lower()
    if "minutes" in t:
        return -100
    score = 0
    if "packet" in t:
        score += 50
    if "agenda" in t:
        score += 30
    if "regular" in t or "work session" in t or "council" in t:
        score += 3
    return score

def _extract_file_candidates_from_soup(soup: BeautifulSoup, base_url: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not href:
            continue
        lab = " ".join([
            a.get_text(" ", strip=True) or "",
            a.get("aria-label", "") or "",
            a.get("title", "") or "",
            a.get("data-file-name", "") or "",
        ]).strip()
        m = _FILEID_FROM_HREF.search(href)
        if m:
            fid = m.group(1)
            w = _choose_best_file(lab)
            out.append((w, fid))
    # Also catch viewer already open (pdf.js route sometimes embeds a link)
    viewer_src = soup.select_one("iframe[src], embed[src]")
    if viewer_src and viewer_src.get("src"):
        m2 = _FILEID_FROM_HREF.search(viewer_src.get("src", ""))
        if m2:
            out.append((_choose_best_file("Agenda Packet"), m2.group(1)))
    # Best-first
    out.sort(reverse=True, key=lambda t: t[0])
    return out

def _extract_file_candidates_with_playwright(files_url: str) -> List[Tuple[int, str]]:
    if sync_playwright is None:
        return []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(25000)
            page.goto(files_url, wait_until="networkidle")
            html_doc = page.content()
            soup = BeautifulSoup(html_doc, "html.parser")
            return _extract_file_candidates_from_soup(soup, files_url)
        finally:
            browser.close()

# ---------- public API ----------
def _hosts_to_try() -> Iterable[str]:
    tried = [SALIDA_BASE_URL] + ALT_HOSTS
    seen: Set[str] = set()
    for h in tried:
        if h and h not in seen:
            seen.add(h)
            yield h

def parse_salida() -> List[Dict]:
    tried_urls: List[str] = []
    results: List[Dict] = []

    # Crawl likely entry points; prefer Playwright discovery
    for host in _hosts_to_try():
        for path in ENTRY_PATHS:
            entry = (host + path).rstrip("/")
            tried_urls.append(entry)

            items = _playwright_candidates(entry)
            if not items:
                items = _requests_candidates(entry)

            if items:
                results.extend(items)
            if results:
                break
        if results:
            break

    # de-dup
    seen: Set[Tuple[str, str, str]] = set()
    unique: List[Dict] = []
    for m in results:
        key = (m.get("date", "") or "", m.get("title", "") or "", m.get("url", "") or "")
        if key not in seen:
            seen.add(key)
            unique.append(m)

    # Enrich: find agenda URLs (PDF + plain text via CivicClerk API)
    for m in unique:
        u = (m.get("url") or "").strip()

        # If we already have a direct PDF, mirror to agenda_url
        if u.lower().endswith(".pdf"):
            m["agenda_url"] = u
            continue

        # Resolve event files URL
        source_for_files = u or (m.get("source") or "").strip()
        agenda_pdf, agenda_txt = find_agenda_pdf(source_for_files)
        if agenda_pdf:
            m["agenda_url"] = agenda_pdf
        if agenda_txt:
            m["agenda_text_url"] = agenda_txt

    print(f"[salida] Visited {len(tried_urls)} entry url(s); accepted {len(unique)} items")
    return unique

def find_agenda_pdf(source_url: str, soup: Optional[BeautifulSoup] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (pdf_url, plain_text_url) for the best Salida agenda/packet found on an
    event's *files page*. Handles viewer subroutes and JS-rendered lists.

    Strategy:
      1) Normalize to /event/<id>/files
      2) Prefer Playwright to render and collect candidate fileIds.
      3) Fallback to requests+BeautifulSoup.
      4) Build API GetMeetingFileStream URLs for PDF and plain text.
    """
    # 1) Normalize to /files
    m = re.search(r"(/event/\d+)(/files)?", urlparse(source_url).path or "")
    if m and not m.group(2):
        source_url = urljoin(source_url, m.group(1) + "/files")

    # 2) Try Playwright first (JS-rendered)
    file_candidates: List[Tuple[int, str]] = []
    try:
        file_candidates = _extract_file_candidates_with_playwright(source_url)
    except Exception:
        file_candidates = []

    # 3) Fallback: requests + BS4 (if nothing from Playwright or Playwright unavailable)
    if not file_candidates:
        try:
            if soup is None:
                r = requests.get(source_url, timeout=30, headers=UA)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "html.parser")
            file_candidates = _extract_file_candidates_from_soup(soup, source_url)
        except Exception:
            file_candidates = []

    if not file_candidates:
        if SALIDA_DEBUG:
            print(f"[salida] No agenda fileIds found on {source_url}")
        return None, None

    # Pick best candidate by weight
    _, file_id = file_candidates[0]

    # 4) Build API URLs
    api_base = _api_base_from_portal(source_url)
    pdf_url = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
    txt_url = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=true)"

    if SALIDA_DEBUG:
        print(f"[salida] agenda fileId={file_id} -> {pdf_url}")

    return pdf_url, txt_url
