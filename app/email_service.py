import smtplib
import secrets
import string
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv
from sqlalchemy.orm import Session
from . import models
import os

load_dotenv()

GMAIL_ADDRESS      = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
APP_NAME           = os.getenv("APP_NAME")

# ── GENERATE OTP ───────────────────────────────────────────────
def generate_otp() -> str:
    return ''.join(secrets.choice(string.digits) for _ in range(6))

# ── SAVE OTP TO DATABASE ───────────────────────────────────────
def save_otp(student_id: str, email: str, otp: str, db: Session):
    # Expire all old OTPs for this student
    old_otps = db.query(models.OTPCode).filter(
        models.OTPCode.student_id == student_id,
        models.OTPCode.is_used    == False
    ).all()
    for old in old_otps:
        old.is_used = True
    db.commit()

    # Create new OTP (expires in 10 minutes)
    new_otp = models.OTPCode(
        student_id = student_id,
        email      = email,
        otp_code   = otp,
        is_used    = False,
        expires_at = datetime.utcnow() + timedelta(minutes=10)
    )
    db.add(new_otp)
    db.commit()

# ── SEND OTP EMAIL ─────────────────────────────────────────────
def send_otp_email(to_email: str, student_id: str, otp: str) -> bool:
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'🔐 Your OTP Code — {APP_NAME}'
        msg['From']    = GMAIL_ADDRESS
        msg['To']      = to_email

        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 500px; margin: auto;">
            <div style="background: #1a1a2e; padding: 30px; border-radius: 10px;">
                <h2 style="color: #e94560; text-align: center;">
                    🔐 Password Reset OTP
                </h2>
                <div style="background: #16213e; padding: 20px; border-radius: 8px; margin: 20px 0;">
                    <p style="color: #a8b2d8; margin: 0;">Student ID:</p>
                    <p style="color: white; font-size: 18px; margin: 5px 0;">
                        {student_id}
                    </p>
                </div>
                <div style="background: #e94560; padding: 20px; border-radius: 8px;
                            text-align: center; margin: 20px 0;">
                    <p style="color: white; margin: 0; font-size: 14px;">
                        Your OTP Code
                    </p>
                    <p style="color: white; font-size: 40px; font-weight: bold;
                              letter-spacing: 10px; margin: 10px 0;">
                        {otp}
                    </p>
                    <p style="color: #ffcccc; margin: 0; font-size: 12px;">
                        ⏱️ Valid for 10 minutes only
                    </p>
                </div>
                <p style="color: #a8b2d8; font-size: 12px; text-align: center;">
                    If you didn't request this, please ignore this email.<br>
                    Your account is safe.
                </p>
                <hr style="border-color: #333; margin: 20px 0;">
                <p style="color: #555; font-size: 11px; text-align: center;">
                    {APP_NAME}<br>
                    This is an automated email. Please do not reply.
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())

        print(f"✅ OTP email sent to {to_email}")
        return True

    except Exception as e:
        print(f"❌ Email error: {e}")
        return False

# ── SEND WELCOME EMAIL ─────────────────────────────────────────
def send_welcome_email(to_email: str, student_id: str, full_name: str) -> bool:
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'✅ Welcome to {APP_NAME}'
        msg['From']    = GMAIL_ADDRESS
        msg['To']      = to_email

        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 500px; margin: auto;">
            <div style="background: #1a1a2e; padding: 30px; border-radius: 10px;">
                <h2 style="color: #4ecca3; text-align: center;">
                    ✅ Account Created Successfully!
                </h2>
                <div style="background: #16213e; padding: 20px;
                            border-radius: 8px; margin: 20px 0;">
                    <p style="color: #a8b2d8; margin: 5px 0;">
                        Welcome, <strong style="color: white;">{full_name}</strong>
                    </p>
                    <p style="color: #a8b2d8; margin: 5px 0;">
                        Student ID: <strong style="color: #4ecca3;">{student_id}</strong>
                    </p>
                </div>
                <div style="background: #0f3460; padding: 15px;
                            border-radius: 8px; margin: 20px 0;">
                    <p style="color: white; margin: 0; font-size: 14px;">
                        📌 <strong>Next Steps:</strong>
                    </p>
                    <p style="color: #a8b2d8; margin: 10px 0 0 0; font-size: 13px;">
                        1. Install the VS Code Extension<br>
                        2. Login with your Student ID and password<br>
                        3. You are ready for your exam! ✅
                    </p>
                </div>
                <hr style="border-color: #333; margin: 20px 0;">
                <p style="color: #555; font-size: 11px; text-align: center;">
                    {APP_NAME}<br>
                    This is an automated email. Please do not reply.
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, to_email, msg.as_string())

        print(f"✅ Welcome email sent to {to_email}")
        return True

    except Exception as e:
        print(f"❌ Welcome email error: {e}")
        return False