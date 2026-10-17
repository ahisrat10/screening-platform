"""
Admin dataset exports (JSON).

  GET /admin/export/dataset       labeled sessions only
  GET /admin/export/all-sessions  every session (label = null when unlabeled)

A "session" is one StudentSession row (the unit teachers label). Anything that
can't be found in the database is returned as null rather than raising.
"""
import json
import logging
import math
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import auth, models
from .database import get_db

logger = logging.getLogger("screening.export")
router = APIRouter(tags=["export"])

EXPORT_VERSION = "1.0"

# ── rate limit: 10 exports / hour / admin ──────────────────────
# In-memory (per process): resets on restart and isn't shared between workers,
# same limitation as the OTP limiter.
_EXPORT_LIMIT = 10
_EXPORT_WINDOW_SEC = 3600
_export_calls: dict = {}


def _check_rate_limit(admin_id: str):
    now = time.time()
    calls = [t for t in _export_calls.get(admin_id, []) if now - t < _EXPORT_WINDOW_SEC]
    if len(calls) >= _EXPORT_LIMIT:
        retry = int(_EXPORT_WINDOW_SEC - (now - calls[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Export limit reached ({_EXPORT_LIMIT} per hour). Try again in {retry // 60 + 1} minutes.",
            headers={"Retry-After": str(retry)},
        )
    calls.append(now)
    _export_calls[admin_id] = calls


# ── small null-safe helpers ────────────────────────────────────
def _iso(dt) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _clean(value):
    """Make a value JSON-safe: datetimes -> ISO, NaN/inf -> null."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _raw(beh) -> dict:
    raw = getattr(beh, "raw_events", None)
    return raw if isinstance(raw, dict) else {}


# export key -> (key in raw_events, BehavioralData column fallback)
BEHAVIORAL_FIELDS = {
    "total_keystrokes":           ("total_keystrokes", "total_keystrokes"),
    "backspace_frequency":        ("backspace_frequency", "backspace_frequency"),
    "keystroke_rhythm_stddev_ms": ("keystroke_rhythm_stddev_ms", "keystroke_rhythm_std"),
    "avg_time_between_keys_ms":   ("avg_time_between_keys_ms", "avg_time_between_keys"),
    "keys_per_minute_avg":        ("keys_per_minute_avg", "keys_per_minute"),
    "keys_per_minute_series":     ("keys_per_minute_series", None),
    "typing_pause_count":         ("typing_pause_count", "typing_pause_count"),
    "typing_pause_avg_ms":        ("typing_pause_avg_ms", None),
    "screen_freeze_events":       ("screen_freeze_events", "screen_freeze_count"),
    "sudden_code_jump_count":     ("sudden_code_jump_count", "sudden_code_jumps"),
    "code_deletion_events":       ("code_deletion_events", None),
    "code_deletion_chars_total":  ("code_deletion_chars_total", None),
    "undo_count":                 ("undo_count", None),
    "redo_count":                 ("redo_count", None),
    "copy_from_same_file_count":  ("copy_from_same_file_count", "copy_from_same_file"),
    "variable_rename_count":      ("variable_rename_count", "variable_rename_count"),
    "sudden_skill_jump_count":    ("sudden_skill_jump_count", None),
    "typing_speed_variance":      ("typing_speed_variance", "typing_speed_variance"),
    "typing_speed_avg_cps":       ("typing_speed_avg_cps", None),
    "time_per_line_ms":           ("time_per_line_ms", None),
    "session_break_count":        ("session_break_count", "session_break_count"),
    "scroll_event_count":         ("scroll_event_count", "scroll_events"),
    "code_complexity_avg":        ("code_complexity_avg", None),
    "code_fingerprint":           ("code_fingerprint", None),
    "total_pastes":               ("total_pastes", None),
    "total_tab_switches":         ("total_tab_switches", "tab_switch_count"),
    "total_idle_events":          ("total_idle_events", None),
    "session_duration_ms":        ("session_duration_ms", None),
    "suspicious_score":           ("suspicious_score", "suspicion_score"),
}


def _behavioral(beh) -> Optional[dict]:
    if beh is None:
        return None
    raw = _raw(beh)
    out = {}
    for key, (raw_key, column) in BEHAVIORAL_FIELDS.items():
        value = raw.get(raw_key)
        if value is None and column:
            value = getattr(beh, column, None)
        out[key] = value
    return out


def _ai_detection(sub) -> Optional[dict]:
    if sub is None:
        return None
    scores = sub.model_scores if isinstance(sub.model_scores, dict) else {}
    return {
        "final_score":     sub.ai_probability,
        "prediction":      sub.ai_prediction,
        "codebert_score":  scores.get("codebert"),
        "unixcoder_score": scores.get("unixcoder"),
        "behavioral_risk": scores.get("behavioral_risk"),
    }


def _code_section(snaps: list, language: Optional[str]) -> Optional[dict]:
    if not snaps:
        return None
    first, last = snaps[0], snaps[-1]
    return {
        "language":              last.language or language,
        "total_lines":           last.line_count,
        "code_snapshots_count":  len(snaps),
        "first_snapshot_code":   first.code,
        "intermediate_snapshots": [
            {"timestamp": _iso(s.timestamp), "code": s.code, "snapshot_number": s.snapshot_number}
            for s in snaps[1:-1]
        ],
        "final_code":            last.code,
    }


# ── build one session ──────────────────────────────────────────
def _build_session(db: Session, sess: models.StudentSession, user, label) -> dict:
    sid = sess.student_id
    started = sess.connected_at
    ended = sess.disconnected_at if (sess.disconnected_at and started and sess.disconnected_at >= started
                                     and not sess.is_connected) else None

    # exam problem that was current when the session began
    problem = None
    if started:
        problem = db.query(models.Problem).filter(
            models.Problem.created_at <= started
        ).order_by(models.Problem.created_at.desc(), models.Problem.id.desc()).first()

    # the AI-detection run and telemetry closest to (latest in) this session
    sub_q = db.query(models.Submission).filter(
        models.Submission.student_id == sid,
        models.Submission.ai_prediction.isnot(None),
    )
    if started:
        sub_q = sub_q.filter(models.Submission.timestamp >= started)
    sub = sub_q.order_by(models.Submission.timestamp.desc(), models.Submission.id.desc()).first()

    beh = None
    if sub is not None and sub.behavioral_data_id:
        beh = db.query(models.BehavioralData).filter(models.BehavioralData.id == sub.behavioral_data_id).first()
    if beh is None:
        beh_q = db.query(models.BehavioralData).filter(models.BehavioralData.student_id == sid)
        if started:
            beh_q = beh_q.filter(models.BehavioralData.created_at >= started)
        beh = beh_q.order_by(models.BehavioralData.created_at.desc(), models.BehavioralData.id.desc()).first()

    snap_q = db.query(models.CodeSnapshot).filter(models.CodeSnapshot.student_id == sid)
    if started:
        snap_q = snap_q.filter(models.CodeSnapshot.timestamp >= started)
    if ended:
        snap_q = snap_q.filter(models.CodeSnapshot.timestamp <= ended)
    snaps = snap_q.order_by(models.CodeSnapshot.timestamp.asc(), models.CodeSnapshot.id.asc()).all()

    previous = db.query(models.ExamSession).filter(models.ExamSession.student_id == sid).count()

    end_for_duration = ended or sess.last_heartbeat
    duration = None
    if started and end_for_duration and end_for_duration >= started:
        duration = round((end_for_duration - started).total_seconds() / 60, 2)

    return {
        "session_id": str(sess.id),
        "student": {
            "student_id":              sid,
            "full_name":               user.full_name if user else None,
            "previous_sessions_count": previous,
        },
        "exam": {
            "problem_title":       problem.title,
            "problem_description": problem.description,
            "time_limit_minutes":  problem.time_limit,
        } if problem else None,
        "label": {
            "value":              label.label,
            "teacher_confidence": label.confidence,
            "teacher_notes":      label.notes,
            "labeled_by":         label.teacher_id,
            "labeled_at":         _iso(label.created_at),
        } if label else None,
        "ai_detection": _ai_detection(sub),
        "behavioral":   _behavioral(beh),
        "code":         _code_section(snaps, sub.language if sub else None),
        "session": {
            "started_at":       _iso(started),
            "ended_at":         _iso(ended),
            "duration_minutes": duration,
        },
    }


def _latest_labels(db: Session) -> dict:
    """session_id -> most recent TeacherLabel."""
    latest = {}
    rows = db.query(models.TeacherLabel).order_by(
        models.TeacherLabel.created_at.asc(), models.TeacherLabel.id.asc()
    ).all()
    for r in rows:
        latest[r.session_id] = r      # later rows overwrite earlier ones
    return latest


def _validate_date_filter(date: Optional[str]) -> Optional[str]:
    """YYYY-MM-DD, or None. Raises 400 on a malformed OR invalid (e.g. month 99) value."""
    if date is None:
        return None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be a valid date in YYYY-MM-DD format")
    return date


def _export(db: Session, admin, labeled_only: bool, date: Optional[str] = None) -> Response:
    _check_rate_limit(admin.student_id)
    date = _validate_date_filter(date)

    students = {u.student_id: u for u in db.query(models.User).filter(models.User.role == "student").all()}
    sessions = []
    if students:
        sess_q = db.query(models.StudentSession).filter(
            models.StudentSession.student_id.in_(list(students.keys()))
        )
        if date:
            # The session's timestamp is its connect time (StudentSession.connected_at).
            sess_q = sess_q.filter(func.date(models.StudentSession.connected_at) == date)
        sessions = sess_q.order_by(models.StudentSession.id.asc()).all()
    labels = _latest_labels(db)

    labeled_count = sum(1 for s in sessions if s.id in labels)
    selected = [s for s in sessions if s.id in labels] if labeled_only else sessions

    payload = {
        "metadata": {
            "exported_at":     datetime.utcnow().isoformat() + "Z",
            "total_sessions":  len(sessions),
            "labeled_count":   labeled_count,
            "unlabeled_count": len(sessions) - labeled_count,
            "export_version":  EXPORT_VERSION,
        },
        "sessions": [_build_session(db, s, students.get(s.student_id), labels.get(s.id)) for s in selected],
    }

    body = json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False)
    filename = f"codeverity-dataset-{datetime.utcnow().strftime('%Y-%m-%d')}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/export/dataset")
def export_dataset_json(
    date: Optional[str] = Query(None, description="Filter by date, format YYYY-MM-DD"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    return _export(db, admin, labeled_only=True, date=date)


@router.get("/admin/export/all-sessions")
def export_all_sessions(
    date: Optional[str] = Query(None, description="Filter by date, format YYYY-MM-DD"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    return _export(db, admin, labeled_only=False, date=date)
