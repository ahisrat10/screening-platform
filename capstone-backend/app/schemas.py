from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional
import re

# ── AUTH SCHEMAS ───────────────────────────────────────────────

class RegisterRequest(BaseModel):
    student_id: str
    email:      EmailStr
    full_name:  str
    password:   str
    confirm_password: str

    @field_validator('student_id')
    def validate_student_id(cls, v):
        # Format: 2023-2-60-148
        pattern = r'^\d{4}-\d{1,2}-\d{2,3}-\d{1,3}$'
        if not re.match(pattern, v):
            raise ValueError(
                'Invalid Student ID format. Expected: YYYY-D-DD-DDD (e.g. 2023-2-60-148)'
            )
        return v

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

class ForgotPasswordRequest(BaseModel):
    student_id: str
    email:      EmailStr

class VerifyOTPRequest(BaseModel):
    student_id: str
    otp_code:   str

class ResetPasswordRequest(BaseModel):
    student_id:       str
    otp_code:         str
    new_password:     str
    confirm_password: str

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