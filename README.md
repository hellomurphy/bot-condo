# bot-condo

A personal automation bot that scrapes Facebook rental groups, filters listings with AI, and ranks them against your preferences — so you never have to scroll through hundreds of posts manually.

---

## How it works

```
Facebook Groups
      │
      ▼
 scraper/feed.py        Playwright scrolls the feed, extracts post text + images
      │                 classify_post_intent() pre-filters: rent listing vs. seeker vs. junk
      ▼
analysis/extractor.py   DeepSeek parses each promising post → structured JSON
      │                 (rent, size, location, amenities, contact, confidence)
      ▼
rules/scoring.py        Hard filters (budget, size, washer…) + weighted score → tier
      │
      ▼
database/db.py          SQLite — deduplicates by content hash, stores all listings
      │
      ▼
Web UI / Excel export   Browse results in browser, or download .xlsx
```

### Tiers

| Tier | Score | Meaning |
|---|---|---|
| `must_call` | ≥ 80 | Matches all preferences — contact now |
| `shortlist` | ≥ 65 | Strong match |
| `maybe` | ≥ 50 | Worth a look |
| `need_info` | — | AI couldn't extract key details |
| `skip` | — | Over budget, too small, or irrelevant |

---

## Tech stack

| Layer | Tools |
|---|---|
| Browser automation | Playwright (async) |
| AI extraction | DeepSeek API (`deepseek-chat`) |
| Optional vision | OpenAI `gpt-4o-mini` (floor plan analysis) |
| Storage | SQLite via `aiosqlite` |
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
OPENAI_API_KEY=sk-...          # optional — only needed for ENABLE_VISION=true
```

Preferences (budget, areas, must-haves) are configured through the Web UI at runtime — no need to set them in `.env`.

### 3. Log in to Facebook

Run once without headless mode to save a browser session:

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

Open [http://localhost:8000](http://localhost:8000), paste Facebook group URLs, set your budget and preferences, then hit **Start**.

- Live log stream while scraping
- Results table with tier badges
- Download `.xlsx` export

### CLI

```bash
python main.py
```

Configure via `.env` or environment variables. Results are saved to `data/condo.db` and `data/results/`.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `TARGET_BUDGET` | `12000` | Ideal monthly rent (THB) |
| `MAX_BUDGET` | `15000` | Hard ceiling |
| `MAX_MOVE_IN_COST` | — | Max deposit + advance (optional) |
| `MIN_SIZE_SQM` | `24` | Minimum room size |
| `PREFERRED_AREAS` | `On Nut,Udom Suk` | Comma-separated area names |
| `PREFERRED_STATIONS` | `BTS On Nut,BTS Udom Suk` | Comma-separated BTS/MRT stations |
| `MUST_HAVE_WASHER` | `false` | Hard filter: washer required |
| `NEED_PARKING` | `false` | Hard filter: parking required |
| `PET_FRIENDLY` | `false` | Prefer pet-friendly units |
| `PREFERRED_ROOM_TYPES` | `studio,1br` | `studio`, `1br`, `2br` |
| `MAX_SCROLL_ROUNDS` | `8` | How many scroll pages per group |
| `MAX_POSTS_PER_RUN` | `150` | Post cap per run |
| `HEADLESS` | `false` | Run browser in background |
| `ENABLE_VISION` | `false` | Analyze floor plan images with GPT-4o |
| `DEEPSEEK_MODEL` | `deepseek-chat` | DeepSeek model ID |
| `ALERT_MIN_TIER` | `shortlist` | Minimum tier to trigger LINE alert |
| `DATA_RETENTION_DAYS` | `30` | Auto-delete old listings after N days |

---

## Project structure

```
bot-condo/
├── scraper/
│   ├── feed.py          # Facebook feed scroll + intent classification
│   └── browser.py       # Playwright session management
├── analysis/
│   ├── extractor.py     # DeepSeek API calls + JSON parsing
│   └── vision.py        # Optional image analysis (GPT-4o)
├── rules/
│   ├── scoring.py       # Hard filters + weighted score → tier
│   └── preferences.py   # Preferences dataclass
├── database/
│   └── db.py            # SQLite schema + async queries
├── web/
│   ├── app.py           # FastAPI routes
│   ├── scrape_runner.py # Subprocess management for live log streaming
│   ├── db_queries.py    # Query helpers for the UI
│   ├── forms.py         # Form parsing + validation
│   ├── state.py         # In-memory run state
│   └── templates/       # Jinja2 HTML templates
├── output/
│   └── excel.py         # .xlsx export
├── alerts/
│   └── notify.py        # LINE Notify integration
├── main.py              # CLI entry point
├── web.py               # Web UI entry point
├── config.py            # Env var loading
└── .env.example
```

---

## Notes

- The bot reads **public Facebook group posts** only — it does not interact with or modify any content.
- Session cookies are stored locally in `user_data/` and are excluded from version control.
- DeepSeek API costs roughly **< 0.01 THB per post** analyzed.
