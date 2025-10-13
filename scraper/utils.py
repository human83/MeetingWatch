# scraper/utils.py
from __future__ import annotations
from datetime import datetime
from typing import Dict, List, Optional
import re
import requests
import pytz

MT_TZ = pytz.timezone("America/Denver")

def now_mt() -> datetime:
    return datetime.now(MT_TZ)

def to_mt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return MT_TZ.localize(dt)
    return dt.astimezone(MT_TZ)

def is_future(dt: datetime) -> bool:
    return dt.date() >= now_mt().date()

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def make_meeting(
    city_or_body: str,
    meeting_type: str,
    date: str,  # YYYY-MM-DD
    start_time_local: str,
    status: str,
    location: Optional[str],
    agenda_url: Optional[str],
    agenda_summary,  # list[str] or str
    source: str,
) -> Dict:
    return {
        "city_or_body": city_or_body,
        "meeting_type": meeting_type,
        "date": date,
        "start_time_local": start_time_local,
        "status": status,
        "location": location,
        "agenda_url": agenda_url,
        "agenda_summary": agenda_summary,
        "source": source,
    }

def summarize_pdf_if_any(url: str) -> List[str]:
    """
    Best-effort: if URL is a PDF, extract some text and return up to ~10 short bullets.
    Works without an API key; if you later want LLM summaries, we can plug that in here.
    """
    try:
        r = requests.get(url, timeout=30)
        ct = (r.headers.get("content-type") or "").lower()
        if "pdf" not in ct and not url.lower().endswith(".pdf"):
            return []

        # Lazy import so non-PDF paths don't need pdfminer
        from pdfminer.high_level import extract_text
        import io

        text = extract_text(io.BytesIO(r.content)) or ""
        text = clean_text(text)
        if not text:
            return []

        bullets: List[str] = []
        for line in text.split("\n"):
            line = clean_text(line)
            if not line:
                continue
            bullets.append(line[:220])
            if len(bullets) >= 10:
                break
        return bullets
    except Exception:
        return []
