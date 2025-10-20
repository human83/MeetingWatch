# scraper/salida_civicclerk.py
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


# ----------------------------
# Host configuration
# ----------------------------

def _ensure_https(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return u
    if not u.startswith("http"):
        u = "https://" + u
    return u

def _to_portal_host(u: str) -> str:
    """
    Turn salidaco.civicclerk.com -> salidaco.portal.civicclerk.com
    Leave *.portal.civicclerk.com as-is.
    """
    try:
        p = urlparse(_ensure_https(u))
        host = p.netloc
        if not host:
            return urlunparse(p)
        if host.endswith(".portal.civicclerk.com"):
            return urlunparse(p)
        if host.endswith(".civicclerk.com"):
            parts = host.split(".")
            if len(parts) >= 3:  # e.g., salidaco.civicclerk.com
                parts.insert(-2, "portal")
                p = p._replace(netloc=".".join(parts))
                return urlunparse(p)
    except Exception:
        pass
    return _ensure_https(u)

def _unique(seq: List[str]) -> List[str]:
    out, seen = [], set()
    for s in seq:
        s = s.rstrip("/")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out

# Primary + alternates from env
_env_primary = os.getenv("SALIDA_CIVICCLERK_URL", "https://salidaco.portal.civicclerk.com")
_env_alts = os.getenv(
    "SALIDA_CIVICCLERK_ALT_HOSTS",
    "https://salidaco.civicclerk.com,https://cityofsalida.civicclerk.com",
)

_base_candidates = [_ensure_https(_env_primary)] + [
    _ensure_https(h.strip()) for h in _env_alts.split(",") if h.strip()
]

# Include each base + its portalized variant so we always try the right host
_HOSTS_TO_TRY = _unique(
    _base_candidates + [_to_portal_host(h) for h in _base_candidates]
)

# Where we look for the tiles/listing
_ENTRY_PATHS = ["/", "/Meetings", "/agendacenter"]


# ----------------------------
# Utilities
# ----------------------------

def _tenant_from_host(url: str) -> Optional[str]:
    """
    CivicClerk tenants look like 'salidaco' in:
      - https://salidaco.civicclerk.com
      - https://salidaco.portal.civicclerk.com
    """
    m = re.search(r"https?://([a-z0-9\-]+)\.(?:portal\.)?civicclerk\.com", url, re.I)
    return m.group(1) if m else None


def _requests_candidates(entry_url: str) -> List[Dict]:
    """
    Very light HTML scan (useful only if the app renders anchors server-side).
    Most CivicClerk portals are SPA and need Playwright, but keep this as a fallback.
    """
    out: List[Dict] = []
    try:
        html = requests.get(entry_url, timeout=20).text
    except Exception:
        return out

    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href*='/event/']"):
        href = a.get("href") or ""
        if not href.startswith("http"):
            href = entry_url.rstrip("/") + "/" + href.lstrip("/")
        text = a.get_text(strip=True) or ""
        m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", text)
        date = m.group(1) if m else ""
        out.append(
            {
                "city": "Salida",
                "provider": "CivicClerk",
                "title": text or date or href,
                "date": date,
                "url": href,
                "source": entry_url,
            }
        )
    return out


