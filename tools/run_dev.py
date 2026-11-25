"""Local review server. Throwaway SQLite DB + demo accounts. Not for production.
Run from the repo root:  python tools/run_dev.py   ->  http://127.0.0.1:8765
"""
import os, sys
here = os.path.dirname(os.path.abspath(__file__))
root = os.path.dirname(here)
db = os.path.join(here, "dev.db")  # recreated on every start
if os.path.exists(db): os.remove(db)
os.environ["DATABASE_URL"] = "sqlite:///" + db.replace("\\", "/")
os.environ.setdefault("SECRET_KEY", "dev-secret")
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB
@compiles(JSONB, "sqlite")
def _j(t, c, **kw): return "JSON"
sys.path.insert(0, root)
from app import main, models, auth
from app.database import SessionLocal
d = SessionLocal()
def U(sid, email, name, role, pw): d.add(models.User(student_id=sid, email=email, full_name=name, role=role, password_hash=auth.hash_password(pw), is_active=True))
U("admin1", "admin@ewu.edu", "Admin", "admin", "adminpass1")
U("teacher-safi", "safi@gmail.com", "Safi Sir", "teacher", "password123")
U("2021-1-60-018", "s1@std.ewubd.edu", "Jafreen Jahan", "student", "user@1234")
U("2022-1-60-391", "s2@std.ewubd.edu", "Mohaimen Mostafa", "student", "user@1234")
d.commit()
c = models.Classroom(teacher_id="teacher-safi", semester="Fall 2026", course_name="CSE438", section="1", class_code="ABC123"); d.add(c); d.commit()
for sid in ("2021-1-60-018", "2022-1-60-391"): d.add(models.ClassMember(class_id=c.id, student_id=sid))
d.add(models.LabSession(class_id=c.id, started_by="teacher-safi", is_active=False)); d.commit()
d.add(models.CodeSnapshot(student_id="2021-1-60-018", filename="main.py", language="python", code="def add(a, b):\n    return a + b\n\nprint(add(2, 3))\n", line_count=5, snapshot_number=1))
d.add(models.CodeSnapshot(student_id="2021-1-60-018", filename="sort.py", language="python", code="nums = [3,1,2]\nnums.sort()\nprint(nums)\n", line_count=3, snapshot_number=1))
d.commit()
from app.rooms import save_dataset_for_lab
save_dataset_for_lab(d, c, None, "teacher-safi")
d.close()
from contextlib import asynccontextmanager
@asynccontextmanager
async def _noop(app):
    yield
main.app.router.lifespan_context = _noop
import uvicorn
uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")), log_level="warning")
