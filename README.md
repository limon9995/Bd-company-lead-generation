# BD Company Lead Generation

Self-hosted system that finds Bangladeshi companies in a chosen industry, identifies their decision makers
(CEO / MD / Founder / Chairman …), stores them in a CRM-style admin panel (mirrored to Google Sheets),
notifies you on Telegram (or WhatsApp Cloud API), and sends personalised, scheduled emails.

Every API key and token is entered in the **admin panel** (Settings) — no code changes needed.

```
Campaign (industry + cities) ─▶ Google Places API ─▶ company website crawl ─▶ Gemini extraction
      ─▶ search fallback ─▶ confidence score + source ─▶ Leads / Google Sheet ─▶ Telegram ─▶ Outbox ─▶ Gmail SMTP
```

## Quick start (Docker)

```bash
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"   # → APP_SECRET_KEY
python3 -c "import secrets;print(secrets.token_urlsafe(32))"                              # → SESSION_SECRET
# edit .env: APP_SECRET_KEY, SESSION_SECRET, POSTGRES_PASSWORD, BASE_URL
docker compose up -d --build
docker compose exec web python -m scripts.create_admin you@example.com
# open http://localhost:8000 (on a server: ssh -L 8000:localhost:8000 user@server)
```

Then in the admin panel: **Settings** → add keys → press **Test** on each group → **Campaigns → New** → **Run now**.

Telegram chat ID: send `/start` to your bot, then Settings → Telegram → **Detect chat ID**, or run
`python scripts/get_telegram_chat_id.py --token <BOT_TOKEN>` (standard library only, runs anywhere).

Full step-by-step (API keys, VPS, HTTPS): [docs/setup.md](docs/setup.md) · How it works: [docs/architecture.md](docs/architecture.md) (visual version: docs/system-design.html)
· Client proposal: [docs/proposal.md](docs/proposal.md)

## Local development

```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
playwright install chromium            # or set CHROMIUM_EXECUTABLE to an existing Chromium
export APP_SECRET_KEY=... SESSION_SECRET=dev DATABASE_URL=sqlite:///./dev.db
python -m app.bootstrap                # migrations + seed industries/template
python -m scripts.create_admin you@example.com
uvicorn app.main:app --reload          # admin panel
python -m app.worker                   # pipeline worker + scheduler (second terminal)
pytest                                 # 63 tests, no network needed (external APIs are mocked)
```

Tests also run on Postgres: `DATABASE_URL=postgresql+psycopg://user:pw@localhost/test pytest`.

## Data sources (chosen per campaign / in Settings)
| Need | API mode (key, reliable) | Browser mode (free, headless Chromium) |
|---|---|---|
| Find companies | Google Places API | Google Maps pages, or any directory URL you give |
| Decision-maker search | Serper / Brave | DuckDuckGo / Bing result pages |
| Company details | company website crawl (always) | + public Facebook page when there is no website |

**Smart mode** (default, Settings → Browser scraping → "How the browser searches" = `type`): the browser opens the
site, types into its search box, presses Enter, wheel-scrolls the results and clicks each one. For directory sites an
**AI browser agent** (Gemini) reads each page and decides the next step — type a search, pick a filter, scroll, click
Next, extract listings — within guardrails: same website only, never logs in or fills email/password forms, never
clicks buy/pay/sign-up, step limit.

Browser mode waits 4–9 s between page loads, runs one job per site at a time, and **stops** on a CAPTCHA,
"unusual traffic" or login page — the source is paused (default 6 h, "Resume now" in Settings). It never solves
CAPTCHAs, never logs in, and uses no stealth tricks. Google, Bing, DuckDuckGo and Facebook terms do not allow
automated collection, so API mode remains the compliant choice.

## Honest limits
- Company data (name, phone, address, website) coverage is high; **decision-maker coverage is not** — small BD
  businesses often don't publish leadership. The dashboard shows the *measured* hit-rate per campaign.
- Personal emails are rarely public; pattern guesses are marked `guessed` and never auto-sent.
- No LinkedIn login scraping. Browser mode for Google Maps/Search/Facebook is available but against those sites'
  terms and can be blocked; Google changes its Maps page layout from time to time, so the Maps parser
  (`app/services/maps_browser.py`) may need a selector update.
- Browser-mode parsers were tested against local copies of the page structure, not the live sites (no internet in
  the build environment) — run a small pilot first.
