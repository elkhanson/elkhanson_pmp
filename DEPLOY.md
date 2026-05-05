# Deployment Guide

You picked **"Deploy to a cloud server (always on)"**. Here are three options ranked from easiest to most flexible. Pick one — I recommend **Railway** for first-timers.

---

## Before you deploy: get your bot token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Pick a display name (e.g. `My PMP Tutor`)
4. Pick a username ending in `bot` (e.g. `mypmptutor_bot`)
5. **Copy the token** it gives you — looks like `1234567890:AAH...`. Keep it secret.

Optional: get an **Anthropic API key** at <https://console.anthropic.com/> if you want the "Ask Claude" button. Add ~$5 of credit; you'll burn through pennies per question.

---

## Option 1 — Railway (recommended, easiest)

Railway gives you ~$5/month free trial credit which is plenty for an always-on bot.

1. Push the project to GitHub:
   ```bash
   cd pmp_bot
   git init
   git add .
   git commit -m "Initial commit"
   gh repo create pmp-bot --private --source=. --push   # or use the GitHub website
   ```

2. Go to <https://railway.app>, sign in with GitHub.

3. **New Project → Deploy from GitHub repo → pick `pmp-bot`**.

4. Railway auto-detects the `Dockerfile`. Once the build starts, click into the service and go to **Variables** → add:
   - `TELEGRAM_BOT_TOKEN` = (your token)
   - `ANTHROPIC_API_KEY` = (optional, for Ask Claude)

5. Add a **Volume** so SQLite stats persist across redeploys:
   - In the service, click **Volumes** → **+ Add Volume**
   - Mount path: `/app`
   - Save. (This stores `pmp_bot.db` between deploys.)

6. Redeploy. Open Telegram, find your bot, send `/start`. You're live.

**Cost:** ~$0.50–$2/month for an idle bot. Anthropic API is pay-per-use.

---

## Option 2 — Fly.io

Free allowance covers a small always-on bot.

1. Install flyctl: <https://fly.io/docs/hands-on/install-flyctl/>

2. From the project directory:
   ```bash
   fly launch --no-deploy
   ```
   Accept the defaults. It will detect the Dockerfile.

3. Set secrets:
   ```bash
   fly secrets set TELEGRAM_BOT_TOKEN=your_token_here
   fly secrets set ANTHROPIC_API_KEY=your_anthropic_key   # optional
   ```

4. Create a volume for the SQLite DB:
   ```bash
   fly volumes create pmp_data --size 1 --region <your-region>
   ```

5. Edit `fly.toml` to mount the volume — add this section:
   ```toml
   [mounts]
     source = "pmp_data"
     destination = "/app"
   ```

6. Deploy:
   ```bash
   fly deploy
   ```

---

## Option 3 — DigitalOcean / any VPS ($4–6/mo)

If you prefer a "real" server you control.

1. Spin up the cheapest droplet (`s-1vcpu-512mb-10gb`, ~$4/month) on Ubuntu 24.04.

2. SSH in and install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

3. Copy the project up (from your local machine):
   ```bash
   scp -r pmp_bot root@your-droplet-ip:/opt/
   ```

4. On the server, build and run:
   ```bash
   cd /opt/pmp_bot
   docker build -t pmp-bot .
   docker run -d \
     --name pmp-bot \
     --restart unless-stopped \
     -e TELEGRAM_BOT_TOKEN=your_token_here \
     -e ANTHROPIC_API_KEY=your_anthropic_key \
     -v /opt/pmp_bot_data:/app \
     pmp-bot
   ```

5. Check logs: `docker logs -f pmp-bot`. To update later: `git pull`, `docker build -t pmp-bot .`, `docker rm -f pmp-bot`, then re-run the `docker run` command.

---

## Verifying it works

In Telegram, message your bot:

- `/start` → should show the welcome message + main menu buttons
- `🎲 Random Question` → fires a question
- Answer it → see ✅/❌ + the explanation from the answer PDF
- `🤖 Ask Claude about this` (if API key set) → Claude responds to follow-ups
- `/stats` after a few questions → shows your accuracy

If something's off: `docker logs -f pmp-bot` (or Railway's "Deployments" log tab) will show what happened. The most common issue is a missing or wrong `TELEGRAM_BOT_TOKEN`.

---

## Updating the question bank

If you get new mock-test PDFs:

1. Replace the PDFs in `/mnt/user-data/uploads/` (or wherever `parse_pdfs.py` reads from — adjust the `UPLOADS` path).
2. Run `python parse_pdfs.py` locally — produces a fresh `questions.json`.
3. Commit and push (or re-upload the file). Redeploy.
