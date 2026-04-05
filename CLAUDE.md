# NYC Webclock

Automated clock in/out for NYC CityTime (webclock.nyc.gov). Runs on Railway as a Docker container.

## What it does

- Flask web app with a password-protected dashboard (Clock In / Clock Out buttons)
- Sends Telegram notifications at 9am and 5pm ET, Monday–Friday, with inline buttons to clock in/out
- Logs every punch attempt to a Postgres database
- Scheduler and Telegram webhook run inside the same single-worker gunicorn process

## Stack

- **Python / Flask** — web app and API
- **requests** — HTTP automation against webclock.nyc.gov (no browser/Playwright needed)
- **APScheduler** — in-process cron for 9am/5pm notifications
- **Telegram Bot API** — push notifications with inline action buttons
- **Postgres** — punch history (provisioned on Railway)
- **gunicorn** — WSGI server, single worker

## Project structure

```
webclock.py        # Main app — all logic in one file
gunicorn.conf.py   # Gunicorn config + post_worker_init hook (starts scheduler + DB init)
Dockerfile         # python:3.12-slim
requirements.txt   # Dependencies
```

## Railway deployment

**Project:** nyc-webclock
**Service:** nyc-webclock
**URL:** https://nyc-webclock-production.up.railway.app

### Deploy

```bash
railway login
railway up --detach   # deploys current directory, non-blocking
railway logs          # tail logs
```

### Environment variables

Set via Railway dashboard or CLI. Never hardcode in source.

| Variable | Purpose |
|---|---|
| `CITYTIME_USER` | NYC CityTime username |
| `CITYTIME_PASS` | NYC CityTime password |
| `APP_PASSWORD` | Password for the web dashboard |
| `SECRET_KEY` | Flask session signing key (random hex string) |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID (from @userinfobot) |
| `DATABASE_URL` | Set automatically by Railway Postgres service |
| `RAILWAY_PUBLIC_DOMAIN` | Set automatically by Railway — used for Telegram webhook registration |

```bash
# View current variables
railway variables

# Add or update a variable (triggers redeploy)
railway variables --set KEY=value
```

### Linking services

The app is linked to the Postgres service via `DATABASE_URL`. If you ever need to re-link:

```bash
railway service nyc-webclock   # switch CLI context to the app service
```

## Telegram bot

- Created via @BotFather
- Webhook is auto-registered on every container startup using `RAILWAY_PUBLIC_DOMAIN`
- To switch Telegram accounts: update `TELEGRAM_CHAT_ID` env var — no code change needed
- Webhook URL format: `https://<domain>/telegram/webhook/<WEBHOOK_SECRET>`
- `WEBHOOK_SECRET` is auto-generated at startup if not set as an env var

## How the punch flow works

1. Login POST to `https://webclock.nyc.gov/pkmslogin.form`
2. GET `WebClockServlet` to establish session (page body is empty — JS-rendered, that's expected)
3. POST to `SavePunchServlet` with `punchType=TIME-IN` or `TIME-OUT`
4. SSL verification disabled (`verify=False`) — webclock.nyc.gov uses a non-standard NYC gov CA chain that Python can't verify in Docker

## Notifications schedule

- **9:00am ET Mon–Fri** — Clock In reminder
- **5:00pm ET Mon–Fri** — Clock Out reminder
- Scheduler uses APScheduler with `day_of_week="mon-fri"`
- If the container restarts exactly at 9am or 5pm, that notification will be missed (rare)

## Database

Single table `punches`:

```sql
CREATE TABLE punches (
    id         SERIAL PRIMARY KEY,
    punch_type VARCHAR(10) NOT NULL,   -- TIME-IN or TIME-OUT
    success    BOOLEAN NOT NULL,
    message    TEXT,
    punched_at TIMESTAMPTZ DEFAULT NOW()
);
```

Dashboard shows the 10 most recent punches. Table is created automatically on startup if it doesn't exist.

## Testing without punching

The dashboard has a **Testing** section with two safe buttons:

- **Verify Credentials** — logs into CityTime and confirms credentials work, no punch submitted
- **Send Test Notification** — fires a Telegram message immediately with test buttons; tapping them runs credential verify and reports back via Telegram

## Common tasks

### Change notification times
Edit `start_scheduler()` in `webclock.py`, update the `hour` values in the two `CronTrigger` calls, then deploy.

### Change to a different Telegram account
Update `TELEGRAM_CHAT_ID` in Railway env vars. The webhook re-registers automatically on next deploy.

### View punch history
Bottom of the dashboard, or query Postgres directly:
```sql
SELECT * FROM punches ORDER BY punched_at DESC LIMIT 20;
```

### Check if the app is running
```bash
railway logs
```
Look for `Scheduler started — reminders at 9:00am and 5:00pm ET, Monday–Friday.`
