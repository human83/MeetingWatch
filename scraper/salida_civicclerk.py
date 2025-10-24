
from __future__ import annotations

import os
import re
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as _dtparser

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None
    
from datetime import datetime
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None

SALIDA_ONLY_TODAY_FWD = os.getenv("SALIDA_ONLY_TODAY_FWD", "1") == "1"
SALIDA_TZ = os.getenv("SALIDA_TZ", "America/Denver")  # Salida is MT

# Only keep City Council meetings; exclude work/study/workshop/retreat sessions
SALIDA_ONLY_COUNCIL = os.getenv("SALIDA_ONLY_COUNCIL", "1") == "1"

# Allow if it looks like a council meeting (city optional, "meeting" required)
# Matches: "City Council Meeting", "Council Regular Meeting", etc.
SALIDA_COUNCIL_ALLOW_RE = re.compile(os.getenv(
    "SALIDA_COUNCIL_ALLOW_RE",
    r"\b(?:city\s+)?council\b.*\bmeeting\b"
), re.I)

# Block common non-meeting council sessions (handles "worksession", hyphens, etc.)
SALIDA_COUNCIL_BLOCK_RE = re.compile(os.getenv(
    "SALIDA_COUNCIL_BLOCK_RE",
    r"\b(work[\s-]*session|worksession|study[\s-]*session|workshop|retreat|strategy[\s-]*session)\b"
), re.I)

CITY_NAME = "Salida"
PROVIDER = "CivicClerk"

PORTAL_BASE = os.getenv("SALIDA_CIVICCLERK_URL", "https://salidaco.portal.civicclerk.com").rstrip("/")
ALT_HOSTS: List[str] = [
    h.strip().rstrip("/") for h in os.getenv("SALIDA_CIVICCLERK_ALT_HOSTS", "").split(",") if h.strip()
]

ENTRY_PATHS = ["/", "/Meetings", "/en-US/Meetings", "/en/Meetings", "/en-US", "/en"]
MAX_TILES = int(os.getenv("CIVICCLERK_MAX_TILES", "200"))
MAX_DISCOVERY_PAGES = int(os.getenv("CIVICCLERK_MAX_DISCOVERY", "30"))
SALIDA_DEBUG = os.getenv("SALIDA_DEBUG", "0") == "1"

UA = {"User-Agent": "MeetingWatch/1.0 (+https://github.com/human83/MeetingWatch)"}

_MONTHS = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
_DAY = r"(?:Mon|Tues|Tue|Wed|Thu|Thur|Fri|Sat|Sun|Sunday|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday)"
_TIME = r"(?:\d{1,2}:\d{2}\s*(?:AM|PM))"
_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.I)
_INSERT_SPACES = [
    (re.compile(rf"({_DAY})(?={_MONTHS})", re.I), r"\1 "),
    (re.compile(rf"({_MONTHS})(?=\d)", re.I), r"\1 "),
    (re.compile(r"(\d{4})(?=\d{1,2}:\d{2})"), r"\1 "),
]

def _clean(s: Optional[str]) -> str:
    txt = " ".join((s or "").split())
    for pat, rep in _INSERT_SPACES:
        txt = pat.sub(rep, txt)
    return txt

def _parse_date(text: str) -> Optional[str]:
    if not text:
        return None
    t = _ORDINAL_RE.sub(r"\1", _clean(text))
    t = re.sub(r"\s+at\s+", " ", t, flags=re.I)
    m = re.search(rf"{_MONTHS}\s+\d{{1,2}},\s*\d{{4}}(?:\s+{_TIME})?", t, re.I)
    if m:
        try:
            return _dtparser.parse(m.group(0), fuzzy=True).date().isoformat()
        except Exception:
            pass
    try:
        return _dtparser.parse(t, fuzzy=True, dayfirst=False).date().isoformat()
    except Exception:
        return None

def _normalize(base: str, href: str) -> str:
    return urljoin(base if base.endswith('/') else base + '/', (href or '').lstrip('/'))

def _same_site(a: str, b: str) -> bool:
    try:
        ha, hb = urlparse(a).hostname or "", urlparse(b).hostname or ""
        return ha.split(':')[0].endswith("civicclerk.com") and hb.split(':')[0].endswith("civicclerk.com")
    except Exception:
        return False