def _playwright_candidates(entry_url: str) -> List[Dict]:
    """
    Use a robust tile/listing scan with Playwright to extract event links.
    - Forces portal host
    - Waits for hydration
    - Scrolls to trigger lazy loading
    - Extracts anchors via both Locator and JS DOM query (in case of timing issues)
    """
    out: List[Dict] = []
    entry_url = _to_portal_host(entry_url)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
        )

        # Speed up: drop heavy assets
        def _route(route):
            req = route.request
            if req.resource_type in {"image", "font", "media"}:
                return route.abort()
            return route.continue_()
        ctx.route("**/*", _route)

        page = ctx.new_page()
        try:
            page.goto(entry_url, wait_until="networkidle", timeout=45000)

            # The SPA sometimes needs an extra beat.
            page.wait_for_timeout(1500)

            # If list not present yet, try a deterministic selector then scroll.
            # Common containers for CivicClerk: any anchors with /event/ plus tiles.
            selectors = [
                "a[href*='/event/']",
                "div:has(a[href*='/event/']) a[href*='/event/']",
            ]

            found = set()

            # A few incremental scrolls to trigger lazy load
            for _ in range(8):
                try:
                    for sel in selectors:
                        els = page.locator(sel)
                        if els.count() > 0:
                            for i in range(min(200, els.count())):
                                try:
                                    href = els.nth(i).get_attribute("href") or ""
                                    if not href:
                                        continue
                                    if not href.startswith("http"):
                                        href = entry_url.rstrip("/") + "/" + href.lstrip("/")
                                    found.add(href)
                                except Exception:
                                    pass
                except Exception:
                    pass
                # Scroll a bit and give it a moment
                page.evaluate("window.scrollBy(0, document.body.scrollHeight / 3);")
                page.wait_for_timeout(500)

            # As a belt-and-suspenders fallback, run a DOM query in the page
            try:
                hrefs = page.evaluate(
                    """() => Array.from(document.querySelectorAll("a[href*='/event/']"))
                           .map(a => a.getAttribute('href') || '')
                           .filter(Boolean)"""
                )
                for href in hrefs:
                    if not href.startswith("http"):
                        href = entry_url.rstrip("/") + "/" + href.lstrip("/")
                    found.add(href)
            except Exception:
                pass

            # Build items
            for href in sorted(found):
                # grab the tile text (best-effort)
                title = ""
                try:
                    # nearest text in the same tile row
                    node = page.locator(f"a[href$='{href.split('/')[-1]}']")
                    if node.count():
                        title = (node.first.inner_text() or "").strip()
                except Exception:
                    pass

                m = re.search(r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})", title)
                date = m.group(1) if m else ""
                out.append(
                    {
                        "city": "Salida",
                        "provider": "CivicClerk",
                        "title": title or date or href,
                        "date": date,
                        "url": href,
                        "source": entry_url,
                    }
                )

        finally:
            ctx.close()
            browser.close()

    return out


# ----------------------------
# Public: parse_salida
# ----------------------------

def parse_salida() -> List[Dict]:
    tried_urls: List[str] = []
    results: List[Dict] = []

    for host in _HOSTS_TO_TRY:
        portal_host = _to_portal_host(host)
        for path in _ENTRY_PATHS:
            entry = (portal_host + path).rstrip("/")
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

    # de-dup (date, title, url)
    seen: Set[Tuple[str, str, str]] = set()
    unique: List[Dict] = []
    for m in results:
        key = (m.get("date", ""), m.get("title", ""), m.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique.append(m)

    print(f"[salida] Visited {len(tried_urls)} entry url(s); accepted {len(unique)} items")
    return unique


# ----------------------------
# Agenda PDF resolver
# ----------------------------

def _ensure_portal_host(u: str) -> str:
    try:
        p = urlparse(_ensure_https(u))
        host = p.netloc
        if host.endswith(".portal.civicclerk.com"):
            return urlunparse(p)
        if host.endswith(".civicclerk.com"):
            parts = host.split(".")
            if len(parts) >= 3:
                parts.insert(-2, "portal")
                p = p._replace(netloc=".".join(parts))
                return urlunparse(p)
    except Exception:
        pass
    return _ensure_https(u)

def _tenant_only(u: str) -> str:
    host = urlparse(_ensure_https(u)).netloc
    return host.split(".")[0] if host else "salidaco"

def find_agenda_pdf(source_url: str, soup: Optional[BeautifulSoup] = None) -> Optional[str]:
    """
    For CivicClerk, the Files view exposes /files/agenda/<id> in the HTML or URL.
    Convert that to:
      https://{tenant}.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId=<id>,plainText=false)
    """
    try:
        normalized = _ensure_portal_host(source_url)

        if soup is None:
            try:
                r = requests.get(normalized, timeout=20)
                if r.status_code != 200:
                    return None
                soup = BeautifulSoup(r.text, "html.parser")
            except Exception:
                return None

        # If we're already on /files/agenda/<id>, grab it from the URL
        m = re.search(r"/files/agenda/(\d+)", normalized)
        if not m:
            # otherwise scan the HTML (left rail / viewer markup includes it)
            html = soup.decode() if hasattr(soup, "decode") else str(soup)
            m = re.search(r"/files/agenda/(\d+)", html)

        if not m:
            return None

        file_id = m.group(1)
        tenant = _tenant_only(normalized)
        return f"https://{tenant}.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
    except Exception:
        return None


# ----------------------------
# Dev/test hook
# ----------------------------

if __name__ == "__main__":  # pragma: no cover
    data = parse_salida()
    print(json.dumps(data, indent=2))

    for m in data:
        if "salida" in (m.get("source", "") + m.get("url", "")).lower():
            print("Resolving agenda for:", m.get("url"))
            a = find_agenda_pdf(m["url"])
            print("Agenda URL:", a)
            break
