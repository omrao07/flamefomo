"""FLAME FOMO backend.

One Flask app (file: app.py, variable: app). It answers /api/... and also
serves the website from public/ so one command runs everything.

Database:
  * No DATABASE_URL  -> a SQLite file (great for trying it on your laptop)
  * DATABASE_URL set -> Postgres (needed on Vercel, because Vercel forgets files)
"""
import hashlib
import hmac
import json
import os
import secrets
import smtplib
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

import jwt
from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

# --------------------------------------------------------------------------
# Settings (all come from environment variables)
# --------------------------------------------------------------------------
IS_VERCEL = bool(os.environ.get("VERCEL"))
ROOT = os.path.dirname(os.path.abspath(__file__))
SECRET = os.environ.get("SECRET_KEY") or ("" if IS_VERCEL else "dev-only-secret-change-me")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "flame.edu.in").lower()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
SHOW_DEV_CODE = os.environ.get("DEV_SHOW_CODE") == "1"  # test mode: show the code on screen
COOKIE = "fomo_session"
SESSION_DAYS = 30
OTP_MINUTES = 10
OTP_COOLDOWN = 30  # seconds between code requests
OTP_MAX_TRIES = 5

IST = timezone(timedelta(hours=5, minutes=30))
FMT = "%Y-%m-%dT%H:%M"
CATEGORIES = ["Music", "Tech", "Sports", "Arts", "Career", "Food", "Academic", "Social"]
SOURCES = ["Student clubs", "Departments", "Official"]


def _pg_dsn():
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    keep = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items() if k == "sslmode"}
    if "sslmode" not in keep and p.hostname not in ("localhost", "127.0.0.1"):
        keep["sslmode"] = "require"
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(keep), ""))


PG_DSN = _pg_dsn()
IS_PG = bool(PG_DSN)
if IS_PG:
    import psycopg2
    import psycopg2.extras

SQLITE_PATH = os.environ.get("DB_PATH") or ("/tmp/fomo.db" if IS_VERCEL else os.path.join(ROOT, "fomo.db"))

app = Flask(__name__)


