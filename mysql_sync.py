"""Pull teacher/student data straight from a college MySQL database.

Mirrors the CSV upload path: rows come back as (lineno, clean-dict) with the
same normalized header names, so they flow through the exact same validation
and save logic as an uploaded CSV. Nothing downstream (allocation, risk
scores, chatbot) knows or cares where the rows came from.

Expected tables (see mysql_schema.sql):
  students: roll, name, programme, semester, section (+ optional attendance, cgpa)
  subjects: teacher, subject, programme, semester, section
"""
import re

REQUIRED_SUBJECTS = ["teacher", "subject", "programme", "semester", "section"]
REQUIRED_STUDENTS = ["roll", "name", "programme", "semester", "section"]

MAX_ROWS = 50000


class MySQLError(Exception):
    """A user-facing MySQL problem (connection, table, or columns)."""


def _norm(h):
    return (h or "").strip().lower().replace(" ", "_")


def _check_table_name(table):
    if not re.fullmatch(r"[A-Za-z0-9_]+", table or ""):
        raise MySQLError(f"Bad table name '{table}'. Use letters, digits and underscores only.")


def fetch_table(params, table, required):
    """Read one MySQL table. Returns [(lineno, {col: value})] like read_csv."""
    _check_table_name(table)
    try:
        import pymysql
    except ImportError:
        raise MySQLError("PyMySQL is not installed on this PC. Run:  pip install pymysql")
    host, port = params.get("host") or "localhost", params.get("port") or 3306
    try:
        conn = pymysql.connect(host=host, port=int(port), user=params.get("user"),
                               password=params.get("password") or "",
                               database=params.get("database"),
                               connect_timeout=10, read_timeout=30)
    except Exception as e:
        raise MySQLError(f"Could not connect to MySQL at {host}:{port} as "
                         f"'{params.get('user')}'. Check host, port, username, password "
                         f"and that the MySQL server allows remote connections. ({e})")
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(f"SELECT * FROM `{table}` LIMIT {MAX_ROWS}")
            except Exception:
                raise MySQLError(f"Table '{table}' was not found in database "
                                 f"'{params.get('database')}'. Check the table name.")
            cols = [_norm(c[0]) for c in cur.description]
            missing = [c for c in required if c not in cols]
            if missing:
                raise MySQLError(f"Table '{table}' is missing required column(s): "
                                 + ", ".join(missing) + ". Found: " + ", ".join(sorted(cols))
                                 + ". See mysql_schema.sql for the expected layout.")
            rows = []
            for i, rec in enumerate(cur.fetchall(), start=2):
                clean = {_norm(k): ("" if v is None else str(v)).strip()
                         for k, v in zip(cols, rec)}
                if any(clean.values()):
                    rows.append((i, clean))
            if not rows:
                raise MySQLError(f"Table '{table}' has no data rows.")
            return rows
    finally:
        try:
            conn.close()
        except Exception:
            pass
