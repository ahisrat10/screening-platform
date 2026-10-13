from datetime import datetime
from typing import Optional

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import auth, models, schemas
from .database import get_db

router = APIRouter(tags=["teacher"])

HEARTBEAT_TIMEOUT_S = 90  # same liveness rule as /admin/all-students (extension beats every 30s)
SUSPICIOUS_THRESHOLD = 50  # same cut-off as /admin/flagged


# ── helpers ────────────────────────────────────────────────────
def _assigned_ids(db: Session, teacher) -> Optional[set]:
    """Student IDs this teacher may see, or None meaning "all students"
    (admins, and teachers with no assignments)."""
    if teacher.role == "admin":
        return None
    rows = db.query(models.TeacherAssignment.student_id).filter(
        models.TeacherAssignment.teacher_id == teacher.student_id
    ).all()
    return {r[0] for r in rows} if rows else None


def _require_visible(db: Session, teacher, student_id: str) -> models.User:
    student = db.query(models.User).filter(
        models.User.student_id == student_id, models.User.role == "student"
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    allowed = _assigned_ids(db, teacher)
    if allowed is not None and student_id not in allowed:
        raise HTTPException(status_code=403, detail="Student is not assigned to you")
    return student


def _is_live(session: Optional[models.StudentSession]) -> bool:
    return bool(
        session and session.is_connected and session.last_heartbeat
        and (datetime.utcnow() - session.last_heartbeat).total_seconds() < HEARTBEAT_TIMEOUT_S
    )


# ── dashboard helpers ──────────────────────────────────────────
def _first(*values):
    """First value that is not None (0 counts as a real value)."""
    for v in values:
        if v is not None:
            return v
    return None


def _event_count(db: Session, student_id: str, event_type: str) -> int:
    return db.query(models.StudentEvent).filter(
        models.StudentEvent.student_id == student_id,
        models.StudentEvent.event_type == event_type,
    ).count()


def _signals_summary(db: Session, student_id: str, session, beh) -> dict:
    """The handful of behavioral signals shown on the dashboard. Falls back
    to event counts when no AI-detection run has recorded telemetry yet."""
    raw = (beh.raw_events if beh and isinstance(beh.raw_events, dict) else {}) or {}
    return {
        "paste_count":      _first(raw.get("total_pastes"), raw.get("paste_count"),
                                   _event_count(db, student_id, "PASTE_DETECTED")),
        "backspace_freq":   _first(beh.backspace_frequency if beh else None, 0),
        "typing_speed_kpm": _first(beh.keys_per_minute if beh else None, 0),
        "undo_count":       _first(raw.get("undo_count"), beh.undo_redo_count if beh else None, 0),
        "tab_switches":     _first(raw.get("total_tab_switches"), beh.tab_switch_count if beh else None,
                                   _event_count(db, student_id, "WINDOW_UNFOCUSED")),
        "scroll_events":    _first(raw.get("scroll_event_count"), beh.scroll_events if beh else None, 0),
        "suspicious_score": session.suspicious_score or 0,
        "has_telemetry":    beh is not None,
    }


def _latest_ai_detection(db: Session, student_id: str) -> Optional[dict]:
    sub = db.query(models.Submission).filter(
        models.Submission.student_id == student_id,
        models.Submission.ai_prediction.isnot(None),
    ).order_by(models.Submission.timestamp.desc(), models.Submission.id.desc()).first()
    if not sub:
        return None
    return {
        "submission_id":  sub.id,
        "prediction":     sub.ai_prediction,
        "ai_probability": sub.ai_probability,
        "confidence":     sub.ai_confidence,
        "model_scores":   sub.model_scores or {},
        "timestamp":      str(sub.timestamp),
    }


def _recent_warnings(db: Session, student_id: str, limit: int = 10) -> list:
    rows = db.query(models.StudentWarning).filter(
        models.StudentWarning.student_id == student_id
    ).order_by(models.StudentWarning.created_at.desc(), models.StudentWarning.id.desc()).limit(limit).all()
    return [{
        "id": w.id, "message": w.message, "teacher_id": w.teacher_id,
        "created_at": str(w.created_at),
    } for w in rows]


def _exam_state(db: Session) -> models.ExamState:
    st = db.query(models.ExamState).filter(models.ExamState.id == 1).first()
    if not st:
        st = models.ExamState(id=1, is_active=False)
        db.add(st)
        db.commit()
        db.refresh(st)
    return st


def _problem_dict(p: Optional[models.Problem]) -> Optional[dict]:
    if not p:
        return None
    return {
        "id": p.id, "title": p.title, "description": p.description,
        "time_limit": p.time_limit, "created_by": p.created_by,
        "created_at": str(p.created_at),
    }


def _exam_dict(st: models.ExamState) -> dict:
    return {
        "is_active":  bool(st.is_active),
        "started_at": str(st.started_at) if st.started_at else None,
        "ended_at":   str(st.ended_at) if st.ended_at else None,
        "started_by": st.started_by,
        "problem_id": st.problem_id,
    }


def _current_problem(db: Session) -> Optional[models.Problem]:
    return db.query(models.Problem).filter(
        models.Problem.is_current == True  # noqa: E712
    ).order_by(models.Problem.id.desc()).first()


# ── teacher endpoints ──────────────────────────────────────────
@router.get("/teacher/students")
def teacher_students(
    active_only: bool = True,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    """Students visible to this teacher with live suspicion scores.
    active_only=true (default) lists only currently connected students."""
    q = db.query(models.User).filter(models.User.role == "student")
    allowed = _assigned_ids(db, teacher)
    if allowed is not None:
        q = q.filter(models.User.student_id.in_(allowed))

    result = []
    for s in q.all():
        session = db.query(models.StudentSession).filter(
            models.StudentSession.student_id == s.student_id
        ).first()
        live = _is_live(session)
        if active_only and not live:
            continue
        result.append({
            "student_id":       s.student_id,
            "full_name":        s.full_name,
            "session_id":       session.id if session else None,
            "is_connected":     live,
            "suspicious_score": (session.suspicious_score or 0) if session else 0,
            "last_heartbeat":   str(session.last_heartbeat) if session and session.last_heartbeat else None,
        })
    result.sort(key=lambda r: r["suspicious_score"], reverse=True)
    return result


@router.get("/teacher/student/{student_id}/session")
def teacher_student_session(
    student_id: str,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    student = _require_visible(db, teacher, student_id)

    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == student_id
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Student has no session yet")

    snap = db.query(models.CodeSnapshot).filter(
        models.CodeSnapshot.student_id == student_id
    ).order_by(models.CodeSnapshot.timestamp.desc(), models.CodeSnapshot.id.desc()).first()

    beh = db.query(models.BehavioralData).filter(
        models.BehavioralData.student_id == student_id
    ).order_by(models.BehavioralData.created_at.desc(), models.BehavioralData.id.desc()).first()

    behavioral = None
    if beh:
        behavioral = {col: getattr(beh, col) for col in models.BEHAVIORAL_FEATURE_COLUMNS}
        behavioral["raw_signals"] = beh.raw_events   # full extension payload
        behavioral["recorded_at"] = str(beh.created_at)

    return {
        "session_id":       session.id,
        "student_id":       student.student_id,
        "full_name":        student.full_name,
        "signals_summary":  _signals_summary(db, student_id, session, beh),
        "ai_detection":     _latest_ai_detection(db, student_id),
        "warnings":         _recent_warnings(db, student_id),
        "is_connected":     _is_live(session),
        "suspicious_score": session.suspicious_score or 0,
        "connected_at":     str(session.connected_at) if session.connected_at else None,
        "last_heartbeat":   str(session.last_heartbeat) if session.last_heartbeat else None,
        "code_snapshot": {
            "filename":   snap.filename,
            "language":   snap.language,
            "code":       snap.code,
            "line_count": snap.line_count,
            "ai_score":   snap.ai_score,
            "timestamp":  str(snap.timestamp),
        } if snap else None,
        "behavioral_signals": behavioral,
    }


@router.post("/teacher/label")
def teacher_label(
    data: schemas.TeacherLabelRequest,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    _require_visible(db, teacher, data.student_id)

    session = db.query(models.StudentSession).filter(
        models.StudentSession.id == data.session_id,
        models.StudentSession.student_id == data.student_id,
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found for this student")

    row = models.TeacherLabel(
        session_id = data.session_id,
        student_id = data.student_id,
        teacher_id = teacher.student_id,
        label      = data.label,
        confidence = data.confidence,
        notes      = data.notes,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"message": "Label recorded", "success": True, "label_id": row.id}


@router.get("/teacher/labels")
def teacher_labels(
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    rows = db.query(models.TeacherLabel).filter(
        models.TeacherLabel.teacher_id == teacher.student_id
    ).order_by(models.TeacherLabel.created_at.desc(), models.TeacherLabel.id.desc()).all()
    return [{
        "id":         r.id,
        "session_id": r.session_id,
        "student_id": r.student_id,
        "label":      r.label,
        "confidence": r.confidence,
        "notes":      r.notes,
        "created_at": str(r.created_at),
    } for r in rows]


# ── dashboard page ─────────────────────────────────────────────
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "teacher.html"


@router.get("/teacher", response_class=HTMLResponse, include_in_schema=False)
def teacher_page():
    if not TEMPLATE.is_file():
        raise HTTPException(status_code=404, detail="Page not found: teacher.html")
    return HTMLResponse(TEMPLATE.read_text(encoding="utf-8"))


# ── summary stats ──────────────────────────────────────────────
@router.get("/teacher/summary")
def teacher_summary(
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    q = db.query(models.User).filter(models.User.role == "student")
    allowed = _assigned_ids(db, teacher)
    if allowed is not None:
        q = q.filter(models.User.student_id.in_(allowed))
    ids = [u.student_id for u in q.all()]

    sessions = db.query(models.StudentSession).filter(
        models.StudentSession.student_id.in_(ids)
    ).all() if ids else []
    online = [s for s in sessions if _is_live(s)]

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    labels_today = db.query(models.TeacherLabel).filter(
        models.TeacherLabel.teacher_id == teacher.student_id,
        models.TeacherLabel.created_at >= today,
    ).count()

    return {
        "students_total":    len(ids),
        "online":            len(online),
        "suspicious_flags":  sum(1 for s in sessions if (s.suspicious_score or 0) >= SUSPICIOUS_THRESHOLD),
        "labels_today":      labels_today,
        "avg_suspicion":     round(sum((s.suspicious_score or 0) for s in online) / len(online), 1) if online else 0,
    }


# ── exam control ───────────────────────────────────────────────
@router.get("/teacher/problem/current")
def current_problem(
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    return {
        "problem": _problem_dict(_current_problem(db)),
        "exam":    _exam_dict(_exam_state(db)),
    }


@router.post("/teacher/problem")
def create_problem(
    data: schemas.ProblemRequest,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    db.query(models.Problem).filter(
        models.Problem.is_current == True  # noqa: E712
    ).update({"is_current": False})
    p = models.Problem(
        title=data.title, description=data.description, time_limit=data.time_limit,
        created_by=teacher.student_id, is_current=True,
    )
    db.add(p)
    db.flush()

    st = _exam_state(db)
    if st.is_active:
        st.problem_id = p.id
    db.commit()
    db.refresh(p)
    return {"message": "Problem saved", "success": True, "problem": _problem_dict(p)}


@router.post("/teacher/exam/start")
def exam_start(
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    st = _exam_state(db)
    if st.is_active:
        raise HTTPException(status_code=400, detail="Exam is already active")
    problem = _current_problem(db)
    st.is_active  = True
    st.started_at = datetime.utcnow()
    st.ended_at   = None
    st.started_by = teacher.student_id
    st.problem_id = problem.id if problem else None
    db.commit()
    return {"message": "Exam started", "success": True, "exam": _exam_dict(st)}


@router.post("/teacher/exam/end")
def exam_end(
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    st = _exam_state(db)
    if not st.is_active:
        raise HTTPException(status_code=400, detail="Exam is not active")
    st.is_active = False
    st.ended_at  = datetime.utcnow()
    db.commit()
    return {"message": "Exam ended", "success": True, "exam": _exam_dict(st)}


# ── per-student quick actions ──────────────────────────────────
@router.post("/teacher/student/{student_id}/warn")
def warn_student(
    student_id: str,
    data: schemas.WarnRequest,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    _require_visible(db, teacher, student_id)
    db.add(models.StudentWarning(
        student_id=student_id, teacher_id=teacher.student_id, message=data.message,
    ))
    db.commit()
    return {"message": "Warning recorded", "success": True,
            "warnings": _recent_warnings(db, student_id)}


@router.post("/teacher/student/{student_id}/disconnect")
def disconnect_student(
    student_id: str,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    _require_visible(db, teacher, student_id)
    now = datetime.utcnow()

    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == student_id
    ).first()
    if session:
        session.is_connected    = False
        session.disconnected_at = now

    control = db.query(models.StudentControl).filter(
        models.StudentControl.student_id == student_id
    ).first()
    if not control:
        control = models.StudentControl(student_id=student_id)
        db.add(control)
    control.force_disconnect = True
    control.set_by           = teacher.student_id
    control.set_at           = now
    db.commit()
    return {"message": "Student disconnected. The extension will disconnect on its next heartbeat.",
            "success": True}


@router.get("/teacher/student/{student_id}/report")
def student_report(
    student_id: str,
    db: Session = Depends(get_db),
    teacher = Depends(auth.get_teacher_user),
):
    student = _require_visible(db, teacher, student_id)

    subs = db.query(models.Submission).filter(
        models.Submission.student_id == student_id
    ).order_by(models.Submission.timestamp.desc(), models.Submission.id.desc()).limit(50).all()

    events = db.query(models.StudentEvent).filter(
        models.StudentEvent.student_id == student_id
    ).order_by(models.StudentEvent.timestamp.desc(), models.StudentEvent.id.desc()).limit(100).all()

    snaps = db.query(models.CodeSnapshot).filter(
        models.CodeSnapshot.student_id == student_id
    ).order_by(models.CodeSnapshot.timestamp.desc(), models.CodeSnapshot.id.desc())

    labels = db.query(models.TeacherLabel).filter(
        models.TeacherLabel.student_id == student_id
    ).order_by(models.TeacherLabel.created_at.desc(), models.TeacherLabel.id.desc()).limit(50).all()

    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == student_id
    ).first()

    return {
        "student": {"student_id": student.student_id, "full_name": student.full_name, "email": student.email},
        "session": {
            "session_id":       session.id if session else None,
            "is_connected":     _is_live(session),
            "suspicious_score": (session.suspicious_score or 0) if session else 0,
            "connected_at":     str(session.connected_at) if session and session.connected_at else None,
        },
        "snapshot_count": snaps.count(),
        "submissions": [{
            "id": s.id, "language": s.language, "prediction": s.ai_prediction,
            "ai_probability": s.ai_probability, "timestamp": str(s.timestamp),
        } for s in subs],
        "events": [{
            "event_type": e.event_type, "severity": e.severity, "timestamp": str(e.timestamp),
        } for e in events],
        "warnings": _recent_warnings(db, student_id, limit=50),
        "labels": [{
            "id": l.id, "label": l.label, "confidence": l.confidence, "notes": l.notes,
            "teacher_id": l.teacher_id, "created_at": str(l.created_at),
        } for l in labels],
    }


# ── admin endpoints for managing teachers ──────────────────────
@router.post("/admin/register-teacher", response_model=schemas.MessageResponse)
def register_teacher(
    data: schemas.RegisterRequest,
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    if db.query(models.User).filter(models.User.student_id == data.student_id).first():
        raise HTTPException(status_code=400, detail="This ID is already registered")
    if db.query(models.User).filter(models.User.email == data.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    db.add(models.User(
        student_id    = data.student_id,
        email         = data.email,
        full_name     = data.full_name,
        password_hash = auth.hash_password(data.password),
        role          = "teacher",
        is_active     = True,
    ))
    db.commit()
    return {"message": f"Teacher account created for {data.full_name}!", "success": True}


@router.post("/admin/teacher/assign", response_model=schemas.MessageResponse)
def assign_students(
    data: schemas.AssignStudentsRequest,
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    teacher = db.query(models.User).filter(
        models.User.student_id == data.teacher_id, models.User.role == "teacher"
    ).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    added = 0
    for sid in set(data.student_ids):
        if not db.query(models.User).filter(
            models.User.student_id == sid, models.User.role == "student"
        ).first():
            raise HTTPException(status_code=404, detail=f"Student {sid} not found")
        if not db.query(models.TeacherAssignment).filter(
            models.TeacherAssignment.teacher_id == data.teacher_id,
            models.TeacherAssignment.student_id == sid,
        ).first():
            db.add(models.TeacherAssignment(teacher_id=data.teacher_id, student_id=sid))
            added += 1
    db.commit()
    return {"message": f"Assigned {added} new student(s) to {data.teacher_id}", "success": True}


@router.delete("/admin/teacher/assign/{teacher_id}/{student_id}", response_model=schemas.MessageResponse)
def unassign_student(
    teacher_id: str,
    student_id: str,
    db: Session = Depends(get_db),
    admin = Depends(auth.get_admin_user),
):
    n = db.query(models.TeacherAssignment).filter(
        models.TeacherAssignment.teacher_id == teacher_id,
        models.TeacherAssignment.student_id == student_id,
    ).delete()
    db.commit()
    return {"message": "Assignment removed" if n else "No such assignment", "success": bool(n)}
