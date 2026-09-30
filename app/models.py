from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func
from .database import Base

# Column names for the 34 behavioral features, in a stable order used for
# building BehavioralData rows from request payloads and for CSV export.
BEHAVIORAL_FEATURE_COLUMNS = [
    "large_paste_count", "small_paste_count", "avg_paste_size", "paste_after_tab",
    "copy_from_same_file", "tab_switch_count", "rapid_tab_switches", "single_tab_switch",
    "long_absence_count", "window_unfocused_count", "window_refocused_count",
    "total_unfocused_seconds", "total_keystrokes", "backspace_count", "backspace_frequency",
    "keys_per_minute", "typing_ratio", "typing_speed_variance", "avg_time_between_keys",
    "keystroke_rhythm_std", "typing_pause_count", "fast_burst_count", "sudden_code_jumps",
    "undo_redo_count", "code_deletion_ratio", "variable_rename_count", "scroll_events",
    "file_switches", "screen_freeze_count", "session_break_count", "time_per_line_ratio",
    "suspicion_score", "sudden_skill_jump", "total_session_seconds",
]

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    student_id      = Column(String, unique=True, index=True)
    email           = Column(String, unique=True, index=True)
    password_hash   = Column(String)
    full_name       = Column(String, nullable=True)
    role            = Column(String, default="student")
    is_active       = Column(Boolean, default=True)
    failed_attempts = Column(Integer, default=0)
    locked_until    = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, onupdate=func.now())

# User.role is a plain string: "student" | "teacher" | "admin".
# (No column change needed, so existing databases keep working.)

class TeacherAssignment(Base):
    """Which students a teacher is responsible for. A teacher with no rows
    here can see all students."""
    __tablename__ = "teacher_assignments"
    __table_args__ = (UniqueConstraint("teacher_id", "student_id", name="uq_teacher_student"),)

    id         = Column(Integer, primary_key=True, index=True)
    teacher_id = Column(String(50), ForeignKey("users.student_id"), index=True)
    student_id = Column(String(50), ForeignKey("users.student_id"), index=True)
    created_at = Column(DateTime, server_default=func.now())


class TeacherLabel(Base):
    __tablename__ = "teacher_labels"

    id         = Column(Integer, primary_key=True, index=True)
    # StudentSession.id (the live monitoring session row)
    session_id = Column(Integer, ForeignKey("student_sessions.id"), index=True)
    student_id = Column(String(50), ForeignKey("users.student_id"), index=True)
    teacher_id = Column(String(50), ForeignKey("users.student_id"), index=True)
    label      = Column(String(10))        # human | ai | unsure
    confidence = Column(Integer)           # 1-5
    notes      = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


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

    id                = Column(Integer, primary_key=True, index=True)
    student_id        = Column(String, index=True)
    is_typing         = Column(Boolean, default=False)
    is_idle           = Column(Boolean, default=False)
    is_window_focused = Column(Boolean, default=True)
    suspicious_score  = Column(Integer, default=0)
    timestamp         = Column(DateTime, server_default=func.now())

class ExamSession(Base):
    __tablename__ = "exam_sessions"

    id             = Column(Integer, primary_key=True, index=True)
    student_id     = Column(String(50), ForeignKey("users.student_id"), index=True)
    problem_id     = Column(String(50), nullable=True)
    session_start  = Column(DateTime, server_default=func.now())
    session_end    = Column(DateTime, nullable=True)
    is_active      = Column(Boolean, default=True)
    total_duration = Column(Integer, nullable=True)
    created_at     = Column(DateTime, server_default=func.now())


