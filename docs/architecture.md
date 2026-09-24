# Architecture

## Components
| Container | Role |
|---|---|
| `web` | FastAPI admin panel (server-rendered Jinja2, no JS build). Runs DB migrations on start. |
| `worker` | Job runner threads + APScheduler (campaign crons, email ticks every 30 s, stale-job recovery, daily digest 09:00). |
| `db` | PostgreSQL 16 — single source of truth, also the job queue (`SELECT … FOR UPDATE SKIP LOCKED`). |
| `backup` | Daily `pg_dump`, 7-day retention. |
| `caddy` (optional) | HTTPS reverse proxy. |

Google Sheets is a **one-way mirror** (DB → Sheet); CRM edits happen in the admin panel so nothing conflicts.

## Pipeline (one job per stage, idempotent)
```
run_campaign ─▶ discover[i] | discover_maps[i] | discover_directory[url] ─▶ crawl_company ─▶ find_decision_maker ─▶ finalize_run
```
* **discover** — Places Text Search (≤ 3 pages × 20). Skips permanently-closed places. Dedupe: `place_id` → website
  domain → name+phone. Facebook-only "websites" are stored as socials, never used as a domain. Page progress is saved in
  the job so a retry doesn't pay for the same page twice. Stops at the campaign's max companies.
* **crawl_company** — httpx first, headless Chromium if the page is JS-rendered. Visits only same-site pages whose URL/label
  suggests leadership or contact (English + Bangla keywords), max N pages, robots.txt respected, ≥ N s between requests
  per host. Extracts emails (incl. `[at]` obfuscation), BD phone numbers (+880 normalised), social links, page text.
  Companies enriched within `recrawl_after_days` are not crawled again.
* **find_decision_maker** — Gemini reads the pages and returns `{name, title, source_index, evidence_quote}`.
  **Hallucination guard:** a name that does not literally appear in the source text is discarded; a non-verbatim quote
  costs −30 points. If no senior (rank ≤ 2) candidate with confidence ≥ 55 is found, it runs 2 search queries (Serper/Brave)
  and asks Gemini about the snippets; snippets that don't mention the company are rejected.
* **finalize_run** — counters, Sheet sync, Telegram summary + cards for high-confidence leads, email drafts.

### Browser-mode sources
* **discover_maps** — Google Maps search in headless Chromium: scroll the results list, open each place page, parse
  name / category / address / phone / website / rating from `data-item-id` and `aria-label` attributes. Places already
  known and fresh are not opened again. Place page loads count against `maps_daily_cap`.
* **discover_directory** — each admin-provided URL: render, Gemini lists the businesses on the page (a name must appear
  in the page text), follow `rel=next` / "Next" links up to `directory_max_pages`.
* **Browser search** — DuckDuckGo HTML or Bing results page instead of Serper/Brave (Settings → search provider).
* **Facebook** — when a company has no website but a Facebook page, the public page is read without logging in;
  a login wall is counted and skipped.
* Politeness: random 4–9 s wait before each page load, one browser job per site (`source_lock`), a normal Chrome UA.
* Blocks: the URL and visible text are checked for CAPTCHA / "unusual traffic" / login pages → `SourceBlocked` →
  source paused for `blocked_pause_hours`, job postponed, one Telegram alert. No CAPTCHA solving or bypass.

### Confidence score (shown per person with the reasons)
+40 on company's own website · +25 in a search result mentioning the company · +15 title matches campaign titles ·
+10 both sources agree · +10 top-level title · −30 evidence quote not verbatim. ≥70 high, 40–69 medium, <40 low.

### Email address choice
person's own address seen on the site (`found`) → company address from the site (`company`) → pattern guess on a domain
with MX records (`guessed`, never auto-approved) → none.

## Reliability
* Retries with backoff 1 min / 5 min / 30 min, then `failed` (visible in Runs → Problems, with a Retry button).
* Missing API key → stage `skipped` with a note; the rest of the pipeline continues.
* Daily call caps per paid API → jobs postponed to 00:05 Dhaka + one Telegram alert.
* Worker restart → interrupted jobs re-queued; stages are idempotent.

## Email sending rules
Only `approved` messages are sent, one per tick, inside the send window (days + hours, Asia/Dhaka), under the daily cap,
with a random gap between sends. Suppression list (email or `@domain`), do-not-contact leads and a per-company cooldown are
checked right before sending. Unsubscribe link + `List-Unsubscribe` header on every email; SMTP "recipient refused" →
suppressed.

## Security
API keys Fernet-encrypted at rest (`APP_SECRET_KEY`), masked in the UI and never logged; bcrypt passwords; signed
session cookie; CSRF token on every POST; login throttling; audit log of setting changes (key names only).

## Code map
```
app/services/   places, search, crawler, llm (Gemini), extractor, scoring, email_finder, notifier, sheets, mailer, usage
app/pipeline/   queue, stages, emails (drafts + dispatcher), providers (clients from settings)
app/worker/     runner, scheduler, __main__
app/routers/    admin pages; public.py = unsubscribe + /health
app/settings_store.py  every admin-panel setting (add a SettingDef to add a field)
```
