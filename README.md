# bot-condo

A personal automation bot that scrapes Facebook rental groups, extracts structured listings with AI, and exports results to Excel — so you never have to scroll through hundreds of posts manually.

---

## How it works

```
Facebook Groups
      │
      ▼
 scraper/feed.py        Playwright scrolls the feed, extracts post text + comments
      │                 Hard-skips: restricted/locked posts, sale-only posts
      ▼
analysis/extractor.py   Step 1 — check_relevance(): ask DeepSeek if any rental offer exists
      │                 Step 2 — extract_listings_with_comments(): extract 8-field JSON
      │                 (condo_name, room_type, size_sqm, floor, rent, location_tags, status, summary)
      ▼
database/db.py          SQLite — dual-gate dedup (post_id + content hash)
      │                 move_in_cost = rent × 3, inserted alongside extracted fields
      ▼
Web UI / Excel export   Browse results in browser, or download single-sheet .xlsx
```

---

## Filtering approach: v1 vs v2 vs v3

### v1 — Keyword-based pre-filter (original)

Before calling DeepSeek, the system used heavy keyword scoring to pre-filter posts:

- `classify_post_intent()` counted keywords like "ให้เช่า", "หาห้อง", "ขาย" and classified posts as `for_rent / seeking / sale / ambiguous`
- `seeking` posts had to pass `filter_listing_comments()` — comments with a supply score < 2 were dropped
- Each comment was sent to DeepSeek separately (parallel, 5 concurrent)

**Problem:** Facebook comment patterns are inconsistent. Keyword scoring dropped many real listings, so dozens of rooms were scraped but only a handful reached the user.

---

### v2 — AI-first 2-step pipeline

Replaced keyword rules with DeepSeek making all relevance decisions:

**Step 1 — Relevance check (cheap)**
- Post text + all comments are sent as a single blob (capped at 50 comments)
- DeepSeek replies with only `{"relevant": true/false, "reason": "..."}`
- If `false` → skip immediately, no Step 2 tokens spent

**Step 2 — Full extraction (only if Step 1 passes)**
- Same blob sent again; DeepSeek extracts structured fields including a Thai-language `summary`
- No seeking/for_rent split — every post uses the same flow

**Removed:** `filter_listing_comments()`, per-comment parallel extraction  
**Added:** `summary` field — one Thai sentence covering all found details

---

### v3 — Lean AI schema, no scoring (current)

Removed all rule-based scoring and tiering. DeepSeek now returns a compact 9-field JSON that is inserted directly to the DB.

**Step 2 — Full extraction (updated schema)**
- DeepSeek extracts 8 fields: `condo_name`, `room_type`, `size_sqm`, `floor`, `rent`, `location_tags`, `status`, `summary`
- `status` combines owner/agent flag and risk notes in a single field (≤ 30 chars)
- `move_in_cost` is computed in Python as `rent × 3` before DB insert

**Removed:**
- `rules/scoring.py` — no tiers, no weighted scores
- `listing_scores` table — dropped from schema entirely
- `--tier` CLI flag, `run_alerts()`, and all preference-based hard filters

**DB schema (listings table)**

| Column | Source |
|---|---|
| `condo_name` | AI |
| `room_type` | AI |
| `size_sqm` | AI |
| `floor` | AI |
| `rent` | AI |
| `location_tags` | AI |
| `status` | AI |
| `summary` | AI (Thai) |
| `move_in_cost` | Computed: `rent × 3` |

**Excel export** — single sheet "Listings" with 12 columns:  
Timestamp | Source | Rent (฿) | Move-in Cost | Condo Name | Room Type | Size (sqm) | Floor | Location Tags | Status | Summary | Post Link

---

## Tech stack

| Layer | Tools |
|---|---|
| Browser automation | Playwright (async) |
| AI extraction | DeepSeek API (`deepseek-chat`) |
| Storage | SQLite |
| Web UI | FastAPI + Jinja2 |
| Export | pandas + openpyxl |

---

## Setup

### 1. Install dependencies

