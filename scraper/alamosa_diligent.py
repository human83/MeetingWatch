# scraper/alamosa_diligent.py
from __future__ import annotations

import os
from datetime import datetime, date
from zoneinfo import ZoneInfo
import re
from typing import List, Dict, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, Page, BrowserContext

from .utils import make_meeting, summarize_pdf_if_any

PORTAL_URL = "https://cityofalamosa.community.diligentoneplatform.com/Portal/MeetingSchedule.aspx"
ALAMOSA_TZ = "America/Denver"
WANTED_TYPES = ("CITY COUNCIL REGULAR MEETING", "CITY COUNCIL SPECIAL MEETING", "CITY COUNCIL WORK SESSION")


def _today_denver() -> date:
    return datetime.now(ZoneInfo(ALAMOSA_TZ)).date()


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_meeting_detail_page(context: BrowserContext, meeting_url: str) -> Optional[Dict]:
    """
    Parses a specific meeting detail page in a new, isolated page (tab).
    """
    page = None
    try:
        page = context.new_page()
        print(f"[alamosa] Parsing detail page: {meeting_url}")
        page.goto(meeting_url, wait_until="networkidle")

        header_el = page.locator("h2#ctl00_MainContent_MeetingTitle").first
        # Explicitly wait for the header to be visible before reading it
        header_el.wait_for(timeout=10000)
        
        header_text = _norm_space(header_el.inner_text()).upper()

        mtg_type = None
        for t in WANTED_TYPES:
            if t in header_text:
                mtg_type = t.title()
                break
        if not mtg_type:
            print(f"[alamosa] Skipping: Meeting type '{header_text}' not in WANTED_TYPES.")
            return None

        date_match = re.search(r"-\s+([A-Z]{3}\s+\d{1,2}\s+\d{4})$", header_text)
        if not date_match:
            print(f"[alamosa] Skipping: Could not parse date from header: {header_text}")
            return None
        
        try:
            date_obj = datetime.strptime(date_match.group(1), "%b %d %Y").date()
        except ValueError:
            print(f"[alamosa] Skipping: Could not parse date string from header: '{date_match.group(1)}'")
            return None

        if date_obj < _today_denver():
            print(f"[alamosa] Skipping: Past meeting from {date_obj.isoformat()}")
            return None

        time_el = page.locator("span#meeting-time").first
        time_str = _norm_space(time_el.inner_text()) if time_el.is_visible() else None

        loc_el = page.locator("span#meeting-location").first
        location_str = _norm_space(loc_el.inner_text()) if loc_el.is_visible() else None

        pdf_url, summary = None, []
        pdf_link_el = page.locator("a#document-cover-pdf[href]").first
        if pdf_link_el.is_visible():
            pdf_href = pdf_link_el.get_attribute("href")
            if pdf_href:
                pdf_url = urljoin(page.url, pdf_href)
                print(f"[alamosa] Found agenda PDF: {pdf_url}")
                summary = summarize_pdf_if_any(pdf_url) or []
                if summary:
                    print(f"[alamosa] Successfully generated {len(summary)} summary bullets.")

        return make_meeting(
            city_or_body="Alamosa",
            meeting_type=mtg_type,
            date=date_obj.isoformat(),
            start_time_local=time_str,
            status="Scheduled",
            location=location_str,
            agenda_url=pdf_url,
            agenda_summary=summary,
            source=meeting_url,
        )
    except Exception as e:
        print(f"[alamosa] Error parsing detail page {meeting_url}: {e}")
        return None
    finally:
        if page:
            page.close()


def parse_alamosa() -> List[Dict]:
    """
    Entry point for Alamosa scraper. Finds links on the main schedule page,
    then scrapes each one in a new page context.
    """
    print(f"[alamosa] starting; url: {PORTAL_URL}")
    items: List[Dict] = []
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30000)

        try:
            print(f"[alamosa] Navigating to {PORTAL_URL}")
            page.goto(PORTAL_URL, wait_until="networkidle")

            link_selector = "#ctl00_UpcomingMeetings a.list-link, #ctl00_RecentMeetings a.list-link, #ctl00_TodaysMeetings a.list-link"
            page.wait_for_selector("#ctl00_RightSidebar", timeout=20000)
            
            meeting_links = page.locator(link_selector).all()
            print(f"[alamosa] Found {len(meeting_links)} potential meeting links.")
            
            detail_urls = list(dict.fromkeys(
                urljoin(page.url, link.get_attribute('href')) for link in meeting_links if link.get_attribute('href')
            ))
            
            print(f"[alamosa] Found {len(detail_urls)} unique detail URLs to scrape.")

            for url in detail_urls:
                # Pass the browser context to the parsing function
                meeting_item = _parse_meeting_detail_page(context, url)
                if meeting_item:
                    items.append(meeting_item)

        except Exception as e:
            print(f"[alamosa] A critical error occurred during main page scraping: {e}")
        finally:
            if browser.is_connected():
                browser.close()

    # Deduplicate by source URL
    final_items = list({item['source']: item for item in items}.values())
    sorted_items = sorted(final_items, key=lambda d: (d.get("date") or "9999-12-31", d.get("meeting_type") or ""))
    
    print(f"[alamosa] produced {len(sorted_items)} item(s)")
    return sorted_items
