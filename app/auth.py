from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from .database import get_db
from . import models
from dotenv import load_dotenv
import os

load_dotenv()

SECRET_KEY  = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    # Without this every login would fail later with an opaque 500.
    raise RuntimeError("SECRET_KEY environment variable is not set")
ALGORITHM   = os.getenv("ALGORITHM", "HS256")
EXPIRE_MINS = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security    = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)

# ── PASSWORD ───────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

# ── JWT TOKEN ──────────────────────────────────────────────────
def create_token(data: dict) -> str:
    to_encode = data.copy()
    expire    = datetime.utcnow() + timedelta(minutes=EXPIRE_MINS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None

# ── GET CURRENT USER ───────────────────────────────────────────
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    token   = credentials.credentials
    payload = decode_token(token)

    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

    student_id = payload.get("sub")
    user       = db.query(models.User).filter(
        models.User.student_id == student_id
    ).first()

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive"
        )

    return user

# ── ADMIN ONLY ─────────────────────────────────────────────────
def get_admin_user(current_user = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

# ── TEACHER (or admin) ─────────────────────────────────────────
def get_teacher_user(current_user = Depends(get_current_user)):
    if current_user.role not in ("teacher", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Teacher access required"
        )
    return current_user


def is_staff(user) -> bool:
    return user.role in ("teacher", "admin")


def require_self_or_staff(user, student_id: str):
    """Students may only touch their own data; teachers/admins any."""
    if user.student_id != student_id and not is_staff(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only access your own data"
        )


def admin_from_token(token: Optional[str], db: Session):
    """Return the active admin user for a raw JWT, or None (used by the
    WebSocket, which cannot send an Authorization header from a browser)."""
    payload = decode_token(token) if token else None
    if not payload:
        return None
    user = db.query(models.User).filter(
        models.User.student_id == payload.get("sub")
    ).first()
    return user if user and user.is_active and user.role == "admin" else None


def get_admin_if_any_exist(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(optional_security),
    db: Session = Depends(get_db),
):
    """Bootstrap guard for admin registration: the very first admin may be
    created without a token; after that an admin token is required."""
    if not db.query(models.User).filter(models.User.role == "admin").first():
        return None
    user = admin_from_token(credentials.credentials if credentials else None, db)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required to create another admin"
        )
    return user
