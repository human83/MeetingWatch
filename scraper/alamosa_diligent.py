# scraper/alamosa_diligent.py
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import pytz
from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from .utils import make_meeting, summarize_pdf_if_any

MT = pytz.timezone("America/Denver")
_LOG = logging.getLogger(__name__)

PORTAL_URL = (
    "https://cityofalamosa.diligent.community/Portal/MeetingInformation.aspx?Org=Cal&Id=107"
)

# We only accept City Council Regular or Special meetings
TYPE_PATTERNS = {
    "City Council Regular Meeting": [r"\bcity council\b.*\bregular\b", r"\bregular meeting\b"],
    "City Council Special Meeting": [r"\bcity council\b.*\bspecial\b", r"\bspecial meeting\b"],
}

DATE_FMTS = [
    "%A, %B %d, %Y",   # Wednesday, October 29, 2025
    "%B %d, %Y",       # October 29, 2025
    "%b %d, %Y",       # Oct 29, 2025
]
MONTHS_ABBR = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
MONTHS_FULL = r"(January|February|March|April|May|June|July|August|September|October|November|December)"


def _classify_meeting(detail_text: str) -> Optional[str]:
    t = " ".join(detail_text.lower().split())
    if "city council" not in t:
        return None
    for mtype, pats in TYPE_PATTERNS.items():
        if any(re.search(p, t, re.I) for p in pats):
            return mtype
    return None


def _extract_pdf_from_detail(page) -> Optional[str]:
    # Priority: explicit Agenda / Packet buttons/links
    try:
        a = page.locator("#document-cover-pdf").first
        if a and a.count() > 0:
            href = a.get_attribute("href") or ""
            if href:
                return href
    except Exception:
        pass

    try:
        for a in page.locator(
            ".dg-portal-detail a, .dgc-meeting-detail a, .meeting-detail a, "
            "#content a, #MainContent a, body a"
        ).all():
            href = (a.get_attribute("href") or "").strip()
            text = (a.inner_text() or "").strip().lower()
            if not href:
                continue
            if ".pdf" in href.lower() and ("agenda" in text or "packet" in text):
                return href
        # fallback: any .pdf
        for a in page.locator(
            ".dg-portal-detail a, .dgc-meeting-detail a, .meeting-detail a, "
            "#content a, #MainContent a, body a"
        ).all():
            href = (a.get_attribute("href") or "").strip()
            if href and ".pdf" in href.lower():
                return href
    except Exception:
        pass
    return None


def _get_detail_text(page) -> str:
    for sel in [
        ".dg-portal-detail",
        ".dgc-meeting-detail",
        ".meeting-detail",
        "#content",
        "#MainContent",
        "body",
    ]:
        try:
            if page.locator(sel).count():
                txt = page.locator(sel).inner_text(timeout=1000)
                if txt and len(txt) > 40:
                    return txt
        except Exception:
            continue
    try:
        return page.inner_text("body", timeout=1000)
    except Exception:
        return ""


def _normalize_ordinal(day: str) -> str:
    # strip 1st, 2nd, 3rd, 4th → 1,2,3,4
    return re.sub(r"(st|nd|rd|th)$", "", day, flags=re.I)


