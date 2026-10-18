"""Smart Mentor Allocation & Student Support System - Flask + SQLite backend."""
import csv
import datetime as dt
import io
import os
import sqlite3
from collections import defaultdict

from flask import Flask, g, jsonify, request, send_from_directory

from allocation import NO_ELIGIBLE, allocate

import ai_helper
from mysql_sync import (REQUIRED_STUDENTS, REQUIRED_SUBJECTS, MySQLError,
                        fetch_table as mysql_fetch_table)

BASE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS teaching(            -- approved subject allocation
  id INTEGER PRIMARY KEY, teacher TEXT, subject TEXT,
  programme TEXT, semester TEXT, section TEXT);
CREATE TABLE IF NOT EXISTS teachers(name TEXT PRIMARY KEY, capacity INTEGER);
CREATE TABLE IF NOT EXISTS students(
  roll TEXT PRIMARY KEY, name TEXT, programme TEXT, semester TEXT, section TEXT,
  attendance REAL, cgpa REAL, mentor TEXT);
CREATE TABLE IF NOT EXISTS requirements(
  roll TEXT PRIMARY KEY, exam_form TEXT DEFAULT 'pending',
  informed INTEGER DEFAULT 0, sessional TEXT DEFAULT 'pending',
  fee_status TEXT DEFAULT 'pending', remarks TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS queries(
  id INTEGER PRIMARY KEY, roll TEXT, kind TEXT, topic TEXT, message TEXT,
  status TEXT DEFAULT 'open', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS followups(
  id INTEGER PRIMARY KEY, query_id INTEGER, roll TEXT, author TEXT, role TEXT,
  kind TEXT, topic TEXT, note TEXT, created_at TEXT);
"""

TOPICS = {"exam_form", "informed", "sessional", "fee", "academic", "other"}
EXAM_VALUES = {"pending", "submitted"}
SESS_VALUES = {"pending", "completed"}
FEE_VALUES = {"paid", "partial", "pending"}
QUERY_KINDS = {"completion", "issue"}
QUERY_STATUS = {"open", "in_progress", "resolved"}
MAX_UPLOAD = 2_000_000


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return dt.date.today()


def norm(h):
    return (h or "").strip().lower().replace(" ", "_")


def ckey(programme, semester, section):
    """Class identity, tolerant of case/spacing differences."""
    return (programme.strip().lower(), str(semester).strip(), section.strip().lower())


def create_app(db_path=None):
    app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
    app.config["DB_PATH"] = db_path or os.environ.get("MENTOR_DB", os.path.join(BASE, "mentor.db"))
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD + 100_000

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DB_PATH"])
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close(_):
        d = g.pop("db", None)
        if d:
            d.close()

    with app.app_context():
        d = db()
        d.executescript(SCHEMA)
        defaults = {
            "default_capacity": "25",
            "exam_form_deadline": str(today() + dt.timedelta(days=7)),
            "sessional_deadline": str(today() + dt.timedelta(days=14)),
            "fee_deadline": str(today() + dt.timedelta(days=21)),
            "gemini_api_key": "",
        }
        for k, v in defaults.items():
            d.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
        d.commit()

    # ------------------------------------------------------------ helpers
    def err(msg, code=400, **extra):
        return jsonify(error=msg, **extra), code

    def settings():
        return {r["key"]: r["value"] for r in db().execute("SELECT * FROM settings")}

    def capacity_map():
        s = settings()
        default = int(s["default_capacity"])
        return {r["name"]: (r["capacity"] if r["capacity"] is not None else default)
                for r in db().execute("SELECT * FROM teachers")}

    def eligible_map():
        el = defaultdict(set)
        for r in db().execute("SELECT * FROM teaching"):
            el[ckey(r["programme"], r["semester"], r["section"])].add(r["teacher"])
        return el

    def read_csv(file, required):
        """Validate an uploaded file. Returns (rows, errors)."""
        if file is None or not file.filename:
            return None, ["No file was uploaded."]
        if not file.filename.lower().endswith(".csv"):
            return None, ["Only .csv files are accepted (save Excel files as CSV)."]
        raw = file.read()
        if not raw.strip():
            return None, ["The file is empty."]
        if len(raw) > MAX_UPLOAD:
            return None, ["The file is too large (limit 2 MB)."]
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, ["The file is not readable text. Please upload a plain CSV file."]
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return None, ["The file has no header row."]
        headers = {norm(h) for h in reader.fieldnames if h}
        missing = [c for c in required if c not in headers]
        if missing:
            return None, ["Missing required column(s): " + ", ".join(missing)
                          + ". Found: " + ", ".join(sorted(headers))]
        rows = []
        for i, r in enumerate(reader, start=2):
            clean = {norm(k): (v or "").strip() for k, v in r.items() if k}
            if any(clean.values()):
                rows.append((i, clean))
        if not rows:
            return None, ["The file has a header but no data rows."]
        return rows, []

    def int_in(value, lo, hi):
        try:
            n = int(float(value))
            return n if lo <= n <= hi and float(value) == n else None
        except (TypeError, ValueError):
            return None

    def revoke_ineligible():
        """Unassign any student whose mentor does not teach their class."""
        el = eligible_map()
        n = 0
        for s in db().execute("SELECT * FROM students WHERE mentor IS NOT NULL").fetchall():
            if s["mentor"] not in el.get(ckey(s["programme"], s["semester"], s["section"]), ()):
                db().execute("UPDATE students SET mentor=NULL WHERE roll=?", (s["roll"],))
                n += 1
        return n

    def risk(s, req, open_issues):
        """Transparent rule-based support score (no black box)."""
        pts, why = 0, []
        st = settings()
        t = today()

        def overdue(key):
            try:
                return dt.date.fromisoformat(st[key]) < t
            except (ValueError, KeyError):
                return False
        if s["attendance"] is not None:
            if s["attendance"] < 75:
                pts += 3; why.append(f"Low attendance ({s['attendance']:.0f}%)")
            elif s["attendance"] < 85:
                pts += 1; why.append(f"Attendance borderline ({s['attendance']:.0f}%)")
        if s["cgpa"] is not None:
            if s["cgpa"] < 5:
                pts += 3; why.append(f"Low CGPA ({s['cgpa']:.1f})")
            elif s["cgpa"] < 6:
                pts += 1; why.append(f"CGPA below 6 ({s['cgpa']:.1f})")
        if req["exam_form"] != "submitted":
            pts += 2 if overdue("exam_form_deadline") else 0
            if overdue("exam_form_deadline"):
                why.append("Exam form overdue")
        if req["sessional"] != "completed" and overdue("sessional_deadline"):
            pts += 2; why.append("Sessional requirement overdue")
        if req["fee_status"] != "paid" and overdue("fee_deadline"):
            pts += 2; why.append("Fees overdue (%s)" % req["fee_status"])
        elif req["fee_status"] == "pending":
            pts += 1; why.append("Fees pending")
        if open_issues:
            pts += 1; why.append(f"{open_issues} open issue(s)")
        level = "high" if pts >= 4 else "medium" if pts >= 2 else "low"
        return {"score": pts, "level": level, "reasons": why}

    def req_view(req):
        st = settings()
        out = {}
        for key, dl in (("exam_form", "exam_form_deadline"), ("sessional", "sessional_deadline"),
                        ("fee", "fee_deadline")):
            done = {"exam_form": req["exam_form"] == "submitted",
                    "sessional": req["sessional"] == "completed",
                    "fee": req["fee_status"] == "paid"}[key]
            try:
                days = (dt.date.fromisoformat(st[dl]) - today()).days
            except ValueError:
                days = None
            out[key] = {"deadline": st[dl], "days_left": days, "done": done,
                        "overdue": (not done) and days is not None and days < 0}
        return out

    def student_row(s):
        r = db().execute("SELECT * FROM requirements WHERE roll=?", (s["roll"],)).fetchone()
        oi = db().execute("SELECT COUNT(*) c FROM queries WHERE roll=? AND status!='resolved'",
                          (s["roll"],)).fetchone()["c"]
        return {**dict(s), "requirements": dict(r), "deadlines": req_view(r),
                "open_queries": oi, "risk": risk(s, r, oi)}

    # ------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    # ------------------------------------------------------------ uploads
    # _save_*_rows take [(lineno, clean-dict)] exactly like read_csv returns,
    # so CSV uploads AND the MySQL sync share one validation + save path.
    def _save_subject_rows(rows):
        """Validate + save subject-allocation rows. Returns (ok, data, code)."""
        bad, good, seen = [], [], set()
        for i, r in rows:
            blanks = [c for c in ("teacher", "subject", "programme", "semester", "section") if not r.get(c)]
            if blanks:
                bad.append(f"Row {i}: blank {', '.join(blanks)}"); continue
            sem = int_in(r["semester"], 1, 12)
            if sem is None:
                bad.append(f"Row {i}: semester '{r['semester']}' must be a whole number 1-12"); continue
            key = (r["teacher"].lower(), r["subject"].lower(), ckey(r["programme"], sem, r["section"]))
            if key in seen:
                continue
            seen.add(key)
            good.append((r["teacher"], r["subject"], r["programme"], str(sem), r["section"]))
        if bad:
            return False, {"error": f"{len(bad)} invalid row(s). Nothing was saved - fix the source and retry.",
                           "errors": bad[:25]}, 400
        d = db()
        d.execute("DELETE FROM teaching")
        d.executemany("INSERT INTO teaching(teacher,subject,programme,semester,section) VALUES(?,?,?,?,?)", good)
        names = {t[0] for t in good}
        d.execute("DELETE FROM teachers WHERE name NOT IN (%s)" % ",".join("?" * len(names)), tuple(names))
        for n in names:
            d.execute("INSERT OR IGNORE INTO teachers(name,capacity) VALUES(?,NULL)", (n,))
        revoked = revoke_ineligible()
        d.commit()
        return True, {"ok": True, "rows_saved": len(good), "teachers": len(names),
                      "mentors_revoked": revoked}, 200

    @app.post("/api/upload/subjects")
    def upload_subjects():
        rows, errs = read_csv(request.files.get("file"),
                              ["teacher", "subject", "programme", "semester", "section"])
        if errs:
            return err("Upload rejected.", errors=errs)
        ok, data, code = _save_subject_rows(rows)
        return jsonify(**data), code

    def _save_student_rows(rows):
        """Validate + save student rows. Returns (ok, data, code)."""
        bad, good, seen = [], [], set()
        for i, r in rows:
            blanks = [c for c in ("roll", "name", "programme", "semester", "section") if not r.get(c)]
            if blanks:
                bad.append(f"Row {i}: blank {', '.join(blanks)}"); continue
            if r["roll"] in seen:
                bad.append(f"Row {i}: duplicate roll '{r['roll']}'"); continue
            seen.add(r["roll"])
            sem = int_in(r["semester"], 1, 12)
            if sem is None:
                bad.append(f"Row {i}: semester '{r['semester']}' must be a whole number 1-12"); continue
            att = cg = None
            if r.get("attendance"):
                try:
                    att = float(r["attendance"])
                    assert 0 <= att <= 100
                except (ValueError, AssertionError):
                    bad.append(f"Row {i}: attendance must be 0-100"); continue
            if r.get("cgpa"):
                try:
                    cg = float(r["cgpa"])
                    assert 0 <= cg <= 10
                except (ValueError, AssertionError):
                    bad.append(f"Row {i}: cgpa must be 0-10"); continue
            good.append((r["roll"], r["name"], r["programme"], str(sem), r["section"], att, cg))
        if bad:
            return False, {"error": f"{len(bad)} invalid row(s). Nothing was saved - fix the source and retry.",
                           "errors": bad[:25]}, 400
        d = db()
        for roll, name, prog, sem, sec, att, cg in good:
            old = d.execute("SELECT * FROM students WHERE roll=?", (roll,)).fetchone()
            mentor = old["mentor"] if old and ckey(old["programme"], old["semester"], old["section"]) \
                == ckey(prog, sem, sec) else None
            d.execute("INSERT OR REPLACE INTO students VALUES(?,?,?,?,?,?,?,?)",
                      (roll, name, prog, sem, sec, att, cg, mentor))
            d.execute("INSERT OR IGNORE INTO requirements(roll) VALUES(?)", (roll,))
        revoked = revoke_ineligible()
        d.commit()
        el = eligible_map()
        no_teacher = sorted({f"{p} Sem {s} Sec {x}" for _, _, p, s, x, _, _ in good
                             if not el.get(ckey(p, s, x))})
        return True, {"ok": True, "students_saved": len(good), "mentors_revoked": revoked,
                      "warning_classes_without_teachers": no_teacher}, 200

    @app.post("/api/upload/students")
    def upload_students():
        rows, errs = read_csv(request.files.get("file"),
                              ["roll", "name", "programme", "semester", "section"])
        if errs:
            return err("Upload rejected.", errors=errs)
        ok, data, code = _save_student_rows(rows)
        return jsonify(**data), code

    @app.post("/api/mysql-sync")
    def mysql_sync():
        """One-click MySQL sync: pull both tables, replace all data, allocate everyone.

        Mirrors /api/demo-reset but the source is the college MySQL database
        instead of the bundled demo CSVs. Credentials are used once for this
        request and never stored.
        """
        body = request.get_json(silent=True) or {}
        host = (body.get("host") or "localhost").strip()
        try:
            port = int(body.get("port") or 3306)
        except (TypeError, ValueError):
            return err("Port must be a number.")
        user = (body.get("user") or "").strip()
        database = (body.get("database") or "").strip()
        students_table = (body.get("students_table") or "students").strip()
        subjects_table = (body.get("subjects_table") or "subjects").strip()
        if not user or not database:
            return err("MySQL username and database name are required.")
        params = {"host": host, "port": port, "user": user,
                  "password": body.get("password") or "", "database": database}
        try:
            subj_rows = mysql_fetch_table(params, subjects_table, REQUIRED_SUBJECTS)
            stud_rows = mysql_fetch_table(params, students_table, REQUIRED_STUDENTS)
        except MySQLError as e:
            return err(str(e), 502)
        d = db()
        for t in ("followups", "queries", "requirements", "students", "teachers", "teaching"):
            d.execute(f"DELETE FROM {t}")
        d.commit()
        ok, sdata, code = _save_subject_rows(subj_rows)
        if not ok:
            return jsonify(**sdata), code
        ok, stdata, code = _save_student_rows(stud_rows)
        if not ok:
            return jsonify(**stdata), code
        res = _run_allocation("reset")
        if "error" in res:
            return err(res["error"])
        return jsonify(ok=True, mysql_sync=True,
                       rows_saved=sdata["rows_saved"], teachers=sdata["teachers"],
                       students_saved=stdata["students_saved"],
                       newly_assigned=res["newly_assigned"], unallocated=res["unallocated"],
                       report=res["report"])

    def _load_sample_data():
        """Wipe the DB and load the bundled demo CSVs. Returns the per-file results."""
        d = db()
        for t in ("followups", "queries", "requirements", "students", "teachers", "teaching"):
            d.execute(f"DELETE FROM {t}")
        d.commit()

        out = {}
        for name, fn in (("subjects", upload_subjects), ("students", upload_students)):
            path = os.path.join(BASE, "sample_data", f"{name}.csv")
            with open(path, "rb") as fh:
                with app.test_request_context(method="POST", data={"file": (io.BytesIO(fh.read()), f"{name}.csv")},
                                              content_type="multipart/form-data"):
                    resp = fn()
                    if isinstance(resp, tuple):  # (Response, status) from _save_*_rows
                        resp = resp[0]
                    out[name] = resp.get_json()
        # sprinkle realistic requirement states + remarks (deterministic)
        import random
        rnd = random.Random(7)
        for s in d.execute("SELECT roll FROM students").fetchall():
            d.execute("UPDATE requirements SET exam_form=?, informed=?, sessional=?, fee_status=?, remarks=? WHERE roll=?",
                      (rnd.choice(["submitted", "submitted", "pending"]), rnd.choice([0, 1, 1]),
                       rnd.choice(["completed", "pending"]), rnd.choice(["paid", "paid", "partial", "pending"]),
                       rnd.choice(["", "", "", "Needs a call to parents", "Fee waiver requested", "Medical leave"]),
                       s["roll"]))
        d.commit()
        return out

    @app.post("/api/load-sample")
    def load_sample():
        return jsonify(ok=True, **_load_sample_data())

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def get_settings():
        st = settings()
        st.pop("gemini_api_key", None)  # never expose the AI key to the browser
        return jsonify(settings=st,
                       teachers=[dict(r) for r in db().execute("SELECT * FROM teachers ORDER BY name")])

    @app.post("/api/settings")
    def set_settings():
        body = request.get_json(silent=True) or {}
        d = db()
        if "default_capacity" in body:
            cap = int_in(body["default_capacity"], 1, 500)
            if cap is None:
                return err("Capacity must be a whole number from 1 to 500.")
            d.execute("UPDATE settings SET value=? WHERE key='default_capacity'", (str(cap),))
        for k in ("exam_form_deadline", "sessional_deadline", "fee_deadline"):
            if k in body:
                try:
                    dt.date.fromisoformat(body[k])
                except (ValueError, TypeError):
                    return err(f"{k} must be a date like 2026-11-30.")
                d.execute("UPDATE settings SET value=? WHERE key=?", (body[k], k))
        for name, cap in (body.get("teacher_capacity") or {}).items():
            if cap in (None, ""):
                d.execute("UPDATE teachers SET capacity=NULL WHERE name=?", (name,))
            else:
                c = int_in(cap, 0, 500)
                if c is None:
                    return err(f"Capacity for {name} must be a whole number 0-500.")
                d.execute("UPDATE teachers SET capacity=? WHERE name=?", (c, name))
        d.commit()
        return get_settings()

    # ------------------------------------------------------------ AI (Gemini)
    def ai_key():
        return (settings().get("gemini_api_key") or "").strip()

    def ai_model_name():
        k = ai_key()
        return ai_helper.PICKED_MODELS.get(k, "auto") if k else "auto"

    def ai_fail_message(reason):
        """Turn a technical failure reason into a message the user can act on."""
        r = reason or "unknown"
        if r.startswith("http_"):
            code = r.split("_", 1)[1]
            if code == "429":
                return ("Too many requests — the free Gemini key is rate-limited. "
                        "Wait a minute, then click Save only once.")
            return f"Google returned error {code}. Wait a bit and try again."
        return {
            "bad_key": "Gemini rejected this key. Copy a fresh one from aistudio.google.com -> Get API key, and make sure there are no extra spaces.",
            "no_internet": "This PC could not reach Google. Check the internet connection and try again.",
            "model_not_found": "The key works, but no usable text model was found for it. Try a fresh key from aistudio.google.com.",
            "empty_reply": "Gemini returned an empty reply. Try again.",
            "bad_response": "Gemini returned something unreadable. Try again.",
        }.get(r, f"Gemini gave no reply (technical reason: {r}). Check the key and internet, then try again.")

    @app.get("/api/ai-key")
    def get_ai_key():
        k = ai_key()
        return jsonify(configured=bool(k), model=ai_model_name(),
                       masked=("...." + k[-4:]) if k else None)

    @app.post("/api/ai-key")
    def set_ai_key():
        body = request.get_json(silent=True) or {}
        key = (body.get("key") or "").strip()
        d = db()
        if not key:
            d.execute("UPDATE settings SET value='' WHERE key='gemini_api_key'")
            d.commit()
            return jsonify(ok=True, configured=False)
        # verify the key with one tiny call before saving it
        probe, reason = ai_helper.gemini_generate_any(key, "Reply with only this word: OK",
                                                      timeout=20, max_tokens=10)
        if not probe:
            print(f"[AI] key check failed, reason={reason}")
            return err(ai_fail_message(reason))
        d.execute("UPDATE settings SET value=? WHERE key='gemini_api_key'", (key,))
        d.commit()
        return jsonify(ok=True, configured=True, model=ai_model_name())

    @app.post("/api/ai-summary/<roll>")
    def ai_summary(roll):
        s = db().execute("SELECT * FROM students WHERE roll=?", (roll,)).fetchone()
        if not s:
            return err("Student not found.", 404)
        text, _provider, reason = ai_helper.ai_generate(ai_key(), ai_helper.summary_prompt(student_row(s)), timeout=30)
        if not text:
            print(f"[AI] summary failed, reason={reason}")
            return err("The AI service did not reply. Check the internet connection and try again.", 502)
        return jsonify(ok=True, roll=roll, summary=text)

    # ------------------------------------------------------------ allocation
    def _run_allocation(mode):
        """Core allocation logic shared by /api/allocate and /api/demo-reset."""
        d = db()
        if not d.execute("SELECT 1 FROM teaching LIMIT 1").fetchone():
            return {"error": "Upload the subject allocation first."}
        if not d.execute("SELECT 1 FROM students LIMIT 1").fetchone():
            return {"error": "Upload the student list first."}
        if mode == "reset":
            d.execute("UPDATE students SET mentor=NULL")
        caps = capacity_map()
        el = eligible_map()
        load = defaultdict(int)
        pending = defaultdict(list)
        for s in d.execute("SELECT * FROM students"):
            if s["mentor"]:
                load[s["mentor"]] += 1
            else:
                pending[ckey(s["programme"], s["semester"], s["section"])].append(s["roll"])
        before = dict(load)
        assigned, unalloc = allocate(pending, el, load, caps)
        for roll, teacher in assigned.items():
            d.execute("UPDATE students SET mentor=? WHERE roll=?", (teacher, roll))
        d.commit()
        return {"ok": True, "newly_assigned": len(assigned), "unallocated": len(unalloc),
                "previous_load": before, "report": report_data()}

    @app.post("/api/allocate")
    def run_allocation():
        mode = (request.get_json(silent=True) or {}).get("mode", "keep")
        if mode not in ("keep", "reset"):
            return err("mode must be 'keep' or 'reset'.")
        res = _run_allocation(mode)
        if "error" in res:
            return err(res["error"])
        return jsonify(**res)

    @app.post("/api/demo-reset")
    def demo_reset():
        """One-click demo: wipe everything, load demo data, allocate everyone."""
        sample = _load_sample_data()
        res = _run_allocation("reset")
        if "error" in res:
            return err(res["error"])
        return jsonify(ok=True, demo_reset=True, sample=sample,
                       newly_assigned=res["newly_assigned"], unallocated=res["unallocated"],
                       report=res["report"])

    def report_data():
        d = db()
        caps, el = capacity_map(), eligible_map()
        students = d.execute("SELECT * FROM students").fetchall()
        load = defaultdict(int)
        for s in students:
            if s["mentor"]:
                load[s["mentor"]] += 1
        teaches = defaultdict(set)
        for r in d.execute("SELECT * FROM teaching"):
            teaches[r["teacher"]].add(f"{r['programme']} S{r['semester']}-{r['section']}")
        mentors = [{"name": n, "capacity": caps[n], "load": load[n],
                    "free": max(0, caps[n] - load[n]), "classes": sorted(teaches[n])}
                   for n in sorted(caps)]
        un = []
        for s in students:
            if not s["mentor"]:
                k = ckey(s["programme"], s["semester"], s["section"])
                if not el.get(k):
                    reason = NO_ELIGIBLE
                    text = "No teacher is assigned to this class in the subject allocation."
                else:
                    reason = "ALL_ELIGIBLE_MENTORS_AT_CAPACITY"
                    text = "Every teacher who teaches this class is at capacity (or not yet allocated)."
                un.append({"roll": s["roll"], "name": s["name"],
                           "class": f"{s['programme']} Sem {s['semester']} Sec {s['section']}",
                           "reason": reason, "explanation": text})
        loads = [m["load"] for m in mentors] or [0]
        return {"total_students": len(students), "assigned": len(students) - len(un),
                "unallocated": un, "mentors": mentors,
                "min_load": min(loads), "max_load": max(loads)}

    @app.get("/api/report")
    def report():
        return jsonify(report_data())

    @app.get("/api/overview")
    def overview():
        d = db()
        studs = [student_row(s) for s in d.execute("SELECT * FROM students")]
        c = lambda f: sum(1 for s in studs if f(s))
        return jsonify(
            students=len(studs), high_risk=c(lambda s: s["risk"]["level"] == "high"),
            exam_pending=c(lambda s: s["requirements"]["exam_form"] != "submitted"),
            not_informed=c(lambda s: not s["requirements"]["informed"]),
            sessional_pending=c(lambda s: s["requirements"]["sessional"] != "completed"),
            fee_paid=c(lambda s: s["requirements"]["fee_status"] == "paid"),
            fee_partial=c(lambda s: s["requirements"]["fee_status"] == "partial"),
            fee_pending=c(lambda s: s["requirements"]["fee_status"] == "pending"),
            open_queries=d.execute("SELECT COUNT(*) c FROM queries WHERE status!='resolved'").fetchone()["c"])

    # ------------------------------------------------------------ students
    @app.get("/api/students")
    def list_students():
        mentor = request.args.get("mentor")
        q = "SELECT * FROM students"
        args = ()
        if mentor:
            q += " WHERE mentor=?"; args = (mentor,)
        rows = [student_row(s) for s in db().execute(q + " ORDER BY roll", args)]
        return jsonify(rows)

    @app.get("/api/student/<roll>")
    def one_student(roll):
        s = db().execute("SELECT * FROM students WHERE roll=?", (roll,)).fetchone()
        if not s:
            return err("Student not found.", 404)
        row = student_row(s)
        row["queries"] = queries_for("q.roll=?", (roll,))
        row["reminders"] = [dict(r) for r in db().execute(
            "SELECT * FROM followups WHERE roll=? AND kind='reminder' ORDER BY id DESC", (roll,))]
        return jsonify(row)

    @app.post("/api/requirements/<roll>")
    def update_req(roll):
        d = db()
        if not d.execute("SELECT 1 FROM students WHERE roll=?", (roll,)).fetchone():
            return err("Student not found.", 404)
        b = request.get_json(silent=True) or {}
        rules = {"exam_form": EXAM_VALUES, "sessional": SESS_VALUES, "fee_status": FEE_VALUES}
        for k, allowed in rules.items():
            if k in b:
                if b[k] not in allowed:
                    return err(f"{k} must be one of: {', '.join(sorted(allowed))}")
                d.execute(f"UPDATE requirements SET {k}=? WHERE roll=?", (b[k], roll))
        if "informed" in b:
            d.execute("UPDATE requirements SET informed=? WHERE roll=?", (1 if b["informed"] else 0, roll))
        if "remarks" in b:
            d.execute("UPDATE requirements SET remarks=? WHERE roll=?", (str(b["remarks"])[:300], roll))
        d.commit()
        return jsonify(student_row(d.execute("SELECT * FROM students WHERE roll=?", (roll,)).fetchone()))

    # ------------------------------------------------------------ queries
    def queries_for(where, args):
        rows = db().execute(f"""SELECT q.*, s.name student_name, s.mentor FROM queries q
                                JOIN students s ON s.roll=q.roll WHERE {where} ORDER BY q.id DESC""", args).fetchall()
        out = []
        for r in rows:
            fu = db().execute("SELECT * FROM followups WHERE query_id=? ORDER BY id", (r["id"],)).fetchall()
            out.append({**dict(r), "followups": [dict(f) for f in fu]})
        return out

    @app.get("/api/queries")
    def list_queries():
        where, args = ["1=1"], []
        if request.args.get("mentor"):
            where.append("s.mentor=?"); args.append(request.args["mentor"])
        if request.args.get("status"):
            where.append("q.status=?"); args.append(request.args["status"])
        return jsonify(queries_for(" AND ".join(where), tuple(args)))

    @app.post("/api/queries")
    def create_query():
        b = request.get_json(silent=True) or {}
        d = db()
        if not d.execute("SELECT 1 FROM students WHERE roll=?", (b.get("roll"),)).fetchone():
            return err("Unknown student roll number.", 404)
        if b.get("kind") not in QUERY_KINDS:
            return err("kind must be 'completion' or 'issue'.")
        if b.get("topic") not in TOPICS:
            return err("topic must be one of: " + ", ".join(sorted(TOPICS)))
        msg = (b.get("message") or "").strip()
        if b["kind"] == "issue" and not msg:
            return err("Please describe the issue.")
        if len(msg) > 500:
            return err("Message is too long (500 characters max).")
        if b["kind"] == "completion":
            msg = msg or "I have completed this."
        t = now()
        cur = d.execute("INSERT INTO queries(roll,kind,topic,message,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (b["roll"], b["kind"], b["topic"], msg, "open", t, t))
        d.commit()
        return jsonify(ok=True, id=cur.lastrowid)

    @app.post("/api/queries/<int:qid>/followup")
    def add_followup(qid):
        b = request.get_json(silent=True) or {}
        d = db()
        q = d.execute("SELECT * FROM queries WHERE id=?", (qid,)).fetchone()
        if not q:
            return err("Query not found.", 404)
        role = b.get("role", "mentor")
        if role not in ("mentor", "student"):
            return err("role must be 'mentor' or 'student'.")
        note = (b.get("note") or "").strip()
        status = b.get("status")
        if status and status not in QUERY_STATUS:
            return err("status must be open, in_progress or resolved.")
        if not note and not status:
            return err("Write a note or choose a status.")
        if role == "student" and status:
            return err("Only a mentor can change the status.")
        if role == "student" and q["status"] == "resolved":
            status = "open"  # student reply re-opens
        author = (d.execute("SELECT mentor FROM students WHERE roll=?", (q["roll"],)).fetchone()["mentor"]
                  if role == "mentor" else q["roll"]) or "Mentor"
        d.execute("INSERT INTO followups(query_id,roll,author,role,kind,topic,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (qid, q["roll"], author, role, "followup", q["topic"], note or f"Status set to {status}", now()))
        if status:
            d.execute("UPDATE queries SET status=?, updated_at=? WHERE id=?", (status, now(), qid))
            # confirming a completion report updates the real requirement
            if status == "resolved" and q["kind"] == "completion" and b.get("verify"):
                m = {"exam_form": ("exam_form", "submitted"), "sessional": ("sessional", "completed"),
                     "fee": ("fee_status", "paid"), "informed": ("informed", 1)}.get(q["topic"])
                if m:
                    d.execute(f"UPDATE requirements SET {m[0]}=? WHERE roll=?", (m[1], q["roll"]))
        d.commit()
        return jsonify(ok=True)

    @app.post("/api/reminders")
    def send_reminders():
        """Mentor reminds one student (roll) or all mentees still pending on a topic."""
        b = request.get_json(silent=True) or {}
        topic, mentor = b.get("topic"), b.get("mentor")
        if topic not in {"exam_form", "informed", "sessional", "fee"}:
            return err("topic must be exam_form, informed, sessional or fee.")
        d = db()
        pend = {"exam_form": "exam_form!='submitted'", "informed": "informed=0",
                "sessional": "sessional!='completed'", "fee": "fee_status!='paid'"}[topic]
        if b.get("roll"):
            rows = d.execute(f"SELECT s.roll, s.mentor FROM students s JOIN requirements r USING(roll) "
                             f"WHERE s.roll=? AND {pend}", (b["roll"],)).fetchall()
        elif mentor:
            rows = d.execute(f"SELECT s.roll, s.mentor FROM students s JOIN requirements r USING(roll) "
                             f"WHERE s.mentor=? AND {pend}", (mentor,)).fetchall()
        else:
            return err("Provide a student roll or a mentor name.")
        labels = {"exam_form": "submit your examination form", "informed": "read the exam notice",
                  "sessional": "complete your sessional requirements", "fee": "clear your pending fees"}
        st = settings()
        dl = {"exam_form": "exam_form_deadline", "sessional": "sessional_deadline", "fee": "fee_deadline"}.get(topic)
        for r in rows:
            note = (b.get("note") or "").strip() or \
                f"Reminder: please {labels[topic]}" + (f" before {st[dl]}." if dl else ".")
            d.execute("INSERT INTO followups(query_id,roll,author,role,kind,topic,note,created_at) VALUES(NULL,?,?,?,?,?,?,?)",
                      (r["roll"], r["mentor"] or mentor or "Mentor", "mentor", "reminder", topic, note, now()))
        d.commit()
        return jsonify(ok=True, reminders_sent=len(rows))

    # ------------------------------------------------------------ student chatbot
    @app.post("/api/chat")
    def chat():
        """Rule-based assistant. Answers only from the student's own records (no made-up facts)."""
        b = request.get_json(silent=True) or {}
        s = db().execute("SELECT * FROM students WHERE roll=?", (b.get("roll"),)).fetchone()
        if not s:
            return err("Unknown student.", 404)
        raw = (b.get("message") or "").strip()
        msg = raw.lower()
        if not msg:
            return err("Type a message.")
        row = student_row(s)
        r, d = row["requirements"], row["deadlines"]

        def when(k):
            x = d[k]
            return (f"deadline {x['deadline']} ({'overdue by %d days' % -x['days_left'] if x['overdue'] else '%d days left' % x['days_left']})")
        has = lambda *w: any(x in msg for x in w)
        topic, text = None, None
        if has("mentor", "who is my", "guide"):
            text = f"Your mentor is {s['mentor']}." if s["mentor"] else "You have not been assigned a mentor yet. Please ask the admin to run allocation."
        elif has("fee", "pay", "tuition"):
            topic = "fee"
            text = f"Your fee status is '{r['fee_status']}'. " + ("Nothing more to do." if r["fee_status"] == "paid" else f"Fee {when('fee')}. Pay the remaining amount at the accounts office, then press 'I have completed this' with topic Fees.")
        elif has("exam form", "examination form", "form"):
            topic = "exam_form"
            text = "Your exam form is submitted." if r["exam_form"] == "submitted" else f"Your exam form is still pending - {when('exam_form')}. Submit it, then report completion so your mentor can confirm."
        elif has("sessional", "internal", "assignment"):
            topic = "sessional"
            text = "Your sessional requirements are completed." if r["sessional"] == "completed" else f"Sessional requirements are pending - {when('sessional')}. Complete them and report it to your mentor."
        elif has("deadline", "due", "last date", "when"):
            text = "Deadlines: " + "; ".join(f"{n}: {d[k]['deadline']} ({'done' if d[k]['done'] else 'pending'})"
                                              for k, n in (("exam_form", "Exam form"), ("sessional", "Sessional"), ("fee", "Fees")))
        elif has("pending", "todo", "to do", "left", "status", "what do i need", "remaining"):
            todo = []
            if r["exam_form"] != "submitted": todo.append("submit exam form")
            if not r["informed"]: todo.append("read the exam notice")
            if r["sessional"] != "completed": todo.append("complete sessional requirements")
            if r["fee_status"] != "paid": todo.append(f"clear fees ({r['fee_status']})")
            text = ("You still need to: " + ", ".join(todo) + ".") if todo else "You have nothing pending. Great job!"
        elif has("attendance", "cgpa", "marks", "grade", "support", "struggl", "fail", "help"):
            reasons = row["risk"]["reasons"]
            text = ("Things that may need attention: " + "; ".join(reasons) + ". Talk to your mentor - you can raise an 'Academic help' issue below.") \
                if reasons else "Your records look healthy. If you still need help, raise an 'Academic help' issue and your mentor will reply."
            topic = "academic"
        elif has("hello", "hi", "hey"):
            text = f"Hi {s['name'].split()[0]}! Ask me about your fees, exam form, sessional, deadlines, mentor, or what is pending."
        else:
            text = "I can answer about fees, exam form, sessionals, deadlines, your mentor, or what is pending. For anything else, I can send your question to your mentor."
        # AI first: Gemini if a key is set, else the free no-key service.
        # Rule-based reply is the final fallback, so the demo never breaks.
        key = ai_key()
        ai_text, _provider, _reason = ai_helper.ai_generate(key, ai_helper.chat_prompt(row, raw), timeout=30)
        if ai_text:
            return jsonify(reply=ai_text, ai=True, can_escalate=True, topic=topic or "other")
        return jsonify(reply=text, ai=False, can_escalate=True, topic=topic or "other")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
