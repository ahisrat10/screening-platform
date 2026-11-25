"""
Classes (Semester -> Course -> Section), enrolment, lab sessions, and the
admin user-management panel.

Teacher side:  /teacher/classes...
Student side:  /student/classes..., /student/invitations..., /student/lab-sessions
Admin side:    /admin/users..., /admin/db-stats
"""
import csv
import io
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, field_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from . import auth, models
from .database import get_db
from .teacher import _is_live

router = APIRouter(tags=["classes"])

# No 0/O/1/I so codes are easy to read out loud
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"^[A-Z2-9]{6}$")
INVITE_DAYS = 7


# ── schemas ────────────────────────────────────────────────────
def _nonblank(v: str, field: str, maxlen: int) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError(f"{field} must not be empty")
    if len(v) > maxlen:
        raise ValueError(f"{field} must be at most {maxlen} characters")
    return v


class ClassCreateRequest(BaseModel):
    semester:    str
    course_name: str
    section:     str
    class_code:  Optional[str] = None     # proposed by the UI; server verifies

    @field_validator("semester")
    def _s(cls, v):
        return _nonblank(v, "semester", 50)

    @field_validator("course_name")
    def _c(cls, v):
        return _nonblank(v, "course_name", 120)

    @field_validator("section")
    def _sec(cls, v):
        return _nonblank(v, "section", 50)

    @field_validator("class_code")
    def _code(cls, v):
        if v is None or v == "":
            return None
        v = v.strip().upper()
        if not CODE_RE.match(v):
            raise ValueError("class_code must be 6 characters (letters A-Z except I/O, digits 2-9)")
        return v


class ToggleRequest(BaseModel):
    enabled: bool


class InviteStudentRequest(BaseModel):
    student_id: str


class JoinCodeRequest(BaseModel):
    code: str


class JoinTokenRequest(BaseModel):
    token: str


# ── helpers ────────────────────────────────────────────────────
def _new_code(db: Session) -> str:
    for _ in range(30):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.query(models.Classroom).filter(models.Classroom.class_code == code).first():
            return code
    raise HTTPException(status_code=500, detail="Could not generate a unique class code")


