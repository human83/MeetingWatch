# scraper/salida_civicclerk.py
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

# Playwright (sync) is already installed/used elsewhere in the project
from playwright.sync_api import sync_playwright, Response, TimeoutError as PWTimeout


# ----------------------------
# Host configuration
# ----------------------------

# Primary and alternates can be set from workflow env
_DEFAULT_BASE = os.getenv("SALIDA_CIVICCLERK_URL", "https://salidaco.civicclerk.com")
_ALT = [
    h.strip()
    for h in os.getenv(
        "SALIDA_CIVICCLERK_ALT_HOSTS",
        "https://salidaco.civicclerk.com,https://cityofsalida.civicclerk.com,https://salidaco.portal.civicclerk.com",
    ).split(",")
    if h.strip()
]

_HOSTS_TO_TRY = []
for h in [_DEFAULT_BASE] + _ALT:
    if h and h not in _HOSTS_TO_TRY:
        _HOSTS_TO_TRY.append(h.rstrip("/"))

# where we try to find meetings tiles/listing on the site
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
    Very light HTML scan in case the page renders anchors server-side.
    (Most CivicClerk tenants are JS, so this often returns nothing.)
    """
    out: List[Dict] = []
    try:
        html = requests.get(entry_url, timeout=20).text
    except Exception:
        return out

    soup = BeautifulSoup(html, "html.parser")
    # Look for civicclerk “event” links
    for a in soup.select("a[href*='/event/']"):
        href = a.get("href") or ""
        if not href.startswith("http"):
            href = entry_url.rstrip("/") + "/" + href.lstrip("/")
        # crude date text capture
        title = a.get_text(strip=True) or ""
        # Attempt to parse a date like "Oct 21, 2025"
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
    return out


def _playwright_candidates(entry_url: str) -> List[Dict]:
    """
    Use a quick tile/listing scan with Playwright to extract event cards/links.
    """
    out: List[Dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(entry_url, wait_until="domcontentloaded", timeout=30000)
            # Give the SPA a moment to render tiles/lists
            page.wait_for_timeout(1200)

            # Common CivicClerk tiles have anchors to /event/<id>
            anchors = page.locator("a[href*='/event/']").all()
            seen: Set[str] = set()
            for a in anchors:
                try:
                    href = a.get_attribute("href") or ""
                    if not href:
                        continue
                    if not href.startswith("http"):
                        href = entry_url.rstrip("/") + "/" + href.lstrip("/")
                    if href in seen:
                        continue
                    seen.add(href)
                    text = a.inner_text().strip()
                    # Try to extract a nice title/date
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
                except Exception:
                    continue
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
        for path in _ENTRY_PATHS:
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

def _click_and_sniff_pdf_api(page, *, menu_text: str = "Agenda Packet (PDF)") -> Optional[str]:
    """
    From a CivicClerk event Files page, click the 'Agenda Packet (PDF)' option
    and capture the network request to /v1/Meetings/GetMeetingFileStream(...).
    Returns the full API URL if seen, else None.
    """
    # Some tenants put “Meeting Files” behind a secondary tab; make sure we’re on it.
    try:
        files_tab = page.get_by_role("link", name=re.compile(r"Meeting\s+Files", re.I))
        if files_tab:
            files_tab.first.click(timeout=2000)
    except Exception:
        pass

    # The “download” icon opens a small MUI menu containing “Agenda Packet (PDF)”.
    # We can click the text directly; MUI renders it as a <li> / menuitem.
    # Set up a listener *before* clicking so we can capture the XHR.
    def _match_pdf(resp: Response) -> bool:
        u = resp.url or ""
        ct = resp.headers.get("content-type", "")
        return (
            "GetMeetingFileStream" in u
            and "application/pdf" in ct.lower()
            and resp.status == 200
        )

    # A few tenants return the file stream as a 302 first; also watch responses broadly.
    seen_url: Optional[str] = None

    def _on_response(resp: Response):
        nonlocal seen_url
        try:
            if _match_pdf(resp):
                seen_url = resp.url
        except Exception:
            pass

    page.on("response", _on_response)

    try:
        # Make the MUI menu visible (there are often multiple download icons; both work)
        # Prefer clicking the visible “Agenda Packet” section’s download icon,
        # but clicking the menu item text is usually enough (MUI handles focus).
        page.get_by_text(menu_text, exact=True).first.click(timeout=3000)
    except Exception:
        # If the direct text isn’t present yet, try opening the small download menu
        # by clicking the first visible download icon near “Agenda Packet”.
        try:
            # Common aria-label on icon buttons
            page.get_by_role("button", name=re.compile(r"download", re.I)).first.click(timeout=3000)
            page.get_by_text(menu_text, exact=True).first.click(timeout=3000)
        except Exception:
            pass

    # Give the XHR some time to fire and resolve
    for _ in range(30):
        if seen_url:
            break
        page.wait_for_timeout(200)
    return seen_url


def _derive_files_page(url: str) -> Optional[str]:
    """
    Normalize any event URL to the Files view:
      - /event/<id>/files
      - some tenants use /Event/<id>/Files
    """
    m = re.search(r"/event/(\d+)/", url, re.I)
    if not m:
        return None
    event_id = m.group(1)
    base = url.split("/event/")[0].rstrip("/")
    return f"{base}/event/{event_id}/files"


def find_agenda_pdf(source_url: str, soup: Optional[BeautifulSoup] = None) -> Optional[str]:
    """
    Try to find an agenda/packet PDF for a CivicClerk meeting page and return its absolute URL.
    This version handles SPA behavior by using Playwright and capturing the API stream URL.
    """
    # If we were accidentally given a direct file/agenda route, short-circuit:
    if "GetMeetingFileStream" in source_url:
        return source_url

    files_url = _derive_files_page(source_url) or source_url

    # Final guard: must be a CivicClerk tenant we understand
    tenant = _tenant_from_host(files_url)
    if not tenant:
        return None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(files_url, wait_until="domcontentloaded", timeout=30000)
            # Wait just a bit for client rendering to finish
            page.wait_for_timeout(1200)

            # Try to click the exact “Agenda Packet (PDF)” item and sniff the API call
            url = _click_and_sniff_pdf_api(page, menu_text="Agenda Packet (PDF)")
            if url:
                return url

            # Fallback: some tenants label it simply “Agenda (PDF)”
            url = _click_and_sniff_pdf_api(page, menu_text="Agenda (PDF)")
            if url:
                return url

            # Last resort: if a plain-text variant exists we can still summarize text
            # (not perfect, but keeps us from returning nothing)
            try:
                page.get_by_text("Agenda Packet (Plain Text)", exact=True).first.click(timeout=2000)
                # Look for any request that returns text/plain
                captured: Optional[str] = None

                def _on_resp_plain(resp: Response):
                    nonlocal captured
                    if "text/plain" in (resp.headers.get("content-type", "").lower()):
                        captured = resp.url

                page.on("response", _on_resp_plain)
                for _ in range(25):
                    if captured:
                        return captured
                    page.wait_for_timeout(160)
            except Exception:
                pass

            return None
        finally:
            ctx.close()
            browser.close()


# ----------------------------
# Dev/test hook
# ----------------------------

if __name__ == "__main__":  # pragma: no cover
    # quick manual smoke run for local testing
    data = parse_salida()
    print(json.dumps(data, indent=2))

    # optional: try resolving an agenda for the first Salida item
    for m in data:
        if "salida" in (m.get("source", "") + m.get("url", "")).lower():
            print("Resolving agenda for:", m.get("url"))
            a = find_agenda_pdf(m["url"])
            print("Agenda URL:", a)
            break
