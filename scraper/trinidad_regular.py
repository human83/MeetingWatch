# scraper/trinidad_regular.py
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urljoin, quote

import requests
from bs4 import BeautifulSoup

from .utils import make_meeting, MT_TZ, _extract_first_pages_text, _openai_bullets, _DEFAULT_MAX_PAGES, _DEFAULT_MODEL, _DEFAULT_HTTP_TIMEOUT

# --- Constants ---
BASE_URL = "https://www.trinidad.co.gov/government/agendas___minutes/"
MEETING_TYPE = "City Council Regular Meeting"

# --- Logging ---
log = logging.getLogger(__name__)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
    log.addHandler(_h)
log.propagate = False
log.setLevel(logging.INFO)

# --- Scraper ---
def fetch_year_page(year: int) -> BeautifulSoup | None:
    """Fetches the agenda page for a given year."""
    url = f"{BASE_URL}{year}.php"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "html.parser")
    except requests.RequestException as e:
        log.error(f"Failed to fetch {url}: {e}")
        return None

def parse_trinidad() -> list[dict]:
    """
    Scrapes the Trinidad agendas___minutes list page for regular council meetings.
    This replaces the previous calendar-based scraper.
    """
    meetings = []
    today = datetime.now(MT_TZ).date()

    for year in [today.year, today.year + 1]:
        log.info(f"Scraping Trinidad agendas for {year}")
        soup = fetch_year_page(year)
        if not soup:
            continue

        # Find all tables that look like meeting containers
        tables = soup.find_all("table", style=lambda s: s and "width: 100%" in s)

        for table in tables:
            # Extract date and title text
            info_cell = table.find("td", {"valign": "top", "width": "40%"})
            if not info_cell:
                continue
            
            info_text = [s.strip() for s in info_cell.get_text(separator="\n").splitlines() if s.strip()]
            if len(info_text) < 2:
                continue

            date_str, meeting_name = info_text[0], info_text[1]

            # Filter for regular meetings
            if "regular meeting" not in meeting_name.lower():
                continue

            # Parse date
            try:
                meeting_date = datetime.strptime(date_str, "%m/%d/%y").date()
            except ValueError:
                log.warning(f"Could not parse date: {date_str}")
                continue

            # Filter for future meetings
            if meeting_date < today:
                continue

            # Find agenda link
            agenda_link_tag = table.find("a", string=lambda s: s and "agenda" in s.lower())
            if not agenda_link_tag or not agenda_link_tag.has_attr("href"):
                continue

            href = agenda_link_tag["href"]
            
            # Sanitize the href from the website, which may contain typos
            # like extra spaces before the file extension (e.g., "Agenda 1.20.26 .pdf")
            sanitized_href = href.replace(" .pdf", ".pdf")

            # URL-encode the path part of the href to handle spaces
            path_part, *query_part = sanitized_href.split('?', 1)
            safe_path = quote(path_part.strip())
            safe_href = '?'.join([safe_path] + query_part)

            agenda_url = urljoin(BASE_URL, safe_href)
            
            # --- BEGIN CUSTOM PDF FETCH AND SUMMARIZATION FOR TRINIDAD ---
            summary = []
            try:
                # Use a general User-Agent to avoid potential blocks
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.75 Safari/537.36"}
                response = requests.get(agenda_url, timeout=_DEFAULT_HTTP_TIMEOUT, headers=headers)
                response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)

                content_type = response.headers.get("Content-Type", "").lower()
                if "application/pdf" in content_type:
                    pdf_bytes = response.content
                    text = _extract_first_pages_text(pdf_bytes, max_pages=_DEFAULT_MAX_PAGES)
                    if text:
                        summary = _openai_bullets(text, model=_DEFAULT_MODEL) or []
                else:
                    log.warning(f"URL {agenda_url} did not return a PDF (Content-Type: {content_type})")

            except requests.RequestException as e:
                log.error(f"Failed to fetch or process PDF for {agenda_url}: {e}")
            # --- END CUSTOM PDF FETCH AND SUMMARIZATION ---

            # Create meeting object
            meeting = make_meeting(
                city_or_body="Trinidad",
                meeting_type=MEETING_TYPE,
                date=meeting_date.isoformat(),
                start_time_local="6:00 PM",  # Time is not on the page, using a reasonable default
                status="Scheduled",
                location="City Council Chambers, City Hall (Trinidad, CO)",
                agenda_url=agenda_url,
                agenda_summary=summary,
                source=f"{BASE_URL}{year}.php",
            )
            meetings.append(meeting)

    log.info(f"Found {len(meetings)} upcoming regular meetings for Trinidad.")
    return meetings

if __name__ == "__main__":
    import json
    print(json.dumps(parse_trinidad(), indent=2))