def _owned_class(db: Session, user, class_id: int) -> models.Classroom:
    c = db.query(models.Classroom).filter(models.Classroom.id == class_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Class not found")
    if user.role != "admin" and c.teacher_id != user.student_id:
        raise HTTPException(status_code=403, detail="This is not your class")
    return c


def _active_lab(db: Session, class_id: int) -> Optional[models.LabSession]:
    return db.query(models.LabSession).filter(
        models.LabSession.class_id == class_id,
        models.LabSession.is_active == True,  # noqa: E712
    ).order_by(models.LabSession.id.desc()).first()


def _member_count(db: Session, class_id: int) -> int:
    return db.query(models.ClassMember).filter(models.ClassMember.class_id == class_id).count()


def _class_dict(db: Session, c: models.Classroom, teacher_name: Optional[str] = None,
                count: Optional[int] = None, lab=None, include_code: bool = True) -> dict:
    out = {
        "id":                   c.id,
        "semester":             c.semester,
        "course_name":          c.course_name,
        "section":              c.section,
        "teacher_id":           c.teacher_id,
        "teacher_name":         teacher_name,
        "student_count":        count if count is not None else _member_count(db, c.id),
        "ai_detection_enabled": bool(c.ai_detection_enabled),
        "created_at":           str(c.created_at),
        "active_lab":           {"id": lab.id, "started_at": str(lab.started_at)} if lab else None,
    }
    if include_code:
        out["class_code"] = c.class_code
    return out


def _student_only(user):
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Only student accounts can join classes")


def _join(db: Session, student_id: str, c: models.Classroom) -> bool:
    """Add membership. Returns False if already a member."""
    if db.query(models.ClassMember).filter(
        models.ClassMember.class_id == c.id, models.ClassMember.student_id == student_id
    ).first():
        return False
    db.add(models.ClassMember(class_id=c.id, student_id=student_id))
    # any pending targeted invitation to this class is now moot
    db.query(models.ClassInvitation).filter(
        models.ClassInvitation.class_id == c.id,
        models.ClassInvitation.student_id == student_id,
        models.ClassInvitation.status == "pending",
    ).update({"status": "accepted", "responded_at": datetime.utcnow()})
    return True


# join-code guessing limiter (in-memory, per process — same caveat as the OTP one)
_join_failures: dict = {}


def _join_check(student_id: str):
    now = time.time()
    fails = [t for t in _join_failures.get(student_id, []) if now - t < 600]
    _join_failures[student_id] = fails
    if len(fails) >= 10:
        raise HTTPException(status_code=429, detail="Too many invalid codes. Try again in 10 minutes.")


def _join_fail(student_id: str):
    _join_failures.setdefault(student_id, []).append(time.time())


def ai_detection_allowed(db: Session, student_id: str) -> bool:
    """AI detection is skipped only when the student belongs to at least one
    class and EVERY class they belong to has detection switched off."""
    flags = [bool(r[0]) for r in db.query(models.Classroom.ai_detection_enabled).join(
        models.ClassMember, models.ClassMember.class_id == models.Classroom.id
    ).filter(models.ClassMember.student_id == student_id).all()]
    return True if not flags else any(flags)


# ══════════════════════════════════════════════════════════════
# TEACHER — classes
# ══════════════════════════════════════════════════════════════
@router.get("/teacher/classes")
def list_classes(db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    q = db.query(models.Classroom)
    if teacher.role != "admin":
        q = q.filter(models.Classroom.teacher_id == teacher.student_id)
    classes = q.order_by(models.Classroom.created_at.desc(), models.Classroom.id.desc()).all()
    if not classes:
        return []
    ids = [c.id for c in classes]
    counts = dict(db.query(models.ClassMember.class_id, func.count(models.ClassMember.id)).filter(
        models.ClassMember.class_id.in_(ids)).group_by(models.ClassMember.class_id).all())
    labs = {l.class_id: l for l in db.query(models.LabSession).filter(
        models.LabSession.class_id.in_(ids), models.LabSession.is_active == True).all()}  # noqa: E712
    names = {u.student_id: u.full_name for u in db.query(models.User).filter(
        models.User.student_id.in_({c.teacher_id for c in classes})).all()}
    return [_class_dict(db, c, names.get(c.teacher_id), counts.get(c.id, 0), labs.get(c.id)) for c in classes]


@router.post("/teacher/classes")
def create_class(data: ClassCreateRequest, db: Session = Depends(get_db),
                 teacher=Depends(auth.get_teacher_user)):
    code = data.class_code
    if code and db.query(models.Classroom).filter(models.Classroom.class_code == code).first():
        raise HTTPException(status_code=409, detail="That class code is already taken")
    if not code:
        code = _new_code(db)
    c = models.Classroom(
        teacher_id=teacher.student_id, semester=data.semester, course_name=data.course_name,
        section=data.section, class_code=code, ai_detection_enabled=True,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"message": "Class created", "success": True,
            "class": _class_dict(db, c, teacher.full_name, 0, None)}


@router.post("/teacher/classes/{class_id}/ai-detection")
def toggle_ai_detection(class_id: int, data: ToggleRequest, db: Session = Depends(get_db),
                        teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    c.ai_detection_enabled = data.enabled
    db.commit()
    return {"message": f"AI detection {'enabled' if data.enabled else 'disabled'}",
            "success": True, "ai_detection_enabled": bool(c.ai_detection_enabled)}


@router.post("/teacher/classes/{class_id}/invite-link")
def invite_link(class_id: int, regenerate: bool = False, db: Session = Depends(get_db),
                teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    now = datetime.utcnow()
    link = db.query(models.ClassInviteLink).filter(
        models.ClassInviteLink.class_id == c.id, models.ClassInviteLink.expires_at > now
    ).order_by(models.ClassInviteLink.id.desc()).first()
    if link is None or regenerate:
        if link is not None:
            db.query(models.ClassInviteLink).filter(
                models.ClassInviteLink.class_id == c.id, models.ClassInviteLink.expires_at > now
            ).update({"expires_at": now})
        link = models.ClassInviteLink(
            class_id=c.id, token=secrets.token_urlsafe(24), created_by=teacher.student_id,
            expires_at=now + timedelta(days=INVITE_DAYS),
        )
        db.add(link)
        db.commit()
        db.refresh(link)
    return {"token": link.token, "path": f"/join/{link.token}",
            "expires_at": link.expires_at.isoformat() + "Z", "valid_days": INVITE_DAYS}


@router.post("/teacher/classes/{class_id}/invite")
def invite_student(class_id: int, data: InviteStudentRequest, db: Session = Depends(get_db),
                   teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    sid = data.student_id.strip()
    student = db.query(models.User).filter(
        models.User.student_id == sid, models.User.role == "student", models.User.is_active == True  # noqa: E712
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="No active student with that ID")
    if db.query(models.ClassMember).filter(
        models.ClassMember.class_id == c.id, models.ClassMember.student_id == sid).first():
        raise HTTPException(status_code=400, detail="Student is already in this class")
    now = datetime.utcnow()
    if db.query(models.ClassInvitation).filter(
        models.ClassInvitation.class_id == c.id, models.ClassInvitation.student_id == sid,
        models.ClassInvitation.status == "pending", models.ClassInvitation.expires_at > now).first():
        raise HTTPException(status_code=400, detail="An invitation is already pending for this student")
    db.add(models.ClassInvitation(
        class_id=c.id, student_id=sid, invited_by=teacher.student_id, status="pending",
        expires_at=now + timedelta(days=INVITE_DAYS),
    ))
    db.commit()
    return {"message": f"Invitation sent to {student.full_name or sid}", "success": True}


@router.get("/teacher/classes/{class_id}/students")
def class_students(class_id: int, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    members = db.query(models.User).join(
        models.ClassMember, models.ClassMember.student_id == models.User.student_id
    ).filter(models.ClassMember.class_id == c.id).all()
    rows = []
    for s in members:
        session = db.query(models.StudentSession).filter(
            models.StudentSession.student_id == s.student_id).first()
        rows.append({
            "student_id":       s.student_id,
            "full_name":        s.full_name,
            "session_id":       session.id if session else None,
            "is_connected":     _is_live(session),
            "suspicious_score": (session.suspicious_score or 0) if session else 0,
            "last_heartbeat":   str(session.last_heartbeat) if session and session.last_heartbeat else None,
        })
    return rows


@router.post("/teacher/classes/{class_id}/lab/start")
def start_lab(class_id: int, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    if _active_lab(db, c.id):
        raise HTTPException(status_code=400, detail="A lab is already running for this class")
    lab = models.LabSession(class_id=c.id, started_by=teacher.student_id, is_active=True)
    db.add(lab)
    db.commit()
    db.refresh(lab)
    return {"message": "Lab started", "success": True,
            "active_lab": {"id": lab.id, "started_at": str(lab.started_at)}}


@router.post("/teacher/classes/{class_id}/lab/end")
def end_lab(class_id: int, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    lab = _active_lab(db, c.id)
    if not lab:
        raise HTTPException(status_code=400, detail="No lab is running for this class")
    lab.is_active = False
    lab.ended_at = func.now()      # DB clock, same as started_at
    db.commit()
    # Save this lab's dataset (e.g. 2026-6-26_cse438.json) into the class.
    dataset = None
    try:
        from .rooms import save_dataset_for_lab
        dataset = save_dataset_for_lab(db, c, lab, teacher.student_id)
    except Exception:
        db.rollback()
        logging.getLogger("screening").warning("Dataset save failed for class %s", c.id, exc_info=True)
    return {"message": "Lab ended", "success": True,
            "dataset": {"id": dataset.id, "filename": dataset.filename} if dataset else None}


# ══════════════════════════════════════════════════════════════
# STUDENT — classes, invitations, history
# ══════════════════════════════════════════════════════════════
@router.get("/student/classes")
def my_classes(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    _student_only(user)
    rows = db.query(models.Classroom, models.User).join(
        models.ClassMember, models.ClassMember.class_id == models.Classroom.id
    ).outerjoin(models.User, models.User.student_id == models.Classroom.teacher_id).filter(
        models.ClassMember.student_id == user.student_id
    ).order_by(models.ClassMember.joined_at.desc()).all()
    out = []
    for c, teacher in rows:
        lab = _active_lab(db, c.id)
        d = _class_dict(db, c, teacher.full_name if teacher else None, None, lab, include_code=False)
        out.append(d)
    return out


def _joined_response(db: Session, c: models.Classroom, joined: bool):
    teacher = db.query(models.User).filter(models.User.student_id == c.teacher_id).first()
    return {
        "message": f"Joined {c.course_name} — Section {c.section}" if joined else "You are already in this class",
        "success": True, "joined": joined,
        "class": _class_dict(db, c, teacher.full_name if teacher else None, None, None, include_code=False),
    }


@router.post("/student/classes/join")
def join_with_code(data: JoinCodeRequest, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    _student_only(user)
    _join_check(user.student_id)
    code = (data.code or "").strip().upper()
    c = db.query(models.Classroom).filter(models.Classroom.class_code == code).first() if CODE_RE.match(code) else None
    if not c:
        _join_fail(user.student_id)
        raise HTTPException(status_code=404, detail="Invalid class code")
    joined = _join(db, user.student_id, c)
    db.commit()
    return _joined_response(db, c, joined)


@router.post("/student/classes/join-invite")
def join_with_invite_link(data: JoinTokenRequest, db: Session = Depends(get_db),
                          user=Depends(auth.get_current_user)):
    _student_only(user)
    _join_check(user.student_id)
    link = db.query(models.ClassInviteLink).filter(models.ClassInviteLink.token == (data.token or "")).first()
    if not link:
        _join_fail(user.student_id)
        raise HTTPException(status_code=404, detail="Invalid invitation link")
    if link.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=410, detail="This invitation link has expired")
    c = db.query(models.Classroom).filter(models.Classroom.id == link.class_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Class no longer exists")
    joined = _join(db, user.student_id, c)
    db.commit()
    return _joined_response(db, c, joined)


@router.get("/student/invitations")
def my_invitations(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    _student_only(user)
    rows = db.query(models.ClassInvitation, models.Classroom, models.User).join(
        models.Classroom, models.Classroom.id == models.ClassInvitation.class_id
    ).outerjoin(models.User, models.User.student_id == models.Classroom.teacher_id).filter(
        models.ClassInvitation.student_id == user.student_id,
        models.ClassInvitation.status == "pending",
        models.ClassInvitation.expires_at > datetime.utcnow(),
    ).order_by(models.ClassInvitation.created_at.desc()).all()
    return [{
        "id": inv.id, "course_name": c.course_name, "section": c.section, "semester": c.semester,
        "teacher_name": t.full_name if t else None,
        "created_at": str(inv.created_at), "expires_at": inv.expires_at.isoformat() + "Z",
    } for inv, c, t in rows]


def _invitation_for(db: Session, user, invitation_id: int) -> models.ClassInvitation:
    inv = db.query(models.ClassInvitation).filter(
        models.ClassInvitation.id == invitation_id,
        models.ClassInvitation.student_id == user.student_id,
    ).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if inv.status != "pending":
        raise HTTPException(status_code=400, detail=f"Invitation already {inv.status}")
    if inv.expires_at <= datetime.utcnow():
        raise HTTPException(status_code=410, detail="This invitation has expired")
    return inv


@router.post("/student/invitations/{invitation_id}/accept")
def accept_invitation(invitation_id: int, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    _student_only(user)
    inv = _invitation_for(db, user, invitation_id)
    c = db.query(models.Classroom).filter(models.Classroom.id == inv.class_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Class no longer exists")
    joined = _join(db, user.student_id, c)
    inv.status, inv.responded_at = "accepted", datetime.utcnow()
    db.commit()
    return _joined_response(db, c, joined)


@router.post("/student/invitations/{invitation_id}/decline")
def decline_invitation(invitation_id: int, db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    _student_only(user)
    inv = _invitation_for(db, user, invitation_id)
    inv.status, inv.responded_at = "declined", datetime.utcnow()
    db.commit()
    return {"message": "Invitation declined", "success": True}


@router.get("/student/lab-status")
def my_lab_status(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    """Is a lab running for any class this student is in? (polled by the extension)"""
    if user.role != "student":
        return {"active": False, "lab": None, "ai_detection_enabled": True}
    row = db.query(models.LabSession, models.Classroom).join(
        models.Classroom, models.Classroom.id == models.LabSession.class_id
    ).join(models.ClassMember, (models.ClassMember.class_id == models.Classroom.id) &
           (models.ClassMember.student_id == user.student_id)).filter(
        models.LabSession.is_active == True,  # noqa: E712
    ).order_by(models.LabSession.id.desc()).first()
    ai = ai_detection_allowed(db, user.student_id)
    if not row:
        return {"active": False, "lab": None, "ai_detection_enabled": ai}
    lab, c = row
    return {
        "active": True,
        "lab": {"id": lab.id, "class_id": c.id, "course_name": c.course_name,
                "section": c.section, "semester": c.semester, "started_at": str(lab.started_at)},
        "ai_detection_enabled": ai,
    }


@router.get("/student/lab-sessions")
def my_lab_history(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    """Finished labs of the student's classes since they joined. `ai_score`
    is the student's latest AI-detection result recorded during that lab (%)."""
    _student_only(user)
    rows = db.query(models.LabSession, models.Classroom).join(
        models.Classroom, models.Classroom.id == models.LabSession.class_id
    ).join(models.ClassMember, (models.ClassMember.class_id == models.Classroom.id) &
           (models.ClassMember.student_id == user.student_id)).filter(
        models.LabSession.is_active == False,  # noqa: E712
        models.LabSession.ended_at.isnot(None),
        models.LabSession.started_at >= models.ClassMember.joined_at,
    ).order_by(models.LabSession.started_at.desc()).limit(50).all()

    out = []
    for lab, c in rows:
        sub = db.query(models.Submission).filter(
            models.Submission.student_id == user.student_id,
            models.Submission.ai_probability.isnot(None),
            models.Submission.timestamp >= lab.started_at,
            models.Submission.timestamp <= lab.ended_at,
        ).order_by(models.Submission.timestamp.desc(), models.Submission.id.desc()).first()

        # Latest behavioral telemetry recorded during this lab's time window
        # (NOT the student's live/current StudentSession — this row is history).
        beh = db.query(models.BehavioralData).filter(
            models.BehavioralData.student_id == user.student_id,
            models.BehavioralData.created_at >= lab.started_at,
            models.BehavioralData.created_at <= lab.ended_at,
        ).order_by(models.BehavioralData.created_at.desc(), models.BehavioralData.id.desc()).first()
        raw = (beh.raw_events if beh and isinstance(beh.raw_events, dict) else {}) or {}

        def _first(*values):
            for v in values:
                if v is not None:
                    return v
            return None

        out.append({
            "id": lab.id, "course_name": c.course_name, "section": c.section, "semester": c.semester,
            "date": lab.started_at.date().isoformat(),
            "started_at": str(lab.started_at), "ended_at": str(lab.ended_at),
            "duration_minutes": round((lab.ended_at - lab.started_at).total_seconds() / 60, 1),
            "ai_score": round(sub.ai_probability * 100, 1) if sub else None,
            "ai_prediction": sub.ai_prediction if sub else None,
            # Behavioral signals recorded during this specific lab (null if no
            # telemetry was ever sent during that window).
            "paste_count":      _first(raw.get("total_pastes"), raw.get("paste_count")),
            "tab_switches":     _first(raw.get("total_tab_switches"), beh.tab_switch_count if beh else None),
            "backspace_freq":   beh.backspace_frequency if beh else None,
            "typing_speed_kpm": beh.keys_per_minute if beh else None,
            "suspicious_score": beh.suspicion_score if beh else None,
        })
    return out


# ══════════════════════════════════════════════════════════════
# ADMIN — user management & DB stats
# ══════════════════════════════════════════════════════════════
def _last_active_map(db: Session) -> dict:
    out = {}
    for s in db.query(models.StudentSession).all():
        t = max([x for x in (s.last_heartbeat, s.disconnected_at, s.connected_at) if x], default=None)
        if t:
            out[s.student_id] = t
    return out


def _status(u: models.User) -> str:
    return "active" if u.is_active else "disabled"


@router.get("/admin/users")
def admin_users(role: Optional[str] = None, q: Optional[str] = None,
                db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    query = db.query(models.User)
    if role:
        if role not in ("student", "teacher", "admin"):
            raise HTTPException(status_code=400, detail="role must be student, teacher or admin")
        query = query.filter(models.User.role == role)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(models.User.full_name.ilike(like), models.User.email.ilike(like),
                                 models.User.student_id.ilike(like)))
    active = _last_active_map(db)
    return [{
        "id": u.id, "student_id": u.student_id, "full_name": u.full_name, "email": u.email,
        "role": u.role, "is_active": bool(u.is_active), "status": _status(u),
        "created_at": str(u.created_at) if u.created_at else None,
        "last_active": str(active[u.student_id]) if u.student_id in active else None,
    } for u in query.order_by(models.User.created_at.desc(), models.User.id.desc()).all()]


class AdminSetPassword(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _len(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        if len(v.encode("utf-8")) > 72:
            raise ValueError("Password must be 72 bytes or fewer")
        return v


@router.put("/admin/users/{user_id}/password")
def admin_set_password(user_id: str, data: AdminSetPassword,
                       db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    """Admin sets a new password for any student, teacher or admin account.
    Also clears a lockout from too many wrong attempts."""
    u = db.query(models.User).filter(models.User.student_id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    u.password_hash = auth.hash_password(data.new_password)
    u.failed_attempts = 0
    u.locked_until = None
    db.commit()
    logging.getLogger("screening").info("Admin %s changed the password of %s", admin.student_id, user_id)
    return {"message": f"Password changed for {u.full_name or user_id}", "success": True}


def _csv_safe(v) -> str:
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _validate_date_filter(date: Optional[str]) -> Optional[str]:
    """YYYY-MM-DD, or None. Raises 400 on a malformed OR invalid (e.g. month 99) value."""
    if date is None:
        return None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be a valid date in YYYY-MM-DD format")
    return date


@router.get("/admin/users/export.csv")
def admin_users_csv(
    date: Optional[str] = Query(None, description="Filter by date, format YYYY-MM-DD"),
    db: Session = Depends(get_db),
    admin=Depends(auth.get_admin_user),
):
    date = _validate_date_filter(date)
    active = _last_active_map(db)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "full_name", "email", "role", "status", "created_at", "last_active"])
    query = db.query(models.User)
    if date:
        # The user's own timestamp column is created_at (registration date).
        query = query.filter(func.date(models.User.created_at) == date)
    for u in query.order_by(models.User.id).all():
        w.writerow([_csv_safe(u.student_id), _csv_safe(u.full_name), _csv_safe(u.email), u.role,
                    _status(u), u.created_at or "", active.get(u.student_id, "")])
    name = f"users-{datetime.utcnow().strftime('%Y-%m-%d')}.csv"
    return Response(content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/admin/db-stats")
def db_stats(db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    by_role = dict(db.query(models.User.role, func.count(models.User.id)).group_by(models.User.role).all())
    return {
        "total_users":       sum(by_role.values()),
        "users_by_role":     {r: by_role.get(r, 0) for r in ("student", "teacher", "admin")},
        "total_sessions":    db.query(models.StudentSession).count(),
        "total_submissions": db.query(models.Submission).count(),
        "code_snapshots":    db.query(models.CodeSnapshot).count(),
        "teacher_labels":    db.query(models.TeacherLabel).count(),
        "classes":           db.query(models.Classroom).count(),
        "lab_sessions":      db.query(models.LabSession).count(),
    }


SIMILARITY_THRESHOLD = 0.7
SIMILARITY_CODE_CHARS = 4000   # cap sent per side so an O(n^2) report stays a sane payload


@router.get("/admin/similarity-report")
def similarity_report(db: Session = Depends(get_db), admin=Depends(auth.get_teacher_user)):
    """Pairwise similarity between every pair of students' latest code
    snapshot, via difflib.SequenceMatcher (character-level; order-sensitive —
    catches literal copy-paste-with-edits, but a straight reorder of the same
    lines scores lower here than it would under a token-set/Jaccard measure).
    Flags pairs scoring >= 0.7 (not same-file paste, which the extension
    scores separately client-side)."""
    import difflib

    students = db.query(models.User).filter(models.User.role == "student").all()

    # One code sample per student: their most recent code snapshot.
    final_code = {}
    for s in students:
        snap = db.query(models.CodeSnapshot).filter(
            models.CodeSnapshot.student_id == s.student_id
        ).order_by(models.CodeSnapshot.timestamp.desc(), models.CodeSnapshot.id.desc()).first()
        if snap and snap.code and snap.code.strip():
            final_code[s.student_id] = snap.code

    ids = list(final_code.keys())

    pairs = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sid1, sid2 = ids[i], ids[j]
            code1, code2 = final_code[sid1], final_code[sid2]
            score = difflib.SequenceMatcher(None, code1, code2, autojunk=False).ratio()
            if score >= SIMILARITY_THRESHOLD:
                pairs.append({
                    "student_a":  sid1,
                    "student_b":  sid2,
                    "similarity": round(score, 4),
                    "code_a":     code1[:SIMILARITY_CODE_CHARS],
                    "code_b":     code2[:SIMILARITY_CODE_CHARS],
                })
    pairs.sort(key=lambda p: p["similarity"], reverse=True)

    return {"pairs": pairs}


def _count(q) -> int:
    return q.delete(synchronize_session=False)


@router.delete("/admin/users/{user_id}")
def delete_user(user_id: str, db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    """Permanently delete a user. Data ABOUT the user is removed; authorship
    of labels/feedback/warnings they made is kept with the author set to null."""
    u = db.query(models.User).filter(models.User.student_id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    if u.student_id == admin.student_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if u.role == "admin" and u.is_active and db.query(models.User).filter(
            models.User.role == "admin", models.User.is_active == True).count() <= 1:  # noqa: E712
        raise HTTPException(status_code=400, detail="Cannot delete the last active admin")

    M = models
    removed = {}
    display_name = u.full_name or user_id     # read before the row is gone
    try:
        # 1) keep what this user authored, drop the link to them
        db.query(M.TeacherLabel).filter(M.TeacherLabel.teacher_id == user_id).update({"teacher_id": None})
        db.query(M.TeacherFeedback).filter(M.TeacherFeedback.teacher_id == user_id).update({"teacher_id": None})
        db.query(M.StudentWarning).filter(M.StudentWarning.teacher_id == user_id).update({"teacher_id": None})
        db.query(M.Problem).filter(M.Problem.created_by == user_id).update({"created_by": None})
        db.query(M.LabSession).filter(M.LabSession.started_by == user_id).update({"started_by": None})
        db.query(M.ClassInvitation).filter(M.ClassInvitation.invited_by == user_id).update({"invited_by": None})
        db.query(M.ClassInviteLink).filter(M.ClassInviteLink.created_by == user_id).update({"created_by": None})

        # 2) classes this user teaches go with them
        class_ids = [r[0] for r in db.query(M.Classroom.id).filter(M.Classroom.teacher_id == user_id).all()]
        if class_ids:
            for model in (M.LabSession, M.ClassMember, M.ClassInvitation, M.ClassInviteLink):
                _count(db.query(model).filter(model.class_id.in_(class_ids)))
            removed["classes"] = _count(db.query(M.Classroom).filter(M.Classroom.id.in_(class_ids)))

        # 3) everything recorded about this user as a student (FK-safe order)
        sub_ids = [r[0] for r in db.query(M.Submission.id).filter(M.Submission.student_id == user_id).all()]
        fb = db.query(M.TeacherFeedback).filter(M.TeacherFeedback.student_id == user_id)
        _count(fb)
        if sub_ids:
            _count(db.query(M.TeacherFeedback).filter(M.TeacherFeedback.submission_id.in_(sub_ids)))
        removed["labels"] = _count(db.query(M.TeacherLabel).filter(M.TeacherLabel.student_id == user_id))
        removed["submissions"] = _count(db.query(M.Submission).filter(M.Submission.student_id == user_id))
        _count(db.query(M.BehavioralData).filter(M.BehavioralData.student_id == user_id))
        _count(db.query(M.ExamSession).filter(M.ExamSession.student_id == user_id))
        for model in (M.StudentProfile, M.StudentControl, M.StudentWarning, M.ClassMember, M.ClassInvitation):
            _count(db.query(model).filter(model.student_id == user_id))
        _count(db.query(M.TeacherAssignment).filter(
            or_(M.TeacherAssignment.teacher_id == user_id, M.TeacherAssignment.student_id == user_id)))
        removed["snapshots"] = _count(db.query(M.CodeSnapshot).filter(M.CodeSnapshot.student_id == user_id))
        for model in (M.StudentEvent, M.Heartbeat, M.OTPCode, M.StudentSession):
            _count(db.query(model).filter(model.student_id == user_id))

        db.query(M.User).filter(M.User.student_id == user_id).delete(synchronize_session=False)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"message": f"Deleted {display_name}", "success": True, "removed": removed}
