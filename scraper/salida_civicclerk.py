# Salida CivicClerk board endpoint; fetch upcoming City Council meetings and summarize agenda PDFs.
# Supports CivicClerk "files/agenda/<id>" pages and direct file streams.
#
# Returns: list[dict] with keys:
#   - title (str)
#   - when (datetime ISO string)
#   - source (str)  # full URL to the meeting page (users asked to see it)
#   - bullets (list[str])  # "newsworthy" bullets from agenda PDF
#
# This module expects a summarizer function in utils.py. It will try, in order:
#   utils.summarize_pdf_bytes(pdf_bytes, *, city, max_bullets)
#   utils.summarize_pdf(pdf_path_or_bytes, *, city, max_bullets)
#   utils.extract_news_bullets(text, *, city, max_bullets)  # fallback if no PDF found
#
# If none exist, it will still return items with an empty bullets list so the site can render something.

from __future__ import annotations
import re
import io
import json
import time
import math
import html
import logging
import datetime as dt
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser, tz

# --- Config ---
BASE = "https://salidaco.portal.civicclerk.com"
BOARD_ID = "41156"  # City Council
LISTING_URL = f"{BASE}/?department=All&boards-commissions={BOARD_ID}"
USER_AGENT = "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"
TIMEZONE = tz.gettz("America/Denver")  # Salida, CO local

# How far ahead to look for "upcoming"
LOOKAHEAD_DAYS = 60

# --- Utilities to integrate with your repo's summarizer(s) ---
def _import_summarizers():
    """
    Probe utils.py for whichever summarizer you wired.
    We don't know your exact function name, so try a few common ones.
    """
    try:
        import utils  # your repo's helper module
    except Exception:
        return None, None, None

    summarize_pdf_bytes = getattr(utils, "summarize_pdf_bytes", None)
    summarize_pdf = getattr(utils, "summarize_pdf", None)
    extract_news_bullets = getattr(utils, "extract_news_bullets", None)
    return summarize_pdf_bytes, summarize_pdf, extract_news_bullets


def _now_local():
    return dt.datetime.now(tz=TIMEZONE)


def _iso(dt_obj: dt.datetime) -> str:
    return dt_obj.astimezone(TIMEZONE).isoformat()


def _get(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)


def _is_future(meeting_dt: dt.datetime) -> bool:
    now = _now_local()
    # same-day at/after now counts as "upcoming" for our purposes
    return meeting_dt >= now.replace(hour=0, minute=0, second=0, microsecond=0)


