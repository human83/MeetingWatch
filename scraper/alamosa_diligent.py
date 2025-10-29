# scraper/alamosa_diligent.py
from __future__ import annotations
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Set

import pytz
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .utils import make_meeting, summarize_pdf_if_any

PORTAL_URL = "https://cityofalamosa.diligent.community/Portal/MeetingInformation.aspx?Org=Cal&Id=107"
MT = pytz.timezone("America/Denver")

ALLOW_TYPES = {
    "City Council Regular Meeting": [r"\bcity council\b.*\bregular\b", r"\bregular meeting\b"],
    "City Council Special Meeting": [r"\bcity council\b.*\bspecial\b", r"\bspecial meeting\b"],
}

# --- helpers -----------------------------------------------------------------

def _abs(url: str) -> str:
    if url.startswith("http"):
        return url
    base = PORTAL_URL.rsplit("/", 1)[0]
    return f"{base}/{url.lstrip('/')}"

def _classify(detail_text: str) -> Optional[str]:
    t = " ".join(detail_text.lower().split())
    if "city council" not in t:
        return None
    for mtype, pats in ALLOW_TYPES.items():
        if any(re.search(p, t, re.I) for p in pats):
            return mtype
    return None

def _detail_text(page) -> str:
    # Body containers
    for sel in [".dg-portal-detail", ".dgc-meeting-detail", ".meeting-detail", "#MainContent", "#content", "body"]:
        try:
            if page.locator(sel).count():
                txt = page.locator(sel).inner_text(timeout=1000)
                if txt and len(txt) > 40:
                    return txt
        except Exception:
            pass
    try:
        return page.inner_text("body", timeout=1000)
    except Exception:
        return ""

def _header_text(page) -> str:
    # Collect header bar text (this is where “— OCT 29 2025” lives)
    bits = []
    try:
        for el in page.locator("h1, h2, .meeting-header, .meeting-title").all():
            t = (el.inner_text() or "").strip()
            if t:
                bits.append(t)
    except Exception:
        pass
    return " | ".join(bits)

def _normalize(s: str) -> str:
    # make unicode dashes/spaces harmless
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\xa0", " ")
    return " ".join(s.split())

