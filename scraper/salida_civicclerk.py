
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Salida (CivicClerk) scraper – updated to robustly fetch Agenda text and generate bullets.

Key improvements:
- Uses Playwright to open each event page (hydrated SPA) and locate the Files/Agenda link.
- Prefers CivicClerk plaintext stream API (when available) to bypass PDF parsing entirely.
- Falls back to fetching the PDF stream and extracting text (pdfminer.six, then PyPDF2).
- Structured logging with clear stage tags and persistent debug fields:
  agenda_pdf_url, agenda_text_len, summary_error.
- Integrates with project's summarize_agenda() if available; otherwise heuristic fallback.
- Dedupes by a stable hash (title|datetime|agenda_url) and filters to today+future by default.

Drop-in expectations:
- The main entrypoint function is `parse_salida(entry_urls: list[str], upcoming_only=True)`,
  returning a list of dicts ready to be serialized to JSON by your pipeline.
- If your project calls a different function name, you can just adapt the call site.

Requirements:
- Playwright (python) installed and browsers bootstrapped (e.g., `playwright install`).
- pdfminer.six and PyPDF2 are optional but recommended for fallback.
"""

from __future__ import annotations

import io
import re
import os
import sys
import json
import time
import math
import hashlib
import logging
import datetime as dt
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
import urllib.parse as urlparse

import requests

# Playwright sync API
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except Exception as _e:
    sync_playwright = None  # allow import without Playwright for tools that only lint

# ---- Logging setup ----
LOG_LEVEL = os.environ.get("SALIDA_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

STAGE = "[SALIDA]"

# ---- Helpers ----

def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:10]

def _now_local_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()

def _is_upcoming(meeting_dt: Optional[dt.datetime]) -> bool:
    if not meeting_dt:
        return True  # if unknown, let it pass
    now = dt.datetime.now(tz=meeting_dt.tzinfo)
    return meeting_dt >= now.replace(microsecond=0)

def _req(session: Optional[requests.Session] = None) -> requests.Session:
    return session or requests.Session()

def _fetch(session: Optional[requests.Session], url: str, timeout: int = 30) -> requests.Response:
    s = _req(session)
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r

def _extract_pdf_text(pdf_bytes: bytes) -> str:
    text = ""
    # Try pdfminer
    try:
        from pdfminer.high_level import extract_text
        with io.BytesIO(pdf_bytes) as f:
            text = extract_text(f) or ""
    except Exception as e:
        logging.warning(f"{STAGE} pdfminer failed: {e}")
    # Fallback PyPDF2 if too short
    if len(text.strip()) < 200:
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:
            logging.warning(f"{STAGE} PyPDF2 failed: {e}")
    return text or ""

def _heuristic_bullets(text: str, max_bullets: int = 20) -> List[str]:
    lines = [ln.strip() for ln in text.splitlines()]
    cand = []
    for ln in lines:
        if not ln:
            continue
        if re.match(r"^(\d+[\).\s]|[-•–]\s)", ln):
            cand.append(ln)
        elif re.search(r"\b(Ordinance|Resolution|Public Hearing|Approval|Consent|Budget|Contract|Bid|Appointment|Report|Presentation|First Reading|Second Reading|Hearing)\b", ln, re.I):
            cand.append(ln)
    # de-dup & clean
    seen = set()
    cleaned = []
    for ln in cand:
        ln = re.sub(r"\s{2,}", " ", ln).strip()
        key = ln.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(ln)
    # bulletify
    bullets = []
    for ln in cleaned[:max_bullets]:
        ln = re.sub(r"^(\d+[\).\s]|[-•–]\s)", "", ln).strip()
        if len(ln) > 280:
            ln = ln[:277] + "…"
        bullets.append(f"• {ln}")
    return bullets

def _summarize_primary(text: str, limit: Optional[int] = None) -> List[str]:
    """
    Bridge to project's summarize_agenda().
    """
    try:
        from utils import summarize_agenda  # project function
        return summarize_agenda(text, max_bullets=limit)
    except Exception as e:
        logging.warning(f"{STAGE} primary summarizer unavailable/failed: {e}")
        return []

def _parse_meeting_datetime_str(s: str) -> Optional[dt.datetime]:
    """
    Attempt to parse common CivicClerk datetime strings.
    Examples: "Tuesday, October 22, 2025 6:00 PM"
    """
    s = re.sub(r"\s+", " ", s).strip()
    patterns = [
        "%A, %B %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%m/%d/%Y %I:%M %p",
        "%Y-%m-%d %H:%M",
    ]
    for p in patterns:
        try:
            return dt.datetime.strptime(s, p).astimezone()
        except Exception:
            continue
    return None

def _origin(url: str) -> str:
    u = urlparse.urlparse(url)
    return f"{u.scheme}://{u.netloc}"

def _resolve_url(base: str, href: str) -> str:
    return urlparse.urljoin(base, href)

# ---- CivicClerk specifics ----

def _find_agenda_link_on_event(page) -> Optional[str]:
    """
    On the hydrated event detail view, try to locate an "Agenda" file link.
    Strategy:
      - Look for elements containing text "Files" or "Agenda" and anchor children.
      - Otherwise, scrape all anchors; pick those with innerText containing "Agenda"
        or href containing '/files/agenda/' or '/GetMeetingFileStream'.
    Returns absolute URL or None.
    """
    # Try tabs/buttons that reveal files
    try:
        # Some portals use a "Files" tab/button
        files_button = page.locator("text=Files").first
        if files_button.count() > 0:
            files_button.click(timeout=3000)
            page.wait_for_timeout(200)  # brief settle
    except Exception:
        pass

    anchors = page.locator("a").all()
    candidates = []
    base = page.url
    for a in anchors:
        try:
            text = (a.inner_text() or "").strip()
            href = (a.get_attribute("href") or "").strip()
        except Exception:
            continue
        if not href:
            continue
        href_abs = _resolve_url(base, href)
        score = 0
        if re.search(r"\bagenda\b", text, re.I):
            score += 5
        if re.search(r"/files/agenda/|GetMeetingFileStream", href_abs, re.I):
            score += 5
        if href_abs.lower().endswith(".pdf"):
            score += 1
        if score > 0:
            candidates.append((score, href_abs, text))

    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], x[1]))
    best = candidates[0][1]
    return best

def _to_plaintext_endpoint(url: str) -> Optional[str]:
    """
    Given an Agenda URL, try to convert to the CivicClerk plaintext stream API.
    Accepts either /files/agenda/<id> or ...GetMeetingFileStream?fileId=<id>.
    """
    # Case 1: /files/agenda/<id>
    m = re.search(r"/files/agenda/(\d+)", url, re.I)
    if m:
        file_id = m.group(1)
        origin = _origin(url)
        return f"{origin}/WebAPI/MeetingFile/GetMeetingFileStream?fileId={file_id}&plainText=true"
    # Case 2: existing GetMeetingFileStream
    if "GetMeetingFileStream" in url:
        # ensure plainText=true
        parsed = urlparse.urlparse(url)
        qs = dict(urlparse.parse_qsl(parsed.query))
        qs["plainText"] = "true"
        new_q = urlparse.urlencode(qs)
        return urlparse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_q, parsed.fragment))
    return None

def _fetch_agenda_text(session: Optional[requests.Session], agenda_url: str) -> tuple[str, Optional[str]]:
    """
    Fetch agenda as text, preferring plaintext endpoint.
    Returns (agenda_text, agenda_pdf_url_if_any).
    """
    # Try plaintext endpoint
    pt = _to_plaintext_endpoint(agenda_url)
    if pt:
        try:
            r = _fetch(session, pt, timeout=30)
            # CivicClerk returns text/plain or application/json-text-likes
            text = r.text or ""
            if len(text.strip()) >= 100:
                return text, None  # no pdf url when plaintext succeeded
        except Exception as e:
            logging.info(f"{STAGE} plaintext endpoint failed ({e}); falling back to PDF")

    # Fall back to PDF
    pdf_url = agenda_url
    try:
        r = _fetch(session, pdf_url, timeout=45)
        text = _extract_pdf_text(r.content)
        return text, pdf_url
    except Exception as e:
        logging.warning(f"{STAGE} agenda PDF fetch/extract failed: {e}")
        return "", pdf_url

# ---- Data model ----

@dataclass
class SalidaItem:
    title: str
    meeting_datetime_iso: str
    source_url: str
    agenda_pdf_url: Optional[str]
    agenda_text_len: int
    bullets: List[str]
    summary_error: Optional[str]
    item_id: str

# ---- Core pipeline ----

def _summarize(text: str) -> List[str]:
    if not text or len(text.strip()) < 40:
        return []
    # Project primary
    bullets = _summarize_primary(text, limit=None) or []
    if bullets:
        return bullets
    # Fallback heuristic
    return _heuristic_bullets(text, max_bullets=20)

def _extract_title_and_dt_from_event(page) -> tuple[str, Optional[dt.datetime]]:
    """
    Attempt to capture title and meeting datetime from the event page content.
    """
    content_txt = ""
    try:
        content_txt = page.locator("body").inner_text()
    except Exception:
        pass

    # Title: prefer a prominent heading
    title = ""
    try:
        for sel in ["h1", "h2", "header h1", "header h2", "[data-testid='event-title']"]:
            loc = page.locator(sel).first
            if loc.count() > 0:
                t = (loc.inner_text() or "").strip()
                if t:
                    title = t
                    break
    except Exception:
        pass
    if not title:
        # fallback: first line in body
        title = (content_txt.splitlines() or [""])[0].strip()

    # Datetime: search for usual patterns in the page text
    mt = None
    for ln in content_txt.splitlines():
        ln = ln.strip()
        mt = _parse_meeting_datetime_str(ln)
        if mt:
            break

    return title, mt

def _gather_event_links(playwright, entry_urls: List[str], max_pages: int = 3) -> List[str]:
    """
    Uses Playwright to open each entry URL (calendar/meetings landing) and collect event links.
    """
    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context()
    page = ctx.new_page()
    links = set()

    for u in entry_urls:
        try:
            logging.info(f"{STAGE} open list {u}")
            page.goto(u, wait_until="domcontentloaded", timeout=30000)
            # try to trigger lazy load/scroll
            for _ in range(3):
                page.mouse.wheel(0, 2000)
                page.wait_for_timeout(200)

            # Collect anchors likely to be events
            for a in page.locator("a").all():
                try:
                    href = a.get_attribute("href") or ""
                except Exception:
                    continue
                if not href:
                    continue
                href_abs = _resolve_url(page.url, href)
                if re.search(r"/event/|/meeting/", href_abs, re.I):
                    links.add(href_abs)
        except PWTimeout:
            logging.warning(f"{STAGE} timeout listing {u}")
        except Exception as e:
            logging.warning(f"{STAGE} error listing {u}: {e}")

    ctx.close()
    browser.close()
    return sorted(links)

def parse_salida(entry_urls: list[str] | None = None, upcoming_only: bool = True, limit: int | None = None) -> list[dict]:
    if entry_urls is None:
        entry_urls = [
            "https://salida.civicclerk.com/",
            "https://portal.salida.civicclerk.com/",
        ]
    """
    Main entry. Given one or more Salida CivicClerk listing URLs, return enriched items with bullets.
    """
    if sync_playwright is None:
        raise RuntimeError("Playwright not available. Please install and run `playwright install`.")

    items: List[SalidaItem] = []
    with sync_playwright() as p:
        event_links = _gather_event_links(p, entry_urls)
        if limit:
            event_links = event_links[:limit]

        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()
        session = requests.Session()

        for ev_url in event_links:
            try:
                logging.info(f"{STAGE} stage=event_open url={ev_url}")
                page.goto(ev_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(300)  # small hydrate window

                title, meeting_dt = _extract_title_and_dt_from_event(page)
                if upcoming_only and not _is_upcoming(meeting_dt):
                    logging.info(f"{STAGE} skip past event title={title!r} url={ev_url}")
                    continue

                agenda_link = _find_agenda_link_on_event(page)
                summary_error = None
                agenda_text = ""
                agenda_pdf_url = None

                if agenda_link:
                    logging.info(f"{STAGE} stage=agenda_link url={agenda_link}")
                    agenda_text, agenda_pdf_url = _fetch_agenda_text(session, agenda_link)
                    logging.info(f"{STAGE} stage=agenda_text len={len(agenda_text)}")
                else:
                    summary_error = "no_agenda_link"

                bullets = []
                if agenda_text and not summary_error:
                    bullets = _summarize(agenda_text)
                    logging.info(f"{STAGE} stage=summary bullets={len(bullets)}")
                    if not bullets:
                        summary_error = "empty_summary_after_fallback"

                meeting_iso = (meeting_dt or dt.datetime.now().astimezone()).isoformat()
                it = SalidaItem(
                    title=title.strip(),
                    meeting_datetime_iso=meeting_iso,
                    source_url=ev_url,
                    agenda_pdf_url=agenda_pdf_url,
                    agenda_text_len=len(agenda_text or ""),
                    bullets=bullets,
                    summary_error=summary_error,
                    item_id=_hash(f"{title}|{meeting_iso}|{agenda_link or ev_url}")
                )
                items.append(it)
                logging.info(f"{STAGE} stage=done bullets={len(it.bullets)} err={it.summary_error} id={it.item_id}")
            except PWTimeout:
                logging.warning(f"{STAGE} timeout opening event {ev_url}")
            except Exception as e:
                logging.warning(f"{STAGE} error processing event {ev_url}: {e}")

        ctx.close()
        browser.close()

    # De-dupe by item_id
    dedup: Dict[str, SalidaItem] = {}
    for it in items:
        dedup[it.item_id] = it
    final = [asdict(it) for it in dedup.values()]
    # Sort by meeting_datetime_iso
    final.sort(key=lambda d: d["meeting_datetime_iso"])
    return final

# ---- CLI entrypoint for quick local testing ----

def _default_entry_urls() -> List[str]:
    # These are examples; replace with your actual Salida CivicClerk URLs.
    # Often index pages are like:
    #   https://salida.civicclerk.com/ or https://portal.salida.civicclerk.com/
    # And/or specific meeting listing pages.
    return [
        "https://salida.civicclerk.com/",
        "https://portal.salida.civicclerk.com/",
    ]

if __name__ == "__main__":
    urls = sys.argv[1:] or _default_entry_urls()
    logging.info(f"{STAGE} run started at { _now_local_iso() }")
    try:
        items = parse_salida(["https://salida.civicclerk.com/", "https://portal.salida.civicclerk.com/",])
        print(json.dumps(items, indent=2, ensure_ascii=False))
    except Exception as e:
        logging.error(f"{STAGE} fatal error: {e}")
        sys.exit(1)
