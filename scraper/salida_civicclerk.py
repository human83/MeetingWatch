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
UA = {"User-Agent": "MeetingWatchBot/1.0 (+https://github.com/human83/MeetingWatch)"}

# TEMP safety-net so the site shows Salida now; remove once API discovery is solid
SEED_EVENT_IDS = ["519"]

LOG = logging.getLogger(__name__)

def _get(url: str) -> requests.Response:
    return requests.get(url, headers=UA, timeout=utils._DEFAULT_HTTP_TIMEOUT)

# ---------------------------
# Try CivicClerk JSON APIs first (if they work for this tenant)
# ---------------------------
def _fetch_board_meetings_api(board_id: str, days_ahead: int = 90) -> List[Dict[str, Any]]:
    start = utils.now_mt().date()
    end = start + timedelta(days=days_ahead)

    candidates = [
        f"{API}/Boards/GetBoardMeetings?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Boards/GetBoardMeetings?boardId={board_id}",
        f"{API}/Meetings/GetUpcomingMeetings?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Meetings/GetUpcomingMeetings?boardId={board_id}",
        f"{API}/Boards/GetMeetingsByBoard?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Boards/GetMeetingsByBoard?boardId={board_id}",
        f"{API}/Search/Meetings?boardId={board_id}&startDate={start}&endDate={end}",
    ]

    for url in candidates:
        try:
            r = _get(url)
            if r.status_code != 200:
                continue
            data = r.json()
            items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if items:
                LOG.info("Salida API: %d meetings from %s", len(items), url)
                return items
        except Exception as e:
            LOG.debug("Salida API: %s failed: %s", url, e)
    LOG.warning("Salida API: no meetings from any endpoint candidates")
    return []

def _meeting_id_from_api_item(item: Dict[str, Any]) -> Optional[str]:
    for key in ("eventId", "EventId", "id", "Id", "meetingId", "MeetingId"):
        if key in item:
            v = item[key]
            try:
                return str(int(v))
            except Exception:
                if isinstance(v, str) and v.isdigit():
                    return v
    for k in ("event", "meeting", "Meeting", "Event"):
        if isinstance(item.get(k), dict):
            nested = _meeting_id_from_api_item(item[k])
            if nested:
                return nested
    return None

def _meeting_datetime_from_api_item(item: Dict[str, Any]) -> Optional[datetime]:
    for key in ("meetingDate", "MeetingDate", "startDate", "StartDate", "date", "Date", "eventDate"):
        if item.get(key):
            try:
                return dtparser.parse(str(item[key]))
            except Exception:
                pass
    for k in ("meeting", "Meeting", "event", "Event"):
        if isinstance(item.get(k), dict):
            dtv = _meeting_datetime_from_api_item(item[k])
            if dtv:
                return dtv
    return None

# ---------------------------
# Files page -> agenda page -> PDF/stream
# ---------------------------
def _agenda_page_urls_from_files_page(meeting_id: str) -> List[str]:
    """Fetch /event/<id>/files and pull links like /event/<id>/files/agenda/<agendaId>."""
    files_url = f"{BASE}/event/{meeting_id}/files"
    try:
        r = _get(files_url)
        r.raise_for_status()
    except Exception as e:
        LOG.warning("Salida: files page fetch failed %s: %s", files_url, e)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if "/files/agenda/" in href:
            full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
            if full:
                links.append(full)
    links = sorted(set(links))
    LOG.info("Salida: %s -> %d agenda pages", files_url, len(links))
    return links

def _resolve_pdf_url_or_stream(agenda_page_url: str) -> Tuple[Optional[str], Optional[bytes]]:
    """Return (pdf_url, pdf_bytes). We download stream bytes when needed."""
    try:
        r = _get(agenda_page_url)
        r.raise_for_status()
    except Exception as e:
        LOG.warning("Salida: agenda page fetch failed %s: %s", agenda_page_url, e)
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # Direct .pdf
    for a in soup.select("a[href]"):
        href = a["href"]
        full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
        if full and href.lower().endswith(".pdf"):
            LOG.info("Salida: found direct PDF %s", full)
            return full, None

    # GetMeetingFileStream
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

# ---------------------------
# PDF text -> date/time/title -> bullets (using your utils pipeline)
# ---------------------------
def _extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int) -> Optional[str]:
    try:
        from pdfminer_high_level import extract_text  # some installs
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

