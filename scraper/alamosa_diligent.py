# scraper/alamosa_diligent.py
from __future__ import annotations

import os
from datetime import datetime, date
from zoneinfo import ZoneInfo
import re
from typing import List, Dict, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, Page

from .utils import make_meeting, summarize_pdf_if_any

# Change the main URL to a specific, known-failing detail page for diagnostics.
PORTAL_URL = "https://cityofalamosa.community.diligentoneplatform.com/Portal/MeetingInformation.aspx?Org=Cal&id=124"
ALAMOSA_TZ = "America/Denver"
WANTED_TYPES = ("CITY COUNCIL REGULAR MEETING", "CITY COUNCIL SPECIAL MEETING")


def parse_alamosa() -> List[Dict]:
    """
    DIAGNOSTIC MODE 2: This function will navigate to a specific meeting
    detail page, wait for it to load, and then print the entire rendered
    HTML content to the log for analysis.
    """
    print(f"[alamosa] starting; url: {PORTAL_URL}")
    print("[alamosa] ##### RUNNING IN DIAGNOSTIC MODE 2 (DETAIL PAGE) #####")
    
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(30000)

        try:
            print(f"[alamosa] Navigating to detail page: {PORTAL_URL}")
            page.goto(PORTAL_URL, wait_until="networkidle")

            print("[alamosa] Page loaded. Dumping HTML content for detail page...")
            html_content = page.content()
            
            print("\n\n" + "="*20 + " ALAMOSA DETAIL PAGE HTML START " + "="*20 + "\n\n")
            print(html_content)
            print("\n\n" + "="*20 + " ALAMOSA DETAIL PAGE HTML END " + "="*20 + "\n\n")

        except Exception as e:
            print(f"[alamosa] A critical error occurred during diagnostic run: {e}")

        finally:
            if browser.is_connected():
                browser.close()

    print("[alamosa] Diagnostic run complete. No items will be produced.")
    return []
