# PangaSalama MVP

Swahili-first Tanzanian rental assistance website using FastAPI, Jinja2 and SQLite. This is an early starter, not yet a public production deployment.

## Included
- Public property search filtered by region and maximum monthly rent
- No-login landlord and agent submissions held for moderation
- No-login home-search and viewing requests
- Admin password sign-in and property approval / rejection
- Editable rent, fees and advance periods: 3, 4, 5, 6 or 12 months
- Persistent notification records for both configured email addresses
- WhatsApp link when a business number is configured

## Local setup
Install Python 3.11+ and run:

```sh
python -m venv .venv
# Activate .venv for your OS, then:
pip install -r requirements.txt
# Set ADMIN_PASSWORD and SESSION_SECRET as shell environment variables
uvicorn app:app --reload
```

Open http://127.0.0.1:8000 and http://127.0.0.1:8000/admin.

## Deployment
Use the environment variable names in `.env.example` as a template and set **real values only in hosting secrets**. This app does not automatically read .env files. Use a strong ADMIN_PASSWORD and independent SESSION_SECRET. Enable SECURE_COOKIES=true behind HTTPS. For Render, the start command is:

```
uvicorn app:app --host 0.0.0.0 --port $PORT
```

**SQLite requires persistent disk**; ephemeral hosting filesystems lose data. For scalable public deployment migrate to a managed relational database. Configure SMTP to actually send email. SMTP acceptance is not proof of inbox delivery.

## Tests
```sh
pytest -q
```

## Release limitations
This repository has not been deployed or end-to-end verified. Photo uploads, audit logs, provider identity verification, email retry queue, robust spam defenses, admin login throttling/MFA, comprehensive privacy terms, English interface and security audit are pending. Admin approval alone does not prove legal property ownership. Avoid collecting real customer information until production hardening and data persistence are verified.
