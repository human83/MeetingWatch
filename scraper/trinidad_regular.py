# scraper/trinidad_regular.py
from __future__ import annotations
import re
import json
import time
from datetime import datetime, timedelta, date
from urllib.parse import urljoin

from dateutil import tz
from dateutil.parser import isoparse as _isoparse

from playwright.sync_api import sync_playwright

MT_TZ = tz.gettz("America/Denver")
BASE = "https://www.trinidad.co.gov"

MONTH_URL = (
    "https://www.trinidad.co.gov/calendar.php"
    "?view=month&month={m}&day=1&year={y}&calendar="
)

def _yyyy_mm_dd_today_mt() -> date:
    return datetime.now(tz=MT_TZ).date()

def _mk_months(start: date, months_ahead: int):
    y = start.year
    m = start.month
    for k in range(months_ahead):
        mm = ((m - 1 + k) % 12) + 1
        yy = y + ((m - 1 + k) // 12)
        yield yy, mm

def _clean(txt: str) -> str:
    return re.sub(r"\s+", " ", (txt or "")).strip()

def _parse_fc_time(tstr: str) -> tuple[int, int]:
    """
    Accepts '6p', '6:00p', '06:00 PM', etc. Returns (hour, minute) 24h.
    """
    s = (tstr or "").strip().lower()
    if not s:
        return (0, 0)

    # normalize like '6p' -> '6 pm'
    s = s.replace("a.m.", "am").replace("p.m.", "pm")
    s = re.sub(r"([0-9])\s*([ap])\b", r"\1 \2m", s)  # 6p -> 6 pm
    # pull hour:min and am/pm
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", s)
    if not m:
        return (0, 0)
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh != 12:
        hh += 12
    if ampm == "am" and hh == 12:
        hh = 0
    return (hh, mm)

def _as_iso_mt(d: date, hh: int, mm: int) -> str:
    dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=MT_TZ)
    return dt.isoformat()

def _accept_title(title: str) -> bool:
    t = (title or "").lower()
    if "city council" not in t:
        return False
    if "regular" not in t:
        return False
    if "work session" in t:
        return False
    return True

def _grab_modal(page) -> str:
    """
    The page is already on ?…&id=#### which auto-opens the modal.
    Wait and pull visible modal text.
    """
    page.wait_for_selector("#event-modal", state="attached", timeout=8000)
    # Give the site a beat to render rich text
    try:
        page.wait_for_selector('#event-modal[aria-hidden="false"], #event-modal.show', timeout=3000)
    except Exception:
        pass
    body = ""
    try:
        body = page.locator("#event-modal").inner_text()
    except Exception:
        pass
    return _clean(body)

def _extract_location_from_modal(text: str) -> str:
    # Very light heuristic; keeps it stable if they change prose slightly
    # Example in screenshot:
    # "in City Council Chambers at City Hall, 135 N. Animas Street, Trinidad, Colorado, 81082"
    m = re.search(r"in\s+(.+?)(?:\n|$)", text, re.I)
    return _clean(m.group(1)) if m else ""

def collect(months_ahead: int = 3):
    out = []
    today = _yyyy_mm_dd_today_mt()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        # Visit month pages and gather candidate events from grid
        for yy, mm in _mk_months(today.replace(day=1), months_ahead):
            url = MONTH_URL.format(m=mm, y=yy)
            print(f"INFO: [trinidad] fetching month: {url}")
            page.goto(url, wait_until="domcontentloaded")

            # The events in month view are anchors:
            # <a class="fc-day-grid-event ..."><div class="fc-content"><span class="fc-time">6p</span><span class="fc-title">City Council Regular Meeting</span></div></a>
            events = page.locator("a.fc-day-grid-event")
            n = events.count()

            for i in range(n):
                el = events.nth(i)
                title = _clean(el.locator(".fc-title").inner_text())
                if not _accept_title(title):
                    continue

                # date from the day-cell
                try:
                    d_attr = el.evaluate('el.closest("td[data-date]")?.getAttribute("data-date")')
                except Exception:
                    d_attr = None
                try:
                    d_val = datetime.strptime(d_attr, "%Y-%m-%d").date() if d_attr else None
                except Exception:
                    d_val = None

                # time from the mini label
                time_str = _clean(el.locator(".fc-time").inner_text())
                hh, mm_ = _parse_fc_time(time_str)

                # detail / modal
                href = el.get_attribute("href") or ""
                detail_url = urljoin(BASE, href)

                agenda_text = ""
                location = ""
                if "&id=" in detail_url:
                    # open on a separate tab to not lose month listing
                    p2 = context.new_page()
                    p2.goto(detail_url, wait_until="domcontentloaded")
                    agenda_text = _grab_modal(p2)
                    if not location:
                        location = _extract_location_from_modal(agenda_text)
                    p2.close()

                # build record (guard: must have a date)
                if d_val:
                    start_iso = _as_iso_mt(d_val, hh, mm_)
                    date_str = (
                        datetime.fromisoformat(start_iso)
                        .astimezone(MT_TZ)
                        .strftime("%A, %B %-d, %Y")
                    )
                    mtg = {
                        "city": "Trinidad",
                        "source": "Trinidad, CO",
                        "title": "City Council Regular Meeting",
                        "start_dt": start_iso,
                        "end_dt": None,  # modal shows 6:00–7:00 PM sometimes; we can add later if desired
                        "date_str": date_str,
                        "location": location,
                        "agenda_text": agenda_text,
                        "agenda_bullets": [],  # summarizer will handle bullets if agenda_text present
                        "source_url_detail_url": detail_url,
                        "event_id__": re.search(r"[?&]id=(\d+)", detail_url).group(1) if "&id=" in detail_url else "",
                    }
                    out.append(mtg)

        context.close()
        browser.close()

    # final safety: only from today forward
    final = []
    for m in out:
        try:
            d = datetime.fromisoformat(m["start_dt"]).astimezone(MT_TZ).date()
        except Exception:
            continue
        if d >= today:
            final.append(m)
    print(f"INFO: [trinidad] candidates seen: {len(out)}; accepted {len(final)} City Council Regular meeting(s)")
    return final

# --- Adapter for your pipeline (keep this near the bottom) ---
__all__ = ["parse_trinidad", "collect"]

def parse_trinidad(months_ahead: int = 3):
    """
    Adapter for scraper.main: return a list[dict] of Trinidad meetings
    from today forward. 'months_ahead' is kept for parity with other parsers.
    """
    return collect(months_ahead=months_ahead)

if __name__ == "__main__":
    import os
    months = int(os.getenv("TRINIDAD_MONTHS_AHEAD", "3"))
    print(json.dumps(parse_trinidad(months_ahead=months), indent=2, ensure_ascii=False))
