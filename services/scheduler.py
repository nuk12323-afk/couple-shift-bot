import asyncio
import logging
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlmodel import Session, select
from config import TIMEZONE
from database import engine
from models import SystemSetting
from services.line_service import send_daily_reminder

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))

def get_setting(key: str, default: str = "") -> str:
    with Session(engine) as session:
        setting = session.exec(select(SystemSetting).where(SystemSetting.key == key)).first()
        return setting.value if setting else default

def setup_scheduler():
    """Configure and start the scheduled jobs (both tomorrow evening and today morning)"""
    # 1. Tomorrow Shift Reminder (Evening summary)
    notify_tomorrow_enabled = get_setting("notify_tomorrow_enabled", "true").lower() == "true"
    notify_tomorrow_time = get_setting("notify_tomorrow_time", "20:00")

    # 2. Today Shift Reminder (Morning alert)
    notify_today_enabled = get_setting("notify_today_enabled", "true").lower() == "true"
    notify_today_time = get_setting("notify_today_time", "06:00")

    # Clear previous jobs
    for job_id in ["job_tomorrow_reminder", "job_today_reminder", "daily_shift_reminder"]:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    # Schedule Tomorrow Reminder (Evening)
    if notify_tomorrow_enabled:
        try:
            h_tom, m_tom = [int(x) for x in notify_tomorrow_time.split(":")]
            scheduler.add_job(
                send_daily_reminder,
                kwargs={"mode": "tomorrow"},
                trigger=CronTrigger(hour=h_tom, minute=m_tom, timezone=pytz.timezone(TIMEZONE)),
                id="job_tomorrow_reminder",
                name=f"Tomorrow Shift Reminder at {notify_tomorrow_time}",
                replace_existing=True
            )
            logger.info(f"✅ Scheduled Tomorrow Reminder at {h_tom:02d}:{m_tom:02d} ({TIMEZONE})")
        except Exception as e:
            logger.error(f"Failed to schedule tomorrow reminder: {e}")

    # Schedule Today Reminder (Morning)
    if notify_today_enabled:
        try:
            h_tod, m_tod = [int(x) for x in notify_today_time.split(":")]
            scheduler.add_job(
                send_daily_reminder,
                kwargs={"mode": "today"},
                trigger=CronTrigger(hour=h_tod, minute=m_tod, timezone=pytz.timezone(TIMEZONE)),
                id="job_today_reminder",
                name=f"Today Shift Reminder at {notify_today_time}",
                replace_existing=True
            )
            logger.info(f"✅ Scheduled Today Reminder at {h_tod:02d}:{m_tod:02d} ({TIMEZONE})")
        except Exception as e:
            logger.error(f"Failed to schedule today reminder: {e}")

    if not scheduler.running:
        try:
            asyncio.get_running_loop()
            scheduler.start()
            logger.info("APScheduler started.")
        except RuntimeError:
            logger.info("Scheduler configured. Waiting for event loop to start.")

def update_reminder_schedule_all(
    tomorrow_time: str,
    tomorrow_enabled: bool,
    today_time: str,
    today_enabled: bool,
    on_upload_enabled: bool
):
    """Dynamically update all schedules from settings"""
    settings_to_update = {
        "notify_tomorrow_time": tomorrow_time,
        "notify_tomorrow_enabled": "true" if tomorrow_enabled else "false",
        "notify_today_time": today_time,
        "notify_today_enabled": "true" if today_enabled else "false",
        "notify_on_upload_enabled": "true" if on_upload_enabled else "false",
    }
    with Session(engine) as session:
        for k, v in settings_to_update.items():
            entry = session.exec(select(SystemSetting).where(SystemSetting.key == k)).first()
            if entry:
                entry.value = v
            else:
                session.add(SystemSetting(key=k, value=v))
        session.commit()

    setup_scheduler()

def update_reminder_schedule(new_time: str, enabled: bool = True):
    update_reminder_schedule_all(new_time, enabled, "06:00", True, True)

    setup_scheduler()
