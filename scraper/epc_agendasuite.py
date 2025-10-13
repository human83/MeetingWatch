import re, requests
from bs4 import BeautifulSoup
from datetime import datetime
from .utils import make_meeting, clean_text, to_mt, is_future
from .summarize import llm_summarize
from .pdf_utils import extract_pdf_text
import tempfile, os

BASE = "https://www.agendasuite.org/iip/elpaso"

def parse_bocc():
    out = []
    r = requests.get(BASE, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # Find meeting links in "Meetings" listing
    for a in soup.select("a[href*='/meeting/details/']"):
        href = a.get("href")
        if not href: 
            continue
        url = href if href.startswith("http") else (BASE.rstrip("/") + "/" + href.lstrip("/"))
        title = a.get_text(strip=True)
        if not re.search(r"Board of County Commissioners", title, re.I):
            continue
        # Go into details page
        rd = requests.get(url, timeout=30)
        if rd.status_code != 200:
            continue
        ds = BeautifulSoup(rd.text, "html.parser")
        # Parse date/time (often in a panel)
        dt_txt = ""
        time_node = ds.find(string=re.compile(r"\d{1,2}/\d{1,2}/\d{4}"))
        if time_node:
            dt_txt = time_node.strip()
        # Try a fallback: find an element with class 'meeting-date' etc.
        mdt = None
        for cand in ds.find_all(text=True):
            if re.search(r"\d{1,2}/\d{1,2}/\d{4}", cand):
                mdt = cand.strip()
                break
        # Very loose parse
        try:
            dt = datetime.strptime(re.search(r"\d{1,2}/\d{1,2}/\d{4}", mdt).group(0) + " 9:00 AM", "%m/%d/%Y %I:%M %p")
            dt = to_mt(dt)
        except Exception:
            continue
        if not is_future(dt):
            continue
        # Agenda attachment
        agenda_url = None
        for a2 in ds.select("a[href]"):
            txt = a2.get_text(" ", strip=True)
            if re.search(r"Agenda", txt, re.I):
                agenda_url = a2["href"]
                if not agenda_url.startswith("http"):
                    agenda_url = BASE.rstrip("/") + "/" + agenda_url.lstrip("/")
                break
        status = "Agenda posted" if agenda_url else "Agenda not yet posted"
        summary = "Agenda not posted yet"
        if agenda_url and agenda_url.lower().endswith(".pdf"):
            try:
                pr = requests.get(agenda_url, timeout=30)
                pr.raise_for_status()
                import tempfile, os
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
                    tf.write(pr.content)
                    tf.flush()
                    text = extract_pdf_text(tf.name)
                os.unlink(tf.name)
                bullets = llm_summarize(text, max_bullets=8)
                summary = bullets
            except Exception:
                summary = "Agenda available but could not be summarized"
        elif agenda_url:
            summary = "Agenda available"
        out.append(make_meeting(
            city_or_body="El Paso County — Board of County Commissioners",
            meeting_type="Regular Meeting",
            date=dt.strftime("%Y-%m-%d"),
            start_time_local=dt.strftime("%-I:%M %p"),
            location=None,
            agenda_url=agenda_url,
            status=status,
            agenda_summary=summary,
            source_url=url
        ))
    return out
