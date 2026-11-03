# PangaSalama MVP

Swahili-first Tanzanian rental assistance website using FastAPI, Jinja2 and SQLite. This is an early marketplace MVP, not yet a complete public production service.

## Included

- Public property search filtered by region and maximum monthly rent
- Property detail pages with location, rent breakdown, bathrooms, bedrooms, and viewing requests
- Multi-photo listing submissions with image validation, resizing, moderation, and private storage
- No-login landlord and agent submissions held for moderation
- No-login home-search and viewing requests with private reference/token tracking
- Admin password sign-in and property approval / rejection
- Editable rent, fees and advance periods: 3, 4, 5, 6 or 12 months
- SQLite-backed revocable admin sessions, audit history, login throttling, and public form rate limits
- Retryable administrative email notifications and an optional WhatsApp link

Listings and customer requests contain personal information. Use synthetic data for demonstrations and restrict database and backup access.
This repository contains no sample rental listings or example photographs. Add demonstration listings only when clearly labeled and use images that are created by the team or have documented reuse rights and attribution.

## Local development

Install Python 3.11+ and run:

```sh
python -m venv .venv
# Activate .venv for your OS, then:
pip install -r requirements.txt
```

Set an explicit environment before starting. On PowerShell:

```sh
$env:APP_ENV = "development"
$env:ADMIN_PASSWORD = "local-development-password"
$env:SESSION_SECRET = "local-development-session-secret"
$env:DATABASE_PATH = ".\\pangasalama-dev.sqlite3"
$env:IMAGE_STORAGE_BACKEND = "local"
$env:LOCAL_IMAGE_STORAGE_PATH = ".\\private_uploads"
uvicorn app:app --reload
```

On macOS/Linux, use `export APP_ENV=development` and set the other values similarly before running Uvicorn. The app rejects a missing or invalid `APP_ENV`; `development` and `test` are non-production modes and must never be exposed publicly.

Open http://127.0.0.1:8000 and http://127.0.0.1:8000/admin. Development mode permits HTTP and local defaults; do not expose it publicly.

## Production configuration

The application does not load `.env` files. Set each value through the hosting provider's secret/environment configuration. `.env.example` contains placeholders only and must not be used as-is.

Production startup requires:

