"""
NYC CityTime Web Clock - Automated Punch Script

Logs into webclock.nyc.gov and submits a time punch (clock in or clock out).
Runs on Railway. Features:
  - HTML login form with 90-day session cookie
  - Telegram bot notifications at 9am and 5pm ET with inline Clock In/Out buttons
  - APScheduler cron jobs for timed notifications
  - Webhook auto-registration on startup
"""

import os
import re
import logging
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import psycopg2
import psycopg2.extras
from flask import (
    Flask, jsonify, request as flask_request,
    Response, session, redirect, url_for, render_template_string
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CITYTIME_USER = os.environ.get("CITYTIME_USER", "")
CITYTIME_PASS = os.environ.get("CITYTIME_PASS", "")

APP_PASSWORD  = os.environ.get("APP_PASSWORD", "changeme")
SECRET_KEY    = os.environ.get("SECRET_KEY", secrets.token_hex(32))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Used to secure the Telegram webhook callback URL
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_hex(16))

PORT = int(os.environ.get("PORT", 8080))

# Railway injects this automatically in production
PUBLIC_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")

BASE_URL        = "https://webclock.nyc.gov"
LOGIN_URL       = f"{BASE_URL}/pkmslogin.form"
CLOCK_PAGE_URL  = f"{BASE_URL}/ctclock/WebClock/WebClockServlet"
SAVE_PUNCH_URL  = f"{BASE_URL}/ctclock/WebClock/SavePunchServlet"

VALID_PUNCH_TYPES = {"TIME-IN", "TIME-OUT"}

DATABASE_URL = os.environ.get("DATABASE_URL", "")

