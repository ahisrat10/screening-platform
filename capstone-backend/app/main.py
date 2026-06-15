from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
from contextlib import asynccontextmanager
import json, asyncio

from .database import engine, get_db
from . import models, schemas, auth
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
    allow_origins     = ["*"],
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ══════════════════════════════════════════════════════════════
# JWT VERIFICATION FOR EXTENSION ROUTES
# ══════════════════════════════════════════════════════════════
def get_student_from_token(
    authorization: Optional[str] = Header(default=None)
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
    return payload.get("sub")

# ══════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.post("/auth/register", response_model=schemas.MessageResponse)
def register(data: schemas.RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first():
        raise HTTPException(
            status_code=400,
            detail="Student ID already registered"
        )
    if db.query(models.User).filter(
        models.User.email == data.email
    ).first():
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )
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
    return {
        "message": f"Account created! Welcome {data.full_name}",
        "success": True
    }


@app.post("/auth/register-admin", response_model=schemas.MessageResponse)
def register_admin(data: schemas.RegisterRequest, db: Session = Depends(get_db)):
    if db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first():
        raise HTTPException(
            status_code=400,
            detail="This ID is already registered"
        )
    if db.query(models.User).filter(
        models.User.email == data.email
    ).first():
        raise HTTPException(
            status_code=400,
            detail="Email already registered"
        )
    admin = models.User(
        student_id    = data.student_id,
        email         = data.email,
        full_name     = data.full_name,
        password_hash = auth.hash_password(data.password),
        role          = "admin",
        is_active     = True
    )
    db.add(admin)
    db.commit()
    return {
        "message": f"Admin account created for {data.full_name}!",
        "success": True
    }


@app.post("/auth/login", response_model=schemas.TokenResponse)
def login(data: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid Student ID or password"
        )

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


@app.post("/auth/forgot-password", response_model=schemas.MessageResponse)
def forgot_password(
    data: schemas.ForgotPasswordRequest,
    db:   Session = Depends(get_db)
):
    user = db.query(models.User).filter(
        models.User.student_id == data.student_id,
        models.User.email      == data.email
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail="No account found with this Student ID and email"
        )

    otp  = generate_otp()
    save_otp(data.student_id, data.email, otp, db)
    sent = send_otp_email(data.email, data.student_id, otp)

    if not sent:
        raise HTTPException(
            status_code=500,
            detail="Failed to send OTP email. Please try again."
        )

    masked = f"{data.email[:3]}***{data.email[data.email.index('@'):]}"
    return {"message": f"OTP sent to {masked}", "success": True}


@app.post("/auth/verify-otp", response_model=schemas.MessageResponse)
def verify_otp(data: schemas.VerifyOTPRequest, db: Session = Depends(get_db)):
    record = db.query(models.OTPCode).filter(
        models.OTPCode.student_id == data.student_id,
        models.OTPCode.otp_code   == data.otp_code,
        models.OTPCode.is_used    == False
    ).first()

    if not record:
        raise HTTPException(status_code=400, detail="Invalid OTP code")
    if record.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="OTP expired. Request a new one.")

    return {"message": "OTP verified", "success": True}


@app.post("/auth/reset-password", response_model=schemas.MessageResponse)
def reset_password(data: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    record = db.query(models.OTPCode).filter(
        models.OTPCode.student_id == data.student_id,
        models.OTPCode.otp_code   == data.otp_code,
        models.OTPCode.is_used    == False
    ).first()

    if not record or record.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    user = db.query(models.User).filter(
        models.User.student_id == data.student_id
    ).first()
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
async def websocket_admin(websocket: WebSocket):
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

    if session:
        session.is_connected     = True
        session.connected_at     = datetime.utcnow()
        session.suspicious_score = 0
    else:
        session = models.StudentSession(
            student_id       = data.student_id,
            is_connected     = True,
            connected_at     = datetime.utcnow(),
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
def my_status(student_id: str, db: Session = Depends(get_db)):
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

    actually_connected = False
    if session.is_connected and session.last_heartbeat:
        diff = datetime.utcnow() - session.last_heartbeat
        if diff.total_seconds() < 30:
            actually_connected = True
        else:
            session.is_connected = False
            db.commit()
    elif session.is_connected and not session.last_heartbeat:
        if session.connected_at:
            diff = datetime.utcnow() - session.connected_at
            if diff.total_seconds() < 15:
                actually_connected = True
            else:
                session.is_connected = False
                db.commit()

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
        "connected_at":     str(session.connected_at) if session.connected_at else None,
        "stats": {
            "is_typing":    latest_hb.is_typing if latest_hb else False,
            "is_idle":      latest_hb.is_idle if latest_hb else False,
            "paste_count":  paste_count,
            "tab_switches": tab_count,
            "snapshots":    snap_count,
        }
    }

# ══════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════

@app.get("/admin/dashboard")
def dashboard(db: Session = Depends(get_db)):
    total_students  = db.query(models.User).filter(
        models.User.role == "student"
    ).count()
    connected_now   = db.query(models.StudentSession).filter(
        models.StudentSession.is_connected == True
    ).count()
    total_events    = db.query(models.StudentEvent).count()
    total_snapshots = db.query(models.CodeSnapshot).count()
    suspicious      = db.query(models.StudentSession).filter(
        models.StudentSession.suspicious_score >= 50
    ).count()

    return {
        "total_students":      total_students,
        "connected_now":       connected_now,
        "total_events":        total_events,
        "total_snapshots":     total_snapshots,
        "suspicious_students": suspicious
    }


@app.get("/admin/all-students")
def get_all_students(db: Session = Depends(get_db)):
    students = db.query(models.User).filter(
        models.User.role == "student"
    ).all()

    result = []
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
            connected_at     = str(session.connected_at) if session.connected_at else None
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
def get_events(student_id: str, db: Session = Depends(get_db)):
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
def get_code(student_id: str, db: Session = Depends(get_db)):
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


@app.get("/admin/flagged")
def get_flagged(db: Session = Depends(get_db)):
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


@app.get("/")
def home():
    return {
        "message": "Automated Technical Screening Platform API v3.1",
        "status":  "running",
        "docs":    "/docs"
    }