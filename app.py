import os, sqlite3, secrets, smtplib, re
from datetime import datetime, timezone
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent
DB = os.getenv("DATABASE_PATH", str(ROOT / "pangasalama.sqlite3"))
app = FastAPI(title="PangaSalama")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48), https_only=os.getenv("SECURE_COOKIES", "false").lower() == "true", same_site="lax")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
env = Environment(loader=FileSystemLoader(str(ROOT / "static")), autoescape=select_autoescape(["html"]))

@contextmanager
def conn():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init_db():
    with conn() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS listings(
            id TEXT PRIMARY KEY,created_at TEXT NOT NULL,owner_name TEXT NOT NULL,owner_phone TEXT NOT NULL,
            provider_type TEXT NOT NULL,region TEXT NOT NULL,area TEXT NOT NULL,property_type TEXT NOT NULL,
            rent INTEGER NOT NULL,bedrooms INTEGER NOT NULL,advance_months INTEGER NOT NULL,
            extra_fee INTEGER NOT NULL DEFAULT 0,description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
            availability_checked_at TEXT);
        CREATE TABLE IF NOT EXISTS requests(
            id TEXT PRIMARY KEY,created_at TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,
            phone TEXT NOT NULL,region TEXT,area TEXT,budget INTEGER,listing_id TEXT,message TEXT,status TEXT NOT NULL DEFAULT 'new');
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,reference_id TEXT NOT NULL,
            recipient TEXT NOT NULL,status TEXT NOT NULL,details TEXT);""")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(listings)")}
        if "extra_fee" not in cols:
            c.execute("ALTER TABLE listings ADD COLUMN extra_fee INTEGER NOT NULL DEFAULT 0")
init_db()

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def ident(prefix): return prefix + "-" + secrets.token_hex(4).upper()
def clean(value, limit=500): return str(value or "").strip()[:limit]
def valid_phone(value): return bool(re.fullmatch(r"[+0-9()\s-]{7,22}", value))
def csrf_token(req):
    if not req.session.get("csrf"): req.session["csrf"] = secrets.token_urlsafe(25)
    return req.session["csrf"]
def check_csrf(req, supplied):
    if not secrets.compare_digest(req.session.get("csrf", ""), supplied or ""):
        raise HTTPException(403, "Invalid form token")
def render(req, page="home", **data):
    return HTMLResponse(env.get_template("index.html").render(
        page=page, csrf=csrf_token(req), whatsapp=os.getenv("BUSINESS_WHATSAPP_NUMBER", ""), **data))

def notify(reference, subject, message):
    recipients = list(dict.fromkeys(filter(None, [os.getenv("PRIMARY_NOTIFICATION_EMAIL", "ezekianyamhanga@gmail.com"),
        os.getenv("BACKUP_NOTIFICATION_EMAIL", "miriamnyamhanga237@gmail.com")])))
    for recipient in recipients:
        status, detail = "unconfigured", "SMTP not configured"
        if all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM_ADDRESS")):
            try:
                email = EmailMessage()
                email["Subject"] = "PangaSalama: " + subject
                email["From"] = os.environ["EMAIL_FROM_ADDRESS"]
                email["To"] = recipient
                email.set_content(message)
                with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=12) as server:
                    server.starttls()
                    server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                    server.send_message(email)
                status, detail = "accepted", "Accepted by SMTP server; inbox receipt unverified"
            except Exception as error:
                status, detail = "failed", type(error).__name__
        with conn() as c:
            c.execute("INSERT INTO notifications(created_at,reference_id,recipient,status,details) VALUES(?,?,?,?,?)",
                      (now(), reference, recipient, status, detail))

@app.get("/", response_class=HTMLResponse)
def home(req: Request, region: str = "", max_rent: int = 0):
    with conn() as c:
        rows = c.execute("SELECT id,region,area,property_type,rent,bedrooms,advance_months,extra_fee,description,availability_checked_at FROM listings WHERE status='published' ORDER BY created_at DESC").fetchall()
    listings = [dict(row) for row in rows if (not region or region.lower() in (row["region"]+" "+row["area"]).lower()) and (not max_rent or row["rent"] <= max_rent)]
    return render(req, listings=listings, region=region, max_rent=max_rent)

@app.post("/submit/listing")
def submit_listing(req: Request, csrf: str=Form(...), owner_name: str=Form(...), owner_phone: str=Form(...),
                   provider_type: str=Form(...), region: str=Form(...), area: str=Form(...),
                   property_type: str=Form(...), rent: int=Form(...), bedrooms: int=Form(1),
                   advance_months: int=Form(3), extra_fee: int=Form(0), description: str=Form(""), consent: str=Form("")):
    check_csrf(req, csrf)
    if (not valid_phone(owner_phone) or provider_type not in ("owner", "authorized_agent") or
        not 10000 <= rent <= 100000000 or not 0 <= bedrooms <= 30 or
        advance_months not in (3,4,5,6,12) or not 0 <= extra_fee <= 100000000 or
        consent != "yes" or not all([owner_name.strip(), region.strip(), area.strip(), property_type.strip()])):
        raise HTTPException(422, "Invalid property details")
    reference = ident("PSL")
    with conn() as c:
        c.execute("""INSERT INTO listings(id,created_at,owner_name,owner_phone,provider_type,region,area,property_type,
                   rent,bedrooms,advance_months,extra_fee,description) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (reference,now(),clean(owner_name,120),clean(owner_phone,22),provider_type,
                   clean(region,100),clean(area,100),clean(property_type,80),rent,bedrooms,advance_months,
                   extra_fee,clean(description,1600)))
    notify(reference, "Tangazo jipya", f"Tangazo {reference} limewasilishwa. Kagua kwenye admin.")
    return render(req, page="success", reference=reference, success_message="Tangazo limepokelewa na linasubiri ukaguzi.")