```bash
python -m venv env
source env/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

```env
DEEPSEEK_API_KEY=sk-...        # required
```

### 3. Log in to Facebook

Run once to save a browser session:

```bash
python main.py --login
```

This opens a real browser window. Log in to Facebook manually. The session is saved to `user_data/` and reused on future runs.

---

## Running

### Web UI (recommended)

```bash
python web.py
```

Open [http://localhost:8000](http://localhost:8000), paste Facebook group URLs, then hit **Start**.

- Live log stream while scraping
- Results table with listing cards
- Download `.xlsx` export

### CLI

```bash
python main.py                   # scrape + extract + export
python main.py --export-only     # re-export Excel from existing DB
python main.py --cleanup         # delete posts older than DATA_RETENTION_DAYS
python main.py --login           # open browser for FB login
python main.py --max-posts 20    # limit posts per run
```

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | DeepSeek API key (required) |
| `DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek model ID |
| `FB_GROUP_URLS` | — | Comma-separated Facebook group URLs |
| `MAX_SCROLL_ROUNDS` | `8` | How many scroll pages per group |
| `MAX_POSTS_PER_RUN` | `150` | Post cap per run |
| `HEADLESS` | `false` | Run browser in background |
| `DATA_RETENTION_DAYS` | `30` | Auto-delete old posts after N days |
| `AUTO_LOOP` | `false` | Keep re-running on an interval |
| `LOOP_INTERVAL_MINUTES` | `60` | Interval between auto-loop runs |

---

## Project structure

```
bot-condo/
├── scraper/
│   ├── feed.py          # Facebook feed scroll + post extraction
│   └── browser.py       # Playwright session management
├── analysis/
│   └── extractor.py     # DeepSeek API calls + JSON parsing
├── database/
│   └── db.py            # SQLite schema + CRUD helpers
├── web/
│   ├── app.py           # FastAPI routes
│   ├── scrape_runner.py # Subprocess management for live log streaming
│   ├── db_queries.py    # Query helpers for the UI
│   ├── forms.py         # Form parsing + validation
│   ├── state.py         # In-memory run state
│   └── templates/       # Jinja2 HTML templates
├── output/
│   └── excel.py         # Single-sheet .xlsx export
├── main.py              # CLI entry point
├── web.py               # Web UI entry point
├── config.py            # Env var loading
└── .env.example
```

---

## Providers Monitor

In addition to the Facebook scraper, the bot includes a **Providers Monitor** for tracking rental listings on property listing sites directly.

### Supported providers

| Provider | Site | Technique |
|---|---|---|
| PropertyHub | [propertyhub.in.th](https://propertyhub.in.th) | Extracts `__NEXT_DATA__` JSON (no browser needed) |
| LivingInsider | [livinginsider.com](https://www.livinginsider.com) | Parses server-rendered HTML cards with BeautifulSoup |

Adding a new provider requires only a single file in `scraper/` implementing `PROVIDER_ID`, `PROVIDER_NAME`, `URL_PATTERN`, and `scrape_project()` — then one line in `scraper/registry.py`.

### Usage

1. Open the Web UI at [http://localhost:8000/providers](http://localhost:8000/providers)
2. Add a watch by pasting a project URL from any supported provider
3. Set optional filters: price range, minimum size (sqm), minimum floor, and poll interval (minutes)
4. The system polls in the background and sends a LINE alert when a new listing passes your filters

### Features

| Feature | Details |
|---|---|
| Multi-provider | Each watch auto-detects its provider from the URL |
| Auto-poll | Background loop checks every 60 seconds which watches are due to run |
| Per-watch interval | Each watch has its own poll interval (default: 30 minutes) |
| Filters | Min/max price, minimum room size, minimum floor |
| Read / Unread | Mark listings as read from the UI; unread state persists across refreshes |
| AJAX refresh | Scan Now and filter changes update the grid without a page reload |
| LINE alert | Instant notification when a new listing matches your filters |
| Price drop alert | Re-alerts when an existing listing drops below your max price |
| URL normalization | Thai percent-encoded URLs are canonicalized automatically |

### Relevant files

```
web/
├── ph_routes.py          # FastAPI router: CRUD watches, listing endpoints
├── ph_poller.py          # Background asyncio poll loop
└── templates/
    └── propertyhub.html  # Main UI (watches panel + listings grid)
scraper/
├── registry.py           # Provider registry — maps provider_id → module
├── propertyhub.py        # PropertyHub scraper (__NEXT_DATA__ JSON)
└── livinginsider.py      # LivingInsider scraper (HTML card parsing)
alerts/
└── notify.py             # notify_ph_listing() → LINE Notify
database/
└── db.py                 # ph_watches / ph_listings tables + CRUD helpers
```

---

## Notes

- The bot reads **public Facebook group posts** only — it does not interact with or modify any content.
- Session cookies are stored locally in `user_data/` and are excluded from version control.
- DeepSeek API costs roughly **< 0.005 THB per post** analyzed.