def _parse_datetime_from_text(text: str) -> Optional[datetime]:
    """
    Salida agendas usually include a line like:
      'City Council Regular Meeting – Tuesday, October 21, 2025 – 6:00 PM'
    We parse the first plausible Month Day, Year + time we see.
    """
    # Try strong patterns first
    m = re.search(r'([A-Za-z]+day,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}).{0,40}(\d{1,2}:\d{2}\s*(AM|PM))', text, re.I)
    if m:
        try:
            return dtparser.parse(f"{m.group(1)} {m.group(2)}")
        except Exception:
            pass
    # Looser fallback: first Month Day, Year and later a time on the same/nearby line
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if re.search(r'[A-Za-z]+\s+\d{1,2},\s+\d{4}', ln):
            # scan this and next two lines for a time
            blob = " ".join(lines[i:i+3])
            m2 = re.search(r'(\d{1,2}:\d{2}\s*(AM|PM))', blob, re.I)
            if m2:
                try:
                    return dtparser.parse(f"{ln} {m2.group(1)}")
                except Exception:
                    pass
            try:
                return dtparser.parse(ln)
            except Exception:
                pass
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

    # --- Preferred: API discovery ---
    raw_meetings = _fetch_board_meetings_api(BOARD_ID, days_ahead=90)
    meeting_ids: List[str] = []
    for m in raw_meetings:
        mid = _meeting_id_from_api_item(m)
        when_dt = _meeting_datetime_from_api_item(m)
        if not mid or not when_dt:
            continue
        if utils.is_future(when_dt):
            meeting_ids.append(mid)
    meeting_ids = sorted(set(meeting_ids))
    LOG.info("Salida API: normalized %d future meeting IDs", len(meeting_ids))

    # --- Last resort seed (listing/event pages are JS-only) ---
    if not meeting_ids:
        LOG.warning("Salida: no meeting IDs from API; seeding %s for now", SEED_EVENT_IDS)
        meeting_ids = SEED_EVENT_IDS[:]

    # Visit each meeting via the FILES page (server-rendered), then agenda PDF
    for mid in meeting_ids:
        try:
            event_url = f"{BASE}/event/{mid}"
            LOG.info("Salida: meeting %s", event_url)

            # 1) find agenda page(s) from FILES page
            agenda_pages = _agenda_page_urls_from_files_page(mid)
            if not agenda_pages:
                LOG.info("Salida: no agenda pages on files page for %s", mid)
                continue

            # 2) resolve and fetch agenda PDF (direct .pdf or stream bytes)
            bullets: List[str] = []
            agenda_url_for_cache = None
            text_for_dt: Optional[str] = None

            for ap in agenda_pages:
                pdf_url, pdf_bytes = _resolve_pdf_url_or_stream(ap)
                if pdf_url:
                    bullets = utils.summarize_pdf_if_any(pdf_url)
                    agenda_url_for_cache = pdf_url
                    # we still want text for date/time if possible
                    try:
                        rr = _get(pdf_url)
                        if rr.ok and rr.content[:4] == b"%PDF":
                            text_for_dt = _extract_text_from_pdf_bytes(rr.content, max_pages=utils._DEFAULT_MAX_PAGES)
                    except Exception:
                        pass
                    if bullets:
                        break
                elif pdf_bytes:
                    text_for_dt = _extract_text_from_pdf_bytes(pdf_bytes, max_pages=utils._DEFAULT_MAX_PAGES)
                    if text_for_dt:
                        bullets = _summarize_text_with_utils(text_for_dt)
                        agenda_url_for_cache = ap
                        if bullets:
                            break

            if not bullets and not agenda_url_for_cache:
                # no agenda yet; skip until posted
                LOG.info("Salida: no agenda content yet for %s", mid)
                continue

            # 3) derive when/location & title
            when_dt = _parse_datetime_from_text(text_for_dt or "") if text_for_dt else None
            if not when_dt:
                # fallback: keep in future bucket by assuming tonight 6pm if unknown, so it still shows
                when_dt = utils.now_mt().replace(hour=18, minute=0, second=0, microsecond=0)

            # title heuristic from text; otherwise generic
            title = "City Council Meeting"
            if text_for_dt:
                if re.search(r"regular\s+council\s+meeting", text_for_dt, re.I):
                    title = "City Council Regular Meeting"
                elif re.search(r"work\s+session", text_for_dt, re.I):
                    title = "City Council Work Session"

            if not _title_is_city_council(title):
                continue
            if not utils.is_future(when_dt):
                continue

            date_str, time_str = _fmt_date_time_local(when_dt)
            item = utils.make_meeting(
                city_or_body="City of Salida",
                meeting_type=title,
                date=date_str,
                start_time_local=time_str,
                status="Scheduled",
                location=None,  # not in PDF reliably; omit
                agenda_url=agenda_url_for_cache,
                agenda_summary=bullets,
                source=event_url,
            )
            items.append(item)

        except Exception as e:
            LOG.warning("Salida: error while processing meeting %s: %s", mid, e)

    return items

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(parse_salida(), indent=2, ensure_ascii=False))
