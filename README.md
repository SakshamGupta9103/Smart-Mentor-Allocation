# Smart Mentor Allocation

Shortlisted Day 1 entry — **Prompt Manthan 2026** (AI competition, MM Institute).

An AI-assisted mentor allocation system for colleges. Upload student/teacher data via CSV — or sync straight from the college MySQL database — and the app auto-allocates every student to a mentor, flags at-risk students, and gives mentors and students an AI chatbot that answers from live records.

## Features

- **One-click demo** — "Load demo & allocate": 188 students, 10 teachers, 0 unallocated
- **CSV upload** with all-or-nothing validation, or **live MySQL sync** (credentials used once, never stored)
- **Risk scoring** — attendance/CGPA-based flags with per-student reasons
- **Mentor dashboard** — allocation tiles + per-student AI summary briefings
- **Student chatbot** — answers only from that student's own record (grounded by design, no invented facts)
- **Unbreakable AI layer** — Gemini API key (optional) → free no-key AI service → rule-based offline fallback, so the demo never dies on stage
- Queries module, reminders module, 33 automated tests

## Tech stack

- Backend: **Flask** (the only required dependency), **SQLite**
- Optional: `pymysql` for MySQL sync — AI calls use stdlib `urllib` only, zero extra AI deps
- Frontend: single static `index.html`, dark theme

## Quickstart

```bash
pip install flask
python app.py
# open http://127.0.0.1:5000
```

Admin tab → **"Load demo & allocate"** → click any tile.

With your own data: Admin tab → upload `students.csv` + `subjects.csv` (see `sample_data/` for the format) → "Re-allocate everyone".

## MySQL sync (alternative to CSV)

Admin tab → **"Sync from MySQL & allocate"** — enter host, port, database, user, password and table names; the app pulls teacher/student tables and runs the same validation + allocation pipeline. Needs `pip install pymysql`. Expected table layout: `mysql_schema.sql`; demo seed: `mysql_seed_demo.sql`.

## AI setup (optional)

Works out of the box with no key. For faster answers, paste a free Gemini key (aistudio.google.com) in Admin → **AI Settings** → Save & enable. The key lives only in the local `mentor.db` on that PC — never in the repo, never shown in the UI. Prompt engineering evidence: `AI_PROMPT_EVIDENCE.md`.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Project structure

| File | What it does |
|---|---|
| `app.py` | Flask backend — every `/api/*` route |
| `allocation.py` | Mentor allocation engine |
| `ai_helper.py` | AI layer — prompts + provider fallback chain |
| `mysql_sync.py` | MySQL sync (input source; backend still runs on SQLite) |
| `static/index.html` | Frontend |
| `sample_data/` | Demo CSVs + `make_sample.py` generator |
| `mysql_schema.sql` / `mysql_seed_demo.sql` | MySQL table layout + demo seed |
| `tests/` | 33 automated tests |

## Built at

Prompt Manthan 2026, Day 1 — designed and built inside the event's build window, then refined for the Day 2 shortlist demo.

## License

MIT — see [LICENSE](LICENSE).
