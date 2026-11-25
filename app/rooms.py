"""
Room-level features:

  * Room roster   POST /teacher/classes/{id}/roster/upload   (Excel/CSV -> students + class members)
  * Teacher code  GET/POST /admin/teacher-code, POST /auth/register-teacher
  * Datasets      one JSON per finished lab, stored per class, kept 30 days
                  GET    /teacher/classes/{id}/datasets
                  GET    /teacher/classes/{id}/datasets/{dataset_id}/download
                  DELETE /teacher/classes/{id}/datasets/{dataset_id}
                  POST   /teacher/classes/{id}/datasets/generate   (make one on demand)
  * Student side  GET /student/my-feedback

Dataset content lives in Postgres (not on disk) because the host's disk is
ephemeral.
"""
import hmac
import io
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from . import auth, models
from .classes import _owned_class
from .database import get_db

router = APIRouter(tags=["rooms"])

DATASET_RETENTION_DAYS = 30
DEFAULT_ROSTER_PASSWORD = "user@1234"      # same default as the admin bulk insert
TEACHER_CODE_KEY = "teacher_signup_code"
DEFAULT_TEACHER_CODE = "EWU_Safi Sir"          # used until the admin sets another one
LOCAL_UTC_OFFSET_HOURS = int(os.getenv("LOCAL_UTC_OFFSET_HOURS", "6"))   # Bangladesh


# ══════════════════════════════════════════════════════════════
# Which classes is a student in? (used by the extension login)
# ══════════════════════════════════════════════════════════════
def student_classes(db: Session, student_id: str) -> list:
    rows = db.query(models.Classroom).join(
        models.ClassMember, models.ClassMember.class_id == models.Classroom.id
    ).filter(models.ClassMember.student_id == student_id).all()
    return [{"class_id": c.id, "course_name": c.course_name,
             "section": c.section, "semester": c.semester} for c in rows]


# ══════════════════════════════════════════════════════════════
# Roster upload (per room)
# ══════════════════════════════════════════════════════════════
_HEADER_ALIASES = {
    "student_id": ("student_id", "studentid", "id", "student id"),
    "full_name":  ("full_name", "fullname", "name", "full name", "student name"),
    "email":      ("email", "e-mail", "mail"),
}


def _col(header_map: dict, key: str) -> Optional[int]:
    for alias in _HEADER_ALIASES[key]:
        if alias in header_map:
            return header_map[alias]
    return None


def parse_roster(filename: str, contents: bytes) -> list:
    """Return [(student_id, full_name, email)]. Only a student ID column is required."""
    name = (filename or "").lower()
    rows = []
    if name.endswith(".csv"):
        import csv
        text = contents.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        data = list(reader)
    elif name.endswith(".xlsx"):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
        except Exception:
            raise HTTPException(status_code=400, detail="Could not read the .xlsx file — is it a valid Excel file?")
        data = [list(r) for r in wb.active.iter_rows(values_only=True)]
    else:
        raise HTTPException(status_code=400, detail="Upload a .xlsx or .csv file")

    if not data:
        raise HTTPException(status_code=400, detail="The file is empty")
    header_map = {str(h).strip().lower(): i for i, h in enumerate(data[0]) if h is not None}
    i_id = _col(header_map, "student_id")
    if i_id is None:
        raise HTTPException(status_code=400, detail="The first row must have a 'student_id' column")
    i_name = _col(header_map, "full_name")
    i_mail = _col(header_map, "email")

    def cell(row, i):
        if i is None or i >= len(row) or row[i] is None:
            return ""
        return str(row[i]).strip()

    for row in data[1:]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        sid = cell(row, i_id)
        if sid:
            rows.append((sid, cell(row, i_name), cell(row, i_mail)))
    return rows


