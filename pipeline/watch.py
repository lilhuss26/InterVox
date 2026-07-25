"""users.watch() registration and its daily renewal.

Gmail expires a watch after 7 days, so a daily job leaves six days of headroom
for transient failures before notifications actually stop arriving.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from pipeline.auth.gmail_auth import get_gmail_service
from pipeline.config import get_settings
from pipeline.storage import db

log = logging.getLogger(__name__)

WATCH_JOB_ID = "gmail_watch"
BOOT_RETRY_JOB_ID = "gmail_watch_boot_retry"
BOOT_RETRY_MINUTES = 5


def start_watch(service=None, settings=None) -> int:
    settings = settings or get_settings()
    settings.require("PUBSUB_TOPIC")
    service = service or get_gmail_service(settings)

    response = (
        service.users()
        .watch(
            userId="me",
            body={
                "topicName": settings.pubsub_topic,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "include",
            },
        )
        .execute()
    )

    history_id = int(response["historyId"])
    # Seed only if unset: clobbering a live cursor on every restart would
    # silently skip everything that arrived since the last notification.
    if db.set_state_if_absent("last_history_id", str(history_id), settings):
        log.info("seeded history cursor at %s", history_id)
    log.info("watch registered, expires %s", response.get("expiration"))
    return history_id


def register_watch(scheduler=None, settings=None) -> BackgroundScheduler:
    """Register the watch now; on failure, retry in the background. Never raises.

    Registration is a network call to Gmail (users.watch), and Gmail rate-limits
    it: a 429 on boot used to propagate out of the FastAPI lifespan, kill uvicorn,
    and send the container into a restart loop that re-hit the same limit every
    few seconds. Here a boot-time failure is only logged; a short-interval job
    keeps retrying until it succeeds, then cancels itself. The daily renewal
    (schedule_watch_renewal) is a separate, longer safety net.
    """
    settings = settings or get_settings()
    scheduler = scheduler or BackgroundScheduler()
    if not scheduler.running:
        scheduler.start()

    try:
        start_watch(settings=settings)
        return scheduler
    except Exception:
        log.warning(
            "initial gmail watch failed; retrying every %d min",
            BOOT_RETRY_MINUTES,
            exc_info=True,
        )

    def _retry():
        try:
            start_watch(settings=settings)
        except Exception:
            log.warning("gmail watch retry failed; will keep trying", exc_info=True)
            return
        log.info("gmail watch registered on retry")
        scheduler.remove_job(BOOT_RETRY_JOB_ID)

    scheduler.add_job(
        _retry,
        "interval",
        minutes=BOOT_RETRY_MINUTES,
        id=BOOT_RETRY_JOB_ID,
        replace_existing=True,
        max_instances=1,
    )
    return scheduler


def schedule_watch_renewal(scheduler=None, settings=None) -> BackgroundScheduler:
    settings = settings or get_settings()
    scheduler = scheduler or BackgroundScheduler()
    scheduler.add_job(
        start_watch,
        "interval",
        hours=24,
        id=WATCH_JOB_ID,
        replace_existing=True,
        max_instances=1,
        kwargs={"settings": settings},
    )
    if not scheduler.running:
        scheduler.start()
    return scheduler