def _parse_date_time(detail_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (YYYY-MM-DD, 'H:MM AM/PM' or 'Time TBD')
    We try several places/patterns:
      1) Big title like "CITY COUNCIL SPECIAL MEETING - OCT 29 2025"
      2) Body long-form "Wednesday, October 29, 2025"
      3) Body "October 29, 2025"
      4) Compact "Oct 29 2025" (no comma)
      5) "When: Oct 29, 2025 6:30 PM" / "Time: 06:30 PM"
    """
    text = " ".join(detail_text.split())

    # 1) Title bar with abbreviated month and no comma: OCT 29 2025
    m = re.search(rf"\b{MONTHS_ABBR}\s+(\d{{1,2}})\s+(\d{{4}})\b", text, re.I)
    if m:
        mon, day, year = m.group(1), _normalize_ordinal(m.group(2)), m.group(3)
        try:
            d = datetime.strptime(f"{mon} {day}, {year}", "%b %d, %Y")
            date_iso = d.strftime("%Y-%m-%d")
        except Exception:
            date_iso = None
    else:
        date_iso = None

    # 2/3) Long/standard month, with/without weekday
    if not date_iso:
        for pat in [
            rf"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+{MONTHS_FULL}\s+\d{{1,2}},\s+\d{{4}}",
            rf"\b{MONTHS_FULL}\s+\d{{1,2}},\s+\d{{4}}\b",
        ]:
            m2 = re.search(pat, text, re.I)
            if m2:
                raw = m2.group(0).replace("Sept", "Sep")
                raw = re.sub(r"(\d{1,2})(st|nd|rd|th),", r"\1,", raw)
                for fmt in DATE_FMTS:
                    try:
                        d = datetime.strptime(raw, fmt)
                        date_iso = d.strftime("%Y-%m-%d")
                        break
                    except Exception:
                        continue
            if date_iso:
                break

    # 4) Compact like "Oct 29 2025"
    if not date_iso:
        m3 = re.search(rf"\b{MONTHS_ABBR}\s+(\d{{1,2}})\s+(\d{{4}})\b", text, re.I)
        if m3:
            mon, day, year = m3.group(1), _normalize_ordinal(m3.group(2)), m3.group(3)
            try:
                d = datetime.strptime(f"{mon} {day}, {year}", "%b %d, %Y")
                date_iso = d.strftime("%Y-%m-%d")
            except Exception:
                date_iso = None

    # TIME
    time_local = None
    # Explicit label first: "Time: 06:30 PM" / "When: ... 6:30 PM"
    mt = re.search(r"\bTime:\s*([0-2]?\d:\d{2}\s*[AP]\.?M\.?)\b", text, re.I)
    if not mt:
        mt = re.search(r"\bWhen:\s*.*?([0-2]?\d:\d{2}\s*[AP]\.?M\.?)\b", text, re.I)
    if not mt:
        mt = re.search(r"\b([0-2]?\d:\d{2}\s*[AP]\.?M\.?)\b", text, re.I)
    if mt:
        time_local = mt.group(1).replace(".", "").upper()
    else:
        time_local = "Time TBD"

    return date_iso, time_local


def parse_alamosa() -> List[Dict]:
    items: List[Dict] = []
    seen: Set[Tuple[str, str, str, str]] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(user_agent="MeetingWatch/Alamosa-Diligent/1.3")
        page = context.new_page()

        # 1) Load the portal page
        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            print("[alamosa] Timeout loading portal")
        except Exception as e:
            print(f"[alamosa] Error loading portal: {e}")

        # 2) Gather candidate detail links from visible anchors
        anchors = page.locator("a:visible").all()
        candidate_hrefs: List[str] = []
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            txt = (a.inner_text() or "").strip()
            if not href:
                continue
            # Diligent detail pages are MeetingInformation/MeetingDetail-style links
            if "MeetingInformation" in href or "MeetingDetail" in href:
                # Heuristic: likely a Council item
                if re.search(r"\b(City|Council|Meeting)\b", txt, re.I):
                    candidate_hrefs.append(href)

        # If none found, click common tiles to tease out links
        if not candidate_hrefs:
            tiles = page.locator(
                "li, .k-listview-item, .dgc-meeting, .meeting, .item-content a, a"
            ).all()
            for t in tiles[:20]:
                try:
                    t.click(timeout=800)
                    page.wait_for_timeout(400)
                    if page.url and "MeetingInformation" in page.url:
                        candidate_hrefs.append(page.url)
                    page.go_back(timeout=2000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass

        # Deduplicate & normalize to absolute URLs
        uniq: List[str] = []
        seen_href: Set[str] = set()
        base = PORTAL_URL.rsplit("/", 1)[0]
        for h in candidate_hrefs:
            if not h:
                continue
            if not h.startswith("http"):
                h = base + "/" + h.lstrip("/")
            if h not in seen_href:
                uniq.append(h)
                seen_href.add(h)

        print(f"[alamosa] Found candidate hrefs: {len(uniq)}")

        # 3) Walk each detail page and accept only Council Regular/Special, today or future
        today_mt = datetime.now(MT).date()
        accepted = 0
        visited = 0

        for href in uniq:
            visited += 1
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                print(f"[alamosa] Skip: cannot open {href}")
                continue

            detail_text = _get_detail_text(page)
            if not detail_text:
                continue

            mtype = _classify_meeting(detail_text)
            if mtype is None:
                # Not a council regular/special meeting
                continue

            date_iso, time_local = _parse_date_time(detail_text)
            if not date_iso:
                print("[alamosa] Skip: could not parse date")
                continue

            try:
                d_obj = datetime.strptime(date_iso, "%Y-%m-%d").date()
            except Exception:
                continue

            if d_obj < today_mt:
                # past meetings excluded
                continue

            # Location (best-effort)
            location = None
            mloc = re.search(
                r"(Council Chambers.*?300 Hunt Avenue.*?Alamosa.*)|"
                r"(300 Hunt Avenue.*Alamosa.*)",
                detail_text,
                re.I,
            )
            if mloc:
                location = (mloc.group(1) or mloc.group(2) or "").strip() or None

            pdf_url = _extract_pdf_from_detail(page) or None

            key = (date_iso, time_local or "", mtype, pdf_url or "")
            if key in seen:
                continue
            seen.add(key)

            agenda_summary = summarize_pdf_if_any(pdf_url)

            items.append(
                make_meeting(
                    city_or_body="City of Alamosa",
                    meeting_type=mtype,
                    date=date_iso,
                    start_time_local=time_local or "Time TBD",
                    status="Scheduled" if pdf_url else "Scheduled (no agenda yet)",
                    location=location,
                    agenda_url=pdf_url,
                    agenda_summary=agenda_summary,
                    source=href,
                )
            )
            accepted += 1

        print(f"[alamosa] Visited {visited} candidates; accepted {accepted} items")

        context.close()
        browser.close()

    return items