def _extract_event_links_from_listing(html_text: str) -> List[str]:
    """
    The main listing is a JS app, but the server still renders anchor tags to /event/<id>.
    Grab unique event links.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    hrefs = set()
    for a in soup.select("a[href]"):
        href = a["href"]
        if re.search(r"/event/\d+/?($|[#?/])", href):
            if href.startswith("/"):
                hrefs.add(BASE + href)
            elif href.startswith("http"):
                hrefs.add(href)
    return sorted(hrefs)


def _parse_event_datetime_from_event_page(soup: BeautifulSoup) -> Optional[dt.datetime]:
    """
    CivicClerk event pages show a human date like: 'Tuesday, October 21, 2025 at 6:00 PM'
    Try to find and parse it.
    """
    # Common spots: h1/h2 headers and detail rows
    candidates = []
    for el in soup.find_all(text=True):
        t = (el or "").strip()
        if not t:
            continue
        # very permissive: look for a weekday + month pattern
        if re.search(r"(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday).+\d{4}", t, re.I):
            candidates.append(t)

    # Try parsing best-first
    for t in candidates:
        try:
            d = dtparser.parse(t, fuzzy=True).astimezone(TIMEZONE)
            return d
        except Exception:
            continue

    # Fallback: look for ISO in data attributes
    for tag in soup.find_all(attrs=True):
        for k, v in tag.attrs.items():
            if isinstance(v, str) and re.search(r"\d{4}-\d{2}-\d{2}T", v):
                try:
                    return dtparser.parse(v).astimezone(TIMEZONE)
                except Exception:
                    pass
    return None


def _find_agenda_pages(event_url: str, soup: BeautifulSoup) -> List[str]:
    """
    Locate links like /event/<id>/files/agenda/<agendaId> from the event page.
    """
    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if "files/agenda" in href:
            if href.startswith("/"):
                links.append(BASE + href)
            elif href.startswith("http"):
                # ensure same host; if not, still keep
                links.append(href)
    return sorted(set(links))


def _extract_pdf_or_stream(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """
    For an agenda page (files/agenda/ID), try to get a PDF:
      - direct .pdf link(s)
      - CivicClerk file stream like .../v1/Meetings/GetMeetingFileStream(fileId=####,plainText=false)
    Returns (pdf_bytes, source_pdf_url or stream_url)
    """
    r = _get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # direct PDF <a>
    for a in soup.select("a[href]"):
        href = a["href"]
        if href.lower().endswith(".pdf") or "GetMeetingFileStream" in href:
            full = href
            if href.startswith("/"):
                full = BASE + href
            try:
                # Some portals embed pdfjs viewer around the actual stream URL; unwrap if needed
                if "file=" in full and "pdfjs" in full:
                    from urllib.parse import parse_qs, urlparse, unquote
                    qs = parse_qs(urlparse(full).query)
                    inner = qs.get("file", [None])[0]
                    if inner:
                        full = unquote(inner)

                rr = _get(full)
                rr.raise_for_status()
                if rr.headers.get("content-type", "").lower().startswith("application/pdf") or rr.content.startswith(b"%PDF"):
                    return rr.content, full
                # Some streams return octet-stream but are still PDFs
                if rr.content[:4] == b"%PDF":
                    return rr.content, full
            except Exception:
                continue

    # Sometimes the page has buttons with data-file-id
    for btn in soup.select("[data-file-id]"):
        file_id = btn.get("data-file-id")
        if file_id and file_id.isdigit():
            # Construct a typical file-stream URL (works across CivicClerk tenants)
            stream = f"https://salidaco.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
            try:
                rr = _get(stream)
                rr.raise_for_status()
                if rr.content[:4] == b"%PDF":
                    return rr.content, stream
            except Exception:
                pass

    return None, None


def _summarize_pdf_bytes(pdf_bytes: bytes, *, city: str, max_bullets: int = 12) -> List[str]:
    summarize_pdf_bytes, summarize_pdf, extract_news_bullets = _import_summarizers()

    # 1) Prefer a byte-oriented summarizer if your utils exposes it
    if callable(summarize_pdf_bytes):
        try:
            return summarize_pdf_bytes(pdf_bytes, city=city, max_bullets=max_bullets)
        except Exception as e:
            logging.warning("summarize_pdf_bytes failed: %s", e)

    # 2) Some repos expose summarize_pdf that can accept bytes or a path
    if callable(summarize_pdf):
        try:
            return summarize_pdf(pdf_bytes, city=city, max_bullets=max_bullets)  # many helpers accept bytes too
        except Exception as e:
            logging.warning("summarize_pdf failed: %s", e)

    # 3) Fallback: crude extraction (no PDF OCR here; we defer to your utils in real runs)
    try:
        import pdfminer.high_level as pdfminer_high
        text = pdfminer_high.extract_text(io.BytesIO(pdf_bytes)) or ""
        if callable(extract_news_bullets):
            return extract_news_bullets(text, city=city, max_bullets=max_bullets)
        # naive fallback: first 10 non-empty lines as bullets
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return [f"- {ln}" for ln in lines[: min(max_bullets, 12)]]
    except Exception:
        return []


def parse_salida() -> List[Dict]:
    """
    Main entry: discover upcoming Salida City Council meetings and return summarized bullets.
    """
    items: List[Dict] = []

    # 1) Load listing (City Council board filtered)
    try:
        resp = _get(LISTING_URL)
        resp.raise_for_status()
    except Exception as e:
        logging.error("Failed to load listing: %s", e)
        return items

    event_links = _extract_event_links_from_listing(resp.text)
    if not event_links:
        logging.info("No event links found on listing (JS may have hidden them).")
        return items

    # 2) Visit each event page, filter to future + City Council only
    cutoff = _now_local() + dt.timedelta(days=LOOKAHEAD_DAYS)
    for event_url in sorted(set(event_links)):
        try:
            rr = _get(event_url)
            rr.raise_for_status()
            soup = BeautifulSoup(rr.text, "html.parser")

            title = soup.find(["h1", "h2"])
            title_text = (title.get_text(strip=True) if title else "City Council Meeting")
            if "council" not in title_text.lower():
                # Only City Council
                continue

            when_dt = _parse_event_datetime_from_event_page(soup)
            if not when_dt:
                # Can't date it; skip
                continue

            if not _is_future(when_dt) or when_dt > cutoff:
                continue

            # 3) Find agenda page(s)
            agenda_pages = _find_agenda_pages(event_url, soup)

            bullets: List[str] = []
            used_pdf_url: Optional[str] = None
            for ap in agenda_pages:
                pdf_bytes, pdf_url = _extract_pdf_or_stream(ap)
                if pdf_bytes:
                    bullets = _summarize_pdf_bytes(pdf_bytes, city="Salida", max_bullets=16)
                    used_pdf_url = pdf_url or ap
                    break  # prefer first available agenda

            # 4) If no agenda bytes yet, leave bullets empty (site can show "Agenda not posted")
            items.append(
                {
                    "title": title_text,
                    "when": _iso(when_dt),
                    "source": event_url,          # show the meeting page (per your request to show full URL)
                    "agenda_url": used_pdf_url,   # optional: direct agenda PDF/stream if found
                    "bullets": bullets,           # newsworthy bullets (may be empty if agenda not posted yet)
                    "city": "Salida",
                    "board_id": BOARD_ID,
                }
            )

        except Exception as e:
            logging.warning("Error while parsing %s: %s", event_url, e)

    return items


if __name__ == "__main__":
    # Manual run helper for local testing
    logging.basicConfig(level=logging.INFO)
    data = parse_salida()
    print(json.dumps(data, indent=2, ensure_ascii=False))