def _parse_date_time(header: str, body: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Accept several formats:
      - '… - OCT 29 2025' (header)
      - 'Wednesday, October 29, 2025' (body)
      - 'October 29, 2025' (body)
      - any 'Time: 06:30 PM' or naked '6:30 PM'
    """
    H = _normalize(header)
    B = _normalize(body)

    date_iso = None

    # 1) Header like 'OCT 29 2025'
    m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b\s+(\d{1,2})\s+(\d{4})", H, re.I)
    if m:
        try:
            d = datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%b %d, %Y")
            date_iso = d.strftime("%Y-%m-%d")
        except Exception:
            pass

    # 2) Long form in body
    if not date_iso:
        m2 = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}", B, re.I)
        if m2:
            raw = m2.group(0)
            for fmt in ("%A, %B %d, %Y", "%A %B %d, %Y"):
                try:
                    d = datetime.strptime(raw, fmt)
                    date_iso = d.strftime("%Y-%m-%d")
                    break
                except Exception:
                    pass

    # 3) Month day, year in body
    if not date_iso:
        m3 = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s+(20\d{2})\b", B)
        if m3:
            try:
                d = datetime.strptime(f"{m3.group(1)} {m3.group(2)}, {m3.group(3)}", "%B %d, %Y")
                date_iso = d.strftime("%Y-%m-%d")
            except Exception:
                try:
                    d = datetime.strptime(f"{m3.group(1)[:3]} {m3.group(2)}, {m3.group(3)}", "%b %d, %Y")
                    date_iso = d.strftime("%Y-%m-%d")
                except Exception:
                    pass

    # TIME
    time_local = None
    for src in (B, H):
        mt = re.search(r"\bTime:\s*([0-2]?\d:\d{2}\s*[AP]\.?M\.?)\b", src, re.I)
        if mt:
            time_local = mt.group(1).replace(".", "").upper()
            break
    if not time_local:
        mt2 = re.search(r"\b([0-2]?\d:\d{2}\s*[AP]\.?M\.?)\b", B, re.I)
        if mt2:
            time_local = mt2.group(1).replace(".", "").upper()
    if not time_local:
        time_local = "Time TBD"

    return date_iso, time_local

def _pdf_link(page) -> Optional[str]:
    # Priority: agenda/packet buttons
    try:
        a = page.query_selector("a#document-cover-pdf")
        if a:
            href = (a.get_attribute("href") or "").strip()
            if href:
                return _abs(href)
    except Exception:
        pass

    # next: any visible anchor with agenda/packet text
    try:
        for a in page.locator("a:visible").all():
            href = (a.get_attribute("href") or "").strip()
            txt = (a.inner_text() or "").strip().lower()
            if href and ".pdf" in href.lower() and ("agenda" in txt or "packet" in txt):
                return _abs(href)
        # fallback: any .pdf
        for a in page.locator("a:visible").all():
            href = (a.get_attribute("href") or "").strip()
            if href and ".pdf" in href.lower():
                return _abs(href)
    except Exception:
        pass
    return None

# --- main --------------------------------------------------------------------

def parse_alamosa() -> List[Dict]:
    items: List[Dict] = []
    seen: Set[Tuple[str, str, str, str]] = set()
    today_mt = datetime.now(MT).date()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        context = browser.new_context(user_agent="MeetingWatch/Alamosa/1.4")
        page = context.new_page()

        # 1) open portal
        try:
            page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            print("[alamosa] Timeout loading portal")
        except Exception as e:
            print(f"[alamosa] Error loading portal: {e}")

        # 2) find candidate detail links
        candidates: List[str] = []
        for a in page.locator("a.list-link:visible").all():
            href = (a.get_attribute("href") or "").strip()
            txt = (a.inner_text() or "").strip().lower()
            if not href:
                continue
            if "MeetingInformation" in href or "MeetingDetail" in href:
                # pre-filter by 'city council'
                if "council" in txt:
                    candidates.append(_abs(href))

        if not candidates:
            # fallback: click through some tiles to surface detail pages
            tiles = page.locator("li, .k-listview-item, .dgc-meeting, .meeting, .item-content a, a.list-link").all()
            print(f"[alamosa] No hrefs; falling back to scanning {len(tiles)} tiles")
            for t in tiles[:25]:
                try:
                    t.click(timeout=800)
                    page.wait_for_timeout(400)
                    if "MeetingInformation" in page.url or "MeetingDetail" in page.url:
                        candidates.append(page.url)
                    page.go_back(timeout=2000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass

        # dedupe
        seen_href: Set[str] = set()
        uniq: List[str] = []
        for h in candidates:
            if h not in seen_href:
                uniq.append(h)
                seen_href.add(h)

        print(f"[alamosa] Found candidate hrefs: {len(uniq)}")

        visited = 0
        accepted = 0

        for href in uniq:
            visited += 1
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                print(f"[alamosa] Skip: cannot open {href}")
                continue

            header = _header_text(page)
            body = _detail_text(page)
            merged_text = f"{header}\n{body}"

            mtype = _classify(merged_text)
            if not mtype:
                continue  # not Regular or Special City Council

            date_iso, time_local = _parse_date_time(header, body)
            if not date_iso:
                print(f"[alamosa] Skip: could not parse date (header='{header[:60]}…')")
                continue

            try:
                d_obj = datetime.strptime(date_iso, "%Y-%m-%d").date()
                if d_obj < today_mt:
                    continue
            except Exception:
                continue

            pdf = _pdf_link(page)

            key = (date_iso, time_local or "", mtype, pdf or "")
            if key in seen:
                continue
            seen.add(key)

            summary = summarize_pdf_if_any(pdf)

            items.append(
                make_meeting(
                    city_or_body="City of Alamosa",
                    meeting_type=mtype,
                    date=date_iso,
                    start_time_local=time_local or "Time TBD",
                    status="Scheduled" if pdf else "Scheduled (no agenda yet)",
                    location=None,  # body carries it but not always consistently; omit if noisy
                    agenda_url=pdf,
                    agenda_summary=summary,
                    source=href,
                )
            )
            accepted += 1

        print(f"[alamosa] Visited {visited} candidates; accepted {accepted} items")
        context.close()
        browser.close()

    return items
