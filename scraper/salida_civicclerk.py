# scraper/salida_civicclerk.py
from __future__ import annotations

import re, io, json, logging
from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

# utils import works in both "python -m scraper.main" and direct runs
try:
    from . import utils
except ImportError:
    import os, sys
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    import utils

BASE = "https://salidaco.portal.civicclerk.com"
API  = "https://salidaco.api.civicclerk.com/v1"
BOARD_ID = "41156"  # City Council

UA_HEADERS = {
    "User-Agent": "MeetingWatchBot/1.1 (+https://github.com/human83/MeetingWatch)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}

# Discover strategy:
# 1) Board-level APIs (often empty for this tenant)
# 2) Probe around known meeting IDs via Files API then HTML
# 3) Emergency: known agenda IDs (guaranteed)
SEED_EVENT_IDS = [519]
PROBE_RADIUS   = 40
SEED_AGENDA_IDS: Dict[int, List[int]] = {
    519: [1101],   #  https://salidaco.portal.civicclerk.com/event/519/files/agenda/1101
}

LOG = logging.getLogger(__name__)

def _get(url: str, referer: Optional[str] = None) -> requests.Response:
    headers = dict(UA_HEADERS)
    if referer:
        headers["Referer"] = referer
    return requests.get(url, headers=headers, timeout=utils._DEFAULT_HTTP_TIMEOUT)

# ---------------- Board APIs (may be empty here) ----------------
def _fetch_board_meetings_api(board_id: str, days_ahead: int = 90) -> List[Dict[str, Any]]:
    start = utils.now_mt().date()
    end = start + timedelta(days=days_ahead)
    endpoints = [
        f"{API}/Boards/GetBoardMeetings?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Boards/GetBoardMeetings?boardId={board_id}",
        f"{API}/Meetings/GetUpcomingMeetings?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Meetings/GetUpcomingMeetings?boardId={board_id}",
        f"{API}/Boards/GetMeetingsByBoard?boardId={board_id}&startDate={start}&endDate={end}",
        f"{API}/Boards/GetMeetingsByBoard?boardId={board_id}",
        f"{API}/Search/Meetings?boardId={board_id}&startDate={start}&endDate={end}",
    ]
    for url in endpoints:
        try:
            r = _get(url)
            LOG.info("Salida API: %s -> %s", url, r.status_code)
            if r.status_code != 200:
                continue
            data = r.json()
            items = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if items:
                LOG.info("Salida API: %d meetings from %s", len(items), url)
                return items
        except Exception as e:
            LOG.debug("Salida API fail %s: %s", url, e)
    LOG.warning("Salida API: no meetings from any endpoint candidates")
    return []

def _meeting_id_from_api_item(item: Dict[str, Any]) -> Optional[int]:
    for key in ("eventId","EventId","meetingId","MeetingId","id","Id"):
        v = item.get(key)
        if v is None: continue
        try:
            return int(v)
        except Exception:
            if isinstance(v, str) and v.isdigit():
                return int(v)
    for k in ("event","Event","meeting","Meeting"):
        sub = item.get(k)
        if isinstance(sub, dict):
            m = _meeting_id_from_api_item(sub)
            if m: return m
    return None

def _meeting_datetime_from_api_item(item: Dict[str, Any]) -> Optional[datetime]:
    for key in ("meetingDate","MeetingDate","startDate","StartDate","date","Date","eventDate"):
        v = item.get(key)
        if not v: continue
        try:
            return dtparser.parse(str(v))
        except Exception:
            pass
    for k in ("event","Event","meeting","Meeting"):
        sub = item.get(k)
        if isinstance(sub, dict):
            d = _meeting_datetime_from_api_item(sub)
            if d: return d
    return None

# ---------------- Per-meeting FILES API / HTML ----------------
def _fetch_files_for_meeting(mid: int) -> List[Dict[str, Any]]:
    urls = [
        f"{API}/Meetings/GetMeetingFiles?meetingId={mid}",
        f"{API}/Meetings/GetMeetingFiles?eventId={mid}",
        f"{API}/Meetings/GetFilesByMeeting?meetingId={mid}",
    ]
    for url in urls:
        try:
            r = _get(url, referer=f"{BASE}/event/{mid}")
            LOG.info("Salida Files API: %s -> %s", url, r.status_code)
            if r.status_code != 200:
                continue
            data = r.json()
            files = data.get("items") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if files:
                return files
        except Exception as e:
            LOG.debug("Files API fail %s: %s", url, e)
    return []

def _agenda_file_ids(files: List[Dict[str, Any]]) -> List[int]:
    ids = []
    for f in files:
        blob = " ".join(str(f.get(k,"")) for k in ("name","fileName","title","typeName","categoryName")).lower()
        if any(tok in blob for tok in ("agenda","packet")):
            fid = f.get("fileId") or f.get("FileId") or f.get("id")
            try:
                ids.append(int(fid))
            except Exception:
                pass
    return sorted(set(ids))

def _stream_pdf_bytes(file_id: int, referer: Optional[str] = None) -> Optional[bytes]:
    url = f"{API}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
    try:
        r = _get(url, referer=referer)
        if r.ok and r.content[:4] == b"%PDF":
            LOG.info("Salida: fetched PDF bytes from stream %s", url)
            return r.content
    except Exception as e:
        LOG.debug("PDF stream fail %s: %s", url, e)
    return None

def _agenda_page_urls_from_files_page(mid: int) -> List[str]:
    files_url = f"{BASE}/event/{mid}/files"
    try:
        r = _get(files_url, referer=f"{BASE}/event/{mid}")
        LOG.info("Salida: GET %s -> %s (len=%d)", files_url, r.status_code, len(r.text or b""))
        r.raise_for_status()
    except Exception as e:
        LOG.debug("Files page fetch failed %s: %s", files_url, e)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if "/files/agenda/" in href:
            full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
            if full: links.append(full)
    return sorted(set(links))

def _resolve_pdf_from_agenda_page(url: str) -> Tuple[Optional[str], Optional[bytes]]:
    """Return (pdf_url, pdf_bytes). Tries anchors, stream links, then regex-extracts fileId."""
    try:
        r = _get(url)
        r.raise_for_status()
    except Exception:
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # A) normal anchors
    for a in soup.select("a[href]"):
        href = a["href"]
        full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
        if full and href.lower().endswith(".pdf"):
            LOG.info("Salida: agenda page has direct PDF %s", full)
            return full, None
        if "GetMeetingFileStream" in href:
            stream = href if href.startswith("http") else BASE + href
            try:
                rr = _get(stream, referer=url)
                rr.raise_for_status()
                if rr.content[:4] == b"%PDF":
                    LOG.info("Salida: agenda page has stream link %s", stream)
                    return None, rr.content
            except Exception:
                pass

    # B) **regex fallback** — fileId in inline JS/HTML
    m = re.search(r'fileId\s*=\s*(\d+)', r.text)
    if not m:
        m = re.search(r'[?&]fileId=(\d+)', r.text)
    if m:
        fid = int(m.group(1))
        stream = f"{API}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
        try:
            rr = _get(stream, referer=url)
            rr.raise_for_status()
            if rr.content[:4] == b"%PDF":
                LOG.info("Salida: regex-extracted fileId=%s -> stream ok", fid)
                return None, rr.content
        except Exception as e:
            LOG.debug("Salida: regex stream fetch failed for fileId %s: %s", fid, e)

    LOG.info("Salida: no PDF found on agenda page %s", url)
    return None, None

# ---------------- PDF → text → date/time → bullets ----------------
def _extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int) -> Optional[str]:
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