def _api_base_from_portal(url_or_host: str) -> str:
    host = urlparse(url_or_host).hostname or url_or_host
    m = re.search(r"^([a-z0-9-]+)(?:\.portal)?\.civicclerk\.com$", host or "", re.I)
    sub = m.group(1) if m else "salidaco"
    return f"https://{sub}.api.civicclerk.com"

def _meeting_id_from_event_url(u: str) -> Optional[str]:
    m = re.search(r"/event/(\d+)", urlparse(u).path or "")
    return m.group(1) if m else None

LIKELY_TILE_SEL = "[role='link'], a.meeting, .meeting, .tile, .card, article, li, .Row, .ListItem"
LIKELY_TIME_CHILDREN = "time[datetime], time, .meeting-date, .date, [data-date], [data-start]"
PRI_WORDS = ("meeting", "agenda", "packet", "council", "board", "commission")

def _get_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        r = requests.get(url, timeout=30, headers=UA)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def _extract_text(tag) -> str:
    t = tag.get_text(" ", strip=True) if getattr(tag, "get_text", None) else ""
    aria = (tag.get("aria-label") or "") if getattr(tag, "get", None) else ""
    title = (tag.get("title") or "") if getattr(tag, "get", None) else ""
    return " ".join([t, aria, title]).strip()

