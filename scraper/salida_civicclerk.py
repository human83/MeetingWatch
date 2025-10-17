# scraper/salida_civicclerk.py
from __future__ import annotations

import re
import io
import json
import logging
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# Reuse your utils (timezone, future filter, meeting factory, summarizers)
try:
    from . import utils        # when run as a package (python -m scraper.main)
except ImportError:
    import os, sys
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))  # add repo root
    import utils

BASE = "https://salidaco.portal.civicclerk.com"
API  = "https://salidaco.api.civicclerk.com/v1"
BOARD_ID = "41156"  # City Council
LISTING_URL = f"{BASE}/?department=All&boards-commissions={BOARD_ID}"
UA = {"User-Agent": "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"}

# TEMP safety-net so the site shows Salida now; remove once API discovery proves solid
SEED_EVENT_IDS = ["519"]

LOG = logging.getLogger(__name__)

def _get(url: str) -> requests.Response:
    return requests.get(url, headers=UA, timeout=utils._DEFAULT_HTTP_TIMEOUT)

# ---------------------------
# NEW: query CivicClerk JSON API for meetings
# ---------------------------
def _fetch_board_meetings_api(board_id: str, days_ahead: int = 90) -> List[Dict[str, Any]]:
    """Try several CivicClerk endpoints; return raw meeting dicts."""
    start = utils.now_mt().date()
    end = start + timedelta(days=days_ahead)

    # A set of plausible endpoints seen across CivicClerk tenants
    candidates = [
        # Common patterns
        f"{API}/Boards/GetBoardMeetings?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Boards/GetBoardMeetings?boardId={board_id}",
        f"{API}/Meetings/GetUpcomingMeetings?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Meetings/GetUpcomingMeetings?boardId={board_id}",
        f"{API}/Boards/GetMeetingsByBoard?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Boards/GetMeetingsByBoard?boardId={board_id}",
        # Some tenants expose a search endpoint
        f"{API}/Search/Meetings?boardId={board_id}&startDate={start}&endDate={end}",
    ]

    for url in candidates:
        try:
            r = _get(url)
            if r.status_code != 200:
                continue
            data = r.json()
            # Normalize: endpoints may return {"items":[...]}, or just a list
            if isinstance(data, dict) and "items" in data:
                items = data.get("items") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []
            if items:
                LOG.info("Salida API: %d meetings from %s", len(items), url)
                return items
        except Exception as e:
            LOG.debug("Salida API: %s failed: %s", url, e)
    LOG.warning("Salida API: no meetings from any endpoint candidates")
    return []

def _meeting_id_from_api_item(item: Dict[str, Any]) -> Optional[str]:
    """Extract an ID to build /event/<id> URL."""
    for key in ("eventId", "EventId", "id", "Id", "meetingId", "MeetingId"):
        if key in item:
            val = item[key]
            try:
                return str(int(val))
            except Exception:
                if isinstance(val, str) and val.isdigit():
                    return val
    # sometimes nested
    for key in ("event", "meeting", "Meeting", "Event"):
        if key in item and isinstance(item[key], dict):
            nested = _meeting_id_from_api_item(item[key])
            if nested:
                return nested
    return None

def _meeting_datetime_from_api_item(item: Dict[str, Any]) -> Optional[datetime]:
    """Try common fields for datetime."""
    for key in ("meetingDate", "MeetingDate", "startDate", "StartDate", "date", "Date", "eventDate"):
        if key in item and item[key]:
            try:
                return dtparser.parse(str(item[key]))
            except Exception:
                pass
    # sometimes nested fields
    for key in ("meeting", "Meeting", "event", "Event"):
        if key in item and isinstance(item[key], dict):
            dtv = _meeting_datetime_from_api_item(item[key])
            if dtv:
                return dtv
    return None

# ---------------------------
# Old HTML fallbacks (kept)
# ---------------------------
def _extract_event_links_from_listing(html_text: str) -> List[str]:
    """HTML discovery (anchors + raw text + 'eventId' JSON), used as a fallback."""
    hrefs = set()

    # anchors
    soup = BeautifulSoup(html_text, "html.parser")
    for a in soup.find_all("a", href=True):
        m = re.search(r"/event/(\d+)", a["href"])
        if m:
            hrefs.add(f"{BASE}/event/{m.group(1)}")

    # raw '/event/<id>'
    for m in re.finditer(r"/event/(\d+)", html_text):
        hrefs.add(f"{BASE}/event/{m.group(1)}")

    # inline JSON "eventId": 123
    for m in re.finditer(r'"eventId"\s*:\s*(\d+)', html_text):
        hrefs.add(f"{BASE}/event/{m.group(1)}")

    LOG.info("Salida HTML: discovered %d event links", len(hrefs))
    return sorted(hrefs)

