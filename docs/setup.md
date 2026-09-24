# Setup guide

## 1. Server
Any Linux VPS with **2 vCPU / 4 GB RAM** (headless Chromium needs the memory), Docker + Docker Compose.
Examples: Hetzner, DigitalOcean, Contabo, or a local office PC.

```bash
git clone <repo> leadgen && cd leadgen
cp .env.example .env    # fill APP_SECRET_KEY, SESSION_SECRET, POSTGRES_PASSWORD, BASE_URL (see README)
docker compose up -d --build
docker compose exec web python -m scripts.create_admin you@example.com
```

> **Keep `APP_SECRET_KEY` safe and never change it** — it encrypts every stored API key. If it changes, re-enter the keys.

### Access & HTTPS
* Default: the panel listens on `127.0.0.1:8000` only. From your laptop: `ssh -L 8000:localhost:8000 user@server` → http://localhost:8000
* **Unsubscribe links in emails must be reachable by recipients.** Options:
  1. Free subdomain (no domain purchase): create one at duckdns.org pointing to the server IP, set `DOMAIN=yourname.duckdns.org`,
     `BASE_URL=https://yourname.duckdns.org`, `SECURE_COOKIES=true`, then `docker compose --profile https up -d` (Caddy gets a free TLS certificate).
  2. Without any URL: recipients can still reply "unsubscribe" (the footer says so) — then add them in **Suppression list**.

Backups: the `backup` service writes a gzip dump to `./backups/` daily and keeps 7 days.
Restore: `gunzip -c backups/leadgen-DATE.sql.gz | docker compose exec -T db psql -U leadgen leadgen`.

## 2. API keys (all entered in Admin → Settings)

| Service | Where | Notes |
|---|---|---|
| Google Places API | console.cloud.google.com → new project → enable **Places API (New)** → Credentials → API key | Needs a billing account (card). Restrict the key to Places API. |
| Serper.dev (search) | serper.dev → sign up → API key | 2,500 free queries. Or Brave Search API. Optional but improves decision-maker coverage. |
| Gemini | aistudio.google.com → Get API key | Free tier is rate-limited and Google may use free-tier data to improve its products; enable billing for paid tier. Press **Test**: if the model name is wrong it lists the models your key can use. |
| Telegram | Telegram → @BotFather → /newbot → token | Send `/start` to the bot → **Detect chat ID**. For a group: add bot to group, send a message, detect. |
| Gmail SMTP | Google Account → Security → 2-Step Verification ON → **App passwords** → create | host `smtp.gmail.com`, port `587`, username = the Gmail address, password = the 16-char app password. Use a separate Gmail account for outreach. |
| Google Sheets | Cloud console → IAM → Service Accounts → create → Keys → JSON; enable **Google Sheets API** | Paste the JSON in Settings, create a sheet, **share it with the service account's `client_email` as Editor**, paste the sheet ID. |
| WhatsApp (optional) | Meta for Developers → WhatsApp → Cloud API | Needs business verification + an approved template with one body variable `{{1}}`. |

### Free browser mode (no Places / Serper keys)
* Campaign → **Where companies come from** → *Google Maps (browser)* or *Directory URLs (browser)*.
* Settings → Web search → provider `duckduckgo` or `bing`.
* Only Gemini is then required. Browser mode is slower (4–9 s per page) and can be paused by a block —
  see Settings → Browser scraping. Keep the delays; lowering them makes blocks more likely.

## 3. First campaign
1. **Industries** — check/edit the presets (search phrases + titles). This is the industry "switch".
2. **Email templates** — edit the default template: your service line, tone. Variables are listed on the page.
3. **Campaigns → New** — pick industry, cities, max companies, template. Leave *auto-approve* off at first.
4. **Run now** → watch **Runs** → then **Leads**. Check a few leads' sources.
5. **Outbox** → review drafts → approve. The worker sends inside the send window (Settings → Email schedule).
6. Set a cron schedule on the campaign when happy (e.g. `0 9 * * 0` = every Sunday 09:00 Dhaka).

## 4. Operations
* Logs: `docker compose logs -f worker`
* A run stuck at "running"? Check **Runs → Problems**; jobs postponed by a daily cap resume after midnight (Dhaka).
* Update: `git pull && docker compose up -d --build` (migrations run automatically).
