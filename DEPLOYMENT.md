# FuelGuard API — Deployment Guide (Railway)

Deploy target: a fresh Railway service from **this repository** (or any git
remote Railway can reach). Everything below assumes a **brand-new Firebase
project** and a **new Cloudinary account** — no values from any previous
deployment are reused.

---

## 1. Environment variables

Set these in Railway → your service → **Variables**. Local development uses
the same names in a `.env` file (copy `.env.example`).

### Core

| Variable | Example / how to get it | Required |
|---|---|---|
| `APP_ENV` | `production` | Yes |
| `APP_NAME` | `Fuel Guard API` | No (has default) |
| `API_V1_PREFIX` | `/api/v1` | No (has default) |
| `ALLOWED_ORIGINS` | Comma-separated origins allowed by CORS: your admin panel URL (e.g. `https://fuel-guard-admin.vercel.app`) plus any local dev origins (`http://localhost:3000`). **No trailing slashes.** | Yes |

### Firebase (new project)

| Variable | Example / how to get it | Required |
|---|---|---|
| `FIREBASE_PROJECT_ID` | Firebase Console → Project settings → General → Project ID (e.g. `fuelguard-prod-xxxx`) | Yes |
| `FIREBASE_API_KEY` | Same page → Web API Key. Powers password-reset emails and the panel login fallback. | Yes |
| `FIREBASE_CREDENTIALS_JSON` | The **entire service-account key file as one single line** of JSON. Firebase Console → Project settings → Service accounts → Generate new private key → open the JSON → paste it minified. Convert with:<br>`python -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))" serviceAccountKey.json` | Yes (Railway) |
| `FIREBASE_CREDENTIALS_PATH` | Local dev only: path to the key file, e.g. `serviceAccountKey.json`. Ignored when `FIREBASE_CREDENTIALS_JSON` is set. | Local only |

### JWT

| Variable | Example | Required |
|---|---|---|
| `JWT_SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` — must be 32+ chars in production or startup refuses to boot | Yes |
| `JWT_ALGORITHM` | `HS256` | No |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | No |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | No |

### Email (password reset)

| Variable | Example | Required |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | Optional |
| `SMTP_PORT` | `587` | Optional |
| `SMTP_USER` | your Gmail address (with a Gmail **App Password**, not your login password) | Optional |
| `SMTP_PASSWORD` | the 16-char app password | Optional |
| `EMAIL_FROM` | `noreply@yourdomain.com` | Optional |

Password reset also works through Firebase's own email delivery via
`FIREBASE_API_KEY`; SMTP is a fallback channel and can be left unset.

### Cloudinary (evidence photos + firmware binaries)

| Variable | Example | Required |
|---|---|---|
| `CLOUDINARY_CLOUD_NAME` | Cloudinary Dashboard → Product environment → Cloud name | Yes (evidence + OTA) |
| `CLOUDINARY_API_KEY` | Dashboard → Access Keys → API Key | Yes |
| `CLOUDINARY_API_SECRET` | Dashboard → Access Keys → API Secret | Yes |

Without these, evidence uploads and firmware publishing return 503.

### AI support chatbot

| Variable | Example | Required |
|---|---|---|
| `GROQ_API_KEY` | console.groq.com → API Keys. Powers the in-app support assistant (Llama 3.3 70B). | Optional |

### Rate limiting (per client IP, sliding 1-minute windows)

| Variable | Default | Notes |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Set `false` to disable entirely |
| `RATE_LIMIT_DEFAULT_PER_MINUTE` | `120` | Unauthenticated/GET surface |
| `RATE_LIMIT_AUTH_PER_MINUTE` | `10` | Login/signup — brute-force protection |
| `RATE_LIMIT_IOT_PER_MINUTE` | `600` | Device telemetry and IoT endpoints |
| `RATE_LIMIT_WRITE_PER_MINUTE` | `60` | All POST/PUT/PATCH/DELETE |

### Retention

| Variable | Default | Notes |
|---|---|---|
| `EVIDENCE_RETENTION_DAYS` | `90` | Evidence photos auto-delete after this |
| `TELEMETRY_RETENTION_DAYS` | `30` | Telemetry rows purged by the cleanup job |

---

## 2. Firebase project checklist (new project)

1. console.firebase.google.com → **Add project**.
2. **Authentication → Sign-in method → enable Email/Password.**
3. **Firestore Database → Create database** (production mode is fine; the API
   is the only client).
4. **Project settings → General → Your APIs → Web API Key** → `FIREBASE_API_KEY`.
5. **Project settings → Service accounts → Generate new private key** →
   minify into `FIREBASE_CREDENTIALS_JSON` (Railway) or save as
   `serviceAccountKey.json` next to `main.py` (local, gitignored).
6. The backend never touches the mobile app's Firebase config — that lives in
   the Flutter app (`google-services.json`) and must come from this same
   project.

## 3. Cloudinary checklist

1. cloudinary.com → sign up (free tier is enough).
2. Dashboard → copy **Cloud name, API Key, API Secret** into the three envs.
3. Nothing else to configure — the API creates the `fuelguard/` folders on
   first upload (evidence images and raw firmware binaries).

## 4. Deploy on Railway

1. Push this repository to GitHub (see repo README).
2. Railway → **New Project → Deploy from GitHub repo** → pick it.
   `railway.toml` + `Procfile` already set the start command
   (`uvicorn main:app --host 0.0.0.0 --port $PORT`) and the health check
   (`/health`).
3. Add every variable from section 1.
4. First boot validates production settings — a weak `JWT_SECRET_KEY` or a
   malformed `FIREBASE_CREDENTIALS_JSON` fails loudly in the deploy logs.
5. Verify: `curl https://<your-service>.up.railway.app/health` →
   `{"status":"healthy"}` and `/docs` renders.

## 5. First-run data

From a machine with the service-account key file:

```bash
pip install -r requirements.txt
python scripts/seed_flagging_system.py --key serviceAccountKey.json
```

This seeds the escalation rules, the flagging/blacklist demo scenario, an
IoT device (`FG-ESP32-0001`, demo key `fgk_demo-device-key-2026`) with
commands and telemetry, complaints, and the admin-panel login:

```
admin@fuelguard.pk / FuelGuard#2026      (super_admin — web panel)
```

Change the panel password after first login. Individual seed sections can be
removed with `--purge` (removes everything the script created).

## 6. CORS notes

`ALLOWED_ORIGINS` must contain the exact origin(s) of:

- the admin panel (fetches the API both server-side and client-side),
- any local development origins you use.

The Flutter app does **not** need an entry — mobile apps are not subject to
CORS.

## 7. Security notes

- Never commit `.env`, `serviceAccountKey.json`, or any
  `*firebase-adminsdk*.json` — both are gitignored; the service account key
  travels only as the `FIREBASE_CREDENTIALS_JSON` env var in production.
- Rotate `JWT_SECRET_KEY` if it ever leaks; all access tokens die with it.
- Device (`iot`) endpoints require the `X-Device-Id` + `X-Device-Key` pair
  issued at device registration; keys are stored hashed.
