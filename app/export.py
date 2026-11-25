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

# ── rate limit: 60 exports / hour / admin / endpoint ────────────
# In-memory (per process): resets on restart and isn't shared between workers,
# same limitation as the OTP limiter. Keyed per-endpoint so working through
# the Datasets panel's six buttons doesn't exhaust a shared quota.
_EXPORT_LIMIT = 60
_EXPORT_WINDOW_SEC = 3600
_export_calls: dict = {}


def _check_rate_limit(admin_id: str, endpoint: str):
    key = f"{admin_id}:{endpoint}"
    now = time.time()
    calls = [t for t in _export_calls.get(key, []) if now - t < _EXPORT_WINDOW_SEC]
    if len(calls) >= _EXPORT_LIMIT:
        retry = int(_EXPORT_WINDOW_SEC - (now - calls[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Export limit reached ({_EXPORT_LIMIT} per hour for this export). Try again in {retry // 60 + 1} minutes.",
            headers={"Retry-After": str(retry)},
        )
    calls.append(now)
    _export_calls[key] = calls


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


def _export(db: Session, admin, labeled_only: bool, date: Optional[str] = None, endpoint: str = "dataset") -> Response:
    _check_rate_limit(admin.student_id, endpoint)
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
    return _export(db, admin, labeled_only=True, date=date, endpoint="dataset")


@router.get("/admin/export/all-sessions")
def export_all_sessions(
    date: Optional[str] = Query(None, description="Filter by date, format YYYY-MM-DD"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    return _export(db, admin, labeled_only=False, date=date, endpoint="all-sessions")


# ══════════════════════════════════════════════════════════════
# Flat (one-row-per-session) exports used by the admin "Datasets" page.
# Reuses the session-building helpers above, flattened for Excel/filters.
# ══════════════════════════════════════════════════════════════
from .models import BEHAVIORAL_FEATURE_COLUMNS  # noqa: E402


def _class_name_for(db: Session, student_id: str) -> Optional[str]:
    cm = db.query(models.ClassMember, models.Classroom).join(
        models.Classroom, models.Classroom.id == models.ClassMember.class_id
    ).filter(models.ClassMember.student_id == student_id).first()
    if not cm:
        return None
    _, c = cm
    return f"{c.course_name} ({c.section})" if c.section else c.course_name


def _flat_row(db: Session, sess: models.StudentSession, user, label, anonymized: bool = False) -> dict:
    sid = sess.student_id
    started = sess.connected_at

    problem = None
    if started:
        problem = db.query(models.Problem).filter(
            models.Problem.created_at <= started
        ).order_by(models.Problem.created_at.desc(), models.Problem.id.desc()).first()

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

    # Quick Human/AI/Paraphrased label buttons (per-class monitoring view)
    # feed teacher_feedback.session_id — separate from the human/ai/unsure
    # TeacherLabel row already joined in via `label` above.
    fb = db.query(models.TeacherFeedback).filter(
        models.TeacherFeedback.session_id == sess.id
    ).order_by(models.TeacherFeedback.created_at.desc(), models.TeacherFeedback.id.desc()).first()

    row = {
        "session_id":   sess.id,
        "class_name":   _class_name_for(db, sid),
        "lab_name":     problem.title if problem else None,
        "submitted_at": _iso(sub.timestamp) if sub else _iso(sess.connected_at),
        "student_id":   sid,
        "student_name": "REDACTED" if anonymized else (user.full_name if user else None),
        "email":        "REDACTED" if anonymized else (user.email if user else None),
        "ai_probability":  sub.ai_probability if sub else None,
        "model_confidence": sub.ai_confidence if sub else None,
        "teacher_label":   (fb.teacher_label if fb else None) or (label.label if label else None),
        "model_prediction": fb.model_prediction if fb else None,
        "teacher_notes":     fb.notes if fb else None,
        "code_length":  len(sub.code) if sub and sub.code else 0,
        "language":     sub.language if sub else None,
    }
    for col in BEHAVIORAL_FEATURE_COLUMNS:
        row[col] = getattr(beh, col, None) if beh else None
    return row


def _rows_to_xlsx(rows: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Sessions"
    headers = list(rows[0].keys()) if rows else (
        ["session_id", "class_name", "lab_name", "submitted_at", "student_id", "student_name",
         "email", "ai_probability", "model_confidence", "teacher_label", "model_prediction",
         "teacher_notes", "code_length", "language"]
        + BEHAVIORAL_FEATURE_COLUMNS
    )
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="23336A")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append([r.get(h) for h in headers])
    ws.freeze_panes = "A2"
    for i, h in enumerate(headers, 1):
        width = max(len(str(h)), *(len(str(r.get(h))) for r in rows)) if rows else len(str(h))
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 40)

    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _respond_rows(rows: list, fmt: str, filename_base: str) -> Response:
    if fmt == "excel":
        content = _rows_to_xlsx(rows)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.xlsx"'},
        )
    body = json.dumps(_clean(rows), ensure_ascii=False, allow_nan=False)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename_base}.json"'},
    )


def _all_sessions_rows(db: Session, anonymized: bool = False,
                        student_ids: Optional[list] = None,
                        since=None, until=None) -> list:
    user_q = db.query(models.User).filter(models.User.role == "student")
    if student_ids is not None:
        user_q = user_q.filter(models.User.student_id.in_(student_ids))
    students = {u.student_id: u for u in user_q.all()}
    if not students:
        return []
    sess_q = db.query(models.StudentSession).filter(
        models.StudentSession.student_id.in_(list(students.keys()))
    )
    if since:
        sess_q = sess_q.filter(models.StudentSession.connected_at >= since)
    if until:
        sess_q = sess_q.filter(models.StudentSession.connected_at <= until)
    sessions = sess_q.order_by(models.StudentSession.id.asc()).all()
    labels = _latest_labels(db)
    return [_flat_row(db, s, students.get(s.student_id), labels.get(s.id), anonymized) for s in sessions]


@router.get("/admin/export/all")
def export_flat_all(
    format: str = Query("json", pattern="^(json|excel)$"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    _check_rate_limit(admin.student_id, "flat-all")
    rows = _all_sessions_rows(db)
    return _respond_rows(rows, format, f"codeverity-all-{datetime.utcnow().strftime('%Y-%m-%d')}")


@router.get("/admin/export/daterange")
def export_flat_daterange(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    format: str = Query("json", pattern="^(json|excel)$"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    _check_rate_limit(admin.student_id, "flat-daterange")
    _validate_date_filter(start)
    _validate_date_filter(end)
    since = datetime.strptime(start, "%Y-%m-%d")
    until = datetime.strptime(end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    rows = _all_sessions_rows(db, since=since, until=until)
    return _respond_rows(rows, format, f"codeverity-{start}-to-{end}")


@router.get("/admin/export/class/{class_id}")
def export_flat_class(
    class_id: int,
    format: str = Query("json", pattern="^(json|excel)$"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    _check_rate_limit(admin.student_id, "flat-class")
    c = db.query(models.Classroom).filter(models.Classroom.id == class_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Class not found")
    student_ids = [m.student_id for m in db.query(models.ClassMember).filter(
        models.ClassMember.class_id == class_id).all()]
    rows = _all_sessions_rows(db, student_ids=student_ids)
    return _respond_rows(rows, format, f"codeverity-class-{class_id}-{datetime.utcnow().strftime('%Y-%m-%d')}")


@router.get("/admin/export/student/{student_id}")
def export_flat_student(
    student_id: str,
    format: str = Query("json", pattern="^(json|excel)$"),
    anonymized: bool = Query(False),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    _check_rate_limit(admin.student_id, "flat-student")
    rows = _all_sessions_rows(db, anonymized=anonymized, student_ids=[student_id])
    if not rows:
        user = db.query(models.User).filter(models.User.student_id == student_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="Student not found")
    return _respond_rows(rows, format, f"codeverity-student-{student_id}-{datetime.utcnow().strftime('%Y-%m-%d')}")


@router.get("/admin/export/flagged")
def export_flat_flagged(
    format: str = Query("json", pattern="^(json|excel)$"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    _check_rate_limit(admin.student_id, "flat-flagged")
    rows = _all_sessions_rows(db)
    rows = [r for r in rows if (r.get("ai_probability") or 0) > 0.5 or r.get("teacher_label") == "ai"]
    return _respond_rows(rows, format, f"codeverity-flagged-{datetime.utcnow().strftime('%Y-%m-%d')}")


@router.get("/admin/export/labeled")
def export_flat_labeled(
    format: str = Query("json", pattern="^(json|excel)$"),
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    _check_rate_limit(admin.student_id, "flat-labeled")
    rows = _all_sessions_rows(db)
    rows = [r for r in rows if r.get("teacher_label")]
    return _respond_rows(rows, format, f"codeverity-labeled-{datetime.utcnow().strftime('%Y-%m-%d')}")


@router.post("/admin/retrain")
def admin_retrain(
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    from .ml import retrain as retrain_mod
    return retrain_mod.run_retrain(db)


@router.get("/admin/retrain/status")
def admin_retrain_status(
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    from .ml import retrain as retrain_mod
    return retrain_mod.get_status(db)
