"""Three separate portals: Student, Teacher and Admin.

Every portal has its own address and its own sign-in page:

    /            landing page (pick a portal)
    /student     + /student/login
    /teacher     + /teacher/login        (teacher dashboard lives in teacher.py)
    /admin       + /admin/login

Also here:
  * GET /student/my-codes   the student's own code files with date + course
  * GET /admin/overview     counts + teacher list for the admin
  * GET /admin/datasets     every class dataset (admin sees all of them)
  * GET /admin/datasets/{id}/download
  * /assets/<file>          the logo and the shared 3D stylesheet
"""
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import auth, models
from .database import get_db
from .rooms import DATASET_RETENTION_DAYS

router = APIRouter(tags=["portals"])

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
_ASSET_TYPES = {".css": "text/css", ".svg": "image/svg+xml", ".js": "application/javascript"}


# ══════════════════════════════════════════════════════════════
# Pages
# ══════════════════════════════════════════════════════════════
def _html(name: str, **subst) -> HTMLResponse:
    path = TEMPLATES / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Page not found: {name}")
    text = path.read_text(encoding="utf-8")
    for k, v in subst.items():
        text = text.replace("{{" + k + "}}", v)
    return HTMLResponse(text)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
@router.head("/", include_in_schema=False)
def landing():
    return _html("landing.html")


@router.get("/login", include_in_schema=False)
def old_login():
    return RedirectResponse("/", status_code=307)


@router.get("/student", response_class=HTMLResponse, include_in_schema=False)
def student_portal():
    return _html("student_dashboard.html")


@router.get("/student/login", response_class=HTMLResponse, include_in_schema=False)
def student_login():
    return _html("portal_login.html", ROLE="student")


@router.get("/teacher/login", response_class=HTMLResponse, include_in_schema=False)
def teacher_login():
    return _html("portal_login.html", ROLE="teacher")


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
def admin_login():
    return _html("portal_login.html", ROLE="admin")


@router.get("/assets/{name}", include_in_schema=False)
def asset(name: str):
    path = (STATIC / name).resolve()
    if STATIC.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type=_ASSET_TYPES.get(path.suffix, "application/octet-stream"),
                        headers={"Cache-Control": "public, max-age=3600"})


# ══════════════════════════════════════════════════════════════
# Student: my code files (date + course)
# ══════════════════════════════════════════════════════════════
def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() + "Z" if dt else None