@router.post("/teacher/classes/{class_id}/roster/upload")
async def upload_roster(class_id: int, file: UploadFile = File(...),
                        db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    """Excel/CSV of student IDs for this room. Unknown IDs get an account
    (default password); every ID becomes a member of this class, so they can
    log in from the VS Code extension."""
    c = _owned_class(db, teacher, class_id)
    rows = parse_roster(file.filename, await file.read())
    if len(rows) > 2000:
        raise HTTPException(status_code=400, detail="At most 2000 students per upload")

    pwd_hash = auth.hash_password(DEFAULT_ROSTER_PASSWORD)
    created = added = already = skipped = 0
    seen = set()
    for sid, name, email in rows:
        if sid in seen:
            continue
        seen.add(sid)
        user = db.query(models.User).filter(models.User.student_id == sid).first()
        if user is None:
            email = email or f"{sid}@std.ewubd.edu"
            if db.query(models.User).filter(models.User.email == email).first():
                skipped += 1            # the email belongs to a different account
                continue
            user = models.User(student_id=sid, full_name=name or sid, email=email,
                               password_hash=pwd_hash, role="student", is_active=True)
            db.add(user)
            db.flush()
            created += 1
        elif user.role != "student":
            skipped += 1                # never turn a teacher/admin into a member
            continue
        if db.query(models.ClassMember).filter(
                models.ClassMember.class_id == c.id, models.ClassMember.student_id == sid).first():
            already += 1
        else:
            db.add(models.ClassMember(class_id=c.id, student_id=sid))
            added += 1
    db.commit()
    return {"success": True, "message": f"{added} added to {c.course_name}",
            "total_rows": len(rows), "accounts_created": created,
            "added_to_class": added, "already_in_class": already, "skipped": skipped}


# ══════════════════════════════════════════════════════════════
# Teacher sign-up code (set by admin)
# ══════════════════════════════════════════════════════════════
def _get_teacher_code(db: Session) -> Optional[str]:
    row = db.query(models.AppSetting).filter(models.AppSetting.key == TEACHER_CODE_KEY).first()
    if row and row.value:
        return row.value
    return os.getenv("TEACHER_SIGNUP_CODE") or DEFAULT_TEACHER_CODE


class TeacherCodeRequest(BaseModel):
    code: str


@router.get("/admin/teacher-code")
def get_teacher_code(db: Session = Depends(get_db), admin=Depends(auth.get_admin_user)):
    return {"code": _get_teacher_code(db)}


@router.post("/admin/teacher-code")
def set_teacher_code(data: TeacherCodeRequest, db: Session = Depends(get_db),
                     admin=Depends(auth.get_admin_user)):
    code = data.code.strip()
    if len(code) < 6 or len(code) > 64:
        raise HTTPException(status_code=400, detail="The code must be 6-64 characters")
    row = db.query(models.AppSetting).filter(models.AppSetting.key == TEACHER_CODE_KEY).first()
    if row:
        row.value = code
    else:
        db.add(models.AppSetting(key=TEACHER_CODE_KEY, value=code))
    db.commit()
    return {"success": True, "message": "Teacher sign-up code updated", "code": code}


class RegisterTeacherRequest(BaseModel):
    code:      str
    email:     EmailStr
    full_name: str
    password:  str


@router.post("/auth/register-teacher")
def register_teacher_with_code(data: RegisterTeacherRequest, db: Session = Depends(get_db)):
    """A teacher opens their own account with the code the admin gave them."""
    expected = _get_teacher_code(db)
    if not expected:
        raise HTTPException(status_code=403, detail="Teacher sign-up is not open. Ask the admin.")
    if not hmac.compare_digest(expected.encode(), data.code.strip().encode()):
        raise HTTPException(status_code=403, detail="Invalid teacher code")
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if db.query(models.User).filter(models.User.email == data.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    base = "teacher-" + (re.sub(r"[^a-z0-9]+", "-", data.email.split("@")[0].lower()).strip("-") or "user")
    login_id, n = base, 1
    while db.query(models.User).filter(models.User.student_id == login_id).first():
        n += 1
        login_id = f"{base}-{n}"
    db.add(models.User(student_id=login_id, email=data.email, full_name=data.full_name.strip(),
                       password_hash=auth.hash_password(data.password), role="teacher", is_active=True))
    db.commit()
    return {"success": True, "message": f"Teacher account created. Your login ID is {login_id}",
            "student_id": login_id}


# ══════════════════════════════════════════════════════════════
# Datasets (one per lab, per class)
# ══════════════════════════════════════════════════════════════
def _local_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)


def dataset_filename(db: Session, c: models.Classroom) -> str:
    """e.g. 2026-6-26_cse438.json (date not zero-padded, course name lowercased).
    A second dataset for the same class and day gets _2, _3, ..."""
    d = _local_now()
    slug = re.sub(r"[^a-z0-9]+", "", (c.course_name or "class").lower()) or "class"
    base = f"{d.year}-{d.month}-{d.day}_{slug}"
    name, n = f"{base}.json", 1
    while db.query(models.ClassDataset).filter(
            models.ClassDataset.class_id == c.id, models.ClassDataset.filename == name).first():
        n += 1
        name = f"{base}_{n}.json"
    return name


def build_class_dataset(db: Session, c: models.Classroom, since: Optional[datetime],
                        until: Optional[datetime] = None) -> dict:
    """One readable record per student (see app/dataset.py)."""
    from .dataset import build_readable_dataset
    return build_readable_dataset(db, c, since, until)


def save_dataset_for_lab(db: Session, c: models.Classroom, lab: Optional[models.LabSession],
                         created_by: Optional[str]) -> models.ClassDataset:
    since = lab.started_at if lab is not None else None
    until = lab.ended_at if lab is not None else None
    content = json.dumps(build_class_dataset(db, c, since, until), ensure_ascii=False, allow_nan=False)
    row = models.ClassDataset(class_id=c.id, filename=dataset_filename(db, c), content=content,
                              size_bytes=len(content.encode("utf-8")), created_by=created_by)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _live_datasets(db: Session, class_id: int):
    cutoff = datetime.utcnow() - timedelta(days=DATASET_RETENTION_DAYS)
    return db.query(models.ClassDataset).filter(
        models.ClassDataset.class_id == class_id, models.ClassDataset.created_at > cutoff)


def purge_expired_datasets() -> int:
    """Called daily by the scheduler. Listing/downloading also filters by age,
    so an expired dataset is never served even if this job missed a run."""
    from .database import SessionLocal
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=DATASET_RETENTION_DAYS)
        n = db.query(models.ClassDataset).filter(models.ClassDataset.created_at <= cutoff).delete()
        db.commit()
        return n
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