def _scan_tiles_bs4(soup: BeautifulSoup, source_url: str) -> List[Dict]:
    items: List[Dict] = []
    tiles = soup.select(LIKELY_TILE_SEL)[:MAX_TILES]
    for tag in tiles:
        href = (getattr(tag, "get", lambda *_: None)("href") or "").strip()
        if not href:
            onclick = getattr(tag, "get", lambda *_: None)("onclick") or ""
            m = re.search(r"(?:location\.href\s*=\s*|window\.open\()\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if m:
                href = m.group(1)
        if not href:
            continue

        full = _normalize(source_url, href)
        if not _same_site(source_url, full):
            continue

        iso = None
        for c in tag.select(LIKELY_TIME_CHILDREN):
            dtxt = _extract_text(c)
            iso = _parse_date(dtxt)
            if iso:
                break

        title = _extract_text(tag) or "Meeting"

        items.append(
            {
                "city": CITY_NAME,
                "provider": PROVIDER,
                "title": title[:150],
                "date": iso or "",
                "url": full,
                "source": source_url,
            }
        )
    return items

def _requests_candidates(url: str) -> List[Dict]:
    soup = _get_soup(url)
    if not soup:
        return []
    out = _scan_tiles_bs4(soup, url)
    if out:
        return out

    links: List[str] = []
    for a in soup.select("a[href], [onclick], [data-href], [data-url], [data-link], [role='link']"):
        href = (getattr(a, "get", lambda *_: None)("href") or "").strip()
        data = (getattr(a, "get", lambda *_: None)("data-href") or "") or (getattr(a, "get", lambda *_: None)("data-url") or "") or (getattr(a, "get", lambda *_: None)("data-link") or "")
        onclick = (getattr(a, "get", lambda *_: None)("onclick") or "")
        text = _extract_text(a).lower()
        target = None
        if href and href != "#" and not href.lower().startswith("javascript:"):
            target = href
        elif data:
            target = data
        else:
            m = re.search(r"(?:location\.href\s*=\s*|window\.open\()\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if m:
                target = m.group(1)
        if not target:
            continue
        if any(w in (target.lower()) for w in PRI_WORDS) or any(w in text for w in PRI_WORDS):
            full = _normalize(url, target)
            if _same_site(url, full):
                links.append(full)

    results: List[Dict] = []
    seen: Set[str] = set()
    for target in links[:MAX_DISCOVERY_PAGES]:
        if target in seen:
            continue
        seen.add(target)
        sub = _get_soup(target)
        if not sub:
            continue
        results.extend(_scan_tiles_bs4(sub, target))
        if results:
            break
    return results

def _playwright_candidates(entry_url: str) -> List[Dict]:
    out: List[Dict] = []
    if sync_playwright is None:
        return out

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(30000)
            if SALIDA_DEBUG:
                print(f"[salida] Navigating to {entry_url}")
            page.goto(entry_url, wait_until="networkidle")

            locator = page.locator("a, [onclick], [data-href], [data-url], [data-link], [role='link']")
            els = locator.all()

            meta: List[Tuple[str, str]] = []
            for el in els:
                try:
                    href = (el.get_attribute("href") or "").strip()
                    data = (el.get_attribute("data-href") or "") or (el.get_attribute("data-url") or "") or (el.get_attribute("data-link") or "")
                    onclick = el.get_attribute("onclick") or ""
                    text = (el.text_content() or "").strip()

                    target = None
                    if href and href != "#" and not href.lower().startswith("javascript:"):
                        target = href
                    elif data:
                        target = data
                    else:
                        m = re.search(r"(?:location\.href\s*=\s*|window\.open\()\s*['\"]([^'\"]+)['\"]", onclick, re.I)
                        if m:
                            target = m.group(1)

                    if not target:
                        continue
                    full = _normalize(entry_url, target)
                    if not _same_site(entry_url, full):
                        continue
                    if "/event/" in full:
                        if not full.endswith("/files"):
                            full = _normalize(full, "files")
                        meta.append((full, text))
                except Exception:
                    pass

            seen=set()
            items: List[Dict]=[]
            for url, txt in meta:
                if url in seen:
                    continue
                seen.add(url)
                items.append({
                    "city": CITY_NAME,
                    "provider": PROVIDER,
                    "title": (txt or "Meeting")[:150] or "Meeting",
                    "date": _parse_date(txt) or "",
                    "url": url,
                    "source": entry_url,
                })

            out.extend(items[:MAX_TILES])

            if not out:
                for path in ["/Meetings", "/en/Meetings", "/en-US/Meetings", "/Agendas-Minutes", "/en/Agendas-Minutes"]:
                    try:
                        page.goto(_normalize(entry_url, path), wait_until="networkidle")
                        els = page.locator("a, [role='link']").all()
                        for el in els:
                            href = (el.get_attribute("href") or "").strip()
                            text = (el.text_content() or "").strip()
                            if not href:
                                continue
                            full = _normalize(entry_url, href)
                            if not _same_site(entry_url, full):
                                continue
                            if "/event/" in full:
                                if not full.endswith("/files"):
                                    full = _normalize(full, "files")
                                out.append({
                                    "city": CITY_NAME,
                                    "provider": PROVIDER,
                                    "title": (text or "Meeting")[:150],
                                    "date": _parse_date(text) or "",
                                    "url": full,
                                    "source": _normalize(entry_url, path),
                                })
                        if out:
                            break
                    except Exception:
                        pass
        finally:
            browser.close()
    return out

FILE_HREF_RE = re.compile(r"/files/(?:agenda|packet)/(\d+)", re.I)
STREAM_FILEID_RE = re.compile(r"GetMeetingFileStream\(fileId=(\d+)", re.I)

def _extract_fileids_from_html(html_text: str) -> List[str]:
    ids = list(dict.fromkeys(FILE_HREF_RE.findall(html_text or "")))
    ids += [m for m in STREAM_FILEID_RE.findall(html_text or "")]
    return list(dict.fromkeys(ids))

def _file_weight(label: str) -> int:
    t = (label or "").lower()
    if "minutes" in t:
        return -100
    score = 0
    if "packet" in t:
        score += 50
    if "agenda" in t:
        score += 30
    if "regular" in t or "council" in t or "work session" in t:
        score += 3
    return score

def _ensure_files_url(u: str) -> str:
    parsed = urlparse(u)
    m = re.search(r"^(/event/\d+)(?:/|$)", parsed.path or "", re.I)
    if m and not m.group(0).endswith("/files") and "/files/" not in parsed.path:
        return urljoin(u, m.group(1) + "/files")
    return u

def _api_list_files(meeting_url: str) -> List[Dict]:
    meeting_id = _meeting_id_from_event_url(meeting_url)
    if not meeting_id:
        return []
    api_base = _api_base_from_portal(meeting_url)
    urls = [
        f"{api_base}/v1/Meetings/GetMeetingFiles?meetingId={meeting_id}",
        f"{api_base}/v1/Meetings/GetMeeting?meetingId={meeting_id}",
        f"{api_base}/v1/Meetings/GetMeetingFilesForEvent?eventId={meeting_id}",
        f"{api_base}/v1/Meetings/GetMeetingFiles?eventId={meeting_id}",
    ]
    out: List[Dict] = []
    for u in urls:
        try:
            r = requests.get(u, timeout=20, headers=UA)
            if r.status_code != 200:
                continue
            data = r.json()
            files = []
            if isinstance(data, dict):
                for k in ("files", "Files", "MeetingFiles", "meetingFiles"):
                    if k in data and isinstance(data[k], list):
                        files = data[k]
                        break
                if not files and "Meeting" in data and isinstance(data["Meeting"], dict):
                    for k in ("files", "Files", "MeetingFiles", "meetingFiles"):
                        if k in data["Meeting"] and isinstance(data["Meeting"][k], list):
                            files = data["Meeting"][k]
                            break
            elif isinstance(data, list):
                files = data

            for f in files or []:
                label = f.get("Name") or f.get("name") or f.get("Title") or f.get("title") or ""
                fid = str(f.get("Id") or f.get("FileId") or f.get("fileId") or f.get("id") or "").strip()
                if not fid or not fid.isdigit():
                    file_obj = f.get("File") if isinstance(f, dict) else None
                    if isinstance(file_obj, dict):
                        fid = str(file_obj.get("Id") or "").strip()
                if fid and fid.isdigit():
                    out.append({"fileId": fid, "label": label})
            if out:
                if SALIDA_DEBUG:
                    print(f"[salida] API files for {meeting_id}: {len(out)} via {u}")
                break
        except Exception:
            continue
    return out

def _collect_file_candidates_requests(files_url: str) -> List[Tuple[int, str]]:
    cands: List[Tuple[int, str]] = []
    soup = _get_soup(files_url)
    if not soup:
        return cands
    for a in soup.select("a[href*='/files/agenda/'], a[href*='/files/packet/']"):
        href = a.get("href") or ""
        lab = " ".join([a.get("aria-label") or "", a.get("title") or "", a.get_text(" ", strip=True) or ""]).strip()
        m = FILE_HREF_RE.search(href)
        if m:
            cands.append((_file_weight(lab), m.group(1)))
    html_text = soup.decode()
    for fid in _extract_fileids_from_html(html_text):
        cands.append((_file_weight("Agenda Packet"), fid))
    cands.sort(key=lambda t: t[0], reverse=True)
    return cands

def _collect_file_candidates_with_playwright(files_url: str) -> List[Tuple[int, str]]:
    cands: List[Tuple[int, str]] = []
    if sync_playwright is None:
        return cands
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_default_timeout(30000)

            captured: List[str] = []
            def on_response(resp):
                try:
                    u = resp.url
                    if "GetMeetingFileStream" in u:
                        m = STREAM_FILEID_RE.search(u)
                        if m:
                            captured.append(m.group(1))
                except Exception:
                    pass
            page.on("response", on_response)

            page.goto(files_url, wait_until="networkidle")

            for text in [
                "Agenda Packet (PDF)",
                "Agenda Packet (Plain Text)",
                "Agenda (PDF)",
                "Agenda (Plain Text)",
                "Packet",
                "Agenda",
                "Download",
            ]:
                try:
                    for b in page.locator("[role='button'], button").all()[:12]:
                        try:
                            lab = ((b.get_attribute("aria-label") or "") + " " + (b.text_content() or "")).lower()
                            if any(k in lab for k in ("agenda", "packet", "download")):
                                b.click(timeout=1000, force=True)
                                time.sleep(0.2)
                        except Exception:
                            pass
                    el = page.get_by_text(text, exact=False).first
                    if el:
                        el.click(timeout=1500, force=True)
                        time.sleep(0.4)
                except Exception:
                    pass

            for sel in [
                "a[data-fileid]",
                "button[data-fileid]",
                "[data-file-id]",
                "a[href*='/files/agenda/'], a[href*='/files/packet/']",
            ]:
                try:
                    for a in page.locator(sel).all():
                        href = a.get_attribute("href") or ""
                        lab = ((a.get_attribute("aria-label") or "") + " " + (a.get_attribute("title") or "") + " " + (a.text_content() or "")).strip()
                        fid = (a.get_attribute("data-fileid") or a.get_attribute("data-file-id") or "").strip()
                        if not fid and href:
                            m = FILE_HREF_RE.search(href)
                            if m:
                                fid = m.group(1)
                        if fid and fid.isdigit():
                            cands.append((_file_weight(lab or "Agenda Packet"), fid))
                except Exception:
                    pass

            for fid in captured:
                cands.append((_file_weight("Agenda Packet"), fid))

        finally:
            browser.close()

    seen = set()
    ranked: List[Tuple[int, str]] = []
    for w, fid in sorted(cands, key=lambda t: t[0], reverse=True):
        if fid not in seen:
            seen.add(fid)
            ranked.append((w, fid))
    return ranked

def find_agenda_pdf(source_url: str) -> Tuple[Optional[str], Optional[str]]:
    files_url = _ensure_files_url(source_url)
    api_base = _api_base_from_portal(files_url)

    api_files = _api_list_files(files_url)
    if api_files:
        api_files.sort(key=lambda f: _file_weight(f.get("label") or ""), reverse=True)
        fid = api_files[0]["fileId"]
        pdf = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
        txt = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText=true)"
        if SALIDA_DEBUG:
            print(f"[salida] API agenda fileId={fid} -> {pdf}")
        return pdf, txt

    try:
        cands = _collect_file_candidates_with_playwright(files_url)
    except Exception:
        cands = []

    if cands:
        _, fid = cands[0]
        pdf = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
        txt = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText=true)"
        if SALIDA_DEBUG:
            print(f"[salida] PW agenda fileId={fid} -> {pdf}")
        return pdf, txt

    cands = _collect_file_candidates_requests(files_url)
    if cands:
        _, fid = cands[0]
        pdf = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText=false)"
        txt = f"{api_base}/v1/Meetings/GetMeetingFileStream(fileId={fid},plainText=true)"
        if SALIDA_DEBUG:
            print(f"[salida] HTML agenda fileId={fid} -> {pdf}")
        return pdf, txt

    if SALIDA_DEBUG:
        print(f"[salida] No agenda fileIds on {files_url}")
    return None, None

