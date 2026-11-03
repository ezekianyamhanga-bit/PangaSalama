import hashlib, hmac, json, logging, os, sqlite3, secrets, smtplib, re, time
from datetime import datetime, timezone
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Mapping
from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader, select_autoescape
from storage import InvalidImage, delete_image, process_image, read_image, store_image

ROOT = Path(__file__).resolve().parent
DB = os.getenv("DATABASE_PATH", str(ROOT / "pangasalama.sqlite3"))
ADMIN_SESSION_IDLE_SECONDS = 30 * 60
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
MAX_LISTING_REQUEST_BYTES = 20 * 1024 * 1024 + 256 * 1024
MAX_REPLACEMENT_REQUEST_BYTES = 5 * 1024 * 1024 + 256 * 1024
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
logger = logging.getLogger("pangasalama")

def validate_settings(settings: Mapping[str, str]) -> None:
    environment = settings.get("APP_ENV", "").strip().lower()
    if environment not in ("development", "test", "production"):
        raise RuntimeError("APP_ENV must be explicitly set to development, test, or production")
    if environment != "production":
        return

    admin_password = settings.get("ADMIN_PASSWORD", "")
    session_secret = settings.get("SESSION_SECRET", "")
    if len(admin_password) < 32 or len(set(admin_password)) < 16 or admin_password.upper().startswith("CHANGE_TO_"):
        raise RuntimeError("Production requires a unique ADMIN_PASSWORD of at least 32 characters")
    if len(session_secret) < 43 or len(set(session_secret)) < 16 or session_secret.upper().startswith("CHANGE_TO_"):
        raise RuntimeError("Production requires a random SESSION_SECRET of at least 43 characters")
    if secrets.compare_digest(admin_password, session_secret):
        raise RuntimeError("ADMIN_PASSWORD and SESSION_SECRET must be different")
    if settings.get("SECURE_COOKIES", "").strip().lower() != "true":
        raise RuntimeError("Production requires SECURE_COOKIES=true behind HTTPS")
    database_path = settings.get("DATABASE_PATH", "")
    if not database_path or not Path(database_path).is_absolute():
        raise RuntimeError("Production requires an absolute DATABASE_PATH on persistent storage")
    if settings.get("IMAGE_STORAGE_BACKEND", "").strip().lower() != "s3":
        raise RuntimeError("Production requires IMAGE_STORAGE_BACKEND=s3")
    if not settings.get("S3_BUCKET", "").strip() or not settings.get("S3_REGION", "").strip():
        raise RuntimeError("Production requires S3_BUCKET and S3_REGION")

validate_settings(os.environ)
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48)
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "false").strip().lower() == "true"
DEFAULT_PRIMARY_NOTIFICATION_EMAIL = "ezekianyamhanga@gmail.com"
DEFAULT_BACKUP_NOTIFICATION_EMAIL = "miriamnyamhanga237@gmail.com"
DEFAULT_BUSINESS_WHATSAPP_NUMBER = "255788525327"


class UploadLimitExceeded(Exception):
    pass


class UploadBodyLimitMiddleware:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.application(scope, receive, send)
            return
        limits = {
            "/submit/listing": MAX_LISTING_REQUEST_BYTES,
            "/admin/photo-replace": MAX_REPLACEMENT_REQUEST_BYTES,
        }
        limit = limits.get(scope["path"])
        if limit is None:
            await self.application(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                await Response("Invalid Content-Length", status_code=400)(scope, receive, send)
                return
            if declared_size > limit:
                await Response("Upload is too large", status_code=413)(scope, receive, send)
                return

        received_size = 0

        async def limited_receive():
            nonlocal received_size
            message = await receive()
            if message["type"] == "http.request":
                received_size += len(message.get("body", b""))
                if received_size > limit:
                    raise UploadLimitExceeded
            return message

        try:
            await self.application(scope, limited_receive, send)
        except UploadLimitExceeded:
            await Response("Upload is too large", status_code=413)(scope, receive, send)


app = FastAPI(title="PangaSalama")
app.add_middleware(UploadBodyLimitMiddleware)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=SESSION_MAX_AGE_SECONDS,
                   https_only=SECURE_COOKIES, same_site="lax")
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
env = Environment(loader=FileSystemLoader(str(ROOT / "static")), autoescape=select_autoescape(["html"]))

