from pydantic import AliasChoices, BaseModel, EmailStr, Field, field_validator, model_validator
from typing import Optional
import re

# ── AUTH SCHEMAS ───────────────────────────────────────────────

class RegisterRequest(BaseModel):
    # Students must give a university ID; teachers have none (one is generated).
    student_id: Optional[str] = None
    email:      EmailStr
    full_name:  str
    password:   str
    confirm_password: str
    role:       str = "student"          # "student" | "teacher"

    @field_validator('role')
    def validate_role(cls, v):
        v = (v or "student").strip().lower()
        if v not in ("student", "teacher"):
            raise ValueError('role must be "student" or "teacher"')
        return v

    @field_validator('student_id')
    def validate_student_id(cls, v):
        if v is None or v == "":
            return None
        # Format: 2023-2-60-148
        pattern = r'^\d{4}-\d{1,2}-\d{2,3}-\d{1,3}$'
        if not re.match(pattern, v):
            raise ValueError(
                'Invalid Student ID format. Expected: YYYY-D-DD-DDD (e.g. 2023-2-60-148)'
            )
        return v

    @model_validator(mode='after')
    def student_needs_id(self):
        if self.role == "student" and not self.student_id:
            raise ValueError('Student ID is required for student accounts')
        return self

    @field_validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[0-9]', v):
            raise ValueError('Password must contain at least one number')
        return v

    @field_validator('confirm_password')
    def passwords_match(cls, v, info):
        if 'password' in info.data and v != info.data['password']:
            raise ValueError('Passwords do not match')
        return v

class AdminRegisterRequest(BaseModel):
    """Admins have no university student ID; one is generated server-side."""
    email:            EmailStr
    full_name:        str
    password:         str
    confirm_password: str

    @field_validator('password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[0-9]', v):
            raise ValueError('Password must contain at least one number')
        return v

    @field_validator('confirm_password')
    def passwords_match(cls, v, info):
        if 'password' in info.data and v != info.data['password']:
            raise ValueError('Passwords do not match')
        return v

class LoginRequest(BaseModel):
    student_id: str
    password:   str
    role:       Optional[str] = None   # UI toggle; checked after the password

class ForgotPasswordRequest(BaseModel):
    # Email is enough; student_id is still accepted (older clients send both).
    email:      EmailStr
    student_id: Optional[str] = None

class VerifyOTPRequest(BaseModel):
    otp_code:   str
    email:      Optional[EmailStr] = None
    student_id: Optional[str] = None

    @model_validator(mode='after')
    def needs_account(self):
        if not self.email and not self.student_id:
            raise ValueError('email is required')
        return self

class ResetPasswordRequest(BaseModel):
    student_id:       Optional[str] = None
    email:            Optional[EmailStr] = None
    otp_code:         str
    new_password:     str
    confirm_password: str

    @model_validator(mode='after')
    def needs_account(self):
        if not self.email and not self.student_id:
            raise ValueError('email is required')
        return self

    @field_validator('new_password')
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter')
        if not re.search(r'[0-9]', v):
            raise ValueError('Password must contain at least one number')
        return v

    @field_validator('confirm_password')
    def passwords_match(cls, v, info):
        if 'new_password' in info.data and v != info.data['new_password']:
            raise ValueError('Passwords do not match')
        return v

# ── RESPONSE SCHEMAS ───────────────────────────────────────────

class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    student_id:   str
    full_name:    str
    role:         str

class UserResponse(BaseModel):
    student_id: str
    email:      str
    full_name:  str
    role:       str
    is_active:  bool

class MessageResponse(BaseModel):
    message: str
    success: bool = True

# ── TEACHER SCHEMAS ────────────────────────────────────────────

class TeacherLabelRequest(BaseModel):
    session_id: int
    student_id: str
    label:      str
    confidence: int
    notes:      str = ""

    @field_validator('label')
    def validate_label(cls, v):
        v = v.strip().lower()
        if v not in ("human", "ai", "unsure"):
            raise ValueError('label must be "human", "ai" or "unsure"')
        return v

    @field_validator('confidence')
    def validate_confidence(cls, v):
        if not 1 <= v <= 5:
            raise ValueError('confidence must be between 1 and 5')
        return v

