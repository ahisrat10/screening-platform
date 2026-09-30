from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
from contextlib import asynccontextmanager
import json, asyncio, logging
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .database import engine, get_db
from . import models, schemas, auth
from .teacher import router as teacher_router
from .email_service import generate_otp, save_otp, send_otp_email, send_welcome_email

models.Base.metadata.create_all(bind=engine)

# ══════════════════════════════════════════════════════════════
# WEBSOCKET MANAGER
# ══════════════════════════════════════════════════════════════
class ConnectionManager:
    def __init__(self):
        self.admin_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.admin_connections.append(websocket)
        print(f"Admin WS connected. Total: {len(self.admin_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.admin_connections:
            self.admin_connections.remove(websocket)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.admin_connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.admin_connections:
                self.admin_connections.remove(ws)

manager   = ConnectionManager()
main_loop: asyncio.AbstractEventLoop = None

def broadcast_event(message: dict):
    global main_loop
    if main_loop and not main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(message), main_loop
        )

# ══════════════════════════════════════════════════════════════
# LIFESPAN
# ══════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_event_loop()
    print("Server started. Event loop captured for WebSocket broadcasting.")
    yield
    print("Server shutting down.")

# ══════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════
app = FastAPI(
    title       = "Automated Technical Screening Platform",
    description = "Backend API — VS Code Extension + Admin Dashboard",
    version     = "3.1.0",
    lifespan    = lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = [
        "https://screening-platform-production.up.railway.app",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

app.include_router(teacher_router)

logger = logging.getLogger("screening")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Any uncaught error -> JSON instead of plain-text "Internal Server Error".
    HTTPException / validation errors keep their own handlers."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})

# ══════════════════════════════════════════════════════════════
# JWT VERIFICATION FOR EXTENSION ROUTES
# ══════════════════════════════════════════════════════════════
def get_student_from_token(
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Login to the extension first."
        )
    token   = authorization.split(" ")[1]
    payload = auth.decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=401,
            detail="Token expired or invalid. Please login again."
        )

    # A valid signature isn't enough: the account must still exist and be active.
    user = db.query(models.User).filter(
        models.User.student_id == payload.get("sub")
    ).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=401,
            detail="Account not found or deactivated. Please login again."
        )
    return user.student_id