@router.get("/teacher/classes/{class_id}/datasets")
def list_datasets(class_id: int, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    rows = _live_datasets(db, c.id).order_by(models.ClassDataset.created_at.desc(),
                                             models.ClassDataset.id.desc()).all()
    return [{"id": r.id, "filename": r.filename, "size_bytes": r.size_bytes,
             "created_at": r.created_at.isoformat() + "Z",
             "expires_at": (r.created_at + timedelta(days=DATASET_RETENTION_DAYS)).isoformat() + "Z"}
            for r in rows]


def dataset_response(row: models.ClassDataset, fmt: str = "json") -> Response:
    """The stored JSON as it is, or the same data as an Excel workbook."""
    if fmt == "xlsx":
        from .dataset import dataset_to_xlsx
        try:
            data = dataset_to_xlsx(row.content)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        name = re.sub(r"\.json$", "", row.filename) + ".xlsx"
        return Response(content=data,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return Response(content=row.content, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{row.filename}"'})


@router.get("/teacher/classes/{class_id}/datasets/{dataset_id}/download")
def download_dataset(class_id: int, dataset_id: int, format: str = "json", db: Session = Depends(get_db),
                     teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    r = _live_datasets(db, c.id).filter(models.ClassDataset.id == dataset_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Dataset not found (it may have expired)")
    return dataset_response(r, format)


@router.delete("/teacher/classes/{class_id}/datasets/{dataset_id}")
def delete_dataset(class_id: int, dataset_id: int, db: Session = Depends(get_db),
                   teacher=Depends(auth.get_teacher_user)):
    c = _owned_class(db, teacher, class_id)
    n = db.query(models.ClassDataset).filter(
        models.ClassDataset.id == dataset_id, models.ClassDataset.class_id == c.id).delete()
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {"success": True, "message": "Dataset deleted"}


@router.post("/teacher/classes/{class_id}/datasets/generate")
def generate_dataset(class_id: int, db: Session = Depends(get_db), teacher=Depends(auth.get_teacher_user)):
    """Build a dataset now from the class's most recent lab (or everything if no lab ran)."""
    c = _owned_class(db, teacher, class_id)
    lab = db.query(models.LabSession).filter(models.LabSession.class_id == c.id).order_by(
        models.LabSession.id.desc()).first()
    r = save_dataset_for_lab(db, c, lab, teacher.student_id)
    return {"success": True, "message": f"Saved {r.filename}", "id": r.id, "filename": r.filename}


# ══════════════════════════════════════════════════════════════
# Student side: my feedback + my AI percentages
# ══════════════════════════════════════════════════════════════
@router.get("/student/my-feedback")
def my_feedback(db: Session = Depends(get_db), user=Depends(auth.get_current_user)):
    """What the teacher said about this student's work, plus the student's own
    per-file and average AI percentages."""
    if user.role != "student":
        raise HTTPException(status_code=403, detail="Student access only")
    sid = user.student_id

    labels = db.query(models.TeacherLabel).filter(models.TeacherLabel.student_id == sid).order_by(
        models.TeacherLabel.created_at.desc(), models.TeacherLabel.id.desc()).limit(50).all()
    notes = db.query(models.TeacherFeedback).filter(models.TeacherFeedback.student_id == sid).order_by(
        models.TeacherFeedback.created_at.desc(), models.TeacherFeedback.id.desc()).limit(50).all()
    teachers = {u.student_id: u.full_name for u in db.query(models.User).filter(
        models.User.student_id.in_({r.teacher_id for r in list(labels) + list(notes) if r.teacher_id})).all()
    } if (labels or notes) else {}

    notes_new = db.query(models.StudentNote).filter(models.StudentNote.student_id == sid).order_by(
        models.StudentNote.id.desc()).limit(50).all()
    names = {u.student_id: u.full_name for u in db.query(models.User).filter(
        models.User.student_id.in_({n.teacher_id for n in notes_new if n.teacher_id})).all()} if notes_new else {}

    from .main import _student_file_scores
    files, average = _student_file_scores(db, sid)
    return {
        "feedback": sorted(
            [{"kind": "note", "text": n.text, "verdict": None, "confidence": None, "filename": n.filename,
              "teacher": names.get(n.teacher_id), "at": n.created_at.isoformat() + "Z" if n.created_at else None}
             for n in notes_new] + 
            [{"kind": "label", "text": l.notes, "verdict": l.label, "confidence": l.confidence,
              "teacher": teachers.get(l.teacher_id), "at": l.created_at.isoformat() + "Z" if l.created_at else None}
             for l in labels if l.notes or l.label] +
            [{"kind": "submission", "text": n.notes, "verdict": n.teacher_label, "confidence": None,
              "teacher": teachers.get(n.teacher_id), "at": n.created_at.isoformat() + "Z" if n.created_at else None}
             for n in notes if n.notes or n.teacher_label],
            key=lambda x: x["at"] or "", reverse=True),
        "files": files,
        "average": average,
    }
