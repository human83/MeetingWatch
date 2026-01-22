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


def parse_alamosa() -> List[Dict]:
    """
    DIAGNOSTIC MODE: This function will navigate to the portal, wait for the
    page to load, and then print the entire rendered HTML content to the log.
    This is to help identify the correct selectors for the meeting data.
    """
    print(f"[alamosa] starting; url: {PORTAL_URL}")
    print("[alamosa] ##### RUNNING IN DIAGNOSTIC MODE #####")
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(30000)

        try:
            print(f"[alamosa] Navigating to {PORTAL_URL}")
            page.goto(PORTAL_URL, wait_until="networkidle")

            print("[alamosa] Page loaded. Dumping HTML content...")
            html_content = page.content()
            
            print("\n\n" + "="*20 + " ALAMOSA RENDERED HTML START " + "="*20 + "\n\n")
            print(html_content)
            print("\n\n" + "="*20 + " ALAMOSA RENDERED HTML END " + "="*20 + "\n\n")

        except Exception as e:
            print(f"[alamosa] A critical error occurred during diagnostic run: {e}")

        finally:
            if browser.is_connected():
                browser.close()

    print("[alamosa] Diagnostic run complete. No items will be produced.")
    return []
