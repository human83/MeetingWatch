# scraper/salidaco_civicclerk.py
from __future__ import annotations

import re
import io
import json
import logging
from typing import List, Optional, Tuple
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# Reuse your utils (timezone, future filter, meeting factory, summarizers)
import utils

BASE = "https://salidaco.portal.civicclerk.com"
BOARD_ID = "41156"  # City Council
LISTING_URL = f"{BASE}/?department=All&boards-commissions={BOARD_ID}"
UA = {"User-Agent": "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"}

LOG = logging.getLogger(__name__)

def _get(url: str) -> requests.Response:
    return requests.get(url, headers=UA, timeout=utils._DEFAULT_HTTP_TIMEOUT)

def _extract_event_links(html_text: str) -> List[str]:
    """CivicClerk main page is JS, but it still renders /event/<id> anchors in HTML."""
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

def _parse_when_and_location(soup: BeautifulSoup) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse human-readable 'Tuesday, October 21, 2025 at 6:00 PM' and any visible location text."""
    # Datetime: scan text nodes for a weekday + year phrase and parse
    when: Optional[datetime] = None
    for node in soup.find_all(text=True):
        t = (node or "").strip()
        if not t:
            continue
        if re.search(r"(Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday).+\d{4}", t, re.I):
            try:
                when = dtparser.parse(t, fuzzy=True)
                break
            except Exception:
                pass

    # Location: look for labels that often show the room/address
    location = None
    candidates = []
    for el in soup.find_all(["div", "li", "p", "span"]):
        s = (el.get_text(" ", strip=True) or "").strip()
        if s and re.search(r"(Room|Street|St\.|Ave|Avenue|City Hall|Council Chambers|Salida)", s, re.I):
            candidates.append(s)
    if candidates:
        # pick the longest plausible string
        location = sorted(candidates, key=len, reverse=True)[0][:160]

    return when, location

def _find_agenda_pages(event_url: str, soup: BeautifulSoup) -> List[str]:
    """Links like /event/<id>/files/agenda/<agendaId>."""
    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if "files/agenda" in href:
            if href.startswith("/"):
                links.append(BASE + href)
            elif href.startswith("http"):
                links.append(href)
    return sorted(set(links))

def _resolve_pdf_url_or_stream(agenda_page_url: str) -> Tuple[Optional[str], Optional[bytes]]:
    """
    Return (pdf_url, pdf_bytes). One of them may be None.
    - If a .pdf link with correct headers exists → (url, None)
    - If only a stream (GetMeetingFileStream) is present → (None, bytes)
    """
    try:
        r = _get(agenda_page_url)
        r.raise_for_status()
    except Exception as e:
        LOG.warning("agenda page fetch failed %s: %s", agenda_page_url, e)
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # Try direct PDF links first
    for a in soup.select("a[href]"):
        href = a["href"]
        full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
        if not full:
            continue
        if href.lower().endswith(".pdf"):
            return full, None

    # Look for CivicClerk stream endpoints
    stream_url = None
    for a in soup.select("a[href]"):
        href = a["href"]
        if "GetMeetingFileStream" in href:
            stream_url = href if href.startswith("http") else BASE + href
            break

    if not stream_url:
        # some portals store file id on data attributes
        btn = soup.select_one("[data-file-id]")
        if btn and btn.get("data-file-id", "").isdigit():
            file_id = btn["data-file-id"]
            stream_url = f"https://salidaco.api.civicclerk.com/v1/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"

    if not stream_url:
        return None, None

    # Download bytes (header may be octet-stream, so utils.summarize_pdf_if_any(url) would skip)
    try:
        rr = _get(stream_url)
        rr.raise_for_status()
        content = rr.content
        if content and content[:4] == b"%PDF":
            return None, content
    except Exception as e:
        LOG.warning("stream fetch failed %s: %s", stream_url, e)

    return None, None

def _extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int) -> Optional[str]:
    # mirror utils’ pdfminer import strategy
    try:
        from pdfminer_high_level import extract_text  # some installs
    except Exception:
        try:
            from pdfminer.high_level import extract_text  # pdfminer.six typical
        except Exception:
            return None
    try:
        with io.BytesIO(pdf_bytes) as fh:
            txt = extract_text(fh, page_numbers=range(max_pages)) or ""
        # light cleanup like utils
        import re as _re
        txt = _re.sub(r"[ \t]+\n", "\n", txt)
        txt = _re.sub(r"\n{3,}", "\n\n", txt)
        return txt.strip()
    except Exception:
        return None

def _summarize_text_with_utils(text: str) -> List[str]:
    """
    Reuse your utils’ exact pipeline/quality:
      single-topic → _openai_bullets → _legistar_rule_based_bullets → _post_filter_bullets
    Obeys your env vars (model, limits).
    """
    # single-topic fast path
    single = utils._is_single_topic_agenda(text)
    if single:
        return [utils.clean_text(single)]

    # LLM
    model = utils._DEFAULT_MODEL
    bullets_llm = utils._openai_bullets(text, model=model) or []

    # Rules
    rules_raw = utils._legistar_rule_based_bullets(text, limit=max(36, utils._MAX_BULLETS * 3))
    rules_best = utils._post_filter_bullets(rules_raw, limit=max(24, utils._MAX_BULLETS * 2))

    # Merge with your precedence and limits
    merged: List[str] = []
    seen = set()
    for src in (bullets_llm, rules_best):
        for b in src:
            k = utils.clean_text(b).lower()
            if not k or k in seen:
                continue
            merged.append(utils.clean_text(b))
            seen.add(k)
            if len(merged) >= utils._MAX_BULLETS:
                break
        if len(merged) >= utils._MAX_BULLETS:
            break

    if not merged and not utils._SUMMARIZER_STRICT:
        merged = utils._post_filter_bullets(utils._legistar_rule_based_bullets(text, limit=36), limit=utils._MAX_BULLETS)

    return merged

def _title_is_city_council(title: str) -> bool:
    t = (title or "").lower()
    return "council" in t  # optional: and "work session" not in t

def _fmt_date_time_local(dt_obj: datetime) -> Tuple[str, str]:
    dt_mt = utils.to_mt(dt_obj)
    date_str = dt_mt.strftime("%Y-%m-%d")
    time_str = dt_mt.strftime("%-I:%M %p") if hasattr(dt_mt, "strftime") else dt_mt.strftime("%I:%M %p").lstrip("0")
    return date_str, time_str

def parse_salida() -> List[dict]:
    items: List[dict] = []

    # 1) Load listing
    try:
        r = _get(LISTING_URL)
        r.raise_for_status()
    except Exception as e:
        LOG.error("Failed to load listing: %s", e)
        return items

    event_links = _extract_event_links(r.text)
    if not event_links:
        LOG.info("No event links found on listing (JS may obscure them).")
        return items

    # 2) Visit each event, filter upcoming City Council
    for event_url in sorted(set(event_links)):
        try:
            er = _get(event_url)
            er.raise_for_status()
            soup = BeautifulSoup(er.text, "html.parser")

            # Title
            h = soup.find(["h1", "h2"])
            title = (h.get_text(strip=True) if h else "").strip() or "City Council Meeting"
            if not _title_is_city_council(title):
                continue

            # When & Location
            when_dt, location = _parse_when_and_location(soup)
            if not when_dt:
                continue
            if not utils.is_future(when_dt):
                continue

            # Agenda page(s) → resolve either a .pdf URL or bytes from CivicClerk stream
            agenda_urls = _find_agenda_pages(event_url, soup)
            agenda_url_for_cache = None
            bullets: List[str] = []

            for ap in agenda_urls:
                pdf_url, pdf_bytes = _resolve_pdf_url_or_stream(ap)

                if pdf_url:
                    # Your utils can handle true .pdf URLs directly (with cache etc.)
                    bullets = utils.summarize_pdf_if_any(pdf_url)
                    agenda_url_for_cache = pdf_url
                    if bullets:
                        break

                elif pdf_bytes:
                    # Handle CivicClerk stream (octet-stream) by summarizing locally
                    text = _extract_text_from_pdf_bytes(pdf_bytes, max_pages=utils._DEFAULT_MAX_PAGES)
                    if text:
                        bullets = _summarize_text_with_utils(text)
                        agenda_url_for_cache = ap  # keep the agenda page as reference URL
                        if bullets:
                            break

            # 3) Build item using your schema
            date_str, time_str = _fmt_date_time_local(when_dt)
            item = utils.make_meeting(
                city_or_body="City of Salida",
                meeting_type=title,
                date=date_str,
                start_time_local=time_str,
                status="Scheduled",
                location=location,
                agenda_url=agenda_url_for_cache,
                agenda_summary=bullets,
                source=event_url,
            )
            items.append(item)

        except Exception as e:
            LOG.warning("Error while parsing %s: %s", event_url, e)

    return items

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(parse_salida(), indent=2, ensure_ascii=False))