def _parse_when_and_location(soup: BeautifulSoup) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse human-readable 'Tuesday, October 21, 2025 at 6:00 PM' and any visible location text."""
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

    location = None
    candidates = []
    for el in soup.find_all(["div", "li", "p", "span"]):
        s = (el.get_text(" ", strip=True) or "").strip()
        if s and re.search(r"(Room|Street|St\.|Ave|Avenue|City Hall|Council Chambers|Salida)", s, re.I):
            candidates.append(s)
    if candidates:
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
    """Return (pdf_url, pdf_bytes)."""
    try:
        r = _get(agenda_page_url)
        r.raise_for_status()
    except Exception as e:
        LOG.warning("Salida: agenda page fetch failed %s: %s", agenda_page_url, e)
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # Direct PDF
    for a in soup.select("a[href]"):
        href = a["href"]
        full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
        if not full:
            continue
        if href.lower().endswith(".pdf"):
            LOG.info("Salida: found direct PDF %s", full)
            return full, None

    # Stream
    stream_url = None
    for a in soup.select("a[href]"):
        href = a["href"]
        if "GetMeetingFileStream" in href:
            stream_url = href if href.startswith("http") else BASE + href
            break
    if not stream_url:
        btn = soup.select_one("[data-file-id]")
        if btn and btn.get("data-file-id", "").isdigit():
            file_id = btn["data-file-id"]
            stream_url = f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
    if not stream_url:
        return None, None

    try:
        rr = _get(stream_url)
        rr.raise_for_status()
        content = rr.content
        if content and content[:4] == b"%PDF":
            LOG.info("Salida: fetched PDF bytes from stream %s", stream_url)
            return None, content
    except Exception as e:
        LOG.warning("Salida: stream fetch failed %s: %s", stream_url, e)
    return None, None

def _extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int) -> Optional[str]:
    # mirror utils’ pdfminer import strategy
    try:
        from pdfminer_high_level import extract_text
    except Exception:
        try:
            from pdfminer.high_level import extract_text
        except Exception:
            return None
    try:
        with io.BytesIO(pdf_bytes) as fh:
            txt = extract_text(fh, page_numbers=range(max_pages)) or ""
        import re as _re
        txt = _re.sub(r"[ \t]+\n", "\n", txt)
        txt = _re.sub(r"\n{3,}", "\n\n", txt)
        return txt.strip()
    except Exception:
        return None

def _summarize_text_with_utils(text: str) -> List[str]:
    single = utils._is_single_topic_agenda(text)
    if single:
        return [utils.clean_text(single)]
    model = utils._DEFAULT_MODEL
    bullets_llm = utils._openai_bullets(text, model=model) or []
    rules_raw = utils._legistar_rule_based_bullets(text, limit=max(36, utils._MAX_BULLETS * 3))
    rules_best = utils._post_filter_bullets(rules_raw, limit=max(24, utils._MAX_BULLETS * 2))
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
    return ("council" in t) and not ("youth council" in t or "advisory council" in t)

def _fmt_date_time_local(dt_obj: datetime) -> Tuple[str, str]:
    dt_mt = utils.to_mt(dt_obj)
    date_str = dt_mt.strftime("%Y-%m-%d")
    time_str = dt_mt.strftime("%-I:%M %p") if hasattr(dt_mt, "strftime") else dt_mt.strftime("%I:%M %p").lstrip("0")
    return date_str, time_str

def parse_salida() -> List[dict]:
    items: List[dict] = []

    # --- Preferred path: API discovery ---
    raw_meetings = _fetch_board_meetings_api(BOARD_ID, days_ahead=90)
    event_links: List[str] = []
    for m in raw_meetings:
        mid = _meeting_id_from_api_item(m)
        when_dt = _meeting_datetime_from_api_item(m)
        if not mid or not when_dt:
            continue
        if not utils.is_future(when_dt):
            continue
        event_links.append(f"{BASE}/event/{mid}")
    event_links = sorted(set(event_links))
    LOG.info("Salida API: normalized %d event links", len(event_links))

    # --- Fallback: HTML discovery (if API yielded nothing) ---
    if not event_links:
        try:
            r = _get(LISTING_URL)
            r.raise_for_status()
            event_links = _extract_event_links_from_listing(r.text)
        except Exception as e:
            LOG.warning("Salida HTML: failed to load listing: %s", e)

    # --- Last resort seed (remove once stable) ---
    if not event_links:
        LOG.warning("Salida: no event links from API or HTML; seeding %s for now", SEED_EVENT_IDS)
        event_links = [f"{BASE}/event/{eid}" for eid in SEED_EVENT_IDS]

    # Visit each event page and build items
    for event_url in event_links:
        try:
            LOG.info("Salida: scanning %s", event_url)
            er = _get(event_url)
            er.raise_for_status()
            soup = BeautifulSoup(er.text, "html.parser")

            h = soup.find(["h1", "h2"])
            title = (h.get_text(strip=True) if h else "").strip() or "City Council Meeting"
            if not _title_is_city_council(title):
                continue

            when_dt, location = _parse_when_and_location(soup)
            if not when_dt or not utils.is_future(when_dt):
                continue

            agenda_urls = _find_agenda_pages(event_url, soup)
            agenda_url_for_cache = None
            bullets: List[str] = []

            for ap in agenda_urls:
                pdf_url, pdf_bytes = _resolve_pdf_url_or_stream(ap)
                if pdf_url:
                    bullets = utils.summarize_pdf_if_any(pdf_url)
                    agenda_url_for_cache = pdf_url
                    if bullets:
                        break
                elif pdf_bytes:
                    text = _extract_text_from_pdf_bytes(pdf_bytes, max_pages=utils._DEFAULT_MAX_PAGES)
                    if text:
                        bullets = _summarize_text_with_utils(text)
                        agenda_url_for_cache = ap
                        if bullets:
                            break

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
            LOG.warning("Salida: error while parsing %s: %s", event_url, e)

    return items

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(parse_salida(), indent=2, ensure_ascii=False))