def _parse_datetime_from_text(text: str) -> Optional[datetime]:
    m = re.search(r'([A-Za-z]+day,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}).{0,40}(\d{1,2}:\d{2}\s*(AM|PM))', text, re.I)
    if m:
        try: return dtparser.parse(f"{m.group(1)} {m.group(2)}")
        except: pass
    for ln in (ln.strip() for ln in text.splitlines() if ln.strip()):
        if re.search(r'[A-Za-z]+\s+\d{1,2},\s+\d{4}', ln):
            try: return dtparser.parse(ln)
            except: pass
    return None

def _summarize_text_with_utils(text: str) -> List[str]:
    single = utils._is_single_topic_agenda(text)
    if single:
        return [utils.clean_text(single)]
    model = utils._DEFAULT_MODEL
    llm = utils._openai_bullets(text, model=model) or []
    rules = utils._post_filter_bullets(utils._legistar_rule_based_bullets(text, limit=max(36, utils._MAX_BULLETS*3)),
                                       limit=max(24, utils._MAX_BULLETS*2))
    out, seen = [], set()
    for src in (llm, rules):
        for b in src:
            k = utils.clean_text(b).lower()
            if not k or k in seen: continue
            out.append(utils.clean_text(b)); seen.add(k)
            if len(out) >= utils._MAX_BULLETS: break
        if len(out) >= utils._MAX_BULLETS: break
    if not out and not utils._SUMMARIZER_STRICT:
        out = utils._post_filter_bullets(utils._legistar_rule_based_bullets(text, limit=36), limit=utils._MAX_BULLETS)
    return out

def _fmt_date_time_local(dt_obj: datetime) -> Tuple[str, str]:
    dt_mt = utils.to_mt(dt_obj)
    return dt_mt.strftime("%Y-%m-%d"), dt_mt.strftime("%-I:%M %p") if hasattr(dt_mt,'strftime') else dt_mt.strftime("%I:%M %p").lstrip("0")

# ---------------- Probing helpers ----------------
def _probe_ids_via_files_api(seeds: List[int], radius: int) -> List[int]:
    hits: List[int] = []
    for seed in seeds:
        for d in range(-radius, radius+1):
            mid = seed + d
            files = _fetch_files_for_meeting(mid)
            if files:
                LOG.info("Salida probe(API): meeting %s has %d files", mid, len(files))
                hits.append(mid)
    return sorted(set(hits))

