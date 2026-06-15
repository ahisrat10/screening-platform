from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func
from .database import Base

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    student_id      = Column(String, unique=True, index=True)  # 2023-2-60-148
    email           = Column(String, unique=True, index=True)
    password_hash   = Column(String)
    full_name       = Column(String, nullable=True)
    role            = Column(String, default="student")  # student / admin
    is_active       = Column(Boolean, default=True)
    failed_attempts = Column(Integer, default=0)
    locked_until    = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, onupdate=func.now())

class OTPCode(Base):
    __tablename__ = "otp_codes"

    id         = Column(Integer, primary_key=True, index=True)
    student_id = Column(String, index=True)
    email      = Column(String)
    otp_code   = Column(String)
    is_used    = Column(Boolean, default=False)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())

class StudentSession(Base):
    __tablename__ = "student_sessions"

    id               = Column(Integer, primary_key=True, index=True)
    student_id       = Column(String, index=True)
    is_connected     = Column(Boolean, default=False)
    suspicious_score = Column(Integer, default=0)
    connected_at     = Column(DateTime, nullable=True)
    disconnected_at  = Column(DateTime, nullable=True)
    last_heartbeat   = Column(DateTime, nullable=True)

class CodeSnapshot(Base):
    __tablename__ = "code_snapshots"

    id              = Column(Integer, primary_key=True, index=True)
    student_id      = Column(String, index=True)
    filename        = Column(String)
    language        = Column(String)
    code            = Column(String)
    line_count      = Column(Integer, default=0)
    ai_score        = Column(Integer, default=0)
    snapshot_number = Column(Integer, default=0)
    timestamp       = Column(DateTime, server_default=func.now())

class StudentEvent(Base):
    __tablename__ = "student_events"

    id         = Column(Integer, primary_key=True, index=True)
    student_id = Column(String, index=True)
    event_type = Column(String)
    severity   = Column(String)
    details    = Column(String)
    timestamp  = Column(DateTime, server_default=func.now())

class Heartbeat(Base):
    __tablename__ = "heartbeats"

    id               = Column(Integer, primary_key=True, index=True)
    student_id       = Column(String, index=True)
    is_typing        = Column(Boolean, default=False)
    is_idle          = Column(Boolean, default=False)
    is_window_focused= Column(Boolean, default=True)
    suspicious_score = Column(Integer, default=0)
    timestamp        = Column(DateTime, server_default=func.now())