@contextmanager
def conn(database=DB):
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(database, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()

def init_db(database=DB):
    with conn(database) as c:
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
            recipient TEXT NOT NULL,status TEXT NOT NULL,details TEXT);
        CREATE TABLE IF NOT EXISTS admin_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT NOT NULL,actor TEXT NOT NULL,
            action TEXT NOT NULL,target_id TEXT NOT NULL,details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS public_rate_limits(
            bucket TEXT PRIMARY KEY,window_start INTEGER NOT NULL,attempts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_login_attempts(
            bucket TEXT PRIMARY KEY,window_start INTEGER NOT NULL,attempts INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_sessions(
            session_hash TEXT PRIMARY KEY,created_at INTEGER NOT NULL,
            last_activity INTEGER NOT NULL,expires_at INTEGER NOT NULL);""")
        c.execute("BEGIN IMMEDIATE")
        c.execute("""CREATE TABLE IF NOT EXISTS listing_photos(
            id TEXT PRIMARY KEY,listing_id TEXT NOT NULL,storage_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',is_cover INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,FOREIGN KEY(listing_id) REFERENCES listings(id))""")
        photo_columns = {r["name"] for r in c.execute("PRAGMA table_info(listing_photos)")}
        if "thumbnail_key" not in photo_columns:
            c.execute("ALTER TABLE listing_photos ADD COLUMN thumbnail_key TEXT NOT NULL DEFAULT ''")
        notice_columns = {r["name"] for r in c.execute("PRAGMA table_info(notifications)")}
        for column, definition in {
            "subject": "TEXT NOT NULL DEFAULT ''",
            "message": "TEXT NOT NULL DEFAULT ''",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in notice_columns:
                c.execute(f"ALTER TABLE notifications ADD COLUMN {column} {definition}")
        listing_columns = {
            "extra_fee": "INTEGER NOT NULL DEFAULT 0",
            "title": "TEXT NOT NULL DEFAULT ''",
            "district": "TEXT NOT NULL DEFAULT ''",
            "location": "TEXT NOT NULL DEFAULT ''",
            "bathrooms": "INTEGER NOT NULL DEFAULT 0",
            "availability": "TEXT NOT NULL DEFAULT 'available'",
            "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
            "verification_note": "TEXT NOT NULL DEFAULT ''",
            "verification_reviewed_at": "TEXT",
        }
        cols = {r["name"] for r in c.execute("PRAGMA table_info(listings)")}
        for column, definition in listing_columns.items():
            if column not in cols:
                c.execute(f"ALTER TABLE listings ADD COLUMN {column} {definition}")
        request_columns = {r["name"] for r in c.execute("PRAGMA table_info(requests)")}
        if "access_hash" not in request_columns:
            c.execute("ALTER TABLE requests ADD COLUMN access_hash TEXT")
        if "consent_at" not in request_columns:
            c.execute("ALTER TABLE requests ADD COLUMN consent_at TEXT")
init_db()

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def log_event(event, **fields):
    logger.info(json.dumps({"timestamp": now(), "event": event, **fields}, sort_keys=True))

def ident(prefix): return prefix + "-" + secrets.token_hex(4).upper()
def clean(value, limit=500): return str(value or "").strip()[:limit]
def valid_phone(value): return bool(re.fullmatch(r"[+0-9()\s-]{7,22}", value))
def request_bucket(req, scope):
    address = req.client.host if req.client else "unknown"
    digest = hmac.new(SESSION_SECRET.encode(), address.encode(), hashlib.sha256).hexdigest()
    return f"{scope}:{digest}"

def enforce_rate_limit(req, scope, limit, window_seconds):
    current_time = int(time.time())
    window_start = current_time // window_seconds * window_seconds
    bucket = request_bucket(req, scope)
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""INSERT INTO public_rate_limits(bucket,window_start,attempts) VALUES(?,?,1)
                     ON CONFLICT(bucket) DO UPDATE SET
                     attempts=CASE WHEN public_rate_limits.window_start=excluded.window_start
                                   THEN public_rate_limits.attempts+1 ELSE 1 END,
                     window_start=excluded.window_start""", (bucket, window_start))
        attempts = c.execute("SELECT attempts FROM public_rate_limits WHERE bucket=?", (bucket,)).fetchone()["attempts"]
        c.execute("DELETE FROM public_rate_limits WHERE window_start < ?", (current_time - 86400,))
    if attempts > limit:
        retry_after = max(1, window_start + window_seconds - current_time)
        log_event("public_rate_limited", scope=scope)
        raise HTTPException(429, "Too many submissions. Please try again later.",
                            headers={"Retry-After": str(retry_after)})

def reserve_login_attempt(req):
    bucket = request_bucket(req, "admin-login")
    current_time = int(time.time())
    window_start = current_time // 900 * 900
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM admin_login_attempts WHERE window_start < ?", (current_time - 86400,))
        row = c.execute("SELECT window_start,attempts FROM admin_login_attempts WHERE bucket=?", (bucket,)).fetchone()
        if row and current_time - row["window_start"] < 900 and row["attempts"] >= 5:
            retry_after = max(1, row["window_start"] + 900 - current_time)
            log_event("admin_login_throttled")
            raise HTTPException(429, "Too many login attempts. Please try again later.",
                                headers={"Retry-After": str(retry_after)})
        if not row or current_time - row["window_start"] >= 900:
            c.execute("""INSERT INTO admin_login_attempts(bucket,window_start,attempts) VALUES(?,?,1)
                         ON CONFLICT(bucket) DO UPDATE SET window_start=excluded.window_start,attempts=1""",
                      (bucket, window_start))
        else:
            c.execute("UPDATE admin_login_attempts SET attempts=attempts+1 WHERE bucket=?", (bucket,))
    return bucket

def clear_login_attempts(bucket):
    with conn() as c:
        c.execute("DELETE FROM admin_login_attempts WHERE bucket=?", (bucket,))

def admin_session_hash(session_id):
    return hmac.new(SESSION_SECRET.encode(), ("admin-session:" + session_id).encode(),
                    hashlib.sha256).hexdigest()

def create_admin_session():
    session_id = secrets.token_urlsafe(32)
    current_time = int(time.time())
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (current_time,))
        c.execute("INSERT INTO admin_sessions(session_hash,created_at,last_activity,expires_at) VALUES(?,?,?,?)",
                  (admin_session_hash(session_id), current_time, current_time,
                   current_time + SESSION_MAX_AGE_SECONDS))
    return session_id

def is_admin(req):
    session_id = req.session.get("admin_session_id")
    if not req.session.get("admin_authenticated") or not isinstance(session_id, str):
        return False

    current_time = int(time.time())
    valid = False
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT last_activity,expires_at FROM admin_sessions WHERE session_hash=?",
                        (admin_session_hash(session_id),)).fetchone()
        if (row and row["expires_at"] > current_time and current_time >= row["last_activity"] and
                current_time - row["last_activity"] <= ADMIN_SESSION_IDLE_SECONDS):
            c.execute("UPDATE admin_sessions SET last_activity=? WHERE session_hash=?",
                      (current_time, admin_session_hash(session_id)))
            valid = True
        elif row:
            c.execute("DELETE FROM admin_sessions WHERE session_hash=?",
                      (admin_session_hash(session_id),))
    if not valid:
        req.session.clear()
        return False
    return True

def add_audit_record(c, action, target_id, details):
    c.execute("INSERT INTO admin_audit(created_at,actor,action,target_id,details) VALUES(?,?,?,?,?)",
              (now(), "admin", action, target_id, json.dumps(details, sort_keys=True)))

def csrf_token(req):
    if not req.session.get("csrf"): req.session["csrf"] = secrets.token_urlsafe(25)
    return req.session["csrf"]
def check_csrf(req, supplied):
    expected = req.session.get("csrf", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(403, "Invalid form token")
def render(req, page="home", **data):
    headers = {"Cache-Control": "no-store"} if page in ("admin", "success", "track") else None
    return HTMLResponse(env.get_template("index.html").render(
        page=page, csrf=csrf_token(req),
        whatsapp=os.getenv("BUSINESS_WHATSAPP_NUMBER", DEFAULT_BUSINESS_WHATSAPP_NUMBER),
        **data), headers=headers)

def notify(reference, subject, message):
    recipients = list(dict.fromkeys(filter(None, [
        os.getenv("PRIMARY_NOTIFICATION_EMAIL", DEFAULT_PRIMARY_NOTIFICATION_EMAIL),
        os.getenv("BACKUP_NOTIFICATION_EMAIL", DEFAULT_BACKUP_NOTIFICATION_EMAIL)])))
    if not recipients:
        with conn() as c:
            c.execute("""INSERT INTO notifications
                (created_at,reference_id,recipient,status,details,subject,message,attempts)
                VALUES(?,?,?,?,?,?,?,0)""",
                      (now(), reference, "", "unconfigured", "No notification recipients configured",
                       subject, message))
        return
    for recipient in recipients:
        with conn() as c:
            c.execute("""INSERT INTO notifications
                (created_at,reference_id,recipient,status,details,subject,message,attempts)
                VALUES(?,?,?,'pending','Queued for delivery',?,?,0)""",
                      (now(), reference, recipient, subject, message))
            notification_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        deliver_notification(notification_id)

def deliver_notification(notification_id):
    with conn() as c:
        row = c.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Notification not found")
    if not row["recipient"]:
        return
    status, detail = "unconfigured", "SMTP not configured"
    if all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM_ADDRESS")):
        try:
            email = EmailMessage()
            email["Subject"] = "PangaSalama: " + row["subject"]
            email["From"] = os.environ["EMAIL_FROM_ADDRESS"]
            email["To"] = row["recipient"]
            email.set_content(row["message"])
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587")), timeout=12) as server:
                server.starttls()
                server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                server.send_message(email)
            status, detail = "accepted", "Accepted by SMTP server; inbox receipt unverified"
        except (OSError, smtplib.SMTPException, ValueError) as error:
            status, detail = "failed", type(error).__name__
    with conn() as c:
        c.execute("""UPDATE notifications SET status=?,details=?,attempts=attempts+1
                     WHERE id=?""", (status, detail, notification_id))

@app.get("/", response_class=HTMLResponse)
def home(req: Request, region: str = "", max_rent: int = 0):
    with conn() as c:
        rows = c.execute("""SELECT id,title,region,district,area,location,property_type,rent,bedrooms,
            bathrooms,advance_months,extra_fee,description,availability,verification_status,
            availability_checked_at FROM listings WHERE status='published' ORDER BY created_at DESC""").fetchall()
    listings = [dict(row) for row in rows if (not region or region.lower() in
                (row["region"]+" "+row["district"]+" "+row["area"]+" "+row["location"]).lower())
                and (not max_rent or row["rent"] <= max_rent)]
    with conn() as c:
        for listing in listings:
            photo = c.execute("""SELECT id FROM listing_photos
                WHERE listing_id=? AND status='approved'
                ORDER BY is_cover DESC,created_at LIMIT 1""", (listing["id"],)).fetchone()
            listing["cover_photo"] = photo["id"] if photo else None
    return render(req, listings=listings, region=region, max_rent=max_rent)

@app.get("/property/{listing_id}", response_class=HTMLResponse)
def property_detail(req: Request, listing_id: str):
    with conn() as c:
        row = c.execute("SELECT * FROM listings WHERE id=? AND status='published'",
                        (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Property is unavailable")
        listing = dict(row)
        listing["photos"] = [dict(photo) for photo in c.execute(
            """SELECT id,is_cover FROM listing_photos
               WHERE listing_id=? AND status='approved' ORDER BY is_cover DESC,created_at""",
            (listing_id,)).fetchall()]
    return render(req, page="property", listing=listing)

@app.get("/media/{photo_id}")
def property_photo(photo_id: str, req: Request, variant: str = "full"):
    if variant not in ("full", "thumb"):
        raise HTTPException(422, "Invalid image variant")
    with conn() as c:
        photo = c.execute("""SELECT p.storage_key,p.thumbnail_key,l.status AS listing_status,p.status AS photo_status
            FROM listing_photos p JOIN listings l ON l.id=p.listing_id WHERE p.id=?""",
                          (photo_id,)).fetchone()
    if not photo:
        raise HTTPException(404, "Image not found")
    if not (photo["listing_status"] == "published" and photo["photo_status"] == "approved"):
        if not is_admin(req):
            raise HTTPException(404, "Image not found")
    try:
        image_key = photo["thumbnail_key"] if variant == "thumb" else photo["storage_key"]
        if not image_key:
            image_key = photo["storage_key"]
        data = read_image(image_key)
    except FileNotFoundError as error:
        raise HTTPException(404, "Image not found") from error
    return Response(data, media_type="image/webp",
                    headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

def store_photo(upload: UploadFile) -> tuple[str, str]:
    full_image, thumbnail = process_image(upload)
    full_key = store_image(full_image)
    try:
        thumbnail_key = store_image(thumbnail)
    except Exception:
        delete_image(full_key)
        raise
    return full_key, thumbnail_key

@app.post("/submit/listing")
def submit_listing(req: Request, csrf: str=Form(...), owner_name: str=Form(...), owner_phone: str=Form(...),
                   provider_type: str=Form(...), region: str=Form(...), area: str=Form(...),
                   property_type: str=Form(...), rent: int=Form(...), bedrooms: int=Form(1),
                   advance_months: int=Form(3), extra_fee: int=Form(0), description: str=Form(""), consent: str=Form(""),
                   title: str=Form(""), district: str=Form(""), location: str=Form(""),
                   bathrooms: int=Form(0), photos: list[UploadFile] = File(default=[])):
    check_csrf(req, csrf)
    enforce_rate_limit(req, "listing", 5, 3600)
    if (not valid_phone(owner_phone) or provider_type not in ("owner", "authorized_agent") or
        not 10000 <= rent <= 100000000 or not 0 <= bedrooms <= 30 or
        advance_months not in (3,4,5,6,12) or not 0 <= extra_fee <= 100000000 or not 0 <= bathrooms <= 30 or
        len(photos) > 6 or
        consent != "yes" or not all([owner_name.strip(), region.strip(), area.strip(), property_type.strip()])):
        raise HTTPException(422, "Invalid property details")
    processed_photos = []
    try:
        total_size = 0
        for photo in photos:
            photo.file.seek(0, os.SEEK_END)
            total_size += photo.file.tell()
            photo.file.seek(0)
            if total_size > 20 * 1024 * 1024:
                raise InvalidImage("Combined image upload must not exceed 20 MB")
            processed_photos.append(store_photo(photo))
    except InvalidImage as error:
        for keys in processed_photos:
            for key in keys:
                delete_image(key)
        raise HTTPException(422, str(error)) from error
    except Exception:
        for keys in processed_photos:
            for key in keys:
                delete_image(key)
        raise
    reference = ident("PSL")
    try:
        with conn() as c:
            c.execute("""INSERT INTO listings(id,created_at,owner_name,owner_phone,provider_type,region,area,property_type,
                       rent,bedrooms,advance_months,extra_fee,description,title,district,location,bathrooms)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (reference,now(),clean(owner_name,120),clean(owner_phone,22),provider_type,
                       clean(region,100),clean(area,100),clean(property_type,80),rent,bedrooms,advance_months,
                       extra_fee,clean(description,1600),clean(title,150) or clean(property_type,80),
                       clean(district,100),clean(location,240),bathrooms))
            for index, (key, thumbnail_key) in enumerate(processed_photos):
                c.execute("""INSERT INTO listing_photos
                             (id,listing_id,storage_key,thumbnail_key,status,is_cover,created_at)
                             VALUES(?,?,?,?,'pending',?,?)""",
                          (ident("PSP"), reference, key, thumbnail_key, int(index == 0), now()))
    except Exception:
        for keys in processed_photos:
            for key in keys:
                delete_image(key)
        raise
    notify(reference, "Tangazo jipya", f"Tangazo {reference} limewasilishwa. Kagua kwenye admin.")
    return render(req, page="success", reference=reference,
                  success_message="Tangazo limepokelewa na linasubiri ukaguzi.")

@app.post("/submit/request")
def submit_request(req: Request, csrf: str=Form(...), kind: str=Form(...), name: str=Form(...),
                   phone: str=Form(...), region: str=Form(""), area: str=Form(""),
                   budget: int=Form(0), listing_id: str=Form(""), message: str=Form(""), consent: str=Form("")):
    check_csrf(req, csrf)
    enforce_rate_limit(req, "request", 10, 600)
    if kind not in ("home_search","viewing","contact","report","privacy_delete") or not valid_phone(phone) or not name.strip() or consent != "yes" or not 0 <= budget <= 100000000:
        raise HTTPException(422, "Invalid request")
    if kind == "viewing":
        with conn() as c:
            if not c.execute("SELECT 1 FROM listings WHERE id=? AND status='published'", (listing_id,)).fetchone():
                raise HTTPException(404, "Property is unavailable")
    reference = ident("PSR")
    tracking_token = secrets.token_urlsafe(24)
    with conn() as c:
        c.execute("""INSERT INTO requests
            (id,created_at,kind,name,phone,region,area,budget,listing_id,message,access_hash,consent_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (reference,now(),kind,clean(name,120),clean(phone,22),clean(region,100),
                   clean(area,100),budget,clean(listing_id,30),clean(message,1600),
                   hashlib.sha256(tracking_token.encode()).hexdigest(), now()))
    notify(reference, "Ombi jipya", f"Ombi {reference} ({kind}) limeingia. Kagua kwenye admin.")
    return render(req, page="success", reference=reference, tracking_token=tracking_token,
                  success_message="Ombi lako limepokelewa.")

@app.post("/track", response_class=HTMLResponse)
def track_request(req: Request, csrf: str=Form(...), reference: str=Form(...), tracking_token: str=Form(...)):
    check_csrf(req, csrf)
    if not re.fullmatch(r"PSR-[A-F0-9]{8}", reference) or not 20 <= len(tracking_token) <= 100:
        raise HTTPException(404, "Request not found")
    token_hash = hashlib.sha256(tracking_token.encode()).hexdigest()
    with conn() as c:
        row = c.execute("SELECT id,kind,status,created_at FROM requests WHERE id=? AND access_hash=?",
                        (reference, token_hash)).fetchone()
    if not row:
        raise HTTPException(404, "Request not found")
    return render(req, page="track", tracked_request=dict(row))

@app.get("/admin", response_class=HTMLResponse)
def admin(req: Request):
    if not is_admin(req): return render(req, page="login")
    with conn() as c:
        listings = [dict(row) for row in c.execute("SELECT * FROM listings ORDER BY created_at DESC LIMIT 100")]
        requests = [dict(row) for row in c.execute("SELECT * FROM requests ORDER BY created_at DESC LIMIT 100")]
        notices = [dict(row) for row in c.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50")]
        audit_records = [dict(row) for row in c.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT 50")]
        photos = [dict(row) for row in c.execute("""SELECT p.*,l.title,l.status AS listing_status
            FROM listing_photos p JOIN listings l ON l.id=p.listing_id
            ORDER BY p.created_at DESC LIMIT 100""")]
    return render(req, page="admin", admin_listings=listings, admin_requests=requests, notices=notices,
                  audit_records=audit_records, admin_photos=photos)

@app.post("/admin/login")
def login(req: Request, csrf: str=Form(...), password: str=Form(...)):
    check_csrf(req, csrf)
    bucket = reserve_login_attempt(req)
    secret = os.getenv("ADMIN_PASSWORD", "")
    if not secret or not secrets.compare_digest(secret, password):
        log_event("admin_login_failed")
        raise HTTPException(401, "Login failed")
    clear_login_attempts(bucket)
    req.session.clear()
    req.session["admin_authenticated"] = True
    req.session["admin_session_id"] = create_admin_session()
    csrf_token(req)
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/status")
def update_status(req: Request, csrf: str=Form(...), listing_id: str=Form(...), status: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req): raise HTTPException(403)
    if status not in ("pending","published","rejected","unavailable"): raise HTTPException(422)
    with conn() as c:
        current = c.execute("SELECT status FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Listing not found")
        c.execute("UPDATE listings SET status=?,availability_checked_at=CASE WHEN ?='published' THEN ? ELSE availability_checked_at END WHERE id=?",
                  (status,status,now(),listing_id))
        add_audit_record(c, "listing_status_changed", listing_id,
                         {"from": current["status"], "to": status})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/listing-pricing")
def update_pricing(req: Request, csrf: str=Form(...), listing_id: str=Form(...), rent: int=Form(...),
                   advance_months: int=Form(...), extra_fee: int=Form(0)):
    check_csrf(req, csrf)
    if not is_admin(req): raise HTTPException(403)
    if advance_months not in (3,4,5,6,12) or not 10000 <= rent <= 100000000 or not 0 <= extra_fee <= 100000000:
        raise HTTPException(422)
    with conn() as c:
        current = c.execute("SELECT rent,advance_months,extra_fee FROM listings WHERE id=?", (listing_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Listing not found")
        c.execute("UPDATE listings SET rent=?,advance_months=?,extra_fee=? WHERE id=?",
                  (rent,advance_months,extra_fee,listing_id))
        add_audit_record(c, "listing_pricing_changed", listing_id,
                         {"from": dict(current),
                          "to": {"rent": rent, "advance_months": advance_months, "extra_fee": extra_fee}})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/request-status")
def update_request_status(req: Request, csrf: str=Form(...), request_id: str=Form(...),
                          status: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    if status not in ("new", "contacted", "scheduled", "closed"):
        raise HTTPException(422, "Invalid request status")
    with conn() as c:
        current = c.execute("SELECT status FROM requests WHERE id=?", (request_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Request not found")
        c.execute("UPDATE requests SET status=? WHERE id=?", (status, request_id))
        add_audit_record(c, "request_status_changed", request_id,
                         {"from": current["status"], "to": status})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/listing-verification")
def update_listing_verification(req: Request, csrf: str=Form(...), listing_id: str=Form(...),
                                status: str=Form(...), note: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    if status not in ("unverified", "identity_reviewed", "authority_documents_reviewed") or not note.strip():
        raise HTTPException(422, "A verification status and review note are required")
    with conn() as c:
        current = c.execute(
            "SELECT verification_status,verification_note FROM listings WHERE id=?",
            (listing_id,)).fetchone()
        if not current:
            raise HTTPException(404, "Listing not found")
        c.execute("""UPDATE listings SET verification_status=?,verification_note=?,
                     verification_reviewed_at=? WHERE id=?""",
                  (status, clean(note, 500), now(), listing_id))
        add_audit_record(c, "listing_verification_reviewed", listing_id,
                         {"from": current["verification_status"], "to": status,
                          "note": clean(note, 500)})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/photo-status")
def update_photo_status(req: Request, csrf: str=Form(...), photo_id: str=Form(...),
                        status: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    if status not in ("pending", "approved", "rejected"):
        raise HTTPException(422, "Invalid photo status")
    with conn() as c:
        photo = c.execute("SELECT listing_id,status FROM listing_photos WHERE id=?",
                          (photo_id,)).fetchone()
        if not photo:
            raise HTTPException(404, "Image not found")
        c.execute("UPDATE listing_photos SET status=? WHERE id=?", (status, photo_id))
        add_audit_record(c, "listing_photo_status_changed", photo_id,
                         {"listing_id": photo["listing_id"], "from": photo["status"], "to": status})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/photo-cover")
def set_photo_cover(req: Request, csrf: str=Form(...), photo_id: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    with conn() as c:
        photo = c.execute("SELECT listing_id,status FROM listing_photos WHERE id=?",
                          (photo_id,)).fetchone()
        if not photo:
            raise HTTPException(404, "Image not found")
        if photo["status"] != "approved":
            raise HTTPException(422, "Only an approved image can be the cover")
        c.execute("UPDATE listing_photos SET is_cover=0 WHERE listing_id=?",
                  (photo["listing_id"],))
        c.execute("UPDATE listing_photos SET is_cover=1 WHERE id=?", (photo_id,))
        add_audit_record(c, "listing_photo_cover_changed", photo_id,
                         {"listing_id": photo["listing_id"]})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/photo-replace")
def replace_photo(req: Request, csrf: str=Form(...), photo_id: str=Form(...),
                  image: UploadFile=File(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    try:
        new_key, new_thumbnail_key = store_photo(image)
    except InvalidImage as error:
        raise HTTPException(422, str(error)) from error
    try:
        with conn() as c:
            photo = c.execute("SELECT storage_key,thumbnail_key,listing_id FROM listing_photos WHERE id=?",
                              (photo_id,)).fetchone()
            if not photo:
                raise HTTPException(404, "Image not found")
            old_key, old_thumbnail_key, listing_id = (
                photo["storage_key"], photo["thumbnail_key"], photo["listing_id"])
            c.execute("""UPDATE listing_photos SET storage_key=?,thumbnail_key=?,status='pending',
                         created_at=? WHERE id=?""",
                      (new_key, new_thumbnail_key, now(), photo_id))
            add_audit_record(c, "listing_photo_replaced", photo_id, {"listing_id": listing_id})
    except Exception:
        delete_image(new_key)
        delete_image(new_thumbnail_key)
        raise
    delete_image(old_key)
    if old_thumbnail_key:
        delete_image(old_thumbnail_key)
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/photo-delete")
def remove_photo(req: Request, csrf: str=Form(...), photo_id: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    with conn() as c:
        photo = c.execute("SELECT storage_key,thumbnail_key,listing_id FROM listing_photos WHERE id=?",
                          (photo_id,)).fetchone()
        if not photo:
            raise HTTPException(404, "Image not found")
        keys = (photo["storage_key"], photo["thumbnail_key"])
        listing_id = photo["listing_id"]
        c.execute("DELETE FROM listing_photos WHERE id=?", (photo_id,))
        add_audit_record(c, "listing_photo_deleted", photo_id, {"listing_id": listing_id})
    for key in keys:
        if key:
            delete_image(key)
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/notification-retry")
def retry_notification(req: Request, csrf: str=Form(...), notification_id: int=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req):
        raise HTTPException(403)
    with conn() as c:
        notification = c.execute("SELECT status,reference_id,recipient FROM notifications WHERE id=?",
                                 (notification_id,)).fetchone()
        if not notification:
            raise HTTPException(404, "Notification not found")
        if notification["status"] not in ("failed", "unconfigured"):
            raise HTTPException(409, "Only failed or unconfigured notifications can be retried")
        if not notification["recipient"]:
            raise HTTPException(409, "Notification has no configured recipient")
    deliver_notification(notification_id)
    with conn() as c:
        add_audit_record(c, "notification_retried", str(notification_id),
                         {"reference_id": notification["reference_id"]})
    return RedirectResponse("/admin", status_code=303)

@app.post("/admin/logout")
def logout(req: Request, csrf: str=Form(...)):
    check_csrf(req, csrf)
    if not is_admin(req): raise HTTPException(403)
    session_id = req.session["admin_session_id"]
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM admin_sessions WHERE session_hash=?",
                  (admin_session_hash(session_id),))
    req.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/health")
def health():
    with conn() as c:
        c.execute("SELECT 1").fetchone()
    return {"status":"ok"}
