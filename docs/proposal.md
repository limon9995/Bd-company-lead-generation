# Proposal — Automated B2B Lead Generation for Bangladesh

**Prepared for:** [Client name] · **Prepared by:** [Your name / company] · **Date:** [date]

## 1. What you asked for
1. Choose a target market (healthcare, education, real estate, tech …) and switch it any time.
2. Collect company details for that market in Bangladesh.
3. Identify the decision maker (CEO / MD / Founder / Chairman / whoever decides).
4. Notify you on Telegram / WhatsApp.
5. Store everything in a Google Sheet like a CRM.
6. Send customised emails on a schedule.

## 2. What the system does
A private, self-hosted web application with an **admin panel**. You paste your API keys into the panel and it runs.

| Step | How | Output |
|---|---|---|
| Find companies | Official **Google Places API**, e.g. "hospital in Dhaka" | Name, category, address, phone, website, Google rating, Maps link |
| Read company websites | Our crawler (HTTP + headless Chromium via Playwright) visits About / Board / Management / Contact pages | Emails, phones, Facebook/LinkedIn pages, leadership text |
| Find decision maker | **Google Gemini** AI reads those pages; if needed, a web-search API looks at public news/profile snippets | Name, title, **source link**, **confidence score** |
| Store | Admin panel CRM (status, notes, owner) + automatic **Google Sheet** copy + CSV export | |
| Notify | **Telegram** bot (free). WhatsApp via the official Cloud API optional | Run summary + hot-lead cards |
| Email | Your template + an AI-written opening line → you approve → sent from Gmail inside your chosen hours | Sent log, unsubscribe handling |

The industry is a dropdown in each campaign — switching Healthcare → Education is one click. Search phrases and target
titles per industry are editable in the panel.

## 3. Accuracy — honest answer
| Data | Expected availability in BD |
|---|---|
| Company name, address, phone, website | High (Google Places) |
| Company email (info@…) | Medium — when the company has a website |
| Decision-maker name + title | Medium for mid/large companies, **low for small local businesses** (many don't publish it) |
| Decision-maker personal email | Low — most are not public; guesses are clearly marked and never auto-sent |
| Personal mobile numbers | Not promised |

We do **not** promise a percentage up front. The dashboard measures the real hit-rate for each campaign; we will share the
numbers from a pilot run (e.g. 50 companies) before scaling. Every name comes with the link it was found on, and the AI is
not allowed to "invent" people: a name that is not literally in the source page is thrown away.

## 4. Data sources & legality
| Source | How we use it | Why it's safe |
|---|---|---|
| Google Places API | Official paid API | Allowed under Google's terms (unlike scraping Google Maps pages) |
| Company websites | Public pages only, robots.txt respected, slow request rate | Publicly published business information |
| Search API (Serper / Brave) | Public search results only | Licensed API; we only read snippets |
| LinkedIn | **Not scraped.** Only public search-result snippets and a link for manual checking | LinkedIn's terms forbid scraping; logged-in scraping gets accounts banned |
| WhatsApp | Only the official Meta Cloud API | Unofficial WhatsApp bots get numbers banned |

Emails include an unsubscribe link and honour it automatically. Bangladesh's personal-data protection rules are still
evolving — please have your legal team confirm your outreach policy. The system stores business contact information only.

## 5. Technology
Python (FastAPI) · PostgreSQL · Playwright/Chromium · Google Places API (New) · Google Gemini API · Serper.dev or Brave
Search API · Telegram Bot API · Gmail SMTP · Google Sheets API · Docker. All code and data stay on your server.

## 6. Running costs (per month, estimates — Sept 2026 prices)
| Item | Cost | Notes |
|---|---|---|
| VPS server (2 vCPU / 4 GB) | ~ $6 – 25 | Hetzner / Contabo cheaper; DigitalOcean more expensive |
| Google Places API | **$0** for ~1,000 calls/month, then ~$35 per 1,000 calls | 1 call returns up to 20 companies → free tier covers several thousand companies/month. Needs a billing card. |
| Search API (Serper) | $0 for the first 2,500 queries, then from $50 per 50,000 | ~2 queries per company that needs it |
| Gemini API | $0 on free tier (rate-limited) or roughly a few USD per 1,000 companies on paid tier | Free tier data may be used by Google to improve products |
| Gmail SMTP, Telegram, Google Sheets | $0 | Keep to ~30–50 emails/day on one Gmail account |
| WhatsApp Cloud API (optional) | Meta per-message charge | |
| **Typical total** | **~ $6 – 40 / month** at a few thousand companies per month | Real numbers are visible in the dashboard and your Google billing page |

Prices are from public pricing information in September 2026 and can change; please re-check the providers' pricing pages
before budgeting.

## 7. Delivery
| Phase | What | Time |
|---|---|---|
| 1. Setup | Server, your API accounts (we guide you), admin users, Telegram, Gmail, Sheet | 1–2 days |
| 2. Pilot | One industry, ~50 companies, measured hit-rate report, template tuning | 2–3 days |
| 3. Go-live | Schedules, auto-approve rules, handover + training (recorded) | 1 day |

## 8. Fees
| Item | Amount |
|---|---|
| Development & setup (one-time) | **[FEE_TBD]** |
| Maintenance (monthly, optional) | **[FEE_TBD]** — fixes when websites/APIs change, monitoring, small tweaks, [N] hours/month |
| Third-party API/server costs | Paid directly by you to the providers (section 6) |

## 9. Not included (can be quoted separately)
Scraping of LinkedIn or other login-protected sites · buying contact databases · automatic follow-up sequences and
reply detection (planned phase 2) · WhatsApp template approval work.
