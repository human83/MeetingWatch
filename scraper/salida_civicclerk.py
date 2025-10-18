# scraper/salida_civicclerk.py
from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

try:
    from . import utils
except ImportError:
    import os, sys
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    import utils

LOG = logging.getLogger(__name__)

BASE = "https://salidaco.portal.civicclerk.com"
API  = "https://salidaco.api.civicclerk.com/v1"
BOARD_ID = "41156"  # City Council

UA = {
    "User-Agent": "MeetingWatchBot/1.2 (+https://github.com/human83/MeetingWatch)",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}

# Known-good seed + a bounded probe to find neighboring meetings
SEED_EVENT_IDS = [519]
PROBE_RADIUS   = 40

def _get(url: str, referer: Optional[str] = None) -> requests.Response:
    headers = dict(UA)
    if referer:
        headers["Referer"] = referer
    return requests.get(url, headers=headers, timeout=utils._DEFAULT_HTTP_TIMEOUT)

# ---------------- Playwright helpers ----------------

def _with_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        return None

def _agenda_pages_via_playwright_files(mid: int, timeout_ms: int = 25000) -> List[str]:
    """Render /event/<mid>/files and return agenda page hrefs."""
    spw = _with_playwright()
    if spw is None:
        return []
    files_url = f"{BASE}/event/{mid}/files"
    hrefs: List[str] = []
    with spw()() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            LOG.info("Salida PW: rendering %s", files_url)
            page.goto(files_url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=timeout_ms)

            # Grab all anchors and filter
            all_hrefs = page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.href)"""
            )
            for h in all_hrefs:
                if isinstance(h, str) and "/files/agenda/" in h:
                    if h.startswith("http"):
                        hrefs.append(h)
                    else:
                        hrefs.append(BASE + h if h.startswith("/") else h)
        finally:
            context.close()
            browser.close()
    hrefs = sorted(set(hrefs))
    LOG.info("Salida PW: %s -> %d agenda pages", files_url, len(hrefs))
    return hrefs

def _pdf_bytes_via_playwright(agenda_url: str, timeout_ms: int = 25000) -> Optional[bytes]:
    """Render a JS agenda page and capture the PDF stream response."""
    spw = _with_playwright()
    if spw is None:
        return None
    pdf_bytes: Optional[bytes] = None
    with spw()() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        target_resp = {"got": None}
        def _on_response(resp):
            if "GetMeetingFileStream" in resp.url:
                target_resp["got"] = resp
        page.on("response", _on_response)
        try:
            LOG.info("Salida PW: rendering agenda %s", agenda_url)
            page.goto(agenda_url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            # if listener already caught it:
            if target_resp["got"] is None:
                # try waiting briefly for a late network call
                try:
                    resp = page.wait_for_event(
                        "response",
                        predicate=lambda r: "GetMeetingFileStream" in r.url,
                        timeout=5000,
                    )
                    target_resp["got"] = resp
                except Exception:
                    pass
            if target_resp["got"] is not None:
                LOG.info("Salida PW: captured stream %s", target_resp["got"].url)
                pdf_bytes = target_resp["got"].body()
        finally:
            context.close()
            browser.close()
    return pdf_bytes

# ---------------- Non-JS fallbacks (kept) ----------------

def _agenda_pages_from_html_files(mid: int) -> List[str]:
    files_url = f"{BASE}/event/{mid}/files"
    try:
        r = _get(files_url, referer=f"{BASE}/event/{mid}")
        if r.status_code != 200:
            return []
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a[href]"):
        href = a["href"]
        if "/files/agenda/" in href:
            out.append(href if href.startswith("http") else BASE + href if href.startswith("/") else href)
    return sorted(set(out))

def _resolve_pdf_from_agenda_page(agenda_url: str) -> Tuple[Optional[str], Optional[bytes]]:
    """Try to get a direct .pdf or stream bytes via plain HTTP; no JS."""
    try:
        r = _get(agenda_url)
        r.raise_for_status()
    except Exception:
        return None, None
    soup = BeautifulSoup(r.text, "html.parser")
    for a in soup.select("a[href]"):
        href = a["href"]
        full = href if href.startswith("http") else (BASE + href if href.startswith("/") else None)
        if not full:
            continue
        if href.lower().endswith(".pdf"):
            LOG.info("Salida: direct PDF %s", full)
            return full, None
        if "GetMeetingFileStream" in href:
            try:
                rr = _get(full, referer=agenda_url)
                if rr.ok and rr.content[:4] == b"%PDF":
                    LOG.info("Salida: stream ok %s", full)
                    return None, rr.content
            except Exception:
                pass
    # regex for fileId in inline HTML
    m = re.search(r'fileId\s*=\s*(\d+)', r.text) or re.search(r'[?&]fileId=(\d+)', r.text)
    if m:
        fid = int(m.group(1))
        stream = f"{API}/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
        try:
            rr = _get(stream, referer=agenda_url)
            if rr.ok and rr.content[:4] == b"%PDF":
                LOG.info("Salida: regex fileId=%s stream ok", fid)
                return None, rr.content
        except Exception:
            pass
    return None, None

# ---------------- PDF pipeline ----------------

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
            text = extract_text(fh, page_numbers=range(max_pages)) or ""
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:
        return None

def _parse_datetime_from_text(text: str) -> Optional[datetime]:
    m = re.search(r'([A-Za-z]+day,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}).{0,40}(\d{1,2}:\d{2}\s*(AM|PM))', text, re.I)
    if m:
        try:
            return dtparser.parse(f"{m.group(1)} {m.group(2)}")
        except Exception:
            pass
    for ln in (ln.strip() for ln in text.splitlines() if ln.strip()):
        if re.search(r'[A-Za-z]+\s+\d{1,2},\s+\d{4}', ln):
            try:
                return dtparser.parse(ln)
            except Exception:
                pass
    return None

def _summarize_text_with_utils(text: str) -> List[str]:
    single = utils._is_single_topic_agenda(text)
    if single:
        return [utils.clean_text(single)]
    llm = utils._openai_bullets(text, model=utils._DEFAULT_MODEL) or []
    rules = utils._post_filter_bullets(utils._legistar_rule_based_bullets(text, limit=max(36, utils._MAX_BULLETS*3)),
                                       limit=max(24, utils._MAX_BULLETS*2))
    out, seen = [], set()
    for src in (llm, rules):
        for b in src:
            k = utils.clean_text(b).lower()
            if not k or k in seen:
                continue
            out.append(utils.clean_text(b))
            seen.add(k)
            if len(out) >= utils._MAX_BULLETS:
                break
        if len(out) >= utils._MAX_BULLETS:
            break
    if not out and not utils._SUMMARIZER_STRICT:
        out = utils._post_filter_bullets(utils._legistar_rule_based_bullets(text, limit=36), limit=utils._MAX_BULLETS)
    return out

def _fmt_date_time_local(dt_obj: datetime) -> Tuple[str, str]:
    dt_mt = utils.to_mt(dt_obj)
    date_str = dt_mt.strftime("%Y-%m-%d")
    time_str = dt_mt.strftime("%-I:%M %p") if hasattr(dt_mt, "strftime") else dt_mt.strftime("%I:%M %p").lstrip("0")
    return date_str, time_str

# ---------------- Discovery & build ----------------

def _discover_meeting_ids() -> List[int]:
    """Right now we rely on seeds; APIs on this tenant are closed."""
    # polite probe around seeds so we can pick up neighbors
    ids = set()
    for seed in SEED_EVENT_IDS:
        for d in range(-PROBE_RADIUS, PROBE_RADIUS + 1):
            ids.add(seed + d)
    return sorted(ids)

def parse_salida() -> List[dict]:
    items: List[dict] = []
    # Discover candidate meeting IDs (bounded)
    meeting_ids = _discover_meeting_ids()
    LOG.info("Salida: probing %d candidate meeting IDs (around %s)", len(meeting_ids), SEED_EVENT_IDS)

    for mid in meeting_ids:
        try:
            # 1) Get agenda page(s) from the files page (prefer Playwright)
            agenda_pages = _agenda_pages_via_playwright_files(mid)
            if not agenda_pages:
                # try plain HTML as a backup (rarely works on this tenant)
                agenda_pages = _agenda_pages_from_html_files(mid)

            if not agenda_pages:
                continue

            # 2) Resolve each agenda page to a PDF (plain HTTP, then Playwright)
            bullets: List[str] = []
            agenda_url_for_cache: Optional[str] = None
            text_for_dt: Optional[str] = None

            for ap in agenda_pages:
                pdf_url, pdf_bytes = _resolve_pdf_from_agenda_page(ap)
                if not pdf_url and not pdf_bytes:
                    LOG.info("Salida: no PDF via plain HTTP; using Playwright for %s", ap)
                    pdf_bytes = _pdf_bytes_via_playwright(ap)

                if pdf_url:
                    bullets = utils.summarize_pdf_if_any(pdf_url)
                    agenda_url_for_cache = pdf_url
                    # also try to parse time from the downloaded PDF
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

            if not bullets or not agenda_url_for_cache:
                continue

            # 3) Parse/approximate meeting datetime from PDF text
            when_dt = _parse_datetime_from_text(text_for_dt or "") if text_for_dt else None
            if not when_dt:
                # Keep card visible; refine on a subsequent run when time parses
                when_dt = utils.now_mt().replace(hour=18, minute=0, second=0, microsecond=0)
            if not utils.is_future(when_dt):
                continue

            # 4) Heuristic meeting title
            title = "City Council Meeting"
            if text_for_dt:
                if re.search(r"work\s+session", text_for_dt, re.I):
                    title = "City Council Work Session"
                if re.search(r"regular\s+council\s+meeting", text_for_dt, re.I):
                    title = "City Council Regular Meeting"

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
                    source=f"{BASE}/event/{mid}",
                )
            )

        except Exception as e:
            LOG.warning("Salida: error for meeting %s: %s", mid, e)

    return items

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(parse_salida(), indent=2, ensure_ascii=False))
