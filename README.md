# Fuel Guard API

FastAPI + Firestore backend for **FuelGuard** — an IoT-based fuel dispenser
verification platform. It serves the Flutter mobile app, the Next.js admin
panel, and ESP32 dispenser hardware over one authenticated surface.

## Stack

FastAPI · Google Firestore · Firebase Auth (Admin SDK) · JWT · Cloudinary
(evidence + OTA binaries) · Groq AI (support chatbot) · ReportLab / openpyxl
(receipts + exports).

## What it does

| Area | Highlights |
|---|---|
| Auth | Firebase ID-token login for the app, password login for the panel, JWT access/refresh, five roles (super_admin, admin, employee, customer, driver) |
| Fueling | QR and device-initiated sessions, dual-sensor verification (flow meter vs dispenser, 5% tolerance), evidence photos, PDF receipts |
| Fraud & flagging | Six automated transaction checks, entity flagging (attendant / station / customer / payment method), 30-day escalation ladder to a 3-level blacklist with per-level restrictions — every threshold stored in Firestore, editable from the panel |
| IoT | Device registry with per-device API keys, telemetry with bounds validation, command queue (camera / calibration / OTA) with delivery + ack tracking, firmware registry |
| Fleet | Vehicles, fuel expenses with odometer, km-per-litre analytics, category summaries, monthly budgets, drivers |
| Reports | Transaction / sales / fraud / employee / nozzle reports, chart series, CSV / Excel / PDF export |
| Platform | Per-IP sliding-window rate limiting, tag-based response cache with single-flight loaders, CORS, production config validation at boot |

## Quickstart (local)

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt     # Windows
cp .env.example .env                             # fill in your values
venv/Scripts/python -m uvicorn main:app --reload
```

Interactive docs: http://127.0.0.1:8000/docs — health: `/health`.

Every variable is documented in `.env.example` and **DEPLOYMENT.md** covers
Railway deployment plus the new-Firebase / new-Cloudinary checklists.

## Repository layout

```
main.py                 app factory, router registration, rate limiter
core/                   config, firebase, security (JWT/bcrypt/OTP), rate limiter, cache
models/                 Pydantic schemas (one per domain)
routers/                auth, users, nozzles, sessions, transactions, reports,
                        evidence, fraud, flagging, stations, prices, admin,
                        fleet, iot, notifications
services/               flagging/escalation engine, fraud checks, auth helpers, reporting
scripts/                seed_flagging_system.py (full demo scenario + panel admin)
docs/                   API documentation (LaTeX/PDF)
DEPLOYMENT.md           Railway deployment + environment reference
```

## Demo credentials (after seeding)

```
Panel:  admin@fuelguard.pk / FuelGuard#2026   (super_admin)
Device: FG-ESP32-0001 with key fgk_demo-device-key-2026
```

Run the seeder against your Firebase project:

```bash
python scripts/seed_flagging_system.py --key serviceAccountKey.json
```

## Security posture

- Service-account keys and `.env` are gitignored and travel only as env vars.
- IoT endpoints require `X-Device-Id` + `X-Device-Key`; keys stored hashed.
- Rate limits per IP and per scope (auth 10/min, writes 60/min, IoT 600/min).
- Production boot refuses weak JWT secrets or malformed Firebase credentials.
