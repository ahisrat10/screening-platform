"""Readable class datasets.

A dataset is one JSON file per finished lab. It is meant to be read by people
(and loaded into a spreadsheet), so every student is one tidy record:

    students[]
        name, student_id, status
        session      when they joined / left, minutes
        summary      pastes (and where from), other apps used, tab switches,
                     typing and undo/redo counts, final suspicion score,
                     average AI percentage
        other_apps   which app they went to, when, for how long
        pastes       every paste: when, how many characters, copied from where
        files[]      each file with its code and its own AI percentage
        timeline[]   notable events in time order

Times are local (UTC+LOCAL_UTC_OFFSET_HOURS, default Bangladesh time).
VS Code does not tell an extension which individual keys were pressed, so the
"keys" part is counts of actions (typing, pastes, undo, redo), not key names.

`dataset_to_xlsx` turns the same JSON into an Excel workbook (sheets:
Students, Files, Activity).
"""
import io
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from . import models

log = logging.getLogger("screening")
LOCAL_UTC_OFFSET_HOURS = int(os.getenv("LOCAL_UTC_OFFSET_HOURS", "6"))
FORMAT_VERSION = "2.0-readable"
MAX_CODE_CHARS = 50000

# Events worth listing in the timeline (the rest is noise such as every window blur).
NOTABLE = {
    "PASTE_DETECTED":        "Paste",
    "EXTERNAL_APP_USED":     "Used another app",
    "RAPID_TAB_SWITCHING":   "Switched windows quickly",
    "LONG_ABSENCE":          "Away from VS Code for a long time",
    "STUDENT_IDLE":          "Idle",
    "SUDDEN_SKILL_JUMP":     "Sudden jump in code quality",
    "LARGE_DELETION":        "Large deletion",
    "FAST_BURST_TYPING":     "Very fast typing burst",
    "SCREEN_FREEZE_DETECTED": "Screen froze",
    "AI_DETECTION_RESULT":   "AI detection ran",
    "EXAM_MODE_ENABLED":     "Lab started",
    "EXAM_MODE_DISABLED":    "Lab ended",
    "EARLY_LOGOUT":          "Left before the lab ended",
}


def _local(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return (dt + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)).strftime("%Y-%m-%d %H:%M:%S")


