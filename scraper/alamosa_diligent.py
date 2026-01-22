# scraper/alamosa_diligent.py
from __future__ import annotations

import os
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import re
from typing import List, Dict, Optional

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .utils import make_meeting, summarize_pdf_if_any

PORTAL_URL = "https://cityofalamosa.community.diligentoneplatform.com/Portal/MeetingSchedule.aspx"
ALAMOSA_TZ = "America/Denver"
WANTED_TYPES = ("CITY COUNCIL REGULAR MEETING", "CITY COUNCIL SPECIAL MEETING")


def _today_denver() -> date:
    return datetime.now(ZoneInfo(ALAMOSA_TZ)).date()


def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def parse_alamosa() -> List[Dict]:
    """
    Entry point. Uses Playwright to render the MeetingSchedule page, then
    iterates through the meeting list to find upcoming council meetings.
    For each, it navigates to the detail page to find the agenda PDF and summarize it.
    """
    print(f"[alamosa] starting; url: {PORTAL_URL}")
    items: List[Dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(35000) # Increased timeout for multi-page navigation

        try:
            print(f"[alamosa] Navigating to {PORTAL_URL}")
            page.goto(PORTAL_URL, wait_until="domcontentloaded")
            
            # This selector targets the rows in the main meetings grid
            row_selector = "tr.dxgvDataRow"
            page.wait_for_selector(row_selector, timeout=15000)
            meeting_rows = page.locator(row_selector).all()

            print(f"[alamosa] Found {len(meeting_rows)} potential meeting rows.")

            for i, row in enumerate(meeting_rows):
                # Extract text from all cells to check for meeting type
                cells_text = "".join(row.locator("td").all_inner_texts())
                row_text_upper = _norm_space(cells_text).upper()

                mtg_type = None
                for t in WANTED_TYPES:
                    if t in row_text_upper:
                        mtg_type = t.title()
                        break
                
                if not mtg_type:
                    continue

                print(f"[alamosa] Found council meeting in row {i}: {mtg_type}")

                # Date is in the first column
                date_str = _norm_space(row.locator("td").nth(0).inner_text())
                try:
                    date_obj = datetime.strptime(date_str, "%A, %B %d, %Y").date()
                except ValueError:
                    print(f"[alamosa] Could not parse date: '{date_str}'")
                    continue

                # Filter out past meetings
                if date_obj < _today_denver():
                    print(f"[alamosa] Skipping past meeting on {date_obj.isoformat()}")
                    continue

                time_str = _norm_space(row.locator("td").nth(1).inner_text())
                location_str = _norm_space(row.locator("td").nth(2).inner_text())

                # Find the link to the meeting detail page
                detail_link_el = row.locator("a[href*='MeetingDetail.aspx']").first
                if not detail_link_el:
                    print(f"[alamosa] No detail link found for meeting on {date_obj.isoformat()}")
                    continue
                
                detail_url = page.urljoin(detail_link_el.get_attribute("href"))
                print(f"[alamosa] Navigating to detail page: {detail_url}")

                # Visit detail page to get PDF link
                pdf_url = None
                summary = []
                try:
                    detail_page = browser.new_page()
                    detail_page.goto(detail_url, wait_until="domcontentloaded")
                    
                    pdf_link_el = detail_page.locator("a#document-cover-pdf[href]").first
                    if pdf_link_el:
                        pdf_href = pdf_link_el.get_attribute("href")
                        if pdf_href:
                            pdf_url = detail_page.urljoin(pdf_href)
                            print(f"[alamosa] Found agenda PDF: {pdf_url}")
                            # Now that we have a PDF, summarize it
                            summary = summarize_pdf_if_any(pdf_url) or []
                            if summary:
                                print(f"[alamosa] Successfully generated {len(summary)} summary bullets.")
                    else:
                        print(f"[alamosa] No agenda PDF link found on detail page.")
                        
                    detail_page.close()
                except Exception as e:
                    print(f"[alamosa] Error processing detail page {detail_url}: {e}")
                    if "detail_page" in locals() and not detail_page.is_closed():
                        detail_page.close()


                meeting = make_meeting(
                    city_or_body="Alamosa",
                    meeting_type=mtg_type,
                    date=date_obj.isoformat(),
                    start_time_local=time_str if time_str != "N/A" else None,
                    status="Scheduled",
                    location=location_str if location_str != "N/A" else None,
                    agenda_url=pdf_url,
                    agenda_summary=summary,
                    source=detail_url, # Use the specific meeting detail URL as the source
                )
                meeting["tags"] = ["City Council"]
                items.append(meeting)

        except Exception as e:
            print(f"[alamosa] A critical error occurred during scraping: {e}")
        finally:
            if browser.is_connected():
                browser.close()

    items.sort(key=lambda d: (d.get("date") or "9999-12-31", d.get("meeting_type") or ""))

    print(f"[alamosa] produced {len(items)} item(s)")
    return items