ET = pytz.timezone("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("webclock")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.permanent_session_lifetime = timedelta(days=90)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — punch history will not be recorded.")
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS punches (
                        id         SERIAL PRIMARY KEY,
                        punch_type VARCHAR(10)  NOT NULL,
                        success    BOOLEAN      NOT NULL,
                        message    TEXT,
                        punched_at TIMESTAMPTZ  DEFAULT NOW()
                    )
                """)
        log.info("Database initialised.")
    except Exception as exc:
        log.error("Failed to initialise database: %s", exc)


def record_punch(punch_type: str, message: str):
    if not DATABASE_URL:
        return
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO punches (punch_type, success, message) VALUES (%s, %s, %s)",
                    (punch_type, True, message),
                )
    except Exception as exc:
        log.error("Failed to record punch: %s", exc)


def get_recent_punches(limit: int = 10) -> list:
    if not DATABASE_URL:
        return []
    try:
        with get_db() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT punch_type, success, message,
                           punched_at AT TIME ZONE 'America/New_York' AS punched_at
                    FROM punches
                    ORDER BY punched_at DESC
                    LIMIT %s
                """, (limit,))
                return [dict(row) for row in cur.fetchall()]
    except Exception as exc:
        log.error("Failed to fetch punch history: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# HTML Templates
# ---------------------------------------------------------------------------

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Clock — Login</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f5; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; padding: 1rem; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1);
          padding: 2rem; max-width: 360px; width: 100%; }
  h1 { font-size: 1.4rem; margin-bottom: 1.5rem; }
  label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 0.25rem; color: #555; }
  input[type="password"] {
    width: 100%; padding: 0.65rem 0.75rem; border: 1px solid #ddd;
    border-radius: 6px; font-size: 0.95rem; margin-bottom: 1rem; }
  input:focus { outline: none; border-color: #4a90d9; box-shadow: 0 0 0 2px rgba(74,144,217,0.2); }
  button { width: 100%; padding: 0.75rem; border: none; border-radius: 8px;
           background: #4a90d9; color: #fff; font-size: 1rem; font-weight: 600;
           cursor: pointer; }
  button:hover { background: #357abd; }
  .error { background: #fee2e2; color: #991b1b; padding: 0.6rem 0.75rem;
           border-radius: 6px; font-size: 0.9rem; margin-bottom: 1rem; }
</style>
</head>
<body>
<div class="card">
  <h1>NYC Web Clock</h1>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autofocus placeholder="Enter password">
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""


DASHBOARD_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Web Clock</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f5; color: #333; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; padding: 1rem; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,0.1);
          padding: 2rem; max-width: 400px; width: 100%; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  .time { color: #666; font-size: 0.9rem; margin-bottom: 1.75rem; }
  .buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
  button { padding: 0.85rem; border: none; border-radius: 8px; font-size: 1rem;
           font-weight: 600; cursor: pointer; transition: opacity 0.15s; }
  button:hover { opacity: 0.85; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-in  { background: #22c55e; color: #fff; }
  .btn-out { background: #ef4444; color: #fff; }
  #result { margin-top: 1.25rem; padding: 0.75rem; border-radius: 6px;
            font-size: 0.9rem; display: none; }
  .result-ok  { background: #dcfce7; color: #166534; }
  .result-err { background: #fee2e2; color: #991b1b; }
  .divider { border: none; border-top: 1px solid #eee; margin: 1.5rem 0 1rem; }
  .section-label { font-size: 0.75rem; font-weight: 600; color: #aaa;
                   text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.75rem; }
  .btn-test-creds  { background: #e0f2fe; color: #0369a1; }
  .btn-test-notify { background: #f3e8ff; color: #7e22ce; }
  #history { font-size: 0.85rem; color: #555; }
  .punch-row { display: flex; justify-content: space-between; align-items: center;
               padding: 0.4rem 0; border-bottom: 1px solid #f0f0f0; }
  .punch-row:last-child { border-bottom: none; }
  .punch-badge { font-weight: 600; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.78rem; }
  .badge-in  { background: #dcfce7; color: #166534; }
  .badge-out { background: #fee2e2; color: #991b1b; }
  .badge-fail { background: #fef9c3; color: #854d0e; }
  .punch-time { color: #999; font-size: 0.8rem; }
  .logout { display: block; text-align: right; margin-top: 1.25rem;
            font-size: 0.8rem; color: #999; text-decoration: none; }
  .logout:hover { color: #555; }
</style>
</head>
<body>
<div class="card">
  <h1>NYC Web Clock</h1>
  <div class="time" id="clock"></div>

  <div class="buttons">
    <button class="btn-in"  onclick="punch('TIME-IN')">Clock In</button>
    <button class="btn-out" onclick="punch('TIME-OUT')">Clock Out</button>
  </div>

  <div id="result"></div>

  <hr class="divider">
  <div class="section-label">Testing</div>
  <div class="buttons">
    <button class="btn-test-creds"  onclick="testAction('/api/verify',       this)">Verify Credentials</button>
    <button class="btn-test-notify" onclick="testAction('/api/test-notify',  this)">Send Test Notification</button>
  </div>
  <div id="test-result" style="margin-top:1rem; padding:0.75rem; border-radius:6px;
       font-size:0.9rem; display:none;"></div>

  <hr class="divider">
  <div class="section-label">Recent Punches</div>
  <div id="history">Loading…</div>

  <a class="logout" href="/logout">Sign out</a>
</div>

<script>
  function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleString('en-US', {
      timeZone: 'America/New_York', weekday: 'long', year: 'numeric',
      month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }
  updateClock();
  setInterval(updateClock, 1000);

  async function punch(type) {
    const result  = document.getElementById('result');
    const buttons = document.querySelectorAll('button');

    buttons.forEach(b => b.disabled = true);
    result.style.display = 'block';
    result.className = 'result-ok';
    result.textContent = 'Submitting ' + type + ' punch…';

    try {
      const resp = await fetch('/api/punch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: type })
      });
      const data = await resp.json();
      result.className = data.success ? 'result-ok' : 'result-err';
      result.textContent = data.message;
      if (data.success) loadHistory();
    } catch (err) {
      result.className = 'result-err';
      result.textContent = 'Network error: ' + err.message;
    } finally {
      buttons.forEach(b => b.disabled = false);
    }
  }

  async function loadHistory() {
    const el = document.getElementById('history');
    try {
      const resp = await fetch('/api/history');
      const data = await resp.json();
      if (!data.length) { el.textContent = 'No punches recorded yet.'; return; }
      el.innerHTML = data.map(p => {
        const type  = p.punch_type === 'TIME-IN' ? 'Clock In' : 'Clock Out';
        const badge = p.success
          ? (p.punch_type === 'TIME-IN' ? 'badge-in' : 'badge-out')
          : 'badge-fail';
        const label = p.success ? type : type + ' (failed)';
        const ts    = new Date(p.punched_at).toLocaleString('en-US', {
          timeZone: 'America/New_York', month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit'
        });
        return `<div class="punch-row">
          <span class="punch-badge ${badge}">${label}</span>
          <span class="punch-time">${ts}</span>
        </div>`;
      }).join('');
    } catch { el.textContent = 'Could not load history.'; }
  }
  loadHistory();

  async function testAction(url, btn) {
    const result  = document.getElementById('test-result');
    const buttons = document.querySelectorAll('button');

    buttons.forEach(b => b.disabled = true);
    result.style.display = 'block';
    result.style.background = '#f0f9ff';
    result.style.color = '#0369a1';
    result.textContent = 'Running…';

    try {
      const resp = await fetch(url, { method: 'POST' });
      const data = await resp.json();
      result.style.background = data.success ? '#dcfce7' : '#fee2e2';
      result.style.color      = data.success ? '#166534' : '#991b1b';
      result.textContent = data.message;
    } catch (err) {
      result.style.background = '#fee2e2';
      result.style.color = '#991b1b';
      result.textContent = 'Network error: ' + err.message;
    } finally {
      buttons.forEach(b => b.disabled = false);
    }
  }
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Core punch logic
# ---------------------------------------------------------------------------

def do_verify() -> dict:
    """
    Test CityTime credentials by logging in and loading the clock page,
    but stop before submitting any punch.
    """
    if not CITYTIME_USER or not CITYTIME_PASS:
        return {
            "success": False,
            "message": "Missing CityTime credentials. Set CITYTIME_USER and CITYTIME_PASS env vars.",
            "timestamp": _now(),
        }

    session_r = requests.Session()
    session_r.verify = False  # webclock.nyc.gov uses a non-standard NYC gov CA chain
    session_r.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    })

    log.info("Verifying credentials for %s …", CITYTIME_USER)
    try:
        session_r.post(
            LOGIN_URL,
            data={"username": CITYTIME_USER, "password": CITYTIME_PASS, "login-form-type": "pwd"},
            allow_redirects=True,
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"success": False, "message": f"Login request failed: {exc}", "timestamp": _now()}

    try:
        clock_resp = session_r.get(CLOCK_PAGE_URL, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        return {"success": False, "message": f"Failed to load clock page: {exc}", "timestamp": _now()}

    if "pkmslogin" in clock_resp.url.lower() or "login" in clock_resp.url.lower():
        return {
            "success": False,
            "message": "Credentials rejected — redirected back to login page. Check CITYTIME_USER and CITYTIME_PASS.",
            "timestamp": _now(),
        }

    logged_time = _extract_field(clock_resp.text, "loggedTime")
    log.info("Credentials verified for %s.", CITYTIME_USER)

    return {
        "success": True,
        "message": "Credentials verified. Login successful — no punch submitted.",
        "timestamp": _now(),
    }


def do_punch(punch_type: str) -> dict:
    """
    Perform a full login + punch cycle using env var credentials.
    Returns a dict with keys: success (bool), message (str), timestamp (str).

    Success is indicated by a 302 redirect whose Location header contains
    the confirmation message from the server (e.g. "has been recorded").
    """
    punch_type = punch_type.upper()
    if punch_type not in VALID_PUNCH_TYPES:
        return {
            "success": False,
            "message": f"Invalid punch type '{punch_type}'. Must be one of: {', '.join(sorted(VALID_PUNCH_TYPES))}",
            "timestamp": _now(),
        }

    if not CITYTIME_USER or not CITYTIME_PASS:
        return {
            "success": False,
            "message": "Missing CityTime credentials. Set CITYTIME_USER and CITYTIME_PASS env vars.",
            "timestamp": _now(),
        }

    session_r = requests.Session()
    session_r.verify = False  # webclock.nyc.gov uses a non-standard NYC gov CA chain
    session_r.headers.update({
        "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
    })

    # Step 1: Login
    log.info("Logging in as %s …", CITYTIME_USER)
    try:
        login_resp = session_r.post(
            LOGIN_URL,
            data={
                "username": CITYTIME_USER,
                "password": CITYTIME_PASS,
                "login-form-type": "pwd",
            },
            allow_redirects=True,
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"success": False, "message": f"Login request failed: {exc}", "timestamp": _now()}

    log.info("Login response: status=%s url=%s", login_resp.status_code, login_resp.url)
    log.info("Login cookies: %s", dict(session_r.cookies))

    # Step 2: Load clock page (establishes session state, page is JS-rendered so body is empty)
    log.info("Loading Web Clock page …")
    try:
        clock_resp = session_r.get(CLOCK_PAGE_URL, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        return {"success": False, "message": f"Failed to load clock page: {exc}", "timestamp": _now()}

    log.info("Clock page: status=%s url=%s content-length=%s", clock_resp.status_code, clock_resp.url, len(clock_resp.content))
    log.info("Clock page cookies: %s", dict(session_r.cookies))

    if "pkmslogin" in clock_resp.url.lower() or "login" in clock_resp.url.lower():
        return {
            "success": False,
            "message": "Login failed — redirected back to login page. Check your CityTime credentials.",
            "timestamp": _now(),
        }

    logged_time = _extract_field(clock_resp.text, "loggedTime")
    token       = _extract_field(clock_resp.text, "X-TOKEN-CTWC")
    log.info("Extracted: loggedTime=%r token=%r", logged_time, token)

    # Step 3: Submit punch
    # Use allow_redirects=False — a successful punch returns a 302 whose
    # Location header contains the server's confirmation message.
    # Manually set IV_JCT — IBM Tivoli junction cookie, static value, not set by our login flow
    session_r.cookies.set("IV_JCT", "%2Fctclock", domain="webclock.nyc.gov")

    punch_payload = {
        "X-TOKEN-CTWC": token or "null",
        "actionType":   "submit",
        "loggedTime":   datetime.now(ET).strftime("%m/%d/%Y %H:%M"),
        "punchType":    punch_type,
    }
    log.info("Submitting %s punch with payload: %s", punch_type, punch_payload)
    try:
        punch_resp = session_r.post(
            SAVE_PUNCH_URL,
            data=punch_payload,
            headers={
                "Referer": CLOCK_PAGE_URL,
                "Origin":  BASE_URL,
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            },
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException as exc:
        return {"success": False, "message": f"Punch request failed: {exc}", "timestamp": _now()}

    log.info("Punch response: status=%s", punch_resp.status_code)
    log.info("Punch response headers: %s", dict(punch_resp.headers))
    log.info("Punch response body: %r", punch_resp.text[:500])

    # Success = 302 with a Location header containing the server's confirmation
    if punch_resp.status_code == 302:
        location = punch_resp.headers.get("location", "")
        log.info("Punch redirect location: %s", location)
        if "recorded" in location.lower() or "punch" in location.lower():
            # Extract the human-readable message from the wcMsg query param
            import urllib.parse
            parsed   = urllib.parse.urlparse(location)
            wc_msg   = urllib.parse.parse_qs(parsed.query).get("wcMsg", [""])[0]
            message  = wc_msg if wc_msg else f"{punch_type} punch recorded successfully."
            result = {"success": True, "message": message, "timestamp": _now(), "punch_type": punch_type}
            record_punch(punch_type, message)
            return result
        else:
            # 302 to somewhere unexpected (e.g. logged out without confirmation)
            return {"success": False, "message": f"Unexpected redirect after punch: {location}", "timestamp": _now()}

    # 200 with empty body = server silently rejected (missing token/session state)
    if punch_resp.status_code == 200 and len(punch_resp.content) == 0:
        return {"success": False, "message": "Punch silently rejected by server (200 + empty body). Session or token issue.", "timestamp": _now()}

    # Any other response — log it and treat as failure
    return {
        "success": False,
        "message": f"Unexpected response: status={punch_resp.status_code} body={punch_resp.text[:200]}",
        "timestamp": _now(),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")


def _extract_field(html: str, field_name: str) -> str:
    for pattern in [
        rf'name="{field_name}"[^>]*value="([^"]*)"',
        rf'value="([^"]*)"[^>]*name="{field_name}"',
    ]:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


def _extract_error(html: str) -> str:
    match = re.search(r'class="[^"]*error[^"]*"[^>]*>([^<]+)<', html, re.IGNORECASE)
    return match.group(1).strip() if match else "(could not parse error detail)"


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def tg_send(text: str, reply_markup: dict | None = None) -> bool:
    """Send a message to the configured chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping notification.")
        return False
    payload: dict = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def tg_answer_callback(callback_query_id: str, text: str = "") -> None:
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5,
        )
    except Exception:
        pass


def punch_keyboard(action: str) -> dict:
    """
    Returns an inline keyboard with a single action button + Skip.
    action: 'in' or 'out'
    """
    label     = "Clock In" if action == "in" else "Clock Out"
    punch_type = "TIME-IN" if action == "in" else "TIME-OUT"
    return {
        "inline_keyboard": [[
            {"text": label,  "callback_data": f"punch:{punch_type}:{WEBHOOK_SECRET}"},
            {"text": "Skip", "callback_data": "skip"},
        ]]
    }


def register_telegram_webhook() -> None:
    """Register the Telegram webhook URL with the bot API."""
    if not TELEGRAM_BOT_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN not set — skipping webhook registration.")
        return
    if not PUBLIC_URL:
        log.warning("RAILWAY_PUBLIC_DOMAIN not set — cannot register Telegram webhook.")
        return

    webhook_url = f"https://{PUBLIC_URL}/telegram/webhook/{WEBHOOK_SECRET}"
    try:
        r = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": webhook_url},
            timeout=10,
        )
        data = r.json()
        if data.get("ok"):
            log.info("Telegram webhook registered: %s", webhook_url)
        else:
            log.error("Telegram webhook registration failed: %s", data)
    except Exception as exc:
        log.error("Could not register Telegram webhook: %s", exc)


# ---------------------------------------------------------------------------
# Scheduler — 9am and 5pm ET notifications
# ---------------------------------------------------------------------------

def notify_clock_in() -> None:
    log.info("Sending 9am Clock In reminder …")
    tg_send(
        "Good morning! Time to <b>clock in</b>.",
        reply_markup=punch_keyboard("in"),
    )


def notify_clock_out() -> None:
    log.info("Sending 5pm Clock Out reminder …")
    tg_send(
        "End of day — time to <b>clock out</b>.",
        reply_markup=punch_keyboard("out"),
    )


def start_scheduler() -> None:
    scheduler = BackgroundScheduler(timezone=ET)
    scheduler.add_job(notify_clock_in,  CronTrigger(day_of_week="mon-fri", hour=9,  minute=0, timezone=ET))
    scheduler.add_job(notify_clock_out, CronTrigger(day_of_week="mon-fri", hour=17, minute=15, timezone=ET))
    scheduler.start()
    log.info("Scheduler started — reminders at 9:00am and 5:00pm ET, Monday–Friday.")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if flask_request.method == "POST":
        password = flask_request.form.get("password", "")
        if password == APP_PASSWORD:
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template_string(LOGIN_PAGE, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template_string(DASHBOARD_PAGE)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": _now()})


@app.route("/api/verify", methods=["POST"])
@login_required
def api_verify():
    """Test CityTime credentials without submitting a punch."""
    result = do_verify()
    return jsonify(result), 200 if result["success"] else 500


@app.route("/api/test-notify", methods=["POST"])
@login_required
def api_test_notify():
    """Send a Telegram test message with dummy buttons (no punch on tap)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return jsonify({
            "success": False,
            "message": "Telegram not configured yet. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.",
            "timestamp": _now(),
        }), 500

    keyboard = {
        "inline_keyboard": [[
            {"text": "Clock In (test)",  "callback_data": "test:in"},
            {"text": "Clock Out (test)", "callback_data": "test:out"},
        ]]
    }
    sent = tg_send(
        "🧪 <b>Test notification</b>\nThis is a dry run — tapping a button will <i>not</i> submit a punch.",
        reply_markup=keyboard,
    )
    if sent:
        return jsonify({"success": True,  "message": "Test notification sent! Check Telegram.", "timestamp": _now()})
    return jsonify({"success": False, "message": "Failed to send Telegram message. Check bot token and chat ID.", "timestamp": _now()}), 500


@app.route("/api/history", methods=["GET"])
@login_required
def api_history():
    rows = get_recent_punches(10)
    # Make punched_at JSON-serialisable
    for row in rows:
        if row.get("punched_at"):
            row["punched_at"] = row["punched_at"].isoformat()
    return jsonify(rows)


@app.route("/api/punch", methods=["POST"])
@login_required
def api_punch():
    """Called by the web dashboard buttons."""
    data       = flask_request.get_json(silent=True) or {}
    punch_type = data.get("type", "").upper()
    if not punch_type:
        hour       = datetime.now(ET).hour
        punch_type = "TIME-IN" if hour < 12 else "TIME-OUT"
    result      = do_punch(punch_type)
    status_code = 200 if result["success"] else 500
    return jsonify(result), status_code


@app.route("/telegram/webhook/<secret>", methods=["POST"])
def telegram_webhook(secret: str):
    """Receives button taps from Telegram."""
    if not secrets.compare_digest(secret, WEBHOOK_SECRET):
        return "", 403

    update = flask_request.get_json(silent=True) or {}
    callback = update.get("callback_query")

    if not callback:
        return "", 200

    callback_id = callback.get("id", "")
    data        = callback.get("data", "")

    if data == "skip":
        tg_answer_callback(callback_id, "Skipped.")
        return "", 200

    if data in ("test:in", "test:out"):
        label = "Clock In" if data == "test:in" else "Clock Out"
        tg_answer_callback(callback_id, "Verifying credentials…")
        result = do_verify()
        if result["success"]:
            tg_send(f"🧪 <b>{label} (test)</b> — button working\n✅ {result['message']}")
        else:
            tg_send(f"🧪 <b>{label} (test)</b> — button working\n❌ Credential check failed: {result['message']}")
        return "", 200

    if data.startswith("punch:"):
        parts = data.split(":")
        if len(parts) != 3 or not secrets.compare_digest(parts[2], WEBHOOK_SECRET):
            tg_answer_callback(callback_id, "Invalid request.")
            return "", 200

        punch_type = parts[1]
        tg_answer_callback(callback_id, f"Submitting {punch_type}…")

        result = do_punch(punch_type)
        status_emoji = "✅" if result["success"] else "❌"
        tg_send(f"{status_emoji} {result['message']}")
        return "", 200

    return "", 200


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    register_telegram_webhook()
    start_scheduler()
    log.info("Starting Web Clock server on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
