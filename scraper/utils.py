# scraper/utils.py (v3.3 — single-topic detector; broader drops; AI-first; full-text rules; merge; debug hooks)
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
import io
import os
import re
import json
import hashlib
import logging

from pathlib import Path
import requests
from requests.exceptions import RequestException, Timeout
import pytz

MT_TZ = pytz.timezone("America/Denver")
_LOG = logging.getLogger(__name__)

def now_mt() -> datetime:
    return datetime.now(MT_TZ)

def to_mt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return MT_TZ.localize(dt)
    return dt.astimezone(MT_TZ)

def is_future(dt: datetime) -> bool:
    return to_mt(dt).date() >= now_mt().date()

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def make_meeting(
    city_or_body: str,
    meeting_type: str,
    date: str,
    start_time_local: str,
    status: str,
    location: Optional[str],
    agenda_url: Optional[str],
    agenda_summary,
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

# ------------------------------
# Tunables via environment
# ------------------------------
_DEFAULT_MAX_PAGES = int(os.getenv("PDF_SUMMARY_MAX_PAGES", "25"))
_DEFAULT_MAX_CHARS = int(os.getenv("PDF_SUMMARY_MAX_CHARS", "72000"))
_DEFAULT_HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SEC", "32"))
_DEFAULT_REQ_TIMEOUT = float(os.getenv("PDF_HTTP_TIMEOUT_SEC", str(_DEFAULT_HTTP_TIMEOUT)))
_DEFAULT_MODEL = os.getenv("SUMMARIZER_MODEL", "gpt-4o-mini")
_MAX_BULLETS = int(os.getenv("PDF_SUMMARY_MAX_BULLETS", "12"))
_DISABLE_SUMMARIZER = os.getenv("SUMMARIZER_DISABLE", "").strip() == "1"
_SUMMARIZER_STRICT = os.getenv("SUMMARIZER_STRICT", "").strip() == "1"
_DEBUG = os.getenv("PDF_SUMMARY_DEBUG", "").strip() == "1"

_CACHE_DIR = Path(os.getenv("SUMMARY_CACHE_DIR", "data/cache/agenda_summaries"))
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ------------------------------
# Filters / Signals
# ------------------------------
_DROP_PATTERNS = [
    r"^city of colorado springs\b",
    r"\bcouncil work session\b",
    r"\bregular meeting agenda\b",
    # Watching / streaming / channels
    r"\bHow to Watch\b",
    r"\bHow to Watch the Meeting\b",
    r"\bFacebook Live\b",
    r"\bSPRINGS\s*TV\b",
    r"\bComcast\s*Channel\b",
    r"\bChannel\s*\d+\b",
    r"\bStratus\s*IQ\s*Channel\b",
    r"\bStreaming\b|\bStream\b",
    r"channel\s*18|livestream|televised|broadcast",
    # Accessibility boilerplate
    r"americans? with disabilities act|ADA\b|auxiliary aid|48 hours before",
    # Procedural items
    r"\bcall to order\b|\broll call\b|\bpledge of allegiance\b|\bapproval of (the )?minutes\b|\badjourn",
    r"\bmeeting minutes\b",
    r"\bfirst presentation\b|\bsecond presentation\b",
    # Attachments / presenters / staff
    r"^\s*Attachments?\s*:.*$",
    r"^\s*Presenter\s*:.*$",
    r"^\s*Staff\s*Presentation\b.*$",
    r"^\s*Staff\s*Report\b.*$",
    # Misc boilerplate
    r"documents created by third parties may not meet all accessibilit",
    r"how to watch the meeting|how to comment on agenda items",
    r"^page\s*\d+\b",
    r"^printed on\b",
    r"^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$",
    r"^[A-Z]?\d{2,}-\d{2,}$",
    r"^est\.?\s*time\b",
    r"^presenter\b|^related files\b",
    r"\bexhibit [A-Z]\b",
    r"\blocation map\b",
    r"\battachment\b",
    r"\.(pdf|docx|xlsx)\b",
    r"^[0-9]+_[A-Z]{2}\b",
    r"^[0-9A-Z. -]+:$",
    # Generic section headings we never want as bullets
    r"^\s*Items Under Study\s*$",
    r"^\s*Public Comment\s*$",
]
_DROP_RE = re.compile("|".join(_DROP_PATTERNS), re.IGNORECASE)

# broaden to catch “hearing date”, “set public hearing”, etc.
_POSITIVE_SIGNALS = re.compile(
    r"\b(ordinance|resolution|budget|appropriation|rate case|rate changes|zoning|rezoning|variance|annexation|"
    r"service plan|metropolitan district|metropolitan\s+district|md\b|acquisition of real property|acquire real property|"
    r"plat|subdivision|permit|license|fee|rate|tax|mill levy|bond|grant|contract|agreement|rfp|rfq|procurement|purchase|"
    r"public hearing|hearing date|set.*public hearing|set the public hearing|"
    r"utility|utilities|water|sewer|storm|airport|transit|transportation|street|road|bridge|housing|affordable housing)\b",
    re.IGNORECASE,
)

# ------------------------------
# Single-topic detector (work/study sessions → 1 bullet)
# ------------------------------
_SINGLE_TOPIC_HINTS = re.compile(
    r"(work session|study session|retreat|budget work session|planning workshop)",
    re.IGNORECASE,
)
_SINGLE_TOPIC_HEADINGS = re.compile(
    r"^\s*(items under study|new business|discussion|agenda|call to order|adjourn|public comment)\s*$",
    re.IGNORECASE,
)

def _is_single_topic_agenda(text: str) -> Optional[str]:
    """Return a single topical title if this looks like a single-topic agenda, else None."""
    if not _SINGLE_TOPIC_HINTS.search(text):
        return None
    lines = [clean_text(ln) for ln in text.splitlines()]
    candidates: List[str] = []
    for ln in lines:
        if not ln or _DROP_RE.search(ln) or _SINGLE_TOPIC_HEADINGS.search(ln):
            continue
        # Prefer concise topical lines without obvious boilerplate
        if 3 <= len(ln.split()) <= 20 and not re.search(
            r"\b(roll call|adjourn|call to order|channel|stream|presenter|attachments?)\b",
            ln, re.IGNORECASE
        ):
            candidates.append(ln)

    # Prefer lines containing strong topical keywords
    for kw in ("budget", "zoning", "ordinance", "rate case", "hearing"):
        for ln in candidates:
            if re.search(kw, ln, re.IGNORECASE):
                return ln
    return candidates[0] if candidates else None

# ------------------------------
# PDF helpers
# ------------------------------
def _looks_like_pdf_url(url: str) -> bool:
    return bool(re.search(r"\.pdf($|\?)", url, flags=re.IGNORECASE))

def _download_pdf_bytes(url: str, *, timeout: float) -> Optional[bytes]:
    try:
        try:
            h = requests.head(url, allow_redirects=True, timeout=timeout)
            ctype = (h.headers.get("Content-Type") or "").lower()
            if "application/pdf" not in ctype and not _looks_like_pdf_url(url):
                return None
        except RequestException:
            pass
        r = requests.get(url, stream=True, timeout=timeout)
        r.raise_for_status()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "application/pdf" not in ctype and not _looks_like_pdf_url(url):
            return None
        return r.content
    except (RequestException, Timeout):
        return None

def _extract_first_pages_text(pdf_bytes: bytes, *, max_pages: int) -> Optional[str]:
    """Extract text from the first max_pages pages. Caller chooses max_pages via env."""
    try:
        from pdfminer.high_level import extract_text
    except Exception:
        return None
    try:
        with io.BytesIO(pdf_bytes) as fh:
            txt = extract_text(fh, page_numbers=range(max_pages)) or ""
        txt = re.sub(r"[ \t]+\n", "\n", txt)
        txt = re.sub(r"\n{3,}", "\n\n", txt)
        return txt.strip()
    except Exception:
        return None

# ------------------------------
# LLM summary
# ------------------------------
def _openai_bullets(text: str, *, model: str) -> Optional[List[str]]:
    try:
        from openai import OpenAI
    except Exception:
        return None

    client = OpenAI()
    t = text
    max_chars = _DEFAULT_MAX_CHARS
    if len(t) > max_chars:
        head = t[: int(max_chars * 0.7)]
        tail = t[-int(max_chars * 0.3):]
        t = head + "\n...\n" + tail

    system = (
        "You are a newsroom assistant. Read city-meeting agenda text and extract only the most news-worthy, "
        "actionable items for journalists. Prefer motions, ordinances, spending amounts, rate/fee/tax changes, "
        "annexations/rezones, public hearing dates, contracts/grants, and official actions. "
        "Exclude TV/ADA boilerplate, procedural items (Call to Order, minutes), attachments/exhibits, and generic headers. "
        "If this agenda is a single-topic work/study/retreat, return ONE bullet with that topic only."
    )
    user_json = (
        "Return a JSON array of 6–12 short, self-contained bullets (strings). "
        "AGENDA TEXT BEGIN\n" + t + "\nAGENDA TEXT END\nReturn ONLY JSON."
    )
    user_bullets = (
        "If you cannot produce valid JSON, return 6–12 bullets, one per line, "
        "each prefixed with '- '. AGENDA TEXT BEGIN\n" + t + "\nAGENDA TEXT END\nDo not include any other text."
    )

    # Try JSON first
    try:
        r = client.responses.create(
            model=model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user_json}],
            temperature=0.2,
        )
        raw = (getattr(r, "output_text", "") or "").strip()
        if raw:
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL)
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return [clean_text(str(x)) for x in data if clean_text(str(x))][: _MAX_BULLETS] or None
            except Exception:
                pass
    except Exception:
        pass

    # Fallback: plain lines
    try:
        r = client.responses.create(
            model=model,
            input=[{"role": "system", "content": system}, {"role": "user", "content": user_bullets}],
            temperature=0.2,
        )
        raw = (getattr(r, "output_text", "") or "").strip()
        if raw:
            lines = [re.sub(r"^[-•*]\s*", "", ln.strip()) for ln in raw.splitlines()]
            lines = [clean_text(ln) for ln in lines if clean_text(ln)]
            return lines[: _MAX_BULLETS] or None
    except Exception:
        pass

    # Legacy chat API
    try:
        r = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user_bullets}],
            temperature=0.2,
        )
        raw = (r.choices[0].message.content or "").strip()
        if raw:
            lines = [re.sub(r"^[-•*]\s*", "", ln.strip()) for ln in raw.splitlines()]
            lines = [clean_text(ln) for ln in lines if clean_text(ln)]
            return lines[: _MAX_BULLETS] or None
    except Exception:
        pass

    return None

