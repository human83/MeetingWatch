
from __future__ import annotations

import re
from datetime import datetime, date
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .utils import make_meeting, summarize_pdf_if_any

CITY_NAME = "El Paso County"
PROVIDER = "AgendaSuite"
BASE = "https://www.agendasuite.org/iip/elpaso"
UA = {"User-Agent": "MeetingWatch/1.0 (+https://github.com/human83/MeetingWatch)"}

# Regex examples seen on the homepage list, e.g.:
# "10/28/2025 at 9:00 AM for Board of County Commissioners"
DT_RE = re.compile(
    r"(?P<mdy>\d{1,2}/\d{1,2}/\d{4})\s+at\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)",
    re.I,
)

# We only want "Board of County Commissioners" (exclude work sessions, study sessions, etc.)
ALLOW_TITLE_RE = re.compile(r"\bBoard of County Commissioners\b", re.I)
BLOCK_TITLE_RE = re.compile(r"\bWork\s*Session|Study\s*Session|Workshop|Retreat\b", re.I)


def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r


def _today_iso_denver() -> str:
    try:
        from zoneinfo import ZoneInfo  # py3.9+
        return datetime.now(ZoneInfo("America/Denver")).date().isoformat()
    except Exception:
        return date.today().isoformat()


def _parse_list_datetime(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Parse the "MM/DD/YYYY at HH:MM AM/PM" pattern from homepage list.
    Return (YYYY-MM-DD, time_str) where time_str is e.g., '9:00 AM'.
    """
    m = DT_RE.search(text or "")
    if not m:
        return None, None
    mdy = m.group("mdy")
    tm = m.group("time").upper().replace("  ", " ").strip()
    try:
        mm, dd, yyyy = [int(x) for x in mdy.split("/")]
        return f"{yyyy:04d}-{mm:02d}-{dd:02d}", tm
    except Exception:
        return None, tm


def _text(n) -> str:
    return re.sub(r"\s+", " ", (getattr(n, "get_text", lambda **_: str(n))() or "").strip())


def _find_location(soup: BeautifulSoup) -> Optional[str]:
    # Look for "Held at: XYZ"
    text = _text(soup)
    m = re.search(r"Held at:\s*([^\\n\\r]+)", text, re.I)
    if m:
        loc = m.group(1).strip(" :-")
        return loc[:200]
    return None


def _find_agenda_href(soup: BeautifulSoup) -> Optional[str]:
    # Priority order: explicit "Agenda" link, then any /file/getfile/<id> link
    # AgendaSuite often renders as: <a aria-label="Agenda" href="/iip/elpaso/file/getfile/50721">...</a>
    # or a table row with text "Agenda" and a PDF icon in the Download column.
    # 1) aria/label/text contains "Agenda"
    for a in soup.find_all("a"):
        label = (a.get("aria-label") or "") + " " + _text(a)
        if re.search(r"\bagenda\b", label, re.I):
            href = a.get("href") or ""
            if "/file/getfile/" in href:
                return urljoin(BASE, href)

    # 2) attachments table rows
    for tr in soup.select("table tr"):
        row_text = _text(tr)
        if re.search(r"\bagenda\b", row_text, re.I):
            a = tr.find("a", href=True)
            if a and "/file/getfile/" in a["href"]:
                return urljoin(BASE, a["href"])

    # 3) any getfile link as a fallback
    a = soup.find("a", href=re.compile(r"/file/getfile/"))
    if a:
        return urljoin(BASE, a.get("href") or "")

    return None


def _meeting_title_from_detail(soup: BeautifulSoup) -> Optional[str]:
    # Try to use the big heading line that contains "Board of County Commissioners"
    # (and avoid "Work Session" variants).
    texts = []
    for tag in soup.find_all(["h1", "h2", "h3", "div", "span"]):
        t = _text(tag)
        if ALLOW_TITLE_RE.search(t):
            texts.append(t)
    if texts:
        # Prefer the shortest that still contains the phrase (usually "Board of County Commissioners" or "... Meeting")
        texts.sort(key=len)
        t = texts[0]
        # Drop 'Work Session' etc if present.
        t = BLOCK_TITLE_RE.sub("", t).strip(" -—:")
        return t[:150]

    # Fallback
    return "Board of County Commissioners"


def _extract_detail_info(detail_url: str) -> Dict[str, Optional[str]]:
    r = _get(detail_url)
    soup = BeautifulSoup(r.text, "html.parser")

    agenda_url = _find_agenda_href(soup)
    location = _find_location(soup)
    title = _meeting_title_from_detail(soup)

    return {
        "agenda_url": agenda_url,
        "location": location,
        "title": title,
    }


def _discover_from_homepage() -> List[Dict]:
    r = _get(BASE)
    soup = BeautifulSoup(r.text, "html.parser")

    items: List[Dict] = []
    # The "Upcoming meetings" box appears in a column with class "nextmeetings"
    # Each li contains an <a href="/iip/elpaso/meeting/details/<id>">MM/DD/YYYY at HH:MM AM/PM for Board of County Commissioners</a>
    for a in soup.select("div.nextmeetings a[href]"):
        t = _text(a)
        href = a.get("href") or ""
        if not href:
            continue

        # Keep only BOCC regular meetings; exclude "Work Session" etc.
        if not ALLOW_TITLE_RE.search(t):
            continue
        if BLOCK_TITLE_RE.search(t):
            continue

        iso, time_str = _parse_list_datetime(t)
        if not iso:
            # If the homepage text isn't in the expected format, skip.
            continue

        # Only current day & future
        if iso < _today_iso_denver():
            continue

        detail = urljoin(BASE, href)
        meeting = make_meeting(
            city_or_body=CITY_NAME,
            meeting_type="Board of County Commissioners",  # will refine after detail fetch
            date=iso,
            start_time_local=time_str or "",
            status="Scheduled",
            location=None,
            agenda_url=None,
            agenda_summary=[],
            source=BASE,
        )
        meeting["provider"] = PROVIDER
        meeting["url"] = detail
        items.append(meeting)

    return items


def parse_epc() -> List[Dict]:
    items = _discover_from_homepage()
    accepted: List[Dict] = []

    for m in items:
        try:
            info = _extract_detail_info(m["url"])
            # Update title if we have a better one and still not a work session.
            t = info.get("title") or m.get("meeting_type") or ""
            if BLOCK_TITLE_RE.search(t or ""):
                # Safety: drop if detail page shows it's a work session.
                continue

            m["meeting_type"] = t or "Board of County Commissioners"
            if info.get("location"):
                m["location"] = info["location"]
            if info.get("agenda_url"):
                m["agenda_url"] = info["agenda_url"]
                summary = summarize_pdf_if_any(m["agenda_url"])
                if summary:
                    m["agenda_summary"] = summary


            # Provide a consistent Mountain Time zone for downstream rendering if your pipeline uses it.
            m["tz"] = "America/Denver"

            accepted.append(m)
        except Exception:
            # Skip malformed entries silently; upstream logs will show traceback if needed.
            continue

    with_pdf = sum(1 for x in accepted if x.get("agenda_url"))
    print(f"[epc] Visited 1 entry url; accepted {len(accepted)} BOCC item(s); with agenda: {with_pdf}")

    return accepted


def parse_bocc() -> List[Dict]:
    return parse_epc()


def parse() -> List[Dict]:
    return parse_epc()


if __name__ == "__main__":
    for it in parse_epc():
        print(" -", it.get("date"), it.get("time"), it.get("meeting_type"), "->", it.get("url"))