class BehavioralData(Base):
    __tablename__ = "behavioral_data"

    id               = Column(Integer, primary_key=True, index=True)
    student_id       = Column(String(50), ForeignKey("users.student_id"), index=True)
    exam_session_id  = Column(Integer, ForeignKey("exam_sessions.id"), nullable=True)
    submission_id    = Column(Integer, nullable=True)

    session_start         = Column(DateTime, nullable=True)
    session_end           = Column(DateTime, nullable=True)
    total_session_seconds = Column(Float, default=0)
    active_typing_seconds = Column(Float, default=0)
    idle_seconds          = Column(Float, default=0)

    large_paste_count      = Column(Integer, default=0)
    small_paste_count      = Column(Integer, default=0)
    avg_paste_size         = Column(Float, default=0)
    paste_after_tab        = Column(Integer, default=0)
    copy_from_same_file    = Column(Integer, default=0)

    tab_switch_count   = Column(Integer, default=0)
    rapid_tab_switches = Column(Integer, default=0)
    single_tab_switch  = Column(Integer, default=0)

    long_absence_count      = Column(Integer, default=0)
    window_unfocused_count  = Column(Integer, default=0)
    window_refocused_count  = Column(Integer, default=0)
    total_unfocused_seconds = Column(Float, default=0)

    total_keystrokes      = Column(Integer, default=0)
    backspace_count       = Column(Integer, default=0)
    backspace_frequency   = Column(Float, default=0)
    keys_per_minute       = Column(Float, default=0)
    typing_ratio          = Column(Float, default=0)
    typing_speed_variance = Column(Float, default=0)
    avg_time_between_keys = Column(Float, default=0)
    keystroke_rhythm_std  = Column(Float, default=0)
    typing_pause_count    = Column(Integer, default=0)
    fast_burst_count      = Column(Integer, default=0)

    sudden_code_jumps     = Column(Integer, default=0)
    undo_redo_count       = Column(Integer, default=0)
    code_deletion_ratio   = Column(Float, default=0)
    variable_rename_count = Column(Integer, default=0)

    scroll_events       = Column(Integer, default=0)
    file_switches       = Column(Integer, default=0)
    screen_freeze_count = Column(Integer, default=0)
    session_break_count = Column(Integer, default=0)
    time_per_line_ratio = Column(Float, default=0)

    suspicion_score   = Column(Float, default=0)
    sudden_skill_jump = Column(Boolean, default=False)

    raw_events = Column(JSONB, default=list)
    created_at = Column(DateTime, server_default=func.now())


class StudentProfile(Base):
    __tablename__ = "student_profiles"

    id         = Column(Integer, primary_key=True, index=True)
    student_id = Column(String(50), ForeignKey("users.student_id"), index=True)

    submission_count = Column(Integer, default=0)

    # Rolling-average baselines this student's submissions are compared against.
    avg_identifier_len_baseline = Column(Float, default=0)
    comment_ratio_baseline      = Column(Float, default=0)
    typing_ratio_baseline       = Column(Float, default=0)
    keys_per_minute_baseline    = Column(Float, default=0)
    backspace_freq_baseline     = Column(Float, default=0)
    indent_std_baseline         = Column(Float, default=0)
    avg_line_len_baseline       = Column(Float, default=0)

    # How much the most recent submission deviated from the baseline above.
    current_id_len_deviation   = Column(Float, default=0)
    current_comment_deviation  = Column(Float, default=0)
    current_typing_deviation   = Column(Float, default=0)
    current_style_change_score = Column(Float, default=0)

    has_personal_model       = Column(Boolean, default=False)
    personal_model_path      = Column(String(255), nullable=True)
    personal_model_accuracy  = Column(Float, nullable=True)
    last_model_trained       = Column(DateTime, nullable=True)

    last_updated = Column(DateTime, server_default=func.now())
    created_at   = Column(DateTime, server_default=func.now())


class TeacherFeedback(Base):
    __tablename__ = "teacher_feedback"

    id                = Column(Integer, primary_key=True, index=True)
    submission_id     = Column(Integer, ForeignKey("submissions.id"), index=True)
    student_id        = Column(String(50), ForeignKey("users.student_id"), nullable=True)
    teacher_id        = Column(String(50), ForeignKey("users.student_id"), nullable=True)
    model_prediction  = Column(String(10), nullable=True)
    teacher_label     = Column(String(10), nullable=True)
    confidence        = Column(Float, nullable=True)
    notes             = Column(String, nullable=True)
    is_processed      = Column(Boolean, default=False)
    created_at        = Column(DateTime, server_default=func.now())


class CorrectionMemory(Base):
    __tablename__ = "correction_memory"

    id              = Column(Integer, primary_key=True, index=True)
    code_hash       = Column(String(64), nullable=False, index=True)
    original_pred   = Column(String(10), nullable=True)
    corrected_label = Column(String(10), nullable=True)
    corrected_by    = Column(String(50), nullable=True)
    corrected_at    = Column(DateTime, server_default=func.now())


class Submission(Base):
    __tablename__ = "submissions"

    id             = Column(Integer, primary_key=True, index=True)
    student_id     = Column(String, index=True)
    language       = Column(String)
    code           = Column(String)
    output         = Column(String, nullable=True)
    error          = Column(String, nullable=True)
    is_successful  = Column(Boolean, default=False)
    execution_time = Column(Float, default=0.0)
    ai_score       = Column(Integer, default=0)

    exam_session_id    = Column(Integer, ForeignKey("exam_sessions.id"), nullable=True)
    problem_id         = Column(String(50), nullable=True)
    execution_time_ms  = Column(Float, nullable=True)

    ai_prediction      = Column(String(10), nullable=True)
    ai_confidence      = Column(Float, nullable=True)
    ai_probability     = Column(Float, nullable=True)
    model_scores       = Column(JSONB, default=dict)
    code_features       = Column(JSONB, default=dict)
    behavioral_data_id = Column(Integer, ForeignKey("behavioral_data.id"), nullable=True)

    timestamp      = Column(DateTime, server_default=func.now())