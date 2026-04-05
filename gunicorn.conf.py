"""
Gunicorn configuration.

post_worker_init runs once inside each worker process after it starts.
With workers=1 this fires exactly once, which is where we kick off
the APScheduler background thread and register the Telegram webhook.
"""

workers = 1
bind = "0.0.0.0:8080"
timeout = 60


def post_worker_init(worker):  # noqa: ARG001
    from webclock import init_db, register_telegram_webhook, start_scheduler
    init_db()
    register_telegram_webhook()
    start_scheduler()
