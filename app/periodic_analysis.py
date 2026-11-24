"""
Periodic background job: every 10 minutes, fetch the latest code snapshot
for each active student and call the HuggingFace Inference API (UniXcoder)
to get a transformer-quality AI probability.  The result is stored in
CodeSnapshot.ai_score (0-100 integer) so the teacher dashboard can display
it without loading transformers into Render's 512MB RAM.

Scheduler is started from main.py at app startup.
"""
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func as sqlfunc

from app.database import SessionLocal
from app.models import CodeSnapshot
from app.ml.model_loader import call_hf_inference_api, UNIXCODER_HF_REPO
from app.ml.droid_detector import droid_ai_probability

logger = logging.getLogger(__name__)

ANALYSIS_INTERVAL_MINUTES = 10
ACTIVE_WINDOW_MINUTES = 15


def _run_periodic_analysis():
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=ACTIVE_WINDOW_MINUTES)

        subq = (
            db.query(
                CodeSnapshot.student_id,
                sqlfunc.max(CodeSnapshot.id).label("max_id"),
            )
            .filter(CodeSnapshot.timestamp >= cutoff)
            .group_by(CodeSnapshot.student_id)
            .subquery()
        )

        snapshots = (
            db.query(CodeSnapshot)
            .join(subq, CodeSnapshot.id == subq.c.max_id)
            .all()
        )

        if not snapshots:
            logger.debug("Periodic analysis: no active snapshots found")
            return

        logger.info("Periodic analysis: processing %d active students", len(snapshots))

        for snap in snapshots:
            if not snap.code or len(snap.code.strip()) < 10:
                continue
            try:
                prob = droid_ai_probability(snap.code)  # None unless DROID_MODEL_DIR is set
                if prob is None:
                    prob = call_hf_inference_api(UNIXCODER_HF_REPO, snap.code)
                snap.ai_score = int(round(prob * 100))
                snap.ai_score_updated_at = datetime.now(timezone.utc)
                db.add(snap)
                logger.debug(
                    "Periodic: student=%s snapshot=%d score=%d",
                    snap.student_id, snap.id, snap.ai_score,
                )
            except Exception:
                logger.warning(
                    "Periodic: failed for student %s", snap.student_id, exc_info=True
                )

        db.commit()
        logger.info("Periodic analysis: committed %d scores", len(snapshots))

    except Exception:
        logger.error("Periodic analysis job crashed", exc_info=True)
        db.rollback()
    finally:
        db.close()


_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        _run_periodic_analysis,
        trigger="interval",
        minutes=ANALYSIS_INTERVAL_MINUTES,
        id="periodic_ai_analysis",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    from app.rooms import purge_expired_datasets
    _scheduler.add_job(
        purge_expired_datasets,
        trigger="interval",
        hours=6,
        id="purge_expired_datasets",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    _scheduler.start()
    logger.info(
        "Periodic AI analysis scheduler started (interval=%d min)",
        ANALYSIS_INTERVAL_MINUTES,
    )


def stop_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Periodic AI analysis scheduler stopped")
