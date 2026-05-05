# PMP Prep Telegram Bot

A Telegram bot that quizzes you on **1,238 PMP exam questions** parsed from 7 mock-test PDFs. Powered by Claude for follow-up explanations.

## Features

- **🎲 Random questions** across all 7 mock tests (1,238 questions parsed)
- **Single-answer questions** rendered as Telegram native quiz polls (clean tap UX)
- **Multi-answer questions** ("Choose 2/3") rendered as toggle-able inline buttons
- **Full explanation** shown after every answer, copied verbatim from the answer-key PDFs
- **🤖 Ask Claude** button — chat with Claude about any question's concept
- **🔖 Mark for review** — flag tough questions, drill them later
- **⏱ Exam mode** — 90-second timer per question, auto-graded on timeout
- **📊 Stats** — overall accuracy + per-test breakdown
- **SQLite persistence** — stats survive restarts (no external DB needed)

## Files

```
pmp_bot/
├── bot.py            # Telegram bot entrypoint
├── database.py       # SQLite layer (stats, marked, active question)
├── parse_pdfs.py     # one-time PDF parser → questions.json
├── questions.json    # parsed question bank (1,238 questions)
├── requirements.txt
├── Dockerfile
├── .env.example
└── DEPLOY.md         # step-by-step deployment guide
```

## Quick start (local test)

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and paste your TELEGRAM_BOT_TOKEN (and optionally ANTHROPIC_API_KEY)
export $(cat .env | xargs)
python bot.py
```

For cloud deployment (Railway / Fly.io / DigitalOcean), see `DEPLOY.md`.

## Re-parsing the PDFs

If you ever update the source PDFs, just put them in `/mnt/user-data/uploads` (or edit the `UPLOADS` path in `parse_pdfs.py`) and run:

```bash
python parse_pdfs.py
```

This will regenerate `questions.json`. The parser handles two answer-key formats automatically and skips drag-and-drop slides (which can't be made into multiple-choice).

## Customizing

- **Timer length:** edit `EXAM_TIMER_SECONDS` in `bot.py` (default 90).
- **Claude model:** set `CLAUDE_MODEL` in `.env`. Defaults to `claude-sonnet-4-6`. Use `claude-haiku-4-5` for cheaper/faster, or `claude-opus-4-7` for highest quality.