def _probe_ids_via_files_html(seeds: List[int], radius: int) -> List[int]:
    hits: List[int] = []
    for seed in seeds:
        for d in range(-radius, radius+1):
            mid = seed + d
            if _agenda_page_urls_from_files_page(mid):
                LOG.info("Salida probe(HTML): meeting %s has agenda page(s)", mid)
                hits.append(mid)
    return sorted(set(hits))

# ---------------- Main ----------------
def parse_salida() -> List[dict]:
    items: List[dict] = []

    # 1) Board API discovery (if any)
    raw = _fetch_board_meetings_api(BOARD_ID, days_ahead=90)
    meeting_ids: List[int] = []
    for m in raw:
        mid = _meeting_id_from_api_item(m)
        when = _meeting_datetime_from_api_item(m)
        if mid and when and utils.is_future(when):
            meeting_ids.append(mid)
    meeting_ids = sorted(set(meeting_ids))
    LOG.info("Salida: API normalized %d future meeting IDs", len(meeting_ids))

    # 2) Probe around seeds via Files API then HTML
    if not meeting_ids:
        LOG.warning("Salida: no meeting IDs from API; probing around seeds %s", SEED_EVENT_IDS)
        meeting_ids = _probe_ids_via_files_api(SEED_EVENT_IDS, PROBE_RADIUS)
        if not meeting_ids:
            LOG.warning("Salida: files API probe empty; probing HTML files pages")
            meeting_ids = _probe_ids_via_files_html(SEED_EVENT_IDS, PROBE_RADIUS)

    # 3) If still nothing, use hard-coded agenda pages (emergency)
    use_seed_agendas = False
    if not meeting_ids and SEED_AGENDA_IDS:
        LOG.warning("Salida: neither API nor probing found anything; using SEED_AGENDA_IDS")
        use_seed_agendas = True
        meeting_ids = list(SEED_AGENDA_IDS.keys())

    if not meeting_ids:
        LOG.warning("Salida: nothing found via API or probing; giving up for this run.")
        return items

    # 4) For each meeting, resolve agenda → PDF → summarize → emit
    for mid in meeting_ids:
        try:
            event_url = f"{BASE}/event/{mid}"
            bullets: List[str] = []
            agenda_url_for_cache: Optional[str] = None
            text_for_dt: Optional[str] = None

            agenda_pages: List[str] = []
            file_ids: List[int] = []

            if use_seed_agendas and mid in SEED_AGENDA_IDS:
                agenda_pages = [f"{BASE}/event/{mid}/files/agenda/{aid}" for aid in SEED_AGENDA_IDS[mid]]
                LOG.info("Salida: using seed agenda pages for %s -> %s", mid, agenda_pages)
            else:
                files = _fetch_files_for_meeting(mid)
                if files:
                    file_ids = _agenda_file_ids(files)
                if not file_ids:
                    agenda_pages = _agenda_page_urls_from_files_page(mid)

            # Resolve to a PDF
            if file_ids:
                for fid in file_ids:
                    pdf_bytes = _stream_pdf_bytes(fid, referer=f"{BASE}/event/{mid}/files")
                    if not pdf_bytes:
                        continue
                    text_for_dt = _extract_text_from_pdf_bytes(pdf_bytes, max_pages=utils._DEFAULT_MAX_PAGES)
                    if text_for_dt:
                        bullets = _summarize_text_with_utils(text_for_dt)
                        agenda_url_for_cache = f"{API}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
                        if bullets:
                            break

            if not bullets and agenda_pages:
                for ap in agenda_pages:
                    pdf_url, pdf_bytes = _resolve_pdf_from_agenda_page(ap)
                    if pdf_url:
                        bullets = utils.summarize_pdf_if_any(pdf_url)
                        agenda_url_for_cache = pdf_url
                        # also try to parse date/time
                        try:
                            rr = _get(pdf_url, referer=ap)
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
                LOG.info("Salida: no agenda content for meeting %s; skipping", mid)
                continue

            when_dt = _parse_datetime_from_text(text_for_dt or "") if text_for_dt else None
            if not when_dt:
                # assume 6pm local so it appears; next run will refine when precise time is parsed
                when_dt = utils.now_mt().replace(hour=18, minute=0, second=0, microsecond=0)
            if not utils.is_future(when_dt):
                continue

            title = "City Council Meeting"
            if text_for_dt:
                if re.search(r"regular\s+council\s+meeting", text_for_dt, re.I):
                    title = "City Council Regular Meeting"
                elif re.search(r"work\s+session", text_for_dt, re.I):
                    title = "City Council Work Session"

            date_str, time_str = _fmt_date_time_local(when_dt)
            items.append(
                utils.make_meeting(
                    city_or_body="City of Salida",
                    meeting_type=title,
                    date=date_str,
                    start_time_local=time_str,
                    status="Scheduled",
                    location=None,
                    agenda_url=agenda_url_for_cache,
                    agenda_summary=bullets,
                    source=event_url,
                )
            )

        except Exception as e:
            LOG.warning("Salida: error processing meeting %s: %s", mid, e)

    return items

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(parse_salida(), indent=2, ensure_ascii=False))