- `APP_ENV=production`
- A unique `ADMIN_PASSWORD` of at least 32 characters
- A different random `SESSION_SECRET` of at least 43 characters (generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`)
- `SECURE_COOKIES=true` and HTTPS at the public endpoint
- An absolute `DATABASE_PATH` on persistent storage
- `IMAGE_STORAGE_BACKEND=s3`, `S3_BUCKET`, and `S3_REGION`; AWS credentials should come from the hosting identity/secret manager

Use hosting-generated secrets; never commit them. Store backups separately with access controls. Session cookies are HTTP-only, same-site lax, and expire after eight hours; the admin session expires after 30 minutes idle. Admin sessions are tracked in SQLite, and logout removes the server-side session record so copied cookies are rejected. Admin login is limited to five attempts per IP per 15 minutes. Public listing and request submissions are limited per IP. These SQLite-backed controls are basic abuse mitigations, not a substitute for monitoring, an edge WAF, CAPTCHA, MFA, or security review. A user with many IPs can evade per-IP limits.

Property images accept JPEG, PNG, or WebP up to 5 MB each (maximum six). The app re-encodes images to WebP and limits dimensions. Images are private until both the listing and the image are approved. For production, use a private S3 bucket with public access blocked and a narrowly scoped application role. Image reads go through the application authorization route; do not make uploaded objects public or place them on Render's ephemeral filesystem. `S3_ENDPOINT_URL` is optional for S3-compatible services. Local file storage is for development only.

The maximum combined upload is 20 MB per listing submission. The app produces a full WebP and a 480×360-bounded WebP thumbnail per image. Example photography is not bundled because no license-approved property imagery was supplied; do not use fabricated images as genuine listings.

### Admin session schema migration and rollback

Startup creates an additive `admin_sessions` table with `CREATE TABLE IF NOT EXISTS`; it does not rewrite or remove listing, request, or notification records. Existing databases migrate automatically on application startup. Take and verify a backup before upgrading. Existing admin cookies are not accepted by the new session validator; administrators must sign in again.

The same startup migration adds listing detail fields, request tracking/consent fields, notification retry metadata, and the `listing_photos` table. Existing rows get safe defaults and remain intact. Migration is additive rather than a versioned migration framework; test against a copy of the current database before production use.

To roll back application code, stop the service and keep the database and image-store backups. The previous application ignores `admin_sessions` and `listing_photos`, so it will not enforce server-side revocation or image approval. Rotate `SESSION_SECRET` before running older code to invalidate cookies. The new tables and columns may remain; removing them is unnecessary. Restoring a pre-upgrade backup is not required for code rollback and would discard records created since that backup.

For Render, mount a persistent disk at `/var/data`, set `DATABASE_PATH=/var/data/pangasalama.sqlite3`, and use:

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Do not use an ephemeral filesystem for the database. Form notification recipients default to `ezekianyamhanga@gmail.com`, with a copy to `miriamnyamhanga237@gmail.com`; set `PRIMARY_NOTIFICATION_EMAIL` or `BACKUP_NOTIFICATION_EMAIL` in the hosting environment to override or leave a recipient empty to disable it. The WhatsApp contact link defaults to `0788525327` (configured as international number `255788525327` for `wa.me`). The link lets visitors start a WhatsApp conversation; it does not automatically forward form submissions to WhatsApp.

Email delivery requires working `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, and `EMAIL_FROM_ADDRESS` settings. Without them, the application records notifications as unconfigured for an administrator to review; it does not send email. SMTP acceptance is not proof of inbox delivery.

`/health` performs a basic SQLite connectivity check. Logs are emitted as JSON lines to standard output; configure the host to capture, retain, and alert on them. This is not a substitute for provider-side uptime alerts or monitoring.

### Pre-launch checklist

1. Provision a private persistent database disk and private S3 bucket; block public bucket access and scope the app role to the required bucket operations.
2. Set `APP_ENV=production`, generate independent production secrets, enable HTTPS and `SECURE_COOKIES=true`, and configure only necessary notification recipients.
3. Back up the current database, verify it, and rehearse the additive schema migration and rollback against a copy.
4. Run automated tests and manually verify uploads, moderation, S3 reads, server-side logout revocation, and backup restoration in isolated staging.
5. Add privacy/retention terms, test provider verification procedures, mobile browser checks, monitoring, and a response/recovery owner before collecting real customer data.
6. Do not collect rent or deposits through PangaSalama. There is no payment verification or payment gateway in this MVP.

## SQLite backup and restore

`db_maintenance.py` uses SQLite's online backup API and checks database integrity and required tables. Backups contain personal information: write them to a protected location, retain them according to policy, and test restore access.

Create and verify a backup:

```sh
python db_maintenance.py backup /secure/backups/pangasalama-2026-01-01.sqlite3
python db_maintenance.py verify /secure/backups/pangasalama-2026-01-01.sqlite3
```

Backup works while the app is running and refuses to overwrite an existing backup. For restore, stop the application first, then run:

```sh
python db_maintenance.py restore /secure/backups/pangasalama-2026-01-01.sqlite3
```

Restore validates the source, refuses to proceed if the database has SQLite WAL/SHM sidecars, and creates a timestamped pre-restore safety backup before atomically replacing the database. If restore fails before replacement, the current database remains in place. Keep the safety backup until the restored service and expected records have been checked. The restore command targets the configured `DATABASE_PATH`; verify `DATABASE_PATH` is set correctly before running it.

## Tests

```sh
pytest -q
```

Tests use a temporary database and synthetic records; they do not send email or access production storage or data.

## Remaining release limitations

Customer accounts are not required or implemented; tenants track requests using a one-time secret code, which they must keep private. There is no legal privacy notice/retention schedule, deletion automation, provider identity proofing, mobile-browser end-to-end testing, deployment verification, or PostgreSQL migration. Admin login still uses a shared password without MFA. Rate limits are per IP. Notification retries are manual from the dashboard, not a background queue. No payment collection or payment-verification workflow is enabled. Admin approval or document review is not a guarantee of legal ownership or authority to rent. Do not collect real customer information or accept payments until privacy obligations, provider verification, persistent storage, backups, and operational safeguards have been reviewed. Do not claim production readiness based solely on local tests.