# ------------------------------
# Rule / Heuristic passes
# ------------------------------
_SECTION_START = re.compile(r"^\s*(\d+(?:\.[A-Z])?\.)\s+(.*)", re.IGNORECASE)

def _legistar_rule_based_bullets(text: str, *, limit: int = 24) -> List[str]:
    """Extract likely decision items from full agenda text (no 'Consent' slicing)."""
    lines = [l.strip() for l in text.splitlines()]
    bullets: List[str] = []
    i, n = 0, len(lines)

    def is_header(s: str) -> bool:
        return bool(_SECTION_START.match(s))

    def is_noise(s: str) -> bool:
        return (not s) or _DROP_RE.search(s) is not None

    while i < n and len(bullets) < limit:
        ln = lines[i]
        if is_noise(ln):
            i += 1
            continue

        m = _SECTION_START.match(ln)
        if m:
            head = re.sub(r"^\s*\d+(?:\.[A-Z])?\.\s*", "", ln).strip()
            chunk_parts: List[str] = []
            j = i + 1
            while j < n:
                nxt = lines[j].strip()
                if not nxt or is_header(nxt):
                    break
                if _DROP_RE.search(nxt):
                    j += 1
                    continue
                chunk_parts.append(nxt)
                j += 1
            candidate = " ".join([head] + chunk_parts).strip()
            candidate = re.sub(r"\s+", " ", candidate)
            if (not head or head.endswith(":")) and chunk_parts:
                candidate = " ".join(chunk_parts)
            if candidate and not _DROP_RE.search(candidate) and (
                _POSITIVE_SIGNALS.search(candidate) or re.search(r"[\d$]", candidate)
            ):
                bullets.append(clean_text(candidate))
            i = j
            continue

        # non-numbered lines that still look substantive
        if _POSITIVE_SIGNALS.search(ln) or re.search(r"[\d$]", ln):
            bullets.append(clean_text(ln))
        i += 1

    # dedupe / trim
    out, seen = [], set()
    for b in bullets:
        k = b.lower()
        if k in seen:
            continue
        if _SECTION_START.match(b) or re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", b):
            continue
        out.append(b[:280])
        seen.add(k)
        if len(out) >= limit:
            break
    return out