def student_code_files(db: Session, sid: str):
    """Every file a student has written: latest code, when, for which course and
    its AI percentage. Returns (files, average, classes)."""
    classes = db.query(models.Classroom).join(
        models.ClassMember, models.ClassMember.class_id == models.Classroom.id
    ).filter(models.ClassMember.student_id == sid).all()
    by_id = {c.id: c for c in classes}
    labs = db.query(models.LabSession).filter(
        models.LabSession.class_id.in_(list(by_id.keys()))).all() if by_id else []

    def course_of(ts: Optional[datetime]):
        if ts is not None:
            for lab in labs:
                end = lab.ended_at or datetime.utcnow() + timedelta(days=1)
                if lab.started_at and lab.started_at <= ts <= end:
                    return by_id.get(lab.class_id)
        return classes[0] if len(classes) == 1 else None

    latest = {}     # filename -> dict (newest wins)

    def take(filename, language, code, ts, kind):
        if not filename or not code:
            return
        cur = latest.get(filename)
        if cur is None or (ts and cur["_ts"] and ts > cur["_ts"]) or cur["_ts"] is None:
            latest[filename] = {"filename": filename, "language": language, "code": code,
                                "_ts": ts, "source": kind}

    for s in db.query(models.Submission).filter(models.Submission.student_id == sid).all():
        take(s.filename, s.language, s.code, s.timestamp, "submission")
    for s in db.query(models.CodeSnapshot).filter(models.CodeSnapshot.student_id == sid).all():
        take(s.filename, s.language, s.code, s.timestamp, "snapshot")

    from .main import _student_file_scores
    try:
        files, average = _student_file_scores(db, sid)
    except Exception:
        db.rollback()
        files, average = [], {"classical": None, "transformer": None, "combined": None, "file_count": 0}
    scores = {f["filename"]: f for f in files}

    out = []
    for name, item in latest.items():
        c = course_of(item["_ts"])
        sc = scores.get(name) or {}
        code = item["code"] or ""
        out.append({
            "filename":    name,
            "language":    item["language"],
            "date":        _iso(item["_ts"]),
            "course":      c.course_name if c else None,
            "section":     c.section if c else None,
            "semester":    c.semester if c else None,
            "lines":       code.count("\n") + 1 if code else 0,
            "code":        code[:30000],
            "ai_percent":  sc.get("combined"),
            "ai_percent_classical":   (sc.get("classical") or {}).get("score"),
            "ai_percent_transformer": (sc.get("transformer") or {}).get("score"),
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out, average, classes


@router.get("/student/my-codes")
def my_codes(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    """Every file the student has written: latest code, when, and for which course."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Student access only")
    files, average, classes = student_code_files(db, user.student_id)
    return {"files": files, "average": average, "classes": [
        {"course_name": c.course_name, "section": c.section, "semester": c.semester} for c in classes]}


# ══════════════════════════════════════════════════════════════
# Teacher: a student's code files + written feedback
# ══════════════════════════════════════════════════════════════
@router.get("/teacher/student/{student_id}/codes")
def teacher_student_codes(student_id: str, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    from .teacher import _require_visible
    _require_visible(db, teacher, student_id)
    files, average, _ = student_code_files(db, student_id)
    return {"files": files, "average": average}


class NoteRequest(BaseModel):
    text: str
    filename: Optional[str] = None


@router.post("/teacher/student/{student_id}/notes")
def add_student_note(student_id: str, data: NoteRequest, db: Session = Depends(get_db),
                     teacher=Depends(auth.get_teacher_user)):
    from .teacher import _require_visible
    _require_visible(db, teacher, student_id)
    text = (data.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Write some feedback first")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="Feedback is too long (4000 characters max)")
    db.add(models.StudentNote(student_id=student_id, teacher_id=teacher.student_id,
                              filename=(data.filename or None), text=text))
    db.commit()
    return {"success": True, "message": "Feedback sent to the student"}


@router.get("/teacher/student/{student_id}/notes")
def list_student_notes(student_id: str, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    from .teacher import _require_visible
    _require_visible(db, teacher, student_id)
    rows = db.query(models.StudentNote).filter(models.StudentNote.student_id == student_id).order_by(
        models.StudentNote.id.desc()).limit(50).all()
    return [{"id": r.id, "filename": r.filename, "text": r.text, "created_at": _iso(r.created_at)} for r in rows]


# ══════════════════════════════════════════════════════════════
# Admin: overview + all datasets
# ══════════════════════════════════════════════════════════════
@router.get("/admin/overview")
def admin_overview(db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    cutoff = datetime.utcnow() - timedelta(days=DATASET_RETENTION_DAYS)
    teachers = db.query(models.User).filter(models.User.role == "teacher").order_by(models.User.id).all()
    students = db.query(func.count(models.User.id)).filter(models.User.role == "student").scalar() or 0
    admins = db.query(func.count(models.User.id)).filter(models.User.role == "admin").scalar() or 0

    class_counts = dict(db.query(models.Classroom.teacher_id, func.count(models.Classroom.id))
                        .group_by(models.Classroom.teacher_id).all())
    member_counts = dict(db.query(models.Classroom.teacher_id, func.count(models.ClassMember.id))
                         .join(models.ClassMember, models.ClassMember.class_id == models.Classroom.id)
                         .group_by(models.Classroom.teacher_id).all())
    live = db.query(func.count(models.StudentSession.id)).filter(
        models.StudentSession.is_connected == True).scalar() or 0       # noqa: E712

    return {
        "totals": {
            "teachers":      len(teachers),
            "students":      students,
            "admins":        admins,
            "classes":       db.query(func.count(models.Classroom.id)).scalar() or 0,
            "online_now":    live,
            "snapshots":     db.query(func.count(models.CodeSnapshot.id)).scalar() or 0,
            "submissions":   db.query(func.count(models.Submission.id)).scalar() or 0,
            "datasets":      db.query(func.count(models.ClassDataset.id)).filter(
                                 models.ClassDataset.created_at > cutoff).scalar() or 0,
        },
        "teachers": [{
            "id":         t.student_id,
            "name":       t.full_name,
            "email":      t.email,
            "classes":    class_counts.get(t.student_id, 0),
            "students":   member_counts.get(t.student_id, 0),
            "joined":     _iso(t.created_at),
        } for t in teachers],
    }


@router.get("/admin/datasets")
def admin_datasets(db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    cutoff = datetime.utcnow() - timedelta(days=DATASET_RETENTION_DAYS)
    rows = db.query(models.ClassDataset, models.Classroom).join(
        models.Classroom, models.Classroom.id == models.ClassDataset.class_id
    ).filter(models.ClassDataset.created_at > cutoff).order_by(models.ClassDataset.id.desc()).all()
    names = {u.student_id: u.full_name for u in db.query(models.User).filter(
        models.User.student_id.in_({c.teacher_id for _, c in rows})).all()} if rows else {}
    return [{
        "id":         d.id,
        "filename":   d.filename,
        "size_bytes": d.size_bytes,
        "course":     c.course_name,
        "section":    c.section,
        "semester":   c.semester,
        "teacher":    names.get(c.teacher_id) or c.teacher_id,
        "created_at": _iso(d.created_at),
        "expires_at": _iso(d.created_at + timedelta(days=DATASET_RETENTION_DAYS)) if d.created_at else None,
    } for d, c in rows]


@router.get("/admin/datasets/{dataset_id}/download")
def admin_dataset_download(dataset_id: int, format: str = "json", db: Session = Depends(get_db),
                           admin=Depends(auth.get_admin_user)):
    cutoff = datetime.utcnow() - timedelta(days=DATASET_RETENTION_DAYS)
    d = db.query(models.ClassDataset).filter(
        models.ClassDataset.id == dataset_id, models.ClassDataset.created_at > cutoff).first()
    if not d:
        raise HTTPException(status_code=404, detail="Dataset not found (it may have expired)")
    from .rooms import dataset_response
    return dataset_response(d, format)


# ══════════════════════════════════════════════════════════════
# Teacher: "this student is leaving early" alerts
# ══════════════════════════════════════════════════════════════
@router.get("/teacher/alerts")
def teacher_alerts(since: Optional[int] = None, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    """EARLY_LOGOUT events from students of this teacher's classes that have a
    lab running. Without `since` it only returns the current position (so old
    events are not replayed when the page opens); later calls pass back `last_id`."""
    last_id = db.query(func.max(models.StudentEvent.id)).scalar() or 0
    if since is None:
        return {"last_id": last_id, "alerts": []}

    live = db.query(models.Classroom).join(
        models.LabSession, models.LabSession.class_id == models.Classroom.id
    ).filter(models.LabSession.is_active == True).filter(                      # noqa: E712
        True if teacher.role == "admin" else models.Classroom.teacher_id == teacher.student_id).all()
    if not live:
        return {"last_id": last_id, "alerts": []}

    members = {}
    for c in live:
        for m in db.query(models.ClassMember).filter(models.ClassMember.class_id == c.id).all():
            members.setdefault(m.student_id, c)
    if not members:
        return {"last_id": last_id, "alerts": []}

    rows = db.query(models.StudentEvent).filter(
        models.StudentEvent.event_type == "EARLY_LOGOUT",
        models.StudentEvent.id > since,
        models.StudentEvent.student_id.in_(list(members.keys())),
    ).order_by(models.StudentEvent.id.asc()).limit(50).all()
    names = {u.student_id: u.full_name for u in db.query(models.User).filter(
        models.User.student_id.in_({r.student_id for r in rows})).all()} if rows else {}

    import json as _json
    out = []
    for r in rows:
        try:
            d = _json.loads(r.details or "{}")
        except Exception:
            d = {}
        c = members.get(r.student_id)
        out.append({
            "id":         r.id,
            "student_id": r.student_id,
            "name":       names.get(r.student_id) or r.student_id,
            "course":     c.course_name if c else None,
            "section":    c.section if c else None,
            "reason":     d.get("reason") or "logout",
            "at":         _iso(r.timestamp),
        })
    return {"last_id": last_id, "alerts": out}