# --------------------------------------------------------------------------
# Tiny database helper that behaves the same on SQLite and Postgres
# --------------------------------------------------------------------------
class DB:
    def __init__(self):
        self.depth = 0
        if IS_PG:
            self.conn = psycopg2.connect(PG_DSN, connect_timeout=10)
        else:
            self.conn = sqlite3.connect(SQLITE_PATH, timeout=15, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")

    def run(self, sql, args=()):
        """Run SQL written with ? placeholders. Returns a list of dicts."""
        if IS_PG:
            sql = sql.replace("?", "%s")
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self.conn.cursor()
        try:
            cur.execute(sql, tuple(args))
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        except Exception:
            if IS_PG:
                self.conn.rollback()
            raise
        if IS_PG and not self.depth:
            self.conn.commit()
        return rows

    def one(self, sql, args=()):
        rows = self.run(sql, args)
        return rows[0] if rows else None

    @contextmanager
    def tx(self):
        """All-or-nothing block. On SQLite it takes the write lock first."""
        self.depth += 1
        if not IS_PG:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if IS_PG:
                self.conn.rollback()
            else:
                self.conn.execute("ROLLBACK")
            raise
        else:
            if IS_PG:
                self.conn.commit()
            else:
                self.conn.execute("COMMIT")
        finally:
            self.depth -= 1

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id {PK}, email TEXT UNIQUE NOT NULL, name TEXT, role TEXT NOT NULL DEFAULT 'student', created_at BIGINT);
CREATE TABLE IF NOT EXISTS otps(email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at BIGINT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS venues(id {PK}, name TEXT UNIQUE NOT NULL, x REAL NOT NULL, y REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(id {PK}, title TEXT NOT NULL, description TEXT, category TEXT NOT NULL, venue_id INTEGER, start_at TEXT NOT NULL, end_at TEXT NOT NULL, seats INTEGER, deadline TEXT, source TEXT NOT NULL DEFAULT 'Official', created_by INTEGER);
CREATE TABLE IF NOT EXISTS registrations(event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at BIGINT, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS going(event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS hidden(event_id INTEGER NOT NULL, user_id INTEGER NOT NULL, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS taste(user_id INTEGER NOT NULL, category TEXT NOT NULL, interest INTEGER NOT NULL DEFAULT 0, learned REAL NOT NULL DEFAULT 0, PRIMARY KEY(user_id, category));
CREATE TABLE IF NOT EXISTS busy(id {PK}, user_id INTEGER NOT NULL, title TEXT NOT NULL, kind TEXT NOT NULL, weekday INTEGER, day TEXT, start_min INTEGER NOT NULL, end_min INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
CREATE INDEX IF NOT EXISTS idx_busy_user ON busy(user_id)
"""

VENUES = [
    ("Main Gate", 12, 92), ("Academic Block", 30, 25), ("Library", 52, 22),
    ("Auditorium", 72, 35), ("Amphitheatre", 48, 55), ("Cafeteria", 30, 68),
    ("Sports Complex", 80, 75), ("Student Centre", 62, 60),
]
# title, category, venue, start hour, minutes long, seats (None = unlimited), source, text
TEMPLATES = [
    ("Open Mic Night", "Music", "Amphitheatre", 18, 120, 60, "Student clubs", "Sing, play or just cheer. Sign up to perform."),
    ("Hack the Campus", "Tech", "Academic Block", 10, 240, 40, "Departments", "A four-hour mini hackathon. Teams of three."),
    ("Intro to Product Management", "Career", "Auditorium", 15, 90, 120, "Official", "A talk from an alumnus who works at a startup."),
    ("Sunrise Yoga", "Sports", "Sports Complex", 7, 60, 25, "Student clubs", "Gentle morning stretch. Bring a mat."),
    ("Street Food Fest", "Food", "Cafeteria", 17, 120, None, "Student clubs", "Stalls from seven city kitchens."),
    ("Pottery Workshop", "Arts", "Student Centre", 16, 120, 15, "Student clubs", "Make a cup. Messy hands guaranteed."),
    ("Resume Clinic", "Career", "Library", 14, 60, 30, "Official", "Bring your resume. Leave with a better one."),
    ("Inter-house Football", "Sports", "Sports Complex", 16, 90, None, "Student clubs", "Cheer for your house."),
    ("Film Screening", "Arts", "Auditorium", 19, 150, 200, "Student clubs", "A cosy evening watching a classic."),
    ("Guest Lecture: AI and Society", "Academic", "Auditorium", 11, 75, 150, "Departments", "Open to all years."),
    ("Board Games Evening", "Social", "Student Centre", 19, 120, 30, "Student clubs", "Strategy games and snacks."),
    ("Poetry Slam", "Arts", "Amphitheatre", 18, 90, None, "Student clubs", "Five minutes each. Snaps, not claps."),
    ("Coding Interview Prep", "Tech", "Library", 16, 90, 35, "Departments", "Practice problems with seniors."),
    ("Jam Session", "Music", "Student Centre", 20, 120, None, "Student clubs", "Bring an instrument or your voice."),
]

_READY = False


def now():
    """Campus time (India), no timezone attached, minute precision."""
    return datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)


def parse(s):
    return datetime.strptime(s, FMT)


def iso(dt):
    return dt.strftime(FMT)


def minutes(dt):
    return dt.hour * 60 + dt.minute


def seed_demo(db):
    """Add sample events for the next 7 days so the app is never empty."""
    for name, x, y in VENUES:
        db.run("INSERT INTO venues(name, x, y) VALUES(?,?,?) ON CONFLICT(name) DO NOTHING", (name, x, y))
    vid = {v["name"]: v["id"] for v in db.run("SELECT id, name FROM venues")}
    today = now().replace(hour=0, minute=0)
    added = 0
    for off in range(7):
        for i in range(6):
            t = TEMPLATES[(off * 5 + i) % len(TEMPLATES)]
            title, cat, venue, hour, length, seats, source, text = t
            start = today + timedelta(days=off, hours=hour)
            if start < now() + timedelta(minutes=30):
                continue
            end = start + timedelta(minutes=length)
            deadline = None
            if seats is not None:
                deadline = start - timedelta(hours=(1, 3, 6, 20, 30)[(off + i) % 5])
                if deadline < now() + timedelta(minutes=30):
                    deadline = start
            db.run(
                "INSERT INTO events(title, description, category, venue_id, start_at, end_at, seats, deadline, source) VALUES(?,?,?,?,?,?,?,?,?)",
                (title, text, cat, vid[venue], iso(start), iso(end), seats, iso(deadline) if deadline else None, source),
            )
            added += 1
    return added


def ensure_ready():
    global _READY
    if _READY:
        return
    db = DB()
    try:
        pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        for stmt in SCHEMA.replace("{PK}", pk).split(";\n"):
            if stmt.strip():
                db.run(stmt)
        for name, x, y in VENUES:
            db.run("INSERT INTO venues(name, x, y) VALUES(?,?,?) ON CONFLICT(name) DO NOTHING", (name, x, y))
        upcoming = db.one("SELECT COUNT(*) AS c FROM events WHERE end_at >= ?", (iso(now()),))["c"]
        if upcoming == 0:
            seed_demo(db)
        _READY = True
    finally:
        db.close()


def get_db():
    if "db" not in g:
        g.db = DB()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def err(message, code=400):
    return jsonify(error=message), code


def valid_email(email):
    if not email or len(email) > 120 or email.count("@") != 1:
        return False
    local, domain = email.split("@")
    return bool(local) and domain == ALLOWED_DOMAIN and all(c.isalnum() or c in "._%+-" for c in local)


def code_hash(email, code):
    return hmac.new(SECRET.encode(), (email + ":" + code).encode(), hashlib.sha256).hexdigest()


def send_code(email, code):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    user = os.environ.get("SMTP_USER", "")
    msg = EmailMessage()
    msg["Subject"] = f"Your FLAME FOMO code: {code}"
    msg["From"] = os.environ.get("SMTP_FROM") or user
    msg["To"] = email
    msg.set_content(f"Your FLAME FOMO code is {code}.\nIt works for {OTP_MINUTES} minutes. If this was not you, ignore this email.")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        server = smtplib.SMTP(host, port, timeout=15)
        server.starttls()
    try:
        if user:
            server.login(user, os.environ.get("SMTP_PASS", ""))
        server.send_message(msg)
    finally:
        server.quit()
    return True


def verify_google(id_token):
    url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": id_token})
    with urllib.request.urlopen(url, timeout=8) as r:
        info = json.load(r)
    if not GOOGLE_CLIENT_ID or info.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("wrong app")
    if str(info.get("email_verified")).lower() != "true":
        raise ValueError("email not verified")
    email = str(info.get("email", "")).lower()
    if not valid_email(email) or str(info.get("hd", "")).lower() != ALLOWED_DOMAIN:
        raise ValueError("not a university account")
    return email, info.get("name")


def sign_in_user(db, email, name=None):
    role = "admin" if email in ADMIN_EMAILS else "student"
    nice = name or email.split("@")[0].replace(".", " ").replace("_", " ").title()
    db.run("INSERT INTO users(email, name, role, created_at) VALUES(?,?,?,?) ON CONFLICT(email) DO NOTHING", (email, nice, role, int(time.time())))
    user = db.one("SELECT * FROM users WHERE email=?", (email,))
    if role == "admin" and user["role"] != "admin":
        db.run("UPDATE users SET role='admin' WHERE id=?", (user["id"],))
        user["role"] = "admin"
    return user


def session_response(user):
    token = jwt.encode(
        {"sub": str(user["id"]), "exp": datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)},
        SECRET, algorithm="HS256",
    )
    resp = jsonify(user=public_user(user))
    secure = request.is_secure or request.headers.get("X-Forwarded-Proto") == "https"
    resp.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax", secure=secure, path="/")
    return resp


def public_user(u):
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"]}


def auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        token = request.cookies.get(COOKIE)
        user = None
        if token:
            try:
                data = jwt.decode(token, SECRET, algorithms=["HS256"])
                user = get_db().one("SELECT * FROM users WHERE id=?", (int(data["sub"]),))
            except (jwt.PyJWTError, ValueError, KeyError):
                user = None
        if not user:
            return err("Please sign in first.", 401)
        g.user = user
        return fn(*a, **kw)
    return wrapper


@app.before_request
def guard():
    if not request.path.startswith("/api/"):
        return None
    if not SECRET:
        return err("The server is missing its SECRET_KEY setting.", 500)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "fomo":
        return err("Request blocked.", 403)
    ensure_ready()
    return None


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return err(e.description, e.code)
    app.logger.exception(e)
    return err("Something went wrong on our side. Try again in a moment.", 500)


# --------------------------------------------------------------------------
# Events: loading, clash checks, shaping for the page
# --------------------------------------------------------------------------
def build(db, uid):
    """Load everything we need to describe events for one student."""
    def ids(table):
        return {r["event_id"] for r in db.run(f"SELECT event_id FROM {table} WHERE user_id=?", (uid,))}

    ctx = {
        "venues": {v["id"]: v for v in db.run("SELECT * FROM venues")},
        "taken": {r["event_id"]: r["c"] for r in db.run("SELECT event_id, COUNT(*) AS c FROM registrations GROUP BY event_id")},
        "reg": ids("registrations"),
        "going": ids("going"),
        "hidden": ids("hidden"),
        "busy": db.run("SELECT * FROM busy WHERE user_id=?", (uid,)),
        "events": db.run("SELECT * FROM events WHERE end_at >= ? ORDER BY start_at, id", (iso(now() - timedelta(hours=1)),)),
    }
    mine = ctx["reg"] | ctx["going"]
    ctx["plan"] = [(e["id"], e["title"], parse(e["start_at"]), parse(e["end_at"])) for e in ctx["events"] if e["id"] in mine]
    return ctx


def busy_on(ctx, date):
    """Classes and busy blocks that fall on a date (YYYY-MM-DD)."""
    wd = datetime.strptime(date, "%Y-%m-%d").weekday()
    return [b for b in ctx["busy"] if (b["weekday"] is not None and b["weekday"] == wd) or b["day"] == date]


def overlap(a1, a2, b1, b2):
    return a1 < b2 and b1 < a2


def shape(ev, ctx):
    s, e = parse(ev["start_at"]), parse(ev["end_at"])
    date = ev["start_at"][:10]
    day0 = datetime.strptime(date, "%Y-%m-%d")
    clash_busy = [
        b["title"] for b in busy_on(ctx, date)
        if overlap(s, e, day0 + timedelta(minutes=b["start_min"]), day0 + timedelta(minutes=b["end_min"]))
    ]
    clash_plan = [
        t for (i, t, ps, pe) in ctx["plan"]
        if i != ev["id"] and overlap(s, e, ps, pe)
    ]
    seats = ev["seats"]
    taken = ctx["taken"].get(ev["id"], 0)
    left = None if seats is None else max(seats - taken, 0)
    deadline = parse(ev["deadline"]) if ev["deadline"] else s
    venue = ctx["venues"].get(ev["venue_id"])
    return {
        "id": ev["id"], "title": ev["title"], "description": ev["description"] or "",
        "category": ev["category"], "source": ev["source"],
        "venue_id": ev["venue_id"], "venue": venue["name"] if venue else "To be announced",
        "date": date, "start_min": minutes(s), "end_min": minutes(e),
        "start_at": ev["start_at"], "end_at": ev["end_at"],
        "seats": seats, "taken": taken, "seats_left": left,
        "full": seats is not None and left == 0,
        "deadline": iso(deadline),
        "closes_in_h": round((deadline - now()).total_seconds() / 3600, 2),
        "ended": e < now(),
        "registered": ev["id"] in ctx["reg"], "going": ev["id"] in ctx["going"], "hidden": ev["id"] in ctx["hidden"],
        "clash_busy": clash_busy, "clash": clash_busy + clash_plan,
    }


def wanted_sources():
    raw = request.args.get("sources")
    if raw is None:
        return None
    return {s for s in raw.split(",") if s in SOURCES}


def only_sources(evs):
    want = wanted_sources()
    return evs if want is None else [e for e in evs if e["source"] in want]


def one_event(db, uid, eid):
    ctx = build(db, uid)
    for ev in ctx["events"]:
        if ev["id"] == eid:
            return shape(ev, ctx)
    ev = db.one("SELECT * FROM events WHERE id=?", (eid,))
    return shape(ev, ctx) if ev else None


def taste_for(db, uid):
    return {r["category"]: r for r in db.run("SELECT * FROM taste WHERE user_id=?", (uid,))}


def bump(db, uid, category, delta):
    row = db.one("SELECT learned FROM taste WHERE user_id=? AND category=?", (uid, category))
    value = max(-3.0, min(5.0, (row["learned"] if row else 0.0) + delta))
    db.run(
        "INSERT INTO taste(user_id, category, interest, learned) VALUES(?,?,0,?) "
        "ON CONFLICT(user_id, category) DO UPDATE SET learned=excluded.learned",
        (uid, category, value),
    )


# --------------------------------------------------------------------------
# Routes: config and sign in
# --------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return jsonify(ok=True, database="postgres" if IS_PG else "sqlite")


@app.get("/api/config")
def config():
    return jsonify(
        today=now().strftime("%Y-%m-%d"), now_min=minutes(now()), domain=ALLOWED_DOMAIN,
        google_client_id=GOOGLE_CLIENT_ID, categories=CATEGORIES, sources=SOURCES,
        persistent=IS_PG or not IS_VERCEL,
    )


@app.post("/api/auth/request-code")
def request_code():
    db = get_db()
    email = str((request.get_json(silent=True) or {}).get("email", "")).strip().lower()
    if not valid_email(email):
        return err(f"Use your university email, the one that ends in @{ALLOWED_DOMAIN}.")
    row = db.one("SELECT created_at FROM otps WHERE email=?", (email,))
    if row:
        wait = OTP_COOLDOWN - (int(time.time()) - row["created_at"])
        if wait > 0:
            return err(f"Wait {wait} seconds before asking for another code.", 429)
    code = f"{secrets.randbelow(10**6):06d}"
    t = int(time.time())
    db.run(
        "INSERT INTO otps(email, code_hash, expires_at, attempts, created_at) VALUES(?,?,?,0,?) "
        "ON CONFLICT(email) DO UPDATE SET code_hash=excluded.code_hash, expires_at=excluded.expires_at, attempts=0, created_at=excluded.created_at",
        (email, code_hash(email, code), t + OTP_MINUTES * 60, t),
    )
    try:
        sent = send_code(email, code)
    except Exception:
        app.logger.exception("email failed")
        db.run("DELETE FROM otps WHERE email=?", (email,))
        return err("We could not send the email. Try again in a minute.", 502)
    if not sent:
        if SHOW_DEV_CODE:
            return jsonify(ok=True, dev_code=code)
        db.run("DELETE FROM otps WHERE email=?", (email,))
        return err("Email sending is not set up yet. Ask the app owner to add the email settings.", 503)
    return jsonify(ok=True)


@app.post("/api/auth/verify-code")
def verify_code():
    db = get_db()
    d = request.get_json(silent=True) or {}
    email = str(d.get("email", "")).strip().lower()
    code = str(d.get("code", "")).strip()
    row = db.one("SELECT * FROM otps WHERE email=?", (email,))
    if not row or row["expires_at"] < time.time():
        return err("That code has expired. Ask for a new one.")
    if row["attempts"] >= OTP_MAX_TRIES:
        return err("Too many tries. Ask for a new code.", 429)
    if not hmac.compare_digest(row["code_hash"], code_hash(email, code)):
        db.run("UPDATE otps SET attempts=attempts+1 WHERE email=?", (email,))
        return err("That code is not right. Check the email and try again.")
    db.run("DELETE FROM otps WHERE email=?", (email,))
    return session_response(sign_in_user(db, email))


@app.post("/api/auth/google")
def google_login():
    token = str((request.get_json(silent=True) or {}).get("credential", ""))
    try:
        email, name = verify_google(token)
    except Exception:
        return err(f"Google sign-in only works with your @{ALLOWED_DOMAIN} account.", 401)
    return session_response(sign_in_user(get_db(), email, name))


@app.post("/api/auth/logout")
def logout():
    resp = jsonify(ok=True)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/me")
@auth
def me():
    return jsonify(user=public_user(g.user))


# --------------------------------------------------------------------------
# Routes: events
# --------------------------------------------------------------------------
@app.get("/api/events")
@auth
def list_events():
    ctx = build(get_db(), g.user["id"])
    evs = [shape(e, ctx) for e in ctx["events"]]
    return jsonify(events=only_sources([e for e in evs if not e["ended"]]))


@app.get("/api/venues")
@auth
def venues():
    return jsonify(venues=get_db().run("SELECT * FROM venues ORDER BY id"))


@app.get("/api/best")
@auth
def best():
    db = get_db()
    day = max(0, min(4, int(request.args.get("day", 0) or 0)))
    date = (now() + timedelta(days=day)).strftime("%Y-%m-%d")
    ctx = build(db, g.user["id"])
    taste = taste_for(db, g.user["id"])
    evs = only_sources([shape(e, ctx) for e in ctx["events"] if e["start_at"][:10] == date])
    pool = [e for e in evs if not e["hidden"] and not e["ended"]]

    def score(e):
        t = taste.get(e["category"])
        s = (2.0 * t["interest"] + t["learned"]) if t else 0.0
        if e["seats_left"] is not None and 0 < e["seats_left"] <= 10:
            s += 0.5
        if e["going"] or e["registered"]:
            s += 0.3
        return s

    def reason(e):
        t = taste.get(e["category"])
        if e["going"] or e["registered"]:
            return "You are already going to this one"
        if t and t["learned"] >= 2:
            return f"Because you keep saying yes to {e['category']}"
        if t and t["interest"]:
            return f"You said you like {e['category']}"
        if e["seats_left"] is not None and 0 < e["seats_left"] <= 10:
            return f"Only {e['seats_left']} seats left"
        return "Popular on campus today" if day == 0 else "A good one for this day"

    usable = [e for e in pool if not e["clash_busy"] and not (e["full"] and not e["registered"])]
    usable.sort(key=lambda e: (-score(e), e["start_at"]))
    picks = []
    for e in usable:
        if len(picks) == 5:
            break
        if all(not overlap(parse(e["start_at"]), parse(e["end_at"]), parse(p["start_at"]), parse(p["end_at"])) for p in picks):
            picks.append(e)
    picks.sort(key=lambda e: e["start_at"])
    picked = {p["id"] for p in picks}
    rest = sorted([e for e in pool if e["id"] not in picked], key=lambda e: e["start_at"])
    return jsonify(date=date, picks=[{"event": p, "reason": reason(p)} for p in picks], rest=rest)


@app.get("/api/time-sensitive")
@auth
def time_sensitive():
    ctx = build(get_db(), g.user["id"])
    evs = only_sources([shape(e, ctx) for e in ctx["events"]])
    evs = [
        e for e in evs
        if e["seats"] is not None and not e["ended"] and not e["hidden"]
        and e["closes_in_h"] > 0 and (not e["full"] or e["registered"])
    ]
    evs.sort(key=lambda e: e["deadline"])
    buckets = [("under6", "Closes in under 6 hours", 6), ("day", "Closes within 1 day", 24),
               ("two", "Closes within 2 days", 48), ("week", "Closes this week", 168), ("later", "Closes later", 10**9)]
    groups, used = [], set()
    for key, label, limit in buckets:
        items = [e for e in evs if e["id"] not in used and e["closes_in_h"] <= limit]
        used |= {e["id"] for e in items}
        if items:
            groups.append({"key": key, "label": label, "events": items})
    return jsonify(groups=groups)


@app.post("/api/events")
@auth
def create_event():
    if g.user["role"] not in ("admin", "organiser"):
        return err("Only organisers can add events.", 403)
    d = request.get_json(silent=True) or {}
    title = str(d.get("title", "")).strip()[:120]
    if not title:
        return err("Give the event a title.")
    category = d.get("category")
    if category not in CATEGORIES:
        return err("Pick a category from the list.")
    source = d.get("source") if d.get("source") in SOURCES else "Official"
    db = get_db()
    venue = db.one("SELECT id FROM venues WHERE id=?", (int(d.get("venue_id") or 0),))
    if not venue:
        return err("Pick a venue from the list.")
    try:
        date = str(d.get("date", ""))
        start = parse(f"{date}T{d.get('start')}")
        end = parse(f"{date}T{d.get('end')}")
        seats = int(d["seats"]) if str(d.get("seats") or "").strip() else None
        deadline = parse(str(d["deadline"])) if d.get("deadline") else None
    except (ValueError, TypeError):
        return err("Dates look like 2026-10-12 and times like 18:30. Check them and try again.")
    if end <= start:
        return err("The event must end after it starts.")
    if seats is not None and seats < 1:
        return err("Seats must be 1 or more, or leave it empty for no limit.")
    if seats is not None:
        deadline = deadline or start
        if deadline > start:
            return err("Sign-ups must close before the event starts.")
    else:
        deadline = None
    row = db.one(
        "INSERT INTO events(title, description, category, venue_id, start_at, end_at, seats, deadline, source, created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (title, str(d.get("description", ""))[:600], category, venue["id"], iso(start), iso(end), seats,
         iso(deadline) if deadline else None, source, g.user["id"]),
    )
    return jsonify(event=one_event(db, g.user["id"], row["id"]), message="Event added."), 201


def event_or_404(db, eid):
    ev = db.one("SELECT * FROM events WHERE id=?", (eid,))
    if not ev:
        return None
    return ev


@app.post("/api/events/<int:eid>/register")
@auth
def register(eid):
    db, uid = get_db(), g.user["id"]
    with db.tx():
        ev = db.one("SELECT * FROM events WHERE id=?" + (" FOR UPDATE" if IS_PG else ""), (eid,))
        if not ev:
            return err("That event is gone.", 404)
        if parse(ev["end_at"]) < now():
            return err("That event has already finished.")
        if db.one("SELECT 1 AS x FROM registrations WHERE event_id=? AND user_id=?", (eid, uid)):
            pass  # already registered, nothing to do
        else:
            if ev["deadline"] and parse(ev["deadline"]) < now():
                return err("Sign-up has closed for this event.")
            if ev["seats"] is not None:
                taken = db.one("SELECT COUNT(*) AS c FROM registrations WHERE event_id=?", (eid,))["c"]
                if taken >= ev["seats"]:
                    return err("This event is full. Try another one.", 409)
            db.run("INSERT INTO registrations(event_id, user_id, created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING", (eid, uid, int(time.time())))
            db.run("INSERT INTO going(event_id, user_id) VALUES(?,?) ON CONFLICT DO NOTHING", (eid, uid))
            db.run("DELETE FROM hidden WHERE event_id=? AND user_id=?", (eid, uid))
            bump(db, uid, ev["category"], 1)
    return jsonify(event=one_event(db, uid, eid), message="You are registered. See you there!")


@app.delete("/api/events/<int:eid>/register")
@auth
def unregister(eid):
    db, uid = get_db(), g.user["id"]
    if not event_or_404(db, eid):
        return err("That event is gone.", 404)
    db.run("DELETE FROM registrations WHERE event_id=? AND user_id=?", (eid, uid))
    return jsonify(event=one_event(db, uid, eid), message="Seat released for someone else.")


@app.post("/api/events/<int:eid>/going")
@auth
def mark_going(eid):
    db, uid = get_db(), g.user["id"]
    ev = event_or_404(db, eid)
    if not ev:
        return err("That event is gone.", 404)
    if not db.one("SELECT 1 AS x FROM going WHERE event_id=? AND user_id=?", (eid, uid)):
        db.run("INSERT INTO going(event_id, user_id) VALUES(?,?) ON CONFLICT DO NOTHING", (eid, uid))
        db.run("DELETE FROM hidden WHERE event_id=? AND user_id=?", (eid, uid))
        bump(db, uid, ev["category"], 1)
    return jsonify(event=one_event(db, uid, eid), message="Added to My plan.")


@app.delete("/api/events/<int:eid>/going")
@auth
def unmark_going(eid):
    db, uid = get_db(), g.user["id"]
    ev = event_or_404(db, eid)
    if not ev:
        return err("That event is gone.", 404)
    had = db.one("SELECT 1 AS x FROM going WHERE event_id=? AND user_id=?", (eid, uid))
    db.run("DELETE FROM going WHERE event_id=? AND user_id=?", (eid, uid))
    db.run("DELETE FROM registrations WHERE event_id=? AND user_id=?", (eid, uid))
    if had:
        bump(db, uid, ev["category"], -1)
    return jsonify(event=one_event(db, uid, eid), message="Removed from My plan.")


@app.post("/api/events/<int:eid>/hide")
@auth
def hide(eid):
    db, uid = get_db(), g.user["id"]
    ev = event_or_404(db, eid)
    if not ev:
        return err("That event is gone.", 404)
    if not db.one("SELECT 1 AS x FROM hidden WHERE event_id=? AND user_id=?", (eid, uid)):
        db.run("INSERT INTO hidden(event_id, user_id) VALUES(?,?) ON CONFLICT DO NOTHING", (eid, uid))
        bump(db, uid, ev["category"], -1)
    return jsonify(event=one_event(db, uid, eid), message="Skipped. We will show fewer like this.")


# --------------------------------------------------------------------------
# Routes: taste, plan, busy time, calendar
# --------------------------------------------------------------------------
@app.get("/api/taste")
@auth
def get_taste():
    t = taste_for(get_db(), g.user["id"])
    return jsonify(interests=[c for c in CATEGORIES if t.get(c) and t[c]["interest"]],
                   learned={c: t[c]["learned"] for c in t})


@app.post("/api/taste")
@auth
def set_taste():
    d = request.get_json(silent=True) or {}
    cat = d.get("category")
    if cat not in CATEGORIES:
        return err("Pick a category from the list.")
    db, uid = get_db(), g.user["id"]
    db.run(
        "INSERT INTO taste(user_id, category, interest, learned) VALUES(?,?,?,0) "
        "ON CONFLICT(user_id, category) DO UPDATE SET interest=excluded.interest",
        (uid, cat, 1 if d.get("on") else 0),
    )
    return get_taste()


@app.get("/api/plan")
@auth
def plan():
    ctx = build(get_db(), g.user["id"])
    mine = ctx["reg"] | ctx["going"]
    evs = [shape(e, ctx) for e in ctx["events"] if e["id"] in mine]
    return jsonify(events=[e for e in evs if not e["ended"]])


def to_minutes(text):
    h, m = str(text).split(":")
    v = int(h) * 60 + int(m)
    if not 0 <= v <= 1440:
        raise ValueError
    return v


@app.post("/api/busy")
@auth
def add_busy():
    d = request.get_json(silent=True) or {}
    try:
        day = str(d.get("day", ""))
        wd = datetime.strptime(day, "%Y-%m-%d").weekday()
        s, e = to_minutes(d.get("start")), to_minutes(d.get("end"))
    except (ValueError, TypeError):
        return err("Pick a day and times like 14:00 and 15:30.")
    if e <= s:
        return err("The end time must be after the start time.")
    title = str(d.get("title") or "Busy").strip()[:80] or "Busy"
    weekly = bool(d.get("weekly"))
    row = get_db().one(
        "INSERT INTO busy(user_id, title, kind, weekday, day, start_min, end_min) VALUES(?,?,?,?,?,?,?) RETURNING id",
        (g.user["id"], title, "class" if weekly else "busy", wd if weekly else None, None if weekly else day, s, e),
    )
    return jsonify(id=row["id"], message="Saved. Picks will now avoid this time."), 201


@app.delete("/api/busy/<int:bid>")
@auth
def delete_busy(bid):
    get_db().run("DELETE FROM busy WHERE id=? AND user_id=?", (bid, g.user["id"]))
    return jsonify(ok=True, message="Removed.")


@app.get("/api/calendar")
@auth
def calendar():
    db = get_db()
    day = max(0, min(14, int(request.args.get("day", 0) or 0)))
    start_of_day = now().replace(hour=0, minute=0) + timedelta(days=day)
    date = start_of_day.strftime("%Y-%m-%d")
    ctx = build(db, g.user["id"])
    blocks = [{"id": b["id"], "title": b["title"], "kind": b["kind"], "start_min": b["start_min"], "end_min": b["end_min"]}
              for b in busy_on(ctx, date)]
    mine = ctx["reg"] | ctx["going"]
    plan_events = [shape(e, ctx) for e in ctx["events"] if e["id"] in mine and e["start_at"][:10] == date]
    taken_spans = sorted([(b["start_min"], b["end_min"]) for b in blocks] + [(e["start_min"], e["end_min"]) for e in plan_events])
    lo = 8 * 60
    if day == 0:
        lo = max(lo, minutes(now()))
    hi = 22 * 60
    windows, cursor = [], lo
    for s, e in taken_spans:
        if s > cursor and s - cursor >= 30:
            windows.append((cursor, min(s, hi)))
        cursor = max(cursor, e)
    if hi - cursor >= 30:
        windows.append((cursor, hi))
    others = [shape(e, ctx) for e in ctx["events"] if e["start_at"][:10] == date and e["id"] not in mine]
    others = [e for e in others if not e["hidden"] and not e["ended"] and not (e["full"] and not e["registered"])]
    out = []
    for s, e in windows:
        fits = [x for x in others if x["start_min"] >= s and x["end_min"] <= e][:3]
        out.append({"start_min": s, "end_min": e, "events": fits})
    return jsonify(date=date, blocks=blocks, events=plan_events, free=out)


@app.post("/api/admin/seed-demo")
@auth
def admin_seed():
    if g.user["role"] != "admin":
        return err("Only admins can do this.", 403)
    return jsonify(added=seed_demo(get_db()), message="Sample events added.")


# --------------------------------------------------------------------------
# The website itself. (On Vercel the CDN may serve public/ first; this is the safety net.)
# --------------------------------------------------------------------------
PUBLIC = os.path.join(ROOT, "public")


@app.get("/")
def home():
    return send_from_directory(PUBLIC, "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", "5000")))