class ProblemRequest(BaseModel):
    title:       str
    description: str
    time_limit:  Optional[int] = None      # minutes

    @field_validator('title', 'description')
    def not_blank(cls, v):
        v = v.strip()
        if not v:
            raise ValueError('must not be empty')
        return v

    @field_validator('time_limit')
    def valid_limit(cls, v):
        if v is not None and not 1 <= v <= 600:
            raise ValueError('time_limit must be between 1 and 600 minutes')
        return v

class WarnRequest(BaseModel):
    message: str = "Please follow the exam rules."

    @field_validator('message')
    def valid_message(cls, v):
        v = v.strip()
        if not v or len(v) > 500:
            raise ValueError('message must be 1-500 characters')
        return v

class ResetDatabaseRequest(BaseModel):
    confirm: str          # must be exactly "RESET"

class TeacherAIFeedbackRequest(BaseModel):
    """Session-based quick agree/disagree with the AI-detection verdict
    (teacher.html panel). Writes to correction_memory for later retraining."""
    session_id:    int
    student_id:    str
    agreed:        bool
    ai_prediction: str     # the model's prediction the teacher is reacting to

    @field_validator('ai_prediction')
    def normalize_prediction(cls, v):
        v = (v or "").strip().lower()
        if v not in ("ai", "human"):
            raise ValueError('ai_prediction must be "AI" or "Human"')
        return "AI" if v == "ai" else "Human"

class AssignStudentsRequest(BaseModel):
    teacher_id:  str
    student_ids: list[str]

# ── EXTENSION SCHEMAS ──────────────────────────────────────────

class ConnectData(BaseModel):
    student_id:  str
    timestamp:   str
    device_info: Optional[dict] = None

class EventData(BaseModel):
    student_id:  str
    event_type:  str
    severity:    str
    details:     Optional[dict] = None
    timestamp:   str

class CodeData(BaseModel):
    student_id:      str
    filename:        str
    language:        str
    code:            str
    line_count:      int
    analysis:        Optional[dict] = None
    snapshot_number: int
    timestamp:       str

class HeartbeatData(BaseModel):
    student_id:        str
    is_typing:         bool
    is_idle:           bool
    is_window_focused: bool
    suspicious_score:  int
    stats:             Optional[dict] = None
    current_language:  Optional[str] = None
    timestamp:         str


# ── Code Execution Schema (Phase 4) ───────────────────────────
class CodeExecuteRequest(BaseModel):
    student_id: Optional[str] = None
    language:   str
    code:       str
    problem_id: Optional[str] = None
    timestamp:  Optional[str] = None    





# ══════════════════════════════════════════════════════════════
# PHASE 4: CODE EXECUTION SCHEMAS
# ══════════════════════════════════════════════════════════════

class CodeExecuteRequest(BaseModel):
    student_id: Optional[str] = None
    language:   str
    code:       str
    stdin:      Optional[str] = ""
    timestamp:  Optional[str] = None

class CodeExecuteResponse(BaseModel):
    success:        bool
    output:         str
    error:          str
    execution_time: float
    language:       str
    submission_id:  Optional[int] = None


# ══════════════════════════════════════════════════════════════
# AI DETECTION SCHEMAS
# ══════════════════════════════════════════════════════════════