def _details(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _describe(etype: str, d: dict) -> str:
    if etype == "PASTE_DETECTED":
        src = d.get("source_app") if d.get("paste_source") == "external" else "VS Code"
        if d.get("copy_from_same_file"):
            src = "the same file"
        return f"{d.get('chars_pasted', '?')} characters pasted, copied from {src or 'another app'}"
    if etype == "EXTERNAL_APP_USED":
        return f"{d.get('app', '?')} for {d.get('away_duration_seconds', '?')}s"
    if etype == "EARLY_LOGOUT":
        return "closed VS Code" if d.get("reason") == "closed" else "logged out"
    return d.get("message") or ""


def _student_record(db: Session, user: models.User, since, until, file_scores: dict) -> dict:
    sid = user.student_id

    ev_q = db.query(models.StudentEvent).filter(models.StudentEvent.student_id == sid)
    if since:
        ev_q = ev_q.filter(models.StudentEvent.timestamp >= since)
    if until:
        ev_q = ev_q.filter(models.StudentEvent.timestamp <= until)
    events = ev_q.order_by(models.StudentEvent.timestamp.asc(), models.StudentEvent.id.asc()).all()

    pastes, other_apps, timeline = [], [], []
    app_totals: dict = {}
    summary_evt = {}
    unfocused = 0
    for e in events:
        d = _details(e.details)
        t = _local(e.timestamp)
        if e.event_type == "SESSION_SUMMARY":
            summary_evt = d                       # keep the last one
            continue
        if e.event_type == "WINDOW_UNFOCUSED":
            unfocused += 1
        if e.event_type == "PASTE_DETECTED":
            external = d.get("paste_source") == "external"
            pastes.append({
                "time": t,
                "characters": d.get("chars_pasted"),
                "copied_from": (d.get("source_app") or "another app") if external else
                               ("the same file" if d.get("copy_from_same_file") else "inside VS Code"),
                "from_other_app": external,
                "preview": d.get("paste_preview"),
            })
        if e.event_type == "EXTERNAL_APP_USED":
            secs = d.get("away_duration_seconds") or 0
            other_apps.append({"time": t, "app": d.get("app"), "seconds": secs, "score_points": d.get("points")})
            a = app_totals.setdefault(d.get("app") or "unknown", {"visits": 0, "seconds": 0})
            a["visits"] += 1
            a["seconds"] += secs if isinstance(secs, (int, float)) else 0
        if e.event_type in NOTABLE:
            timeline.append({"time": t, "event": NOTABLE[e.event_type],
                             "detail": _describe(e.event_type, d), "severity": e.severity})

    # code: latest version of each file written in the lab window
    latest: dict = {}

    def take(filename, language, code, ts):
        if not filename or not code:
            return
        cur = latest.get(filename)
        if cur is None or (ts and (cur["_ts"] is None or ts >= cur["_ts"])):
            latest[filename] = {"filename": filename, "language": language, "code": code, "_ts": ts}

    sn_q = db.query(models.CodeSnapshot).filter(models.CodeSnapshot.student_id == sid)
    sb_q = db.query(models.Submission).filter(models.Submission.student_id == sid)
    if since:
        sn_q = sn_q.filter(models.CodeSnapshot.timestamp >= since)
        sb_q = sb_q.filter(models.Submission.timestamp >= since)
    if until:
        sn_q = sn_q.filter(models.CodeSnapshot.timestamp <= until)
        sb_q = sb_q.filter(models.Submission.timestamp <= until)
    for r in sb_q.all():
        take(r.filename, r.language, r.code, r.timestamp)
    for r in sn_q.all():
        take(r.filename, r.language, r.code, r.timestamp)

    files = []
    for name, f in sorted(latest.items()):
        sc = file_scores.get(name) or {}
        code = f["code"] or ""
        files.append({
            "filename":   name,
            "language":   f["language"],
            "last_saved": _local(f["_ts"]),
            "lines":      code.count("\n") + 1,
            "ai_percent": sc.get("combined"),
            "ai_percent_classical":   (sc.get("classical") or {}).get("score"),
            "ai_percent_transformer": (sc.get("transformer") or {}).get("score"),
            "code":       code[:MAX_CODE_CHARS],
        })
    pcs = [f["ai_percent"] for f in files if f["ai_percent"] is not None]

    session = db.query(models.StudentSession).filter(models.StudentSession.student_id == sid).first()
    joined = bool(events or files or (session and session.connected_at and (not since or session.connected_at >= since)))
    started = session.connected_at if session and joined else None
    ended = None
    if session and joined:
        ended = session.disconnected_at if (session.disconnected_at and not session.is_connected) else session.last_heartbeat
    duration = None
    if started and ended and ended >= started:
        duration = round((ended - started).total_seconds() / 60, 1)

    ext_pastes = [p for p in pastes if p["from_other_app"]]
    return {
        "name":       user.full_name,
        "student_id": sid,
        "status":     "took part" if joined else "did not join",
        "session": {
            "joined_at":        _local(started),
            "left_at":          _local(ended),
            "duration_minutes": duration,
        },
        "summary": {
            "suspicion_score_final":      (session.suspicious_score if session and joined else 0) or 0,
            "ai_percent_average":         round(sum(pcs) / len(pcs), 1) if pcs else None,
            "pastes_total":               len(pastes),
            "pastes_from_other_apps":     len(ext_pastes),
            "pastes_inside_vscode":       len(pastes) - len(ext_pastes),
            "other_apps_used":            app_totals,
            "times_left_vscode":          unfocused,
            "keystrokes":                 summary_evt.get("keystrokes"),
            "undo_count":                 summary_evt.get("undo"),
            "redo_count":                 summary_evt.get("redo"),
            "left_before_lab_ended":      any(t["event"] == "Left before the lab ended" for t in timeline),
        },
        "other_apps": other_apps,
        "pastes":     pastes,
        "files":      files,
        "timeline":   timeline,
    }


def build_readable_dataset(db: Session, c: models.Classroom, since: Optional[datetime],
                           until: Optional[datetime] = None) -> dict:
    members = db.query(models.User).join(
        models.ClassMember, models.ClassMember.student_id == models.User.student_id
    ).filter(models.ClassMember.class_id == c.id).order_by(models.User.student_id).all()

    try:
        from .main import _student_file_scores
    except Exception:           # keep the dataset buildable even if the ML stack is missing
        _student_file_scores = None

    students = []
    for u in members:
        scores = {}
        if _student_file_scores:
            try:
                files, _avg = _student_file_scores(db, u.student_id)
                scores = {f["filename"]: f for f in files}
            except Exception:
                log.warning("AI scores unavailable for %s", u.student_id, exc_info=True)
                db.rollback()
        students.append(_student_record(db, u, since, until, scores))

    return {
        "metadata": {
            "course":          c.course_name,
            "section":         c.section,
            "semester":        c.semester,
            "lab_started_at":  _local(since),
            "lab_ended_at":    _local(until),
            "exported_at":     _local(datetime.utcnow()),
            "timezone":        f"UTC{LOCAL_UTC_OFFSET_HOURS:+d}",
            "students_total":  len(students),
            "students_took_part": sum(1 for s in students if s["status"] == "took part"),
            "format":          FORMAT_VERSION,
            "note":            "VS Code does not expose individual key presses; keys are summarised as counts of typing, pastes, undo and redo.",
        },
        "students": students,
    }


# ══════════════════════════════════════════════════════════════
# Excel
# ══════════════════════════════════════════════════════════════
def dataset_to_xlsx(content: str) -> bytes:
    """Excel workbook from a stored dataset: Students, Files and Activity sheets."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    data = json.loads(content)
    meta = data.get("metadata", {})
    wb = Workbook()
    head_fill = PatternFill("solid", fgColor="23336A")
    head_font = Font(bold=True, color="FFFFFF")

    def sheet(ws, headers, rows, widths):
        ws.append(headers)
        for cell in ws[1]:
            cell.fill, cell.font = head_fill, head_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    students = data.get("students")
    if students is None:
        raise ValueError("This dataset is in the old format and has no table view.")

    ws = wb.active
    ws.title = "Students"
    rows = []
    for s in students:
        sm, se = s["summary"], s["session"]
        apps = ", ".join(f"{k} ({v['visits']}x, {int(v['seconds'])}s)" for k, v in (sm.get("other_apps_used") or {}).items())
        rows.append([s["name"], s["student_id"], s["status"], se.get("joined_at"), se.get("left_at"), se.get("duration_minutes"),
                     sm.get("suspicion_score_final"), sm.get("ai_percent_average"), sm.get("pastes_total"),
                     sm.get("pastes_from_other_apps"), sm.get("pastes_inside_vscode"), apps or "—",
                     sm.get("times_left_vscode"), sm.get("keystrokes"), sm.get("undo_count"), sm.get("redo_count"),
                     "Yes" if sm.get("left_before_lab_ended") else "No"])
    sheet(ws, ["Name", "Student ID", "Status", "Joined", "Left", "Minutes", "Suspicion score", "AI % (average)",
               "Pastes", "Pastes from other apps", "Pastes inside VS Code", "Other apps used", "Times left VS Code",
               "Keystrokes", "Undo", "Redo", "Left early"], rows,
          [26, 16, 13, 19, 19, 9, 11, 11, 8, 12, 12, 34, 11, 11, 7, 7, 10])

    ws2 = wb.create_sheet("Files")
    rows = []
    for s in students:
        for f in s["files"]:
            rows.append([s["name"], s["student_id"], f["filename"], f["language"], f["last_saved"], f["lines"],
                         f["ai_percent"], f["code"]])
    sheet(ws2, ["Name", "Student ID", "File", "Language", "Last saved", "Lines", "AI %", "Code"], rows,
          [26, 16, 24, 11, 19, 7, 8, 100])
    for row in ws2.iter_rows(min_row=2):
        row[-1].alignment = Alignment(wrap_text=True, vertical="top")

    ws3 = wb.create_sheet("Activity")
    rows = []
    for s in students:
        for t in s["timeline"]:
            rows.append([s["name"], s["student_id"], t["time"], t["event"], t["detail"], t["severity"]])
    sheet(ws3, ["Name", "Student ID", "Time", "What happened", "Detail", "Severity"], rows, [26, 16, 19, 34, 60, 10])

    ws4 = wb.create_sheet("About")
    for k, v in meta.items():
        ws4.append([k, str(v)])
    ws4.column_dimensions["A"].width = 22
    ws4.column_dimensions["B"].width = 100

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