# ══════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.post("/auth/register", response_model=schemas.MessageResponse)
def register(data: schemas.RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first():
        raise HTTPException(status_code=400, detail="Student ID already registered")
    if db.query(models.User).filter(
        models.User.email == data.email
    ).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = models.User(
        student_id    = data.student_id,
        email         = data.email,
        full_name     = data.full_name,
        password_hash = auth.hash_password(data.password),
        role          = "student",
        is_active     = True
    )
    db.add(user)
    db.commit()
    send_welcome_email(data.email, data.student_id, data.full_name)
    return {"message": f"Account created! Welcome {data.full_name}", "success": True}


@app.post("/auth/register-admin", response_model=schemas.MessageResponse)
def register_admin(
    data: schemas.AdminRegisterRequest,
    db:   Session = Depends(get_db),
    _guard = Depends(auth.get_admin_if_any_exist),
):
    if db.query(models.User).filter(
        models.User.email == data.email
    ).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    # No student ID for admins: generate a unique login ID from the email.
    # (users.student_id is the login key / JWT subject everywhere else.)
    import re
    base     = "admin-" + (re.sub(r"[^a-z0-9]+", "-", data.email.split("@")[0].lower()).strip("-") or "user")
    admin_id = base
    n = 1
    while db.query(models.User).filter(models.User.student_id == admin_id).first():
        n += 1
        admin_id = f"{base}-{n}"

    admin = models.User(
        student_id    = admin_id,
        email         = data.email,
        full_name     = data.full_name,
        password_hash = auth.hash_password(data.password),
        role          = "admin",
        is_active     = True
    )
    db.add(admin)
    db.commit()
    return {
        "message": f"Admin account created for {data.full_name}! Log in with your email or ID: {admin_id}",
        "success": True,
    }


@app.post("/auth/login", response_model=schemas.TokenResponse)
def login(data: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()
    # Admins have no student ID, so allow logging in with the account email.
    if not user and "@" in data.student_id:
        user = db.query(models.User).filter(
            models.User.email == data.student_id.strip()
        ).first()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid Student ID or password")

    if user.locked_until and user.locked_until > datetime.utcnow():
        raise HTTPException(
            status_code=423,
            detail=f"Account locked until {user.locked_until.strftime('%H:%M:%S')}"
        )

    if not auth.verify_password(data.password, user.password_hash):
        user.failed_attempts = (user.failed_attempts or 0) + 1
        if user.failed_attempts >= 5:
            from datetime import timedelta
            user.locked_until    = datetime.utcnow() + timedelta(minutes=30)
            user.failed_attempts = 0
            db.commit()
            raise HTTPException(
                status_code=423,
                detail="Account locked for 30 minutes due to too many failed attempts."
            )
        db.commit()
        remaining = 5 - user.failed_attempts
        raise HTTPException(
            status_code=401,
            detail=f"Wrong password. {remaining} attempts remaining."
        )

    user.failed_attempts = 0
    user.locked_until    = None
    db.commit()

    token = auth.create_token({"sub": user.student_id, "role": user.role})
    return {
        "access_token": token,
        "token_type":   "bearer",
        "student_id":   user.student_id,
        "full_name":    user.full_name,
        "role":         user.role
    }


# In-process limiter so a 6-digit OTP can't be brute-forced.
# LIMITATION: state lives in this process's memory, so it resets on restart and
# is not shared between workers. That is fine while we run a single Railway
# worker; move it to a shared store (e.g. Redis) before scaling out.
_otp_failures: dict = {}
_OTP_MAX_FAILS  = 5
_OTP_WINDOW_SEC = 600


def _otp_check_locked(student_id: str):
    import time
    now   = time.time()
    fails = [t for t in _otp_failures.get(student_id, []) if now - t < _OTP_WINDOW_SEC]
    _otp_failures[student_id] = fails
    if len(fails) >= _OTP_MAX_FAILS:
        raise HTTPException(status_code=429, detail="Too many invalid OTP attempts. Try again in 10 minutes.")


def _otp_record_failure(student_id: str):
    import time
    _otp_failures.setdefault(student_id, []).append(time.time())


@app.post("/auth/forgot-password", response_model=schemas.MessageResponse)
def forgot_password(data: schemas.ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.student_id == data.student_id,
        models.User.email      == data.email
    ).first()

    # Same response whether or not the account exists (no account enumeration),
    # including when the email fails to send.
    generic = {"message": "If this account exists, a reset code has been sent", "success": True}

    if not user:
        return generic

    otp = generate_otp()
    save_otp(data.student_id, data.email, otp, db)
    if not send_otp_email(data.email, data.student_id, otp):
        logger.error("Failed to send OTP email for %s", data.student_id)

    return generic


@app.post("/auth/verify-otp", response_model=schemas.MessageResponse)
def verify_otp(data: schemas.VerifyOTPRequest, db: Session = Depends(get_db)):
    _otp_check_locked(data.student_id)
    record = db.query(models.OTPCode).filter(
        models.OTPCode.student_id == data.student_id,
        models.OTPCode.otp_code   == data.otp_code,
        models.OTPCode.is_used    == False
    ).first()

    if not record:
        _otp_record_failure(data.student_id)
        raise HTTPException(status_code=400, detail="Invalid OTP code")
    if record.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OTP expired. Request a new one.")

    return {"message": "OTP verified", "success": True}


@app.post("/auth/reset-password", response_model=schemas.MessageResponse)
def reset_password(data: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    _otp_check_locked(data.student_id)
    record = db.query(models.OTPCode).filter(
        models.OTPCode.student_id == data.student_id,
        models.OTPCode.otp_code   == data.otp_code,
        models.OTPCode.is_used    == False
    ).first()

    if not record or record.expires_at < datetime.utcnow():
        _otp_record_failure(data.student_id)
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")
    user.password_hash   = auth.hash_password(data.new_password)
    user.failed_attempts = 0
    user.locked_until    = None
    record.is_used       = True
    db.commit()

    return {"message": "Password reset! You can now login.", "success": True}


@app.get("/auth/me")
def get_me(current_user = Depends(auth.get_current_user)):
    return {
        "student_id": current_user.student_id,
        "full_name":  current_user.full_name,
        "email":      current_user.email,
        "role":       current_user.role
    }

# ══════════════════════════════════════════════════════════════
# WEBSOCKET ENDPOINT
# ══════════════════════════════════════════════════════════════
@app.websocket("/ws/admin")
async def websocket_admin(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    # Browsers can't set headers on WebSockets, so the admin JWT comes as ?token=
    db = next(get_db())
    try:
        ok = auth.admin_from_token(token, db) is not None
    finally:
        db.close()
    if not ok:
        await websocket.close(code=1008)   # policy violation
        return
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ══════════════════════════════════════════════════════════════
# EXTENSION ROUTES — JWT protected
# ══════════════════════════════════════════════════════════════

@app.post("/student/connect")
def student_connect(
    data:             schemas.ConnectData,
    db:               Session = Depends(get_db),
    token_student_id: str     = Depends(get_student_from_token)
):
    if data.student_id != token_student_id:
        raise HTTPException(status_code=403, detail="Token does not match Student ID")

    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Student not registered on platform")

    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == data.student_id
    ).first()

    now = datetime.utcnow()   # ← single timestamp for consistency

    if session:
        session.is_connected     = True
        session.connected_at     = now
        session.last_heartbeat   = now   # ← KEY FIX: reset stale heartbeat
        session.suspicious_score = 0
    else:
        session = models.StudentSession(
            student_id       = data.student_id,
            is_connected     = True,
            connected_at     = now,
            last_heartbeat   = now,      # ← KEY FIX: set from start
            suspicious_score = 0
        )
        db.add(session)
    db.commit()

    broadcast_event({
        "type":       "STUDENT_CONNECTED",
        "student_id": data.student_id,
        "full_name":  user.full_name,
        "timestamp":  data.timestamp,
        "severity":   "INFO",
        "message":    f"✅ {user.full_name} joined the exam"
    })

    return {"status": "connected", "full_name": user.full_name}


@app.post("/student/disconnect")
def student_disconnect(
    data:             schemas.ConnectData,
    db:               Session = Depends(get_db),
    token_student_id: str     = Depends(get_student_from_token)
):
    if data.student_id != token_student_id:
        raise HTTPException(status_code=403, detail="Token mismatch")

    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()

    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == data.student_id
    ).first()
    if session:
        session.is_connected    = False
        session.disconnected_at = datetime.utcnow()
        db.commit()

    name = user.full_name if user else data.student_id
    broadcast_event({
        "type":       "STUDENT_DISCONNECTED",
        "student_id": data.student_id,
        "full_name":  name,
        "timestamp":  data.timestamp,
        "severity":   "WARNING",
        "message":    f"⚠️ {name} disconnected from exam!"
    })

    return {"status": "disconnected"}


@app.post("/student/event")
def student_event(
    data:             schemas.EventData,
    db:               Session = Depends(get_db),
    token_student_id: str     = Depends(get_student_from_token)
):
    if data.student_id != token_student_id:
        raise HTTPException(status_code=403, detail="Token mismatch")

    event = models.StudentEvent(
        student_id = data.student_id,
        event_type = data.event_type,
        severity   = data.severity,
        details    = json.dumps(data.details) if data.details else "{}"
    )
    db.add(event)
    db.commit()

    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()

    emoji_map = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵", "INFO": "⚪"}
    emoji     = emoji_map.get(data.severity, "⚪")
    name      = user.full_name if user else data.student_id

    broadcast_event({
        "type":       "STUDENT_EVENT",
        "student_id": data.student_id,
        "full_name":  name,
        "event_type": data.event_type,
        "severity":   data.severity,
        "emoji":      emoji,
        "details":    data.details,
        "timestamp":  data.timestamp,
        "message":    f"{emoji} {name}: {data.event_type.replace('_', ' ')}"
    })

    return {"status": "event_recorded"}


@app.post("/student/submit-code")
def submit_code(
    data:             schemas.CodeData,
    db:               Session = Depends(get_db),
    token_student_id: str     = Depends(get_student_from_token)
):
    if data.student_id != token_student_id:
        raise HTTPException(status_code=403, detail="Token mismatch")

    ai_score = 0
    if data.analysis:
        ai_score = data.analysis.get("ai_score", 0)

    snapshot = models.CodeSnapshot(
        student_id      = data.student_id,
        filename        = data.filename,
        language        = data.language,
        code            = data.code,
        line_count      = data.line_count,
        ai_score        = ai_score,
        snapshot_number = data.snapshot_number
    )
    db.add(snapshot)
    db.commit()

    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()
    name = user.full_name if user else data.student_id

    broadcast_event({
        "type":            "CODE_SNAPSHOT",
        "student_id":      data.student_id,
        "full_name":       name,
        "language":        data.language,
        "line_count":      data.line_count,
        "ai_score":        ai_score,
        "snapshot_number": data.snapshot_number,
        "timestamp":       data.timestamp,
        "message":         f"📸 Snapshot #{data.snapshot_number} from {name} ({data.line_count} lines, AI:{ai_score}%)"
    })

    if ai_score >= 40:
        broadcast_event({
            "type":       "AI_DETECTION_ALERT",
            "student_id": data.student_id,
            "full_name":  name,
            "ai_score":   ai_score,
            "severity":   "HIGH" if ai_score >= 70 else "MEDIUM",
            "message":    f"🤖 AI code detected! {name} — {ai_score}% AI suspicion",
            "timestamp":  data.timestamp
        })

    return {"status": "code_saved"}


@app.post("/student/heartbeat")
def heartbeat(
    data:             schemas.HeartbeatData,
    db:               Session = Depends(get_db),
    token_student_id: str     = Depends(get_student_from_token)
):
    if data.student_id != token_student_id:
        raise HTTPException(status_code=403, detail="Token mismatch")

    # Teacher pressed "Force Disconnect": tell the extension (one-shot) and
    # don't mark the student connected again.
    control = db.query(models.StudentControl).filter(
        models.StudentControl.student_id == data.student_id
    ).first()
    if control and control.force_disconnect:
        control.force_disconnect = False
        sess = db.query(models.StudentSession).filter(
            models.StudentSession.student_id == data.student_id
        ).first()
        if sess:
            sess.is_connected    = False
            sess.disconnected_at = datetime.utcnow()
        db.commit()
        return {"status": "disconnected", "force_disconnect": True}

    hb = models.Heartbeat(
        student_id        = data.student_id,
        is_typing         = data.is_typing,
        is_idle           = data.is_idle,
        is_window_focused = data.is_window_focused,
        suspicious_score  = data.suspicious_score
    )
    db.add(hb)

    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == data.student_id
    ).first()
    if session:
        session.last_heartbeat   = datetime.utcnow()
        session.suspicious_score = data.suspicious_score
        session.is_connected     = True
    else:
        session = models.StudentSession(
            student_id       = data.student_id,
            is_connected     = True,
            connected_at     = datetime.utcnow(),
            last_heartbeat   = datetime.utcnow(),
            suspicious_score = data.suspicious_score
        )
        db.add(session)

    db.commit()

    broadcast_event({
        "type":              "HEARTBEAT",
        "student_id":        data.student_id,
        "is_typing":         data.is_typing,
        "is_idle":           data.is_idle,
        "is_window_focused": data.is_window_focused,
        "suspicious_score":  data.suspicious_score,
        "timestamp":         data.timestamp
    })

    return {"status": "alive"}


@app.get("/student/my-status")
def my_status(
    student_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(auth.get_current_user),
):
    auth.require_self_or_staff(current_user, student_id)
    session = db.query(models.StudentSession).filter(
        models.StudentSession.student_id == student_id
    ).first()

    if not session:
        return {
            "is_connected":     False,
            "suspicious_score": 0,
            "last_heartbeat":   None,
            "connected_at":     None,
            "stats":            None
        }

    # ── Heartbeat timeout check ────────────────────────────────
    actually_connected = False

    if session.last_heartbeat:
        seconds_since = (datetime.utcnow() - session.last_heartbeat).total_seconds()
        if seconds_since < 30:
            actually_connected = True
            if not session.is_connected:
                session.is_connected = True
                db.commit()
        else:
            if session.is_connected:
                session.is_connected = False
                db.commit()
    elif session.connected_at:
        seconds_since_connect = (datetime.utcnow() - session.connected_at).total_seconds()
        if seconds_since_connect < 15:
            actually_connected = True

    latest_hb = db.query(models.Heartbeat).filter(
        models.Heartbeat.student_id == student_id
    ).order_by(models.Heartbeat.timestamp.desc()).first()

    paste_count = db.query(models.StudentEvent).filter(
        models.StudentEvent.student_id == student_id,
        models.StudentEvent.event_type == "PASTE_DETECTED"
    ).count()

    tab_count = db.query(models.StudentEvent).filter(
        models.StudentEvent.student_id == student_id,
        models.StudentEvent.event_type == "WINDOW_UNFOCUSED"
    ).count()

    snap_count = db.query(models.CodeSnapshot).filter(
        models.CodeSnapshot.student_id == student_id
    ).count()

    return {
        "is_connected":     actually_connected,
        "suspicious_score": session.suspicious_score,
        "last_heartbeat":   str(session.last_heartbeat) if session.last_heartbeat else None,
        "connected_at":     str(session.connected_at)   if session.connected_at   else None,
        "stats": {
            "is_typing":    latest_hb.is_typing    if latest_hb else False,
            "is_idle":      latest_hb.is_idle      if latest_hb else False,
            "paste_count":  paste_count,
            "tab_switches": tab_count,
            "snapshots":    snap_count,
        }
    }

# ══════════════════════════════════════════════════════════════
# AI DETECTION ROUTE
# ══════════════════════════════════════════════════════════════

def _deviation(current: float, baseline: float) -> float:
    if not baseline:
        return 0.0
    return abs(current - baseline) / abs(baseline)


def _rolling(old: float, new: float, n: int) -> float:
    old = old or 0.0
    return old + (new - old) / n


def _update_student_profile(db: Session, student_id: str, code_features: dict, behavioral: dict):
    profile = db.query(models.StudentProfile).filter(
        models.StudentProfile.student_id == student_id
    ).first()
    if not profile:
        profile = models.StudentProfile(student_id=student_id)
        db.add(profile)
        db.flush()

    n = (profile.submission_count or 0) + 1

    current_id_len  = code_features.get("avg_identifier_len", 0.0)
    current_comment = code_features.get("comment_ratio", 0.0)
    current_typing  = behavioral.get("typing_ratio", 0.0)

    if n > 1:
        profile.current_id_len_deviation  = _deviation(current_id_len, profile.avg_identifier_len_baseline)
        profile.current_comment_deviation = _deviation(current_comment, profile.comment_ratio_baseline)
        profile.current_typing_deviation  = _deviation(current_typing, profile.typing_ratio_baseline)
        profile.current_style_change_score = (
            profile.current_id_len_deviation
            + profile.current_comment_deviation
            + profile.current_typing_deviation
        ) / 3

    profile.avg_identifier_len_baseline = _rolling(profile.avg_identifier_len_baseline, current_id_len, n)
    profile.comment_ratio_baseline      = _rolling(profile.comment_ratio_baseline, current_comment, n)
    profile.typing_ratio_baseline       = _rolling(profile.typing_ratio_baseline, current_typing, n)
    profile.keys_per_minute_baseline    = _rolling(profile.keys_per_minute_baseline, behavioral.get("keys_per_minute", 0.0), n)
    profile.backspace_freq_baseline     = _rolling(profile.backspace_freq_baseline, behavioral.get("backspace_frequency", 0.0), n)
    profile.indent_std_baseline         = _rolling(profile.indent_std_baseline, code_features.get("indent_std", 0.0), n)
    profile.avg_line_len_baseline       = _rolling(profile.avg_line_len_baseline, code_features.get("avg_line_len", 0.0), n)

    profile.submission_count = n
    db.commit()


@app.post("/analyze/ai-detection", response_model=schemas.AIDetectionResponse)
def analyze_ai_detection(
    data:             schemas.AIDetectionRequest,
    db:               Session = Depends(get_db),
    token_student_id: str     = Depends(get_student_from_token)
):
    from .ml.model_loader import predict_ai_detection

    if data.student_id and data.student_id != token_student_id:
        raise HTTPException(status_code=403, detail="Token does not match Student ID")
    student_id      = token_student_id
    behavioral_dict = data.behavioral.model_dump()

    result = predict_ai_detection(
        code               = data.code,
        language           = data.language,
        behavioral_signals = behavioral_dict,
    )
    code_features = result.get("code_features") or {}
    # Signals mapped onto the DB/profile column names (see ml/behavioral.py).
    normalized    = result.get("behavioral_signals") or behavioral_dict

    active_exam_session = db.query(models.ExamSession).filter(
        models.ExamSession.student_id == student_id,
        models.ExamSession.is_active  == True
    ).order_by(models.ExamSession.session_start.desc()).first()

    behavioral_row = models.BehavioralData(
        student_id      = student_id,
        exam_session_id = active_exam_session.id if active_exam_session else None,
        raw_events      = behavioral_dict,
        **{col: normalized.get(col, 0) for col in models.BEHAVIORAL_FEATURE_COLUMNS}
    )
    db.add(behavioral_row)
    db.flush()

    submission = models.Submission(
        student_id         = student_id,
        language           = data.language,
        code               = data.code,
        exam_session_id    = active_exam_session.id if active_exam_session else None,
        ai_prediction      = result["prediction"],
        ai_confidence      = result["confidence"],
        ai_probability     = result["ai_probability"],
        model_scores       = result["model_scores"],
        code_features      = code_features,
        behavioral_data_id = behavioral_row.id,
    )
    db.add(submission)
    db.flush()

    behavioral_row.submission_id = submission.id
    db.commit()

    _update_student_profile(db, student_id, code_features, normalized)

    broadcast_event({
        "type":       "AI_DETECTION_RESULT",
        "student_id": student_id,
        "prediction": result["prediction"],
        "confidence": result["confidence"],
        "severity":   "HIGH" if result["prediction"] == "AI" and result["confidence"] >= 0.7 else "INFO",
        "message":    f"🤖 AI-detection: {result['prediction']} ({result['confidence']*100:.1f}%)",
        "timestamp":  data.timestamp,
    })

    return result

# ══════════════════════════════════════════════════════════════
# TEACHER FEEDBACK ROUTE
# ══════════════════════════════════════════════════════════════

@app.post("/teacher/feedback", response_model=schemas.TeacherFeedbackResponse)
def submit_teacher_feedback(
    data:         schemas.TeacherFeedbackRequest,
    db:           Session = Depends(get_db),
    current_user = Depends(auth.get_teacher_user)
):
    import hashlib

    submission = db.query(models.Submission).filter(
        models.Submission.id == data.submission_id
    ).first()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")

    feedback = models.TeacherFeedback(
        submission_id    = submission.id,
        student_id       = submission.student_id,
        teacher_id       = current_user.student_id,
        model_prediction = submission.ai_prediction,
        teacher_label    = data.teacher_label,
        confidence       = submission.ai_confidence,
        notes            = data.notes,
        is_processed     = False,
    )
    db.add(feedback)

    if submission.ai_prediction and submission.ai_prediction != data.teacher_label and submission.code:
        code_hash = hashlib.sha256(submission.code.encode("utf-8")).hexdigest()

        existing = db.query(models.CorrectionMemory).filter(
            models.CorrectionMemory.code_hash == code_hash
        ).first()

        if existing:
            existing.original_pred   = submission.ai_prediction
            existing.corrected_label = data.teacher_label
            existing.corrected_by    = current_user.student_id
        else:
            db.add(models.CorrectionMemory(
                code_hash       = code_hash,
                original_pred   = submission.ai_prediction,
                corrected_label = data.teacher_label,
                corrected_by    = current_user.student_id,
            ))

    db.commit()

    return {
        "message":       "Feedback recorded",
        "submission_id": submission.id,
        "teacher_label": data.teacher_label,
    }

# ══════════════════════════════════════════════════════════════
# PHASE 4: CODE EXECUTION ROUTES
# ══════════════════════════════════════════════════════════════

@app.post("/code/execute")
def execute_code_endpoint(
    data: schemas.CodeExecuteRequest,
    db:   Session = Depends(get_db),
    current_user = Depends(auth.get_current_user),
):
    # Students can only run/record code as themselves.
    if auth.is_staff(current_user):
        data.student_id = data.student_id or current_user.student_id
    else:
        data.student_id = current_user.student_id

    unavailable = {
        "success":        False,
        "output":         "",
        "error":          "Code execution not available in cloud mode",
        "execution_time": 0,
        "language":       data.language,
        "available":      False,
    }

    # No docker SDK / no Docker daemon (e.g. Railway) -> friendly JSON, not a 500.
    try:
        from .docker_service import execute_code
    except ImportError:
        return unavailable

    result = execute_code(
        code     = data.code,
        language = data.language,
        stdin    = data.stdin or "",
    )
    if result.get("unavailable"):
        return unavailable

    submission = models.Submission(
        student_id     = data.student_id or "anonymous",
        language       = data.language,
        code           = data.code,
        output         = result["output"],
        error          = result["error"],
        is_successful  = result["success"],
        execution_time = result["execution_time"],
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    if data.student_id:
        broadcast_event({
            "type":           "CODE_EXECUTED",
            "student_id":     data.student_id,
            "language":       data.language,
            "success":        result["success"],
            "execution_time": result["execution_time"],
            "message":        f"{'✅' if result['success'] else '❌'} {data.student_id} ran {data.language} code ({result['execution_time']}s)",
            "timestamp":      data.timestamp or datetime.utcnow().isoformat(),
        })

    return {
        "success":        result["success"],
        "output":         result["output"],
        "error":          result["error"],
        "execution_time": result["execution_time"],
        "language":       result["language"],
        "submission_id":  submission.id,
    }


@app.get("/code/result/{submission_id}")
def get_result(
    submission_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(auth.get_current_user),
):
    sub = db.query(models.Submission).filter(
        models.Submission.id == submission_id
    ).first()

    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")
    auth.require_self_or_staff(current_user, sub.student_id)

    return {
        "id":             sub.id,
        "student_id":     sub.student_id,
        "language":       sub.language,
        "code":           sub.code,
        "output":         sub.output,
        "error":          sub.error,
        "is_successful":  sub.is_successful,
        "execution_time": sub.execution_time,
        "timestamp":      str(sub.timestamp),
    }


@app.get("/admin/submissions")
def get_all_submissions(
    db: Session = Depends(get_db),
    current_user = Depends(auth.get_current_user),
):
    # Staff see everyone's submissions; students only their own
    # (the exam page uses this endpoint for the student's history).
    q = db.query(models.Submission)
    if not auth.is_staff(current_user):
        q = q.filter(models.Submission.student_id == current_user.student_id)
    submissions = q.order_by(models.Submission.timestamp.desc()).limit(100).all()

    return [{
        "id":             s.id,
        "student_id":     s.student_id,
        "language":       s.language,
        "is_successful":  s.is_successful,
        "execution_time": s.execution_time,
        "output":         s.output,
        "error":          s.error,
        "ai_score":       s.ai_score,
        "timestamp":      str(s.timestamp),
    } for s in submissions]
# ══════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/admin/dashboard")
def dashboard(db: Session = Depends(get_db), admin = Depends(auth.get_admin_user)):
    total_students  = db.query(models.User).filter(models.User.role == "student").count()
    connected_now   = db.query(models.StudentSession).filter(models.StudentSession.is_connected == True).count()
    total_events    = db.query(models.StudentEvent).count()
    total_snapshots = db.query(models.CodeSnapshot).count()
    suspicious      = db.query(models.StudentSession).filter(models.StudentSession.suspicious_score >= 50).count()

    return {
        "total_students":      total_students,
        "connected_now":       connected_now,
        "total_events":        total_events,
        "total_snapshots":     total_snapshots,
        "suspicious_students": suspicious
    }


@app.get("/admin/all-students")
def get_all_students(db: Session = Depends(get_db), admin = Depends(auth.get_admin_user)):
    students = db.query(models.User).filter(models.User.role == "student").all()
    result   = []

    for student in students:
        session = db.query(models.StudentSession).filter(
            models.StudentSession.student_id == student.student_id
        ).first()

        actually_connected = False
        suspicious_score   = 0
        last_heartbeat     = None
        connected_at       = None

        if session:
            suspicious_score = session.suspicious_score or 0
            connected_at     = str(session.connected_at)   if session.connected_at   else None
            last_heartbeat   = str(session.last_heartbeat) if session.last_heartbeat else None

            if session.is_connected and session.last_heartbeat:
                diff = datetime.utcnow() - session.last_heartbeat
                if diff.total_seconds() < 30:
                    actually_connected = True

        paste_count = db.query(models.StudentEvent).filter(
            models.StudentEvent.student_id == student.student_id,
            models.StudentEvent.event_type == "PASTE_DETECTED"
        ).count()

        tab_count = db.query(models.StudentEvent).filter(
            models.StudentEvent.student_id == student.student_id,
            models.StudentEvent.event_type == "WINDOW_UNFOCUSED"
        ).count()

        snap_count = db.query(models.CodeSnapshot).filter(
            models.CodeSnapshot.student_id == student.student_id
        ).count()

        result.append({
            "student_id":       student.student_id,
            "full_name":        student.full_name,
            "email":            student.email,
            "is_connected":     actually_connected,
            "suspicious_score": suspicious_score,
            "last_heartbeat":   last_heartbeat,
            "connected_at":     connected_at,
            "paste_count":      paste_count,
            "tab_switches":     tab_count,
            "snapshots":        snap_count,
        })

    return result


@app.get("/admin/students/{student_id}/events")
def get_events(student_id: str, db: Session = Depends(get_db), admin = Depends(auth.get_admin_user)):
    events = db.query(models.StudentEvent).filter(
        models.StudentEvent.student_id == student_id
    ).order_by(models.StudentEvent.timestamp.desc()).all()

    return [{
        "event_type": e.event_type,
        "severity":   e.severity,
        "details":    e.details,
        "timestamp":  str(e.timestamp)
    } for e in events]


@app.get("/admin/students/{student_id}/code")
def get_code(student_id: str, db: Session = Depends(get_db), admin = Depends(auth.get_admin_user)):
    snapshots = db.query(models.CodeSnapshot).filter(
        models.CodeSnapshot.student_id == student_id
    ).order_by(models.CodeSnapshot.timestamp.desc()).all()

    return [{
        "snapshot_number": s.snapshot_number,
        "filename":        s.filename,
        "language":        s.language,
        "line_count":      s.line_count,
        "ai_score":        s.ai_score,
        "code":            s.code,
        "timestamp":       str(s.timestamp)
    } for s in snapshots]


@app.get("/admin/export-dataset")
def export_dataset(
    db:           Session = Depends(get_db),
    current_user = Depends(auth.get_admin_user)
):
    from fastapi.responses import StreamingResponse
    from .ml.feature_extractor import FEATURE_COLUMNS as CODE_FEATURE_COLUMNS
    import csv, io

    submissions = db.query(models.Submission).order_by(models.Submission.id).all()

    header = (
        ["submission_id", "student_id", "language", "is_successful", "execution_time",
         "ai_prediction", "ai_confidence", "ai_probability",
         "model_score_lr", "model_score_rf", "model_score_xgb", "model_score_fusion"]
        + [f"code_{c}" for c in CODE_FEATURE_COLUMNS]
        + [f"behav_{c}" for c in models.BEHAVIORAL_FEATURE_COLUMNS]
        + ["teacher_label", "timestamp"]
    )

    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)

    for s in submissions:
        behavioral_row = (
            db.query(models.BehavioralData).filter(
                models.BehavioralData.id == s.behavioral_data_id
            ).first()
            if s.behavioral_data_id else None
        )
        latest_feedback = (
            db.query(models.TeacherFeedback)
            .filter(models.TeacherFeedback.submission_id == s.id)
            .order_by(models.TeacherFeedback.created_at.desc())
            .first()
        )

        model_scores  = s.model_scores or {}
        code_features = s.code_features or {}

        row = [
            s.id, s.student_id, s.language, s.is_successful, s.execution_time,
            s.ai_prediction, s.ai_confidence, s.ai_probability,
            model_scores.get("logistic_regression"), model_scores.get("random_forest"),
            model_scores.get("xgboost"), model_scores.get("fusion_ann"),
        ]
        row += [code_features.get(c) for c in CODE_FEATURE_COLUMNS]
        row += [
            getattr(behavioral_row, c) if behavioral_row else None
            for c in models.BEHAVIORAL_FEATURE_COLUMNS
        ]
        row += [latest_feedback.teacher_label if latest_feedback else None, str(s.timestamp)]
        writer.writerow(row)

    filename = f"dataset_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type = "text/csv",
        headers    = {"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.get("/admin/flagged")
def get_flagged(db: Session = Depends(get_db), admin = Depends(auth.get_admin_user)):
    flagged = db.query(models.StudentSession).filter(
        models.StudentSession.suspicious_score >= 50
    ).order_by(models.StudentSession.suspicious_score.desc()).all()

    result = []
    for s in flagged:
        user = db.query(models.User).filter(
            models.User.student_id == s.student_id
        ).first()
        result.append({
            "student_id":       s.student_id,
            "full_name":        user.full_name if user else "Unknown",
            "suspicious_score": s.suspicious_score,
            "is_connected":     s.is_connected,
        })
    return result


# ══════════════════════════════════════════════════════════════
# HOME
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
# WEB PAGES (templates/)
# ══════════════════════════════════════════════════════════════
from pathlib import Path
from fastapi.responses import HTMLResponse

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

PAGES = {
    "/":               "login.html",
    "/register":       "register.html",
    "/dashboard":      "dashboard.html",
    "/admin":          "admin.html",
    "/admin/register": "admin_register.html",
    "/forgot":         "forgot.html",
    "/exam":           "exam.html",
}


def _make_page(filename: str):
    def page():
        path = TEMPLATES_DIR / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Page not found: {filename}")
        return HTMLResponse(path.read_text(encoding="utf-8"))
    return page


for _route, _file in PAGES.items():
    app.add_api_route(_route, _make_page(_file), methods=["GET"],
                      response_class=HTMLResponse, include_in_schema=False)


@app.get("/health")
def home():
    return {
        "message": "Automated Technical Screening Platform API v3.1",
        "status":  "running",
        "docs":    "/docs"
    }