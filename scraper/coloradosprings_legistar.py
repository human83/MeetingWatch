import re, requests
from bs4 import BeautifulSoup
from datetime import datetime
from .utils import make_meeting, clean_text, to_mt, is_future, MT_TZ
from .summarize import llm_summarize
from .pdf_utils import extract_pdf_text
import tempfile, os

CAL_URL = "https://coloradosprings.legistar.com/Calendar.aspx"

def parse_legistar():
    out = []
    r = requests.get(CAL_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # Rows in table with id 'ctl00_ContentPlaceHolder1_gridCalendar_ctl00'
    rows = soup.select("table#ctl00_ContentPlaceHolder1_gridCalendar_ctl00 tr")
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < 6: 
            continue
        # Date, Time, Location, Meeting Details, Agenda, Minutes
        date_txt = clean_text(tds[0].get_text())
        time_txt = clean_text(tds[1].get_text())
        loc_txt = clean_text(tds[2].get_text())
        title_txt = clean_text(tds[3].get_text())
        # Filter for Council Meetings & Work Sessions
        if not re.search(r"(Council|City Council)", title_txt, re.I):
            continue
        if not re.search(r"(Work Session|Regular|Meeting)", title_txt, re.I):
            continue
        # Build datetime
        try:
            dt = datetime.strptime(f"{date_txt} {time_txt}", "%m/%d/%Y %I:%M %p")
            dt = to_mt(dt)
        except Exception:
            continue
        if not is_future(dt): 
            continue
        # Agenda link
        agenda_url = None
        status = "Agenda not yet posted"
        ag_a = tds[4].find("a")
        if ag_a and ag_a.get("href"):
            agenda_url = ag_a.get("href")
            if not agenda_url.lower().startswith("http"):
                agenda_url = "https://coloradosprings.legistar.com/" + agenda_url.lstrip("/")
            status = "Agenda posted"
        # meeting type heuristic
        mtype = "Work Session" if re.search(r"Work Session", title_txt, re.I) else "City Council Meeting"
        # summarize if agenda is a PDF
        summary = "Agenda not posted yet"
        if agenda_url and agenda_url.lower().endswith(".pdf"):
            try:
                pr = requests.get(agenda_url, timeout=30)
                pr.raise_for_status()
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
            city_or_body="Colorado Springs City Council",
            meeting_type=mtype,
            date=dt.strftime("%Y-%m-%d"),
            start_time_local=dt.strftime("%-I:%M %p"),
            location=loc_txt or None,
            agenda_url=agenda_url,
            status=status,
            agenda_summary=summary,
            source_url=CAL_URL
        ))
    return out
