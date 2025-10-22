# scraper/summarize.py
from __future__ import annotations

import argparse
import json
import os
import re
import textwrap
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------
# Config via environment
# ---------------------------
MAX_BULLETS = int(os.getenv("PDF_SUMMARY_MAX_BULLETS", "12"))
DEFAULT_MAX_PAGES = int(os.getenv("PDF_SUMMARY_MAX_PAGES", "20"))  # kept for compatibility
DEFAULT_MAX_CHARS = int(os.getenv("PDF_SUMMARY_MAX_CHARS", "50000"))
SUMMARIZER_MODEL = os.getenv("SUMMARIZER_MODEL", "gpt-4o-mini")
DEBUG = os.getenv("PDF_SUMMARY_DEBUG", "0") == "1"

UA = {"User-Agent": "MeetingWatch/1.0 (+https://github.com/human83/MeetingWatch)"}

# ---------------------------
# Utilities
# ---------------------------

def _log(msg: str) -> None:
    print(f"[summarize] {msg}", flush=True)

def _slugify(s: str, length: int = 80) -> str:
    s = re.sub(r"\s+", "-", (s or "").strip().lower())
    s = re.sub(r"[^a-z0-9\-_.]+", "", s)
    return s[:length] or "meeting"

def _looks_like_pdf(resp: requests.Response) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ct:
        return True
    try:
        return resp.content[:5].startswith(b"%PDF-")
    except Exception:
        return False

def _normalize_ws(text: str) -> str:
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\r\n?", "\n", text))

# ---------------------------
# PDF text extraction (prefer project helper if present)
# ---------------------------

def _extract_text_from_pdf_bytes(data: bytes) -> str:
    """
    Try project-local pdf_utils first; else fallback to pdfminer.six.
    """
    # 1) Try local helper
    try:
        from scraper import pdf_utils as pu  # type: ignore
        for name in ("extract_text_from_bytes", "extract_text_from_pdf_bytes", "extract_text_from_pdf"):
            fn = getattr(pu, name, None)
            if callable(fn):
                return fn(data) or ""
    except Exception:
        pass

    # 2) Fallback: pdfminer.six
    try:
        from pdfminer.high_level import extract_text
        return extract_text(BytesIO(data)) or ""
    except Exception as e:
        if DEBUG:
            _log(f"pdfminer failed: {e!r}")
        return ""

# ---------------------------
# LLM summarization
# ---------------------------

def bulletify(text: str, max_bullets: int = 10) -> List[str]:
    """
    Very simple fallback bullet generator for when LLM isn't available.
    """
    lines = [ln.strip(" •-*–\t") for ln in _normalize_ws(text).splitlines() if ln.strip()]
    items = []
    for ln in lines:
        if re.search(r"(^|\s)(item|resolution|ordinance|motion|approve|report|agenda)\b", ln, re.I):
            items.append(ln)
    if not items:
        items = lines
    bullets = []
    for ln in items[: max_bullets * 2]:
        if len(ln) > 240:
            ln = ln[:237] + "..."
        bullets.append("• " + ln)
        if len(bullets) >= max_bullets:
            break
    return bullets