def _hosts_to_try() -> Iterable[str]:
    tried = [PORTAL_BASE] + ALT_HOSTS
    seen: Set[str] = set()
    for h in tried:
        if h and h not in seen:
            seen.add(h)
            yield h

def _today_iso_in_tz(tz_name: str) -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    # Fallback to system local date if zoneinfo not available
    return datetime.now().date().isoformat()

def parse_salida() -> List[Dict]:
    tried_urls: List[str] = []
    discovered: List[Dict] = []

    print('[salida] parse_salida starting; hosts:', ', '.join(list(_hosts_to_try())))

    for host in _hosts_to_try():
        for path in ENTRY_PATHS:
            entry = (host + path).rstrip("/")
            tried_urls.append(entry)

            items: List[Dict] = []
            try:
                items = _playwright_candidates(entry)
            except Exception:
                items = []

            if not items:
                items = _requests_candidates(entry)

            if items:
                discovered.extend(items)
                break
        if discovered:
            break

    seen: Set[Tuple[str, str, str]] = set()
    unique: List[Dict] = []
    for m in discovered:
        key = (m.get("date", "") or "", m.get("title", "") or "", m.get("url", "") or "")
        if key not in seen:
            seen.add(key)
            unique.append(m)
        # --- Keep only today-and-future for Salida ---
        if SALIDA_ONLY_TODAY_FWD:
            cutoff = _today_iso_in_tz(SALIDA_TZ)
            unique = [
                m for m in unique
                if (m.get("date") or "") >= cutoff
            ]
        # --- Keep only City Council meetings; drop work/study/workshop/retreat sessions ---
        if SALIDA_ONLY_COUNCIL:
            def _is_council_meeting(title: str) -> bool:
                t = (title or "").strip()
                if not t:
                    return False
                if not SALIDA_COUNCIL_ALLOW_RE.search(t):
                    return False
                if SALIDA_COUNCIL_BLOCK_RE.search(t):
                    return False
                return True

            before = len(unique)
            unique = [m for m in unique if _is_council_meeting(m.get("title"))]
            if SALIDA_DEBUG:
                dropped = before - len(unique)
                if dropped:
                    print(f"[salida] council filter dropped {dropped} non-meeting item(s)")

    for m in unique:
        u = (m.get("url") or "").strip()
        if u.lower().endswith(".pdf"):
            m["agenda_url"] = u
            continue

        pdf, txt = find_agenda_pdf(u)
        if pdf:
            m["agenda_url"] = pdf
        if txt:
            m["agenda_text_url"] = txt

    with_pdf = sum(1 for x in unique if x.get('agenda_url'))
    print(f"[salida] Visited {len(tried_urls)} entry url(s); accepted {len(unique)} items; with agenda: {with_pdf}")

    return unique

def parse():
    return parse_salida()

if __name__ == "__main__":
    items = parse_salida()
    print(f"[salida] parse() produced {len(items)} items")
    for m in items[:5]:
        print(" -", m.get("date"), m.get("title"), "->", m.get("url"))
