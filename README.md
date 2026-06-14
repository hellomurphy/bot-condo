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
      │                 restricted/locked posts are intercepted before hitting the API
      ▼
analysis/extractor.py   DeepSeek parses each promising post → structured JSON
      │                 (rent, size, location, amenities, contact, confidence)
      ▼
rules/scoring.py        Hard filters (budget, size, washer…) + weighted score → tier
      │
      ▼
database/db.py          SQLite — dual-gate dedup (post_id + content hash), stores all listings
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
| AI extraction | DeepSeek API (`deepseek-v4-flash`) |
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
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek model ID |
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

## PropertyHub Monitor

นอกจาก Facebook scraper แล้ว bot ยังมี **PropertyHub Monitor** สำหรับติดตามประกาศเช่าบน [propertyhub.in.th](https://propertyhub.in.th) โดยเฉพาะ

### วิธีใช้งาน

1. เปิด Web UI ที่ [http://localhost:8000/propertyhub](http://localhost:8000/propertyhub)
2. กด **+ Add Watch** และกรอก URL โครงการจาก propertyhub.in.th (รองรับหลาย URL ต่อ watch)
3. ตั้ง filter ตามต้องการ: ราคา, ขนาด (ตร.ม.), ชั้น, และรอบ poll (นาที)
4. ระบบจะ poll ในพื้นหลังอัตโนมัติ และแจ้งเตือน LINE เมื่อพบห้องใหม่ที่ผ่าน filter

### ฟีเจอร์

| ฟีเจอร์ | รายละเอียด |
|---|---|
| Auto-poll | Background loop ทุก 60 วินาที ตรวจว่า watch ไหนถึงเวลา poll |
| Per-watch interval | กำหนด interval แยกกันได้ต่อ watch (default 30 นาที) |
| Filter | ราคา min/max, ขนาดห้อง, ชั้นต่ำสุด |
| Mute | ปิดการแจ้งเตือนรายการที่ไม่สนใจได้จากหน้า UI |
| AJAX refresh | กด Refresh หรือ Scan Now โดยไม่ต้อง reload หน้า |
| LINE alert | แจ้งเตือนทันทีเมื่อพบห้องใหม่ที่ตรงกับ filter |
| URL normalization | แปลง URL ภาษาไทย (percent-encoded) ให้เป็น canonical อัตโนมัติ |

### ไฟล์ที่เกี่ยวข้อง

```
web/
├── ph_routes.py          # FastAPI router: CRUD watches, listing endpoints
├── ph_poller.py          # Background asyncio poll loop
└── templates/
    └── propertyhub.html  # หน้า UI หลัก (watches + listings table)
scraper/
└── propertyhub.py        # Scraper ดึงข้อมูลจาก __NEXT_DATA__ JSON
alerts/
└── notify.py             # notify_ph_listing() → LINE Notify
database/
└── db.py                 # ph_watches / ph_listings tables + CRUD helpers
```

---

## Notes

- The bot reads **public Facebook group posts** only — it does not interact with or modify any content.
- Session cookies are stored locally in `user_data/` and are excluded from version control.
- DeepSeek API costs roughly **< 0.005 THB per post** analyzed ($0.14 / 1M input tokens for deepseek-v4-flash).