def llm_summarize(text: str, model: str = SUMMARIZER_MODEL, max_bullets: int = MAX_BULLETS) -> List[str]:
    """
    Use OpenAI if keys/model present; fallback to bulletify otherwise.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        if DEBUG:
            _log("OPENAI_API_KEY not set; using simple bulletify fallback")
        return bulletify(text, max_bullets=max_bullets)

    try:
        from openai import OpenAI  # type: ignore
        client = OpenAI(api_key=api_key)
        prompt = textwrap.dedent(f"""
        You are a city agenda summarizer. Read the following agenda text and extract the most notable items.
        Return up to {max_bullets} concise bullet points, each a single sentence.

        Agenda:
        ---
        {text[:DEFAULT_MAX_CHARS]}
        ---
        """).strip()

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You create concise bullet points summarizing municipal meeting agendas."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        content = (resp.choices[0].message.content or "").strip()
        raw = [ln.strip() for ln in content.splitlines() if ln.strip()]
        bullets: List[str] = []
        for ln in raw:
            ln = ln.replace("\u00A0", " ").strip()
            ln = re.sub(r"^\s*[•\-\*\u2013\u2014\u00B7\u2219]\s+", "• ", ln)
            if not re.match(r"^\s*•\s+", ln):
                ln = "• " + ln
            bullets.append(ln)
        if not bullets:
            bullets = bulletify(text, max_bullets=max_bullets)
        return bullets[:max_bullets]
    except Exception as e:
        if DEBUG:
            _log(f"LLM summarize failed: {e!r}; using fallback")
        return bulletify(text, max_bullets=max_bullets)

# ---------------------------
# Fetch + summarize pipeline
# ---------------------------

@dataclass
class SummaryResult:
    ok: bool
    reason: str
    bullets: List[str]
    used_url: Optional[str]
    used_kind: Optional[str]  # "text" or "pdf"
    chars: int

def _fetch_text_url(url: str) -> Tuple[Optional[str], str]:
    try:
        r = requests.get(url, timeout=60, headers=UA)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        ct = (r.headers.get("Content-Type") or "").lower()
        if "text/plain" in ct or "json" in ct or "text" in ct:
            txt = r.text
        else:
            try:
                txt = r.content.decode("utf-8", errors="replace")
            except Exception:
                txt = r.text
        return _normalize_ws(txt), ""
    except Exception as e:
        return None, f"fetch error: {e!r}"

def _fetch_pdf_url(url: str) -> Tuple[Optional[str], str]:
    try:
        r = requests.get(url, timeout=90, headers=UA)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        if not _looks_like_pdf(r):
            return None, f"not a PDF (Content-Type={r.headers.get('Content-Type')})"
        text = _extract_text_from_pdf_bytes(r.content)
        if not text:
            return None, "no extractable text"
        return _normalize_ws(text), ""
    except Exception as e:
        return None, f"fetch error: {e!r}"

def summarize_meeting(meeting: Dict[str, Any]) -> SummaryResult:
    """
    Prefer agenda_text_url; else agenda_url (PDF). Return bullets or reason.
    """
    text_url = (meeting.get("agenda_text_url") or "").strip() or None
    pdf_url = (meeting.get("agenda_url") or "").strip() or None

    # 1) Text stream (best for CivicClerk plainText=true)
    if text_url:
        txt, err = _fetch_text_url(text_url)
        if txt:
            if len(txt) > DEFAULT_MAX_CHARS:
                txt = txt[:DEFAULT_MAX_CHARS]
            bullets = llm_summarize(txt)
            return SummaryResult(True, "", bullets, text_url, "text", len(txt))
        if DEBUG:
            _log(f"Text fetch failed for {text_url}: {err}")

    # 2) PDF stream (accept even without .pdf extension)
    if pdf_url:
        txt, err = _fetch_pdf_url(pdf_url)
        if txt:
            if len(txt) > DEFAULT_MAX_CHARS:
                txt = txt[:DEFAULT_MAX_CHARS]
            bullets = llm_summarize(txt)
            return SummaryResult(True, "", bullets, pdf_url, "pdf", len(txt))
        if DEBUG:
            _log(f"PDF fetch failed for {pdf_url}: {err}")

    return SummaryResult(False, "no agenda_text_url or usable agenda_url", [], text_url or pdf_url, None, 0)

# ---------------------------
# CLI + merge
# ---------------------------

def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

def _write_meta(out_dir: Path, name: str, payload: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.meta.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Summarize meeting agendas into bullet points.")
    ap.add_argument("--input", required=True, help="Path to meetings.json")
    ap.add_argument("--out", required=True, help="Directory to write *.meta.json summaries")
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    out_dir = Path(args.out)

    payload = _load_json(in_path)
    meetings: List[Dict[str, Any]] = payload.get("meetings") or []
    _log(f"Loaded {len(meetings)} meetings from {in_path}")

    produced = 0
    merged = 0

    for idx, m in enumerate(meetings):
        title = (m.get("title") or m.get("meeting") or "Meeting").strip()
        date = (m.get("date") or m.get("meeting_date") or "").strip()
        city = (m.get("city") or m.get("city_or_body") or "").strip()
        slug = _slugify(f"{date}-{city}-{title}") or f"m{idx:03d}"

        res = summarize_meeting(m)

        # meta file for debugging / auditing
        meta = {
            "title": title,
            "city": city,
            "date": date,
            "source": m.get("source") or m.get("url"),
            "agenda_url": m.get("agenda_url"),
            "agenda_text_url": m.get("agenda_text_url"),
            "used_url": res.used_url,
            "used_kind": res.used_kind,
            "chars_summarized": res.chars,
            "bullets": res.bullets,
            "ok": res.ok,
            "reason": res.reason,
        }
        _write_meta(out_dir, slug, meta)

        # --- NEW: merge bullets back into meetings.json for BOTH 'text' and 'pdf' ---
        if res.ok and res.bullets and res.used_kind in {"text", "pdf"}:
            m["agenda_summary"] = res.bullets
            m["agenda_summary_source"] = res.used_kind
            m["agenda_summary_chars"] = res.chars
            merged += 1
            if DEBUG:
                _log(f"✓ {slug}: merged {len(res.bullets)} bullets ({res.used_kind})")
        else:
            if DEBUG:
                _log(f"✗ {slug}: {res.reason}")

        if res.ok and res.bullets:
            produced += 1

    # write back meetings.json (in place)
    _write_json(in_path, payload)

    _log(f"Completed summaries: {produced}/{len(meetings)}; merged into meetings.json: {merged}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