@app.post("/submit/request")
def submit_request(req: Request, csrf: str=Form(...), kind: str=Form(...), name: str=Form(...),
                   phone: str=Form(...), region: str=Form(""), area: str=Form(""),
                   budget: int=Form(0), listing_id: str=Form(""), message: str=Form(""), consent: str=Form("")):
    check_csrf(req, csrf)
    if kind not in ("home_search","viewing","contact","report") or not valid_phone(phone) or not name.strip() or consent != "yes" or not 0 <= budget <= 100000000:
        raise HTTPException(422, "Invalid request")
    if kind == "viewing":
        with conn() as c:
            if not c.execute("SELECT 1 FROM listings WHERE id=? AND status='published'", (listing_id,)).fetchone():
                raise HTTPException(404, "Property is unavailable")
    reference = ident("PSR")
    with conn() as c:
        c.execute("""INSERT INTO requests(id,created_at,kind,name,phone,region,area,budget,listing_id,message)
                     VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (reference,now(),kind,clean(name,120),clean(phone,22),clean(region,100),
                   clean(area,100),budget,clean(listing_id,30),clean(message,1600)))
    notify(reference, "Ombi jipya", f"Ombi {reference} ({kind}) limeingia. Kagua kwenye admin.")
    return render(req, page="success", reference=reference, success_message="Ombi lako limepokelewa.")

@app.get("/admin", response_class=HTMLResponse)
def admin(req: Request):
    if not req.session.get("admin"): return render(req, page="login")
    with conn() as c:
        listings = [dict(row) for row in c.execute("SELECT * FROM listings ORDER BY created_at DESC LIMIT 100")]
        requests = [dict(row) for row in c.execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT 100")]
        notices = [dict(row) for row in c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50")]
    return render(req, page="admin", admin_listings=listings, admin_requests=requests, notices=notices)

@app.post("/admin/login")
def login(req: Request, csrf: str=Form(...), password: str=Form(...)):
    check_csrf(req, csrf)
    secret = os.getenv("ADMIN_PASSWORD", "")
    if not secret or not secrets.compare_digest(secret, password): raise HTTPException(401, "Login failed")
    req.session["admin"] = True
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/status")
def update_status(req: Request, csrf: str=Form(...), listing_id: str=Form(...), status: str=Form(...)):
    check_csrf(req, csrf)
    if not req.session.get("admin"): raise HTTPException(403)
    if status not in ("pending","published","rejected","unavailable"): raise HTTPException(422)
    with conn() as c:
        c.execute("UPDATE listings SET status=?,availability_checked_at=CASE WHEN ?='published' THEN ? ELSE availability_checked_at END WHERE id=?",
                  (status,status,now(),listing_id))
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/listing-pricing")
def update_pricing(req: Request, csrf: str=Form(...), listing_id: str=Form(...), rent: int=Form(...),
                   advance_months: int=Form(...), extra_fee: int=Form(0)):
    check_csrf(req, csrf)
    if not req.session.get("admin"): raise HTTPException(403)
    if advance_months not in (3,4,5,6,12) or not 10000 <= rent <= 100000000 or not 0 <= extra_fee <= 100000000:
        raise HTTPException(422)
    with conn() as c:
        c.execute("UPDATE listings SET rent=?,advance_months=?,extra_fee=? WHERE id=?",
                  (rent,advance_months,extra_fee,listing_id))
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/logout")
def logout(req: Request, csrf: str=Form(...)):
    check_csrf(req, csrf)
    req.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/health")
def health(): return {"status":"ok"}
