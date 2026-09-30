from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from . import auth, models, schemas
from .database import get_db

router = APIRouter(tags=["teacher"])

HEARTBEAT_TIMEOUT_S = 30  # same liveness rule as /admin/all-students


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