def _heuristic_bullets(text: str, *, max_items: int = 36) -> List[str]:
    bullets: List[str] = []
    for raw in text.splitlines():
        line = clean_text(raw)
        if not line or _DROP_RE.search(line):
            continue
        if not (_POSITIVE_SIGNALS.search(line) or re.search(r"[\d$]", line)):
            continue
        if len(line) < 18 and not re.search(r"[\d$]", line):
            continue
        bullets.append(line[:240])
        if len(bullets) >= max_items:
            break
    return bullets

def _post_filter_bullets(bullets: List[str], *, limit: int = 10) -> List[str]:
    out: List[str] = []
    seen = set()
    for b in bullets or []:
        line = clean_text(b)
        if not line:
            continue
        if _DROP_RE.search(line):
            continue
        if re.fullmatch(r"[0-9A-Z.\- ]{3,}", line) and not re.search(r"[\d$]", line):
            continue
        if line.endswith(":"):
            continue
        words = line.split()
        if len(words) < 3 and not re.search(r"[\d$]", line):
            continue
        if len(line) < 25 and not re.search(r"[\d$]", line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
        if len(out) >= limit:
            break
    return out

# ------------------------------
# Summarize (download → extract → single-topic check → LLM → rules → merge → cache)
# ------------------------------
def _cache_path(url: str, *, max_pages: int, model: str) -> Path:
    h = hashlib.sha1(f"{url}|{max_pages}|{model}".encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{h}.json"

def _meta_path(url: str, *, max_pages: int, model: str) -> Path:
    h = hashlib.sha1(f"{url}|{max_pages}|{model}".encode("utf-8")).hexdigest()
    return _CACHE_DIR / f"{h}.meta.json"

def summarize_pdf_if_any(
    url: Optional[str],
    *,
    max_pages: int = _DEFAULT_MAX_PAGES,
    model: str = _DEFAULT_MODEL,
    timeout: float = _DEFAULT_REQ_TIMEOUT,
) -> List[str]:
    if _DISABLE_SUMMARIZER or not url:
        return []

    cache_fp = _cache_path(url, max_pages=max_pages, model=model)
    try:
        if cache_fp.exists():
            data = json.loads(cache_fp.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass

    pdf_bytes = _download_pdf_bytes(url, timeout=timeout)
    if not pdf_bytes:
        return []

    text = _extract_first_pages_text(pdf_bytes, max_pages=max_pages)
    if not text:
        return []

    # 0) Single-topic fast path — return ONE topical bullet
    single = _is_single_topic_agenda(text)
    if single:
        result = [single]
        try:
            cache_fp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if _DEBUG:
                _meta_path(url, max_pages=max_pages, model=model).write_text(
                    json.dumps({"url": url, "max_pages": max_pages, "model": model, "single_topic": True, "merged": 1}, indent=2),
                    encoding="utf-8",
                )
        except Exception:
            pass
        return result

    # 1) LLM pass
    bullets_llm = _openai_bullets(text, model=model) or []

    # 2) Rules/heuristics over FULL text (no early 'Consent Calendar' slice)
    rules_raw = _legistar_rule_based_bullets(text, limit=max(36, _MAX_BULLETS * 3))
    rules_best = _post_filter_bullets(rules_raw, limit=max(24, _MAX_BULLETS * 2))

    # 3) Merge, keeping LLM first, then add any new rule-based items it missed
    merged: List[str] = []
    seen = set()
    for src in (bullets_llm, rules_best):
        for b in src:
            k = clean_text(b).lower()
            if not k or k in seen:
                continue
            merged.append(clean_text(b))
            seen.add(k)
            if len(merged) >= _MAX_BULLETS:
                break
        if len(merged) >= _MAX_BULLETS:
            break

    # 4) If still empty and not strict, try a lightweight heuristic pass
    if not merged and not _SUMMARIZER_STRICT:
        merged = _post_filter_bullets(_heuristic_bullets(text, max_items=36), limit=_MAX_BULLETS)

    # Cache + optional meta for debugging
    try:
        cache_fp.parent.mkdir(parents=True, exist_ok=True)
        cache_fp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        if _DEBUG:
            meta = {
                "url": url,
                "max_pages": max_pages,
                "model": model,
                "single_topic": False,
                "bullets_llm": len(bullets_llm),
                "rules_raw": len(rules_raw),
                "rules_best": len(rules_best),
                "merged": len(merged),
                "max_bullets": _MAX_BULLETS,
            }
            _meta_path(url, max_pages=max_pages, model=model).write_text(
                json.dumps(meta, indent=2), encoding="utf-8"
            )
    except Exception:
        pass

    return merged
