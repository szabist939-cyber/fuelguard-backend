from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import auth, credentials, firestore
from google.cloud.firestore_v1 import Increment  # noqa: F401 — re-exported for routers

from core.config import settings

_db: Optional[firestore.Client] = None


def _load_credentials() -> credentials.Certificate:
    """
    Resolve Firebase credentials in priority order:

    1. FIREBASE_CREDENTIALS_JSON env var — JSON string, used in Railway / CI.
    2. FIREBASE_CREDENTIALS_PATH file   — local development with a key file.

    This keeps secrets out of the repo while still supporting a simple local
    workflow where you drop serviceAccountKey.json in the project root.
    """
    raw = os.getenv("FIREBASE_CREDENTIALS_JSON", "").strip()
    if raw:
        try:
            cert_dict = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "FIREBASE_CREDENTIALS_JSON is set but is not valid JSON. "
                "Paste the entire service-account key file as one line."
            ) from exc
        return credentials.Certificate(cert_dict)

    path = settings.FIREBASE_CREDENTIALS_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Firebase credentials file not found at '{path}'. "
            "Either set FIREBASE_CREDENTIALS_JSON (production) or place the "
            "service-account key file at the path configured by FIREBASE_CREDENTIALS_PATH."
        )
    return credentials.Certificate(path)


def init_firebase() -> None:
    global _db
    if not firebase_admin._apps:
        cred = _load_credentials()
        firebase_admin.initialize_app(cred)
    _db = firestore.client()


def get_db() -> firestore.Client:
    if _db is None:
        raise RuntimeError("Firebase not initialised — call init_firebase() at startup.")
    return _db


def get_auth():
    return auth


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Safely convert a datetime to UTC-aware.

    Firestore returns naive datetimes in some SDK versions and UTC-aware in others.
    Using .replace() to stamp a timezone on a naive datetime does NOT convert the
    value — it only labels it, which causes incorrect comparisons when mixed with
    aware datetimes. This helper normalises both cases correctly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── Firestore collection names ────────────────────────────────────────────────

class Collections:
    USERS           = "users"
    NOZZLES         = "nozzles"
    SESSIONS        = "sessions"
    TRANSACTIONS    = "transactions"
    EVIDENCE        = "evidence"
    FRAUD_ALERTS    = "fraud_alerts"
    STATIONS        = "stations"
    PRICES          = "prices"
    FLEET_VEHICLES  = "fleet_vehicles"
    FLEET_EXPENSES  = "fleet_expenses"
    FLEET_DRIVERS   = "fleet_drivers"
    FLAGS           = "flags"
    FLAGGING_RULES  = "flagging_rules"
    IOT_DEVICES     = "iot_devices"
    IOT_FIRMWARE    = "iot_firmware"
    OTP_STORE       = "otp_store"
    BLACKLIST       = "blacklist"
    COMPLAINTS      = "complaints"
    NOTIFICATIONS   = "notifications"
    FAVORITES       = "favorites"
    PRICE_ALERTS    = "price_alerts"
    BUDGETS         = "budgets"
    TAMPER_ALERTS   = "tamper_alerts"
    DEVICE_COMMANDS = "device_commands"
    TELEMETRY_LOGS  = "telemetry_logs"
    PRICE_HISTORY   = "price_history"
