# scraper/alamosa_diligent.py
from __future__ import annotations

import os
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import re
from typing import List, Dict, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page

from .utils import make_meeting, summarize_pdf_if_any

PORTAL_URL = "https://cityofalamosa.community.diligentoneplatform.com/Portal/MeetingSchedule.aspx"
ALAMOSA_TZ = "America/Denver"
WANTED_TYPES = ("CITY COUNCIL REGULAR MEETING", "CITY COUNCIL SPECIAL MEETING")


def _today_denver() -> date:
    return datetime.now(ZoneInfo(ALAMOSA_TZ)).date()


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _parse_meeting_detail_page(page: Page, meeting_url: str) -> Optional[Dict]:
    """
    Parses a specific meeting detail page (MeetingInformation.aspx) to extract
    all information, including the PDF URL for summarization.
    """
    print(f"[alamosa] Parsing detail page: {meeting_url}")
    try:
        page.goto(meeting_url, wait_until="domcontentloaded")
    except Exception as e:
        print(f"[alamosa] Failed to navigate to detail page {meeting_url}: {e}")
        return None

    header_el = page.locator("h1.ContentLg").first
    if not header_el.is_visible():
        print("[alamosa] Could not find header element on detail page.")
        return None
    
    header_text = _norm_space(header_el.inner_text()).upper()

    mtg_type = None
    for t in WANTED_TYPES:
        if t in header_text:
            mtg_type = t.title()
            break
    if not mtg_type:
        print(f"[alamosa] Skipping page, type not wanted: {header_text}")
        return None

    date_match = re.search(r"-\s+([A-Z]{3}\s+\d{1,2}\s+\d{4})$", header_text)
    if not date_match:
        print(f"[alamosa] Could not parse date from header: {header_text}")
        return None
    
    try:
        date_obj = datetime.strptime(date_match.group(1), "%b %d %Y").date()
    except ValueError:
        print(f"[alamosa] Could not parse date string from header: '{date_match.group(1)}'")
        return None

    if date_obj < _today_denver():
        print(f"[alamosa] Skipping past meeting from {date_obj.isoformat()}")
        return None

    time_el = page.locator("label:has-text('Time:') + div").first
    time_str = _norm_space(time_el.inner_text()) if time_el.is_visible() else None

    loc_el = page.locator("label:has-text('Location:') + div").first
    location_str = _norm_space(loc_el.inner_text()) if loc_el.is_visible() else None

    pdf_url, summary = None, []
    pdf_link_el = page.locator("a#document-cover-pdf[href]").first
    if pdf_link_el.is_visible():
        pdf_href = pdf_link_el.get_attribute("href")
        if pdf_href:
            pdf_url = page.urljoin(pdf_href)
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


def parse_alamosa() -> List[Dict]:
    """
    Entry point for Alamosa scraper. Finds links in "Today's" and "Upcoming"
    sections on the main schedule page and scrapes each one.
    """
    print(f"[alamosa] starting; url: {PORTAL_URL}")
    items: List[Dict] = []
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(30000)

        try:
            print(f"[alamosa] Navigating to {PORTAL_URL}")
            page.goto(PORTAL_URL, wait_until="networkidle")

            link_selector = "div.MeetingUpcoming a[href*='MeetingInformation.aspx']"
            page.wait_for_selector(link_selector, timeout=20000)
            
            meeting_links = page.locator(link_selector).all()
            print(f"[alamosa] Found {len(meeting_links)} potential meeting links.")
            
            detail_urls = list(dict.fromkeys(
                page.urljoin(link.get_attribute('href')) for link in meeting_links
            ))
            
            print(f"[alamosa] Found {len(detail_urls)} unique detail URLs to scrape.")

            for url in detail_urls:
                meeting_item = _parse_meeting_detail_page(page, url)
                if meeting_item:
                    items.append(meeting_item)

        except Exception as e:
            print(f"[alamosa] A critical error occurred during scraping: {e}")
            try:
                page.screenshot(path="alamosa_error_screenshot.png")
                print("[alamosa] Debugging screenshot saved to alamosa_error_screenshot.png")
            except Exception as se:
                print(f"[alamosa] Could not save screenshot: {se}")

        finally:
            if browser.is_connected():
                browser.close()

    sorted_items = sorted(items, key=lambda d: (d.get("date") or "9999-12-31", d.get("meeting_type") or ""))
    print(f"[alamosa] produced {len(sorted_items)} item(s)")
    return sorted_items
