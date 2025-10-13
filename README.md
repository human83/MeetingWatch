# Meeting Watch — Starter Kit

This repo sets up a small pipeline that scrapes the specified municipal sites, summarizes any posted agendas with an LLM, and publishes a simple webpage that stays up to date.

## What you get
- **/scraper** — Python scrapers (Playwright for JS-heavy pages, pdfminer for agenda PDFs) + LLM summarizer.
- **/data/meetings.json** — Generated output consumed by the webpage.
- **/frontend/index.html** — Static webpage that renders future meetings + agenda highlights.
- **GitHub Action** (`.github/workflows/scrape.yml`) — Runs on a schedule, commits updated JSON, and (optionally) triggers your Cloudflare Pages deploy.

## Quick start
1) **Create a new GitHub repo** and upload these files.
2) In your repo **Settings → Secrets and variables → Actions**, add:
   - `OPENAI_API_KEY` (or set `LLM_PROVIDER=none` to disable summaries for now)
   - (Optional) `CF_PAGES_HOOK_URL` if you want to ping a build hook after scraping.
3) Enable GitHub Actions on the repo (if it isn’t already).
4) If you use **Cloudflare Pages**:
   - Connect this repo to Pages, or keep your existing connection.
   - The Action will commit `/data/meetings.json` and you can let Pages rebuild automatically on push or via a build hook.
5) To run locally:
   ```bash
   cd scraper
   python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   python -m playwright install
   python main.py
   ```
   Then open `frontend/index.html` in a local web server or commit and deploy.
   
## Notes
- **Respect robots.txt** and rate-limit requests. This starter uses conservative defaults.
- CivicClerk/Diligent portals are JavaScript-heavy. Playwright is included and used only when required.
- If `OPENAI_API_KEY` is not set, agenda summaries fall back to **extractive** bullet points (first N lines/sections) so the page still updates.
- Times are normalized to **America/Denver** and filtered to **future-only** based on the server time.

---
