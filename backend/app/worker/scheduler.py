"""Background worker.

APScheduler in its own process, plus Postgres advisory locks so that running two
workers (or a worker and a manual API-triggered run) cannot process the same
batch twice. No Redis: the lock and the job history both live in Postgres.

Every job is wrapped so an exception is logged and recorded in `job_runs` but
never kills the scheduler thread.
"""

from __future__ import annotations

import logging
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler

from ..config import settings
from ..db import SessionLocal, advisory_lock, session_scope
from ..llm.fake import build_client
from ..models import JobRun, utcnow
from ..pipeline import runner
from ..seed import loader
from ..sources import registry

log = logging.getLogger(__name__)


def _run_job(name: str, func) -> None:
    """Run `func(db)` under an advisory lock, recording the outcome."""
    db = SessionLocal()
    job = JobRun(job_name=name)
    try:
        with advisory_lock(db, f"job:{name}") as acquired:
            if not acquired:
                log.info("job %s skipped: another worker holds the lock", name)
                return
            db.add(job)
            db.commit()
            detail = func(db)
            job.status = "SUCCESS"
            job.detail = detail if isinstance(detail, dict) else {"result": str(detail)}
            job.finished_at = utcnow()
            db.commit()
    except Exception as exc:
        log.exception("job %s failed", name)
        try:
            db.rollback()
            job.status = "FAILED"
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            job.finished_at = utcnow()
            db.add(job)
            db.commit()
        except Exception:  # pragma: no cover - the DB itself is unhealthy
            log.exception("could not record job failure for %s", name)
    finally:
        db.close()


def poll_job(db) -> dict:
    """Poll only the sources whose interval has elapsed."""
    registry.sync_source_rows(db)
    adapters = registry.due_adapters(db)
    if not adapters:
        return {"polled": 0}
    results = [registry.poll_source(db, adapter) for adapter in adapters]
    db.commit()
    return {"polled": len(results), "results": results}


def pipeline_job(db) -> dict:
    report = runner.run_pipeline(db, limit=200, client=build_client())
    return report.as_dict()


def retry_job(db) -> dict:
    return {"retried": runner.retry_failed_analyses(db, client=build_client())}


def health_job(db) -> dict:
    return {"alerts": len(runner.check_source_health(db))}


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    # The poller runs every minute and decides per source whether that source is
    # due -- so per-source intervals are honoured without a job per source.
    scheduler.add_job(
        lambda: _run_job("poll", poll_job),
        "interval",
        minutes=1,
        id="poll",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_job("pipeline", pipeline_job),
        "interval",
        minutes=settings.pipeline_interval_minutes,
        id="pipeline",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_job("retry_analyses", retry_job),
        "interval",
        minutes=15,
        id="retry_analyses",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_job("source_health", health_job),
        "interval",
        minutes=10,
        id="source_health",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(
        level=logging.INFO,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )
    log.info("worker starting")
    with session_scope() as db:
        loader.seed_reference_data(db)
        loader.seed_user(db)

    scheduler = build_scheduler()
    scheduler.start()

    stopping = False

    def _stop(signum, frame):  # noqa: ARG001
        nonlocal stopping
        log.info("worker received signal %s; shutting down", signum)
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Run one pass immediately so a fresh container has data without waiting.
    _run_job("poll", poll_job)
    _run_job("pipeline", pipeline_job)

    try:
        while not stopping:
            time.sleep(1)
    finally:
        scheduler.shutdown(wait=False)
        log.info("worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