class BehavioralSignals(BaseModel):
    # Legacy fields — consumed by the fusion model's trained feature set.
    # Kept as-is so prediction behavior is unaffected by the schema expansion below.
    paste_count:     int   = 0
    typing_ratio:    float = 0.0
    tab_switches:    int   = 0
    backspace_freq:  float = 0.0
    suspicion_score: float = 0.0

    # Field names exactly as the VS Code extension's collectBehavioralSignals()
    # sends them. Mapped onto the names above/below by
    # app.ml.behavioral.normalize_behavioral_signals.
    total_pastes:                float = 0
    total_tab_switches:          float = 0
    suspicious_score:            float = 0.0
    session_duration_ms:         float = 0.0
    undo_count:                  int   = 0
    redo_count:                  int   = 0
    scroll_event_count:          int   = 0
    total_idle_events:           int   = 0
    keys_per_minute_avg:         float = 0.0
    keystroke_rhythm_stddev_ms:  float = 0.0
    avg_time_between_keys_ms:    float = 0.0
    sudden_code_jump_count:      int   = 0
    code_deletion_events:        int   = 0
    copy_from_same_file_count:   int   = 0
    screen_freeze_events:        int   = 0
    sudden_skill_jump_count:     int   = 0
    # Also sent by the extension; kept so they are stored in raw_events and
    # can be exported (previously dropped because the schema ignored them).
    keys_per_minute_series:      list  = []
    typing_pause_avg_ms:         float = 0.0
    code_deletion_chars_total:   int   = 0
    time_per_line_ms:            float = 0.0
    typing_speed_avg_cps:        float = 0.0
    code_complexity_avg:         float = 0.0
    code_fingerprint:            Optional[str] = None

    # Full 34-feature behavioral telemetry — persisted to behavioral_data,
    # not consumed by the fusion model directly.
    large_paste_count:       int   = 0
    small_paste_count:       int   = 0
    avg_paste_size:          float = 0.0
    paste_after_tab:         int   = 0
    copy_from_same_file:     int   = 0
    tab_switch_count:        int   = 0
    rapid_tab_switches:      int   = 0
    single_tab_switch:       int   = 0
    long_absence_count:      int   = 0
    window_unfocused_count:  int   = 0
    window_refocused_count:  int   = 0
    total_unfocused_seconds: float = 0.0
    total_keystrokes:        int   = 0
    backspace_count:         int   = 0
    backspace_frequency:     float = 0.0
    keys_per_minute:         float = 0.0
    typing_speed_variance:   float = 0.0
    avg_time_between_keys:   float = 0.0
    keystroke_rhythm_std:    float = 0.0
    typing_pause_count:      int   = 0
    fast_burst_count:        int   = 0
    sudden_code_jumps:       int   = 0
    undo_redo_count:         int   = 0
    code_deletion_ratio:     float = 0.0
    variable_rename_count:   int   = 0
    scroll_events:           int   = 0
    file_switches:           int   = 0
    screen_freeze_count:     int   = 0
    session_break_count:     int   = 0
    time_per_line_ratio:     float = 0.0
    sudden_skill_jump:       bool  = False
    total_session_seconds:   float = 0.0

class AIDetectionRequest(BaseModel):
    student_id:  Optional[str] = None
    code:        str
    language:    str
    # The extension posts this under "signals"; older clients use "behavioral".
    behavioral:  BehavioralSignals = Field(
        default_factory=BehavioralSignals,
        validation_alias=AliasChoices("signals", "behavioral"),
    )
    timestamp:   Optional[str] = None

class ModelScores(BaseModel):
    logistic_regression: float
    random_forest:       float
    xgboost:              float
    fusion_ann:           float

class AIDetectionResponse(BaseModel):
    prediction:     str
    confidence:     float
    ai_probability: float
    model_scores:   ModelScores
    notes:          Optional[str] = None


# ══════════════════════════════════════════════════════════════
# TEACHER FEEDBACK SCHEMAS
# ══════════════════════════════════════════════════════════════

class TeacherFeedbackRequest(BaseModel):
    submission_id: int
    teacher_label: str
    notes:         Optional[str] = None

    @field_validator('teacher_label')
    def validate_teacher_label(cls, v):
        if v not in ("AI", "Human"):
            raise ValueError('teacher_label must be "AI" or "Human"')
        return v

class TeacherFeedbackResponse(BaseModel):
    message:       str
    submission_id: int
    teacher_label: str