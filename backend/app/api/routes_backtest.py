"""Backtesting and digest endpoints.
Backtesting is kept on its own router to make the separation from live signals
structural rather than a convention: nothing here writes to `signals`, and the
point-in-time recomputation never persists into the live tables.
"""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import get_db
from ..models import BacktestRun, User
from ..pipeline import digest as digest_mod
from ..pipeline.backtest import BacktestParams, run_backtest, store_run
from ..schemas import BacktestIn

router = APIRouter(prefix="/api", tags=["backtest"])

#: A backtest recomputes every signal point-in-time, so it is seconds of work,
#: not milliseconds. The cap keeps a single request from running unbounded.
MAX_EVENTS = 2000


@router.post("/backtest")
def create_backtest(
    body: BacktestIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    include_observations: bool = Query(True),
) -> dict:
    test_fraction = 1.0 - body.train_fraction - body.validation_fraction
    params = BacktestParams(
        start=body.start,
        end=body.end,
        ticker=body.ticker,
        event_type=body.event_type,
        min_signal=body.min_signal,
        holding_days=body.holding_days,
        sentiment_mode=body.sentiment_mode,
        include_low_confidence=body.include_low_confidence,
        non_overlapping_only=body.non_overlapping_only,
        splits=(body.train_fraction, body.validation_fraction, test_fraction),
    )

    result = run_backtest(db, params, max_events=MAX_EVENTS)
    run = store_run(db, user.id, result)

    payload = result.as_dict()
    payload["run_id"] = run.id
    if not include_observations:
        payload.pop("observations", None)
    return payload


@router.get("/backtest/runs")
def list_backtest_runs(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(25, ge=1, le=100),
) -> dict:
    rows = list(
        db.execute(
            select(BacktestRun)
            .where(BacktestRun.user_id == user.id)
            .order_by(desc(BacktestRun.created_at))
            .limit(limit)
        ).scalars()
    )
    return {
        "items": [
            {
                "id": row.id,
                "params": row.params,
                "results": row.results,
                "sentiment_mode": row.sentiment_mode,
                "llm_contaminated": row.llm_contaminated,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }


@router.get("/backtest/runs/{run_id}")
def get_backtest_run(
    run_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    row = db.get(BacktestRun, run_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return {
        "id": row.id,
        "params": row.params,
        "results": row.results,
        "sentiment_mode": row.sentiment_mode,
        "llm_contaminated": row.llm_contaminated,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/digest/send")
def send_digest_now(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    cadence: str = Query("daily", pattern="^(daily|hourly)$"),
) -> dict:
    """Send this user's digest immediately.

    Idempotent within the period: asking twice in the same local day returns
    `created: false` rather than sending a second copy.
    """
    return digest_mod.send_digest(
        db, user, hours=24 if cadence == "daily" else 1, cadence=cadence
    )


@router.get("/digest/preview")
def preview_digest(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    hours: int = Query(24, ge=1, le=168),
) -> dict:
    """The digest as it would be sent, without sending or recording anything."""
    from ..pipeline import notifications as notif

    digest = notif.build_digest(db, user.id, hours=hours)
    prefs = notif.get_preferences(db, user.id)
    db.commit()
    return {
        "digest": digest,
        "summary_line": digest_mod.digest_summary_line(digest),
        "text": digest_mod.render_digest_text(digest),
        "due_now": digest_mod.is_digest_due(prefs),
        "local_time": digest_mod.local_now(prefs.timezone).isoformat(),
        "digest_hour_local": prefs.digest_hour_local,
        "channels": prefs.channels or ["in_app"],
    }
