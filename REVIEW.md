# Review guide: portal redesign, admin password change, teacher AI report

This branch changes the web portals. Nothing here touches the live server until it is merged.
Screenshots of every page are in `docs/screenshots/`.

## What changed

- **Whole site** restyled as a formal university site (navy header, orange accent, EB Garamond + Source Sans). Structure and JS logic are unchanged. Styles live in `static/ewu.css`.
- **Landing page** (`/`) with three entries: Student, Teacher, Administrator.
- **Sign-in pages** (`/student/login`, `/teacher/login`, `/admin/login`): username box says "Use your university mail", password box says "Use your university portal password". The register page uses the same wording.
- **Admin panel** (`/admin`): live monitoring removed (that is the teacher's job). The admin now sees totals, teachers, saved datasets (Excel/JSON), the teacher sign-up code and user accounts.
- **Admin can change any user's password** (student, teacher, admin): `PUT /admin/users/{id}/password`. Minimum 6 characters. It also clears a lockout.
- **Teacher panel**: the student Report popup lists AI % per code file plus the average (`GET /teacher/student/{id}/codes`).
- **Class cards** are light blue instead of random colours.
- Optional DroidDetect model (`app/ml/droid_detector.py`) is **off by default**. It only runs when `DROID_MODEL_DIR` is set, and it needs 2 to 3 GB of RAM. Leave it unset.

## Run it locally (about 2 minutes)

Needs Python 3.10 or newer. No database server, no torch, no model files.

```powershell
git clone https://github.com/ahisrat10/screening-platform.git
cd screening-platform
git checkout <this-branch>
python -m venv .venv
.venv\Scripts\activate
pip install fastapi uvicorn sqlalchemy python-jose passlib bcrypt==4.0.1 python-multipart "pydantic[email]" openpyxl requests apscheduler python-dotenv numpy
$env:SECRET_KEY = "dev"
python tools/run_dev.py
```

Then open the links below. `tools/run_dev.py` creates a throwaway SQLite file with demo data on each start. It never touches the real database.

## Demo links and accounts (local only)

| Portal | Link | Username | Password |
| --- | --- | --- | --- |
| Student | http://127.0.0.1:8765/student/login | `s1@std.ewubd.edu` or `2021-1-60-018` | `user@1234` |
| Teacher | http://127.0.0.1:8765/teacher/login | `safi@gmail.com` | `password123` |
| Admin | http://127.0.0.1:8765/admin/login | `admin@ewu.edu` | `adminpass1` |

These accounts exist only in the throwaway local database. Use a hard refresh (Ctrl+Shift+R) if styles look old.

## Things to check

1. Admin: Users table, "Change password" on a student, then sign in as that student with the new password.
2. Teacher: open a class, press Report on a student, see AI % per file and the average.
3. Student: class cards are light blue.
4. Teacher/admin cannot be reached by a student (403).

## Deploy notes

- `Dockerfile` now copies `static/` (needed for the new styles).
- Merging this to the branch Railway/Render builds from will redeploy the site. All three portals are one server, so one deploy updates all of them.
- Three new tables are added (`app_settings`, `class_datasets`, `student_notes`). They are created automatically on start; no existing table is altered.
- Saved datasets are now kept in the database (30 days), because the host disk is wiped on every deploy.
