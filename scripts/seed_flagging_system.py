"""
Seed the Flagging & Blacklisting system with a complete demo scenario.

What it creates
---------------
 flagging_rules/default   escalation thresholds (edited from the admin panel)
 users/seed-admin         super_admin for the web panel login
 users/seed-emp-1..4      pump attendants: 3 flagged at levels 1/2/3 + 1 clean
 users/seed-cust-1..3     customers: 1 blacklisted, 1 flagged, 1 clean
 stations/seed-station-a  blacklisted station (High Fraud Risk badge in the app)
 stations/seed-station-b  clean station for contrast
 stations/seed-station-c  clean station for contrast
 nozzles/seed-nz-1..3     nozzles at the flagged station
 prices/seed-station-*    petrol + diesel price rows so compare keeps working
 sessions/seed-sess-*     completed sessions backing the seeded transactions
 transactions/seed-tx-*   ~60 transactions across the last 45 days
 fraud_alerts/seed-fa-*   matching alerts for every discrepancy event
 flags/seed-flag-*        the flag ladder: 3 / 5 / 7 flags per attendant etc.
 blacklist/seed-bl-*      active entries the engine would have produced
 complaints/seed-cpl-*    dispute trail behind the customer flags

Every document id is deterministic, so rerunning the script overwrites in
place instead of duplicating. Use --purge to remove the whole scenario.

Usage (from fuel-guard-backend):
    python scripts/seed_flagging_system.py --key fuelguard-f3112-firebase-adminsdk-....json
    python scripts/seed_flagging_system.py --key ... --dry-run
    python scripts/seed_flagging_system.py --key ... --purge
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, firestore

DEFAULT_PROJECT = "fuelguard-f3112"

PANEL_ADMIN_EMAIL = "admin@fuelguard.pk"
PANEL_ADMIN_PASSWORD = "FuelGuard#2026"

CUTOFF_DAYS = 45  # transactions spread over the recent past

STATIONS = [
    {"id": "seed-station-a", "name": "FuelGuard.seed Station A", "display": "Blue Area Fuel Station",
     "address": "Jinnah Avenue, Blue Area", "city": "Islamabad",
     "latitude": 33.6938, "longitude": 73.0659, "blacklisted": True},
    {"id": "seed-station-b", "name": "F-7 Markaz Fuel Station", "display": "F-7 Markaz Fuel Station",
     "address": "College Road, F-7 Markaz", "city": "Islamabad",
     "latitude": 33.7215, "longitude": 73.0551, "blacklisted": False},
    {"id": "seed-station-c", "name": "G-9 Fuel Point", "display": "G-9 Fuel Point",
     "address": "9th Avenue, G-9 Markaz", "city": "Islamabad",
     "latitude": 33.6895, "longitude": 73.0330, "blacklisted": False},
]

EMPLOYEES = [
    {"id": "seed-emp-1", "name": "Imran Shah", "flags": 3, "station": "seed-station-a",
     "email": "imran.shah@fuelguard.pk"},
    {"id": "seed-emp-2", "name": "Zeeshan Haider", "flags": 5, "station": "seed-station-a",
     "email": "zeeshan.haider@fuelguard.pk"},
    {"id": "seed-emp-3", "name": "Naveed Iqbal", "flags": 7, "station": "seed-station-a",
     "email": "naveed.iqbal@fuelguard.pk"},
    {"id": "seed-emp-4", "name": "Bilal Ahmed", "flags": 0, "station": "seed-station-b",
     "email": "bilal.ahmed@fuelguard.pk"},
]

CUSTOMERS = [
    {"id": "seed-cust-1", "name": "Hamza Tariq", "email": "hamza.tariq@fuelguard.pk",
     "flags": 3, "phone": "+92 301 5550101"},
    {"id": "seed-cust-2", "name": "Sana Yousaf", "email": "sana.yousaf@fuelguard.pk",
     "flags": 1, "phone": "+92 301 5550102"},
    {"id": "seed-cust-3", "name": "Adeel Raza", "email": "adeel.raza@fuelguard.pk",
     "flags": 0, "phone": "+92 301 5550103"},
]


def hash_password(password: str) -> str:
    from passlib.context import CryptContext
    return CryptContext(schemes=["bcrypt"], deprecated="auto").hash(password)


def utc(days_ago=0, hours_ago=0, minute=0) -> datetime:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.replace(minute=minute, second=0, microsecond=0)


def find_key(args_key: str | None) -> str:
    if args_key:
        return args_key
    candidates = list(Path(__file__).resolve().parent.parent.glob("*adminsdk*.json"))
    if not candidates:
        print("No service account key found. Pass --key /path/to/serviceAccountKey.json")
        sys.exit(1)
    print(f"Auto discovered key: {candidates[0].name}")
    return str(candidates[0])


def connect(key_path: str, project: str) -> firestore.Client:
    with open(key_path, encoding="utf-8") as fh:
        key = json.load(fh)
    if key.get("project_id") != project:
        print(f"Refusing to continue: key belongs to {key.get('project_id')}, expected {project}")
        sys.exit(1)
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(key_path))
    return firestore.client()


# ── Scenario maths ────────────────────────────────────────────────────────────

def discrepancy_profile(flags: int) -> list[dict]:
    """One record per flag: how badly the dispenser over reported."""
    profiles = []
    for i in range(flags):
        flow = round(18.0 + i * 1.3, 2)
        over = 1.0 + i * 0.45                      # grows from 5.5% toward 20%
        dispenser = round(flow * (1 + over / 100), 2)
        profiles.append({
            "flowmeter_litres": flow,
            "dispenser_litres": dispenser,
            "discrepancy_percent": round((dispenser / flow - 1) * 100, 2),
        })
    return profiles


def build_rows():
    """Deterministic scenario rows shared by seed and purge."""
    rows = {"users": {}, "stations": {}, "nozzles": {}, "prices": {}, "sessions": {},
            "transactions": {}, "fraud_alerts": {}, "flags": {}, "blacklist": {},
            "complaints": {}, "iot_devices": {}, "iot_firmware": {},
            "device_commands": {}, "telemetry_logs": {}}

    rules = {
        "id": "default",
        "discrepancy_threshold_percent": 5.0,
        "flag_window_days": 30,
        "employee_flags_to_blacklist": 3,
        "station_flags_to_blacklist": 4,
        "customer_flags_to_blacklist": 3,
        "payment_flags_to_flag": 3,
        "level_2_flags": 5,
        "level_3_flags": 7,
        "auto_blacklist_enabled": True,
        "reduced_visibility_enabled": True,
        "updated_by": "seed",
        "updated_at": utc(0, 1),
    }

    # Panel admin + employees + customers --------------------------------------
    rows["users"]["seed-admin"] = {
        "uid": "seed-admin", "email": PANEL_ADMIN_EMAIL, "full_name": "FuelGuard Panel Admin",
        "phone": "+92 300 5550001", "role": "super_admin", "avatar_url": None,
        "is_active": True, "email_verified": True,
        "password_hash": hash_password(PANEL_ADMIN_PASSWORD),
        "seeded": True, "created_at": utc(60),
    }
    for emp in EMPLOYEES:
        rows["users"][emp["id"]] = {
            "uid": emp["id"], "email": emp["email"], "full_name": emp["name"],
            "phone": "+92 300 5550100", "role": "employee", "avatar_url": None,
            "is_active": True, "email_verified": True,
            "password_hash": hash_password("Attendant#2026"),
            "seeded": True, "created_at": utc(50),
        }
    for cust in CUSTOMERS:
        rows["users"][cust["id"]] = {
            "uid": cust["id"], "email": cust["email"], "full_name": cust["name"],
            "phone": cust["phone"], "role": "customer", "avatar_url": None,
            "is_active": True, "email_verified": True,
            "password_hash": hash_password("Customer#2026"),
            "seeded": True, "created_at": utc(40),
        }

    # Stations ------------------------------------------------------------------
    for st in STATIONS:
        rows["stations"][st["id"]] = {
            "id": st["id"], "name": st["display"], "address": st["address"], "city": st["city"],
            "latitude": st["latitude"], "longitude": st["longitude"],
            "contact_phone": "+92 51 555 1000", "manager_uid": None,
            "fuel_types_available": ["petrol", "diesel"],
            "operating_hours": "24/7", "is_active": True, "seeded": True,
            "created_at": utc(60),
        }
        rows["prices"][f"{st['id']}_petrol"] = {
            "id": f"{st['id']}_petrol", "station_id": st["id"], "fuel_type": "petrol",
            "price_per_litre": 271.49 if st["blacklisted"] else 268.63,
            "updated_by": "seed", "effective_from": utc(3), "created_at": utc(3),
        }
        rows["prices"][f"{st['id']}_diesel"] = {
            "id": f"{st['id']}_diesel", "station_id": st["id"], "fuel_type": "diesel",
            "price_per_litre": 279.10 if st["blacklisted"] else 276.85,
            "updated_by": "seed", "effective_from": utc(3), "created_at": utc(3),
        }

    # Nozzles at the flagged station + one clean one -----------------------------
    for i in range(1, 4):
        rows["nozzles"][f"seed-nz-{i}"] = {
            "id": f"seed-nz-{i}", "name": f"Nozzle {i}", "station_id": "seed-station-a",
            "fuel_type": "petrol" if i < 3 else "diesel",
            "ble_uuid": f"SEED-NZ-{i}-BLE", "ble_device_name": "FuelMonitor",
            "hardware_serial": f"SEED-NZ-{i}", "flow_meter_model": "YF-S201",
            "max_flow_rate": 60.0, "status": "idle", "is_active": True,
            "tamper_detected": i == 2, "current_flow_rate": None,
            "total_litres_today": 0.0, "last_seen": None, "seeded": True,
            "created_at": utc(55),
        }
    rows["nozzles"]["seed-nz-4"] = {
        "id": "seed-nz-4", "name": "Nozzle 4", "station_id": "seed-station-b",
        "fuel_type": "petrol", "ble_uuid": "SEED-NZ-4-BLE", "ble_device_name": "FuelMonitor",
        "hardware_serial": "SEED-NZ-4", "flow_meter_model": "YF-S201",
        "max_flow_rate": 60.0, "status": "idle", "is_active": True,
        "tamper_detected": False, "current_flow_rate": None,
        "total_litres_today": 0.0, "last_seen": None, "seeded": True,
        "created_at": utc(55),
    }

    # The fraud ladder: attendants 1/2/3 with 3/5/7 discrepancy events ----------
    nozzle_for = {0: "seed-nz-1", 1: "seed-nz-2", 2: "seed-nz-1"}
    tx_no = 1
    for emp_idx, emp in enumerate([e for e in EMPLOYEES if e["flags"] > 0]):
        for evt_idx, profile in enumerate(discrepancy_profile(emp["flags"])):
            days_ago = 2 + evt_idx * 4                      # 2, 6, 10 ... days back
            tx_id = f"seed-tx-{tx_no:03d}"
            tx_no += 1
            nozzle = nozzle_for[emp_idx]
            amount = round(profile["dispenser_litres"] * 271.49, 2)
            rows["sessions"][f"seed-sess-{tx_id}"] = {
                "id": f"seed-sess-{tx_id}", "nozzle_id": nozzle, "user_id": None,
                "status": "completed", "qr_data": None,
                "started_at": utc(days_ago, 3), "ended_at": utc(days_ago, 3),
                "expires_at": None,
                "total_litres": profile["dispenser_litres"], "total_amount": amount,
                "transaction_id": tx_id, "seeded": True, "created_at": utc(days_ago, 3),
            }
            rows["transactions"][tx_id] = {
                "id": tx_id, "session_id": f"seed-sess-{tx_id}", "nozzle_id": nozzle,
                "user_id": None, "vehicle_id": None, "fuel_type": "petrol",
                "litres_dispensed": profile["dispenser_litres"],
                "price_per_litre": 271.49, "total_amount": amount,
                "payment_method": "cash", "status": "completed",
                "employee_id": emp["id"], "station_id": "seed-station-a",
                "receipt_url": None, "evidence_url": None,
                "is_flagged": True, "disputed": False, "seeded": True,
                "created_at": utc(days_ago, 3),
            }
            rows["fraud_alerts"][f"seed-fa-{tx_id}"] = {
                "id": f"seed-fa-{tx_id}", "alert_type": "flow_discrepancy",
                "transaction_id": tx_id, "nozzle_id": nozzle, "user_id": None,
                "description": (
                    f"Dispenser reported {profile['dispenser_litres']} L against an independent "
                    f"{profile['flowmeter_litres']} L sensor reading, a "
                    f"{profile['discrepancy_percent']} percent over report."
                ),
                "severity": "critical" if profile["discrepancy_percent"] > 10 else "high",
                "status": "open", "resolved_by": None, "resolution_note": None,
                "evidence": {
                    "dispenser_litres": profile["dispenser_litres"],
                    "flowmeter_litres": profile["flowmeter_litres"],
                    "discrepancy_percent": profile["discrepancy_percent"],
                },
                "flagged_by": "system", "seeded": True, "created_at": utc(days_ago, 3),
            }
            rows["flags"][f"seed-flag-{emp['id']}-{evt_idx}"] = {
                "id": f"seed-flag-{emp['id']}-{evt_idx}",
                "entity_type": "employee", "entity_id": emp["id"], "entity_name": emp["name"],
                "source": "automatic", "trigger_type": "flow_discrepancy",
                "severity": "high",
                "description": (
                    f"Dispenser over reported by {profile['discrepancy_percent']} percent "
                    f"on transaction {tx_id}."
                ),
                "status": "active", "transaction_id": tx_id, "nozzle_id": nozzle,
                "station_id": "seed-station-a",
                "discrepancy_percent": profile["discrepancy_percent"],
                "created_by": "system", "seeded": True, "created_at": utc(days_ago, 3),
                "dismissed_by": None, "dismissed_at": None, "dismissal_note": None,
            }

    # Station A flags: one per attendant event, sharing the transactions --------
    station_evt = 0
    for emp in [e for e in EMPLOYEES if e["flags"] > 0 and e["station"] == "seed-station-a"]:
        for evt_idx, profile in enumerate(discrepancy_profile(emp["flags"])):
            if station_evt >= 5:
                break  # four active flags hit the station threshold; one spare reads realistically
            days_ago = 2 + evt_idx * 4
            station_evt += 1
            rows["flags"][f"seed-flag-station-a-{station_evt}"] = {
                "id": f"seed-flag-station-a-{station_evt}",
                "entity_type": "station", "entity_id": "seed-station-a",
                "entity_name": "Blue Area Fuel Station",
                "source": "automatic", "trigger_type": "flow_discrepancy",
                "severity": "high",
                "description": (
                    f"Repeated dispenser over reporting at this station, "
                    f"{profile['discrepancy_percent']} percent on one incident."
                ),
                "status": "active",
                "transaction_id": None, "nozzle_id": None, "station_id": "seed-station-a",
                "discrepancy_percent": profile["discrepancy_percent"],
                "created_by": "system", "seeded": True, "created_at": utc(days_ago, 4),
                "dismissed_by": None, "dismissed_at": None, "dismissal_note": None,
            }

    # Customer dispute ladder -----------------------------------------------------
    claims = [
        ("Disputed a fully verified 20 L fill", 12),
        ("Second dispute on a verified transaction", 8),
        ("Third dispute, no evidence provided", 4),
    ]
    for i, (claim, days_ago) in enumerate(claims[: CUSTOMERS[0]["flags"]]):
        cpl_id = f"seed-cpl-{i + 1}"
        rows["complaints"][cpl_id] = {
            "id": cpl_id, "user_id": CUSTOMERS[0]["id"], "subject": "Under dispensing claim",
            "description": claim, "status": "resolved", "outcome": "invalid",
            "resolution_note": "Sensor log confirms the delivered volume. Claim not upheld.",
            "requires_evidence": i >= 1, "seeded": True, "created_at": utc(days_ago, 5),
            "updated_at": utc(days_ago - 1, 5),
        }
        rows["flags"][f"seed-flag-{CUSTOMERS[0]['id']}-{i}"] = {
            "id": f"seed-flag-{CUSTOMERS[0]['id']}-{i}",
            "entity_type": "customer", "entity_id": CUSTOMERS[0]["id"],
            "entity_name": CUSTOMERS[0]["name"],
            "source": "dispute", "trigger_type": "invalid_claim", "severity": "medium",
            "description": f"Complaint resolved as invalid: {claim}.",
            "status": "active", "transaction_id": None, "nozzle_id": None,
            "station_id": None, "discrepancy_percent": None,
            "created_by": "seed-admin", "seeded": True, "created_at": utc(days_ago - 1, 5),
            "dismissed_by": None, "dismissed_at": None, "dismissal_note": None,
        }

    rows["complaints"]["seed-cpl-4"] = {
        "id": "seed-cpl-4", "user_id": CUSTOMERS[1]["id"], "subject": "Slow dispensing",
        "description": "Pump felt slower than usual, suspicious of meter tampering.",
        "status": "open", "outcome": None, "resolution_note": None,
        "requires_evidence": False, "seeded": True, "created_at": utc(1, 6),
        "updated_at": None,
    }
    rows["flags"][f"seed-flag-{CUSTOMERS[1]['id']}-0"] = {
        "id": f"seed-flag-{CUSTOMERS[1]['id']}-0",
        "entity_type": "customer", "entity_id": CUSTOMERS[1]["id"],
        "entity_name": CUSTOMERS[1]["name"],
        "source": "manual", "trigger_type": "manual_review", "severity": "low",
        "description": "Complaint under review, treated as a good faith claim.",
        "status": "active", "transaction_id": None, "nozzle_id": None,
        "station_id": None, "discrepancy_percent": None,
        "created_by": "seed-admin", "seeded": True, "created_at": utc(1, 5),
        "dismissed_by": None, "dismissed_at": None, "dismissal_note": None,
    }

    # Payment method chargebacks ---------------------------------------------------
    for i in range(3):
        rows["flags"][f"seed-flag-card-{i}"] = {
            "id": f"seed-flag-card-{i}",
            "entity_type": "payment_method", "entity_id": "card", "entity_name": "Card",
            "source": "dispute", "trigger_type": "chargeback_pattern", "severity": "high",
            "description": f"Chargeback case {i + 1} opened through the bank on a card payment.",
            "status": "active", "transaction_id": None, "nozzle_id": None,
            "station_id": None, "discrepancy_percent": None,
            "created_by": "seed-admin", "seeded": True, "created_at": utc(3 + i * 6, 2),
            "dismissed_by": None, "dismissed_at": None, "dismissal_note": None,
        }
    rows["flags"]["seed-flag-qr-pay-0"] = {
        "id": "seed-flag-qr-pay-0",
        "entity_type": "payment_method", "entity_id": "qr_pay", "entity_name": "Qr Pay",
        "source": "dispute", "trigger_type": "disputed_valid_transaction", "severity": "medium",
        "description": "Single disputed qr payment, review pending.",
        "status": "active", "transaction_id": None, "nozzle_id": None,
        "station_id": None, "discrepancy_percent": None,
        "created_by": "seed-admin", "seeded": True, "created_at": utc(2, 2),
        "dismissed_by": None, "dismissed_at": None, "dismissal_note": None,
    }

    # Blacklist entries exactly where the ladder lands ------------------------------
    def blacklist_entry(entity_type, entity_id, name, level, flags_count, reason, auto=True):
        return {
            "id": f"seed-bl-{entity_id}", "entity_type": entity_type, "entity_id": entity_id,
            "entity_name": name, "level": level, "status": "active", "reason": reason,
            "flags_count": flags_count, "window_days": 30, "auto_escalated": auto,
            "flagged_by": "system" if auto else "seed-admin",
            "removed_by": None, "removed_at": None, "removal_note": None,
            "seeded": True, "created_at": utc(2, 1), "updated_at": utc(2, 1),
        }

    rows["blacklist"]["seed-bl-seed-emp-1"] = blacklist_entry(
        "employee", "seed-emp-1", "Imran Shah", 1, 3,
        "Three dispenser over reports above 5 percent within 30 days.")
    rows["blacklist"]["seed-bl-seed-emp-2"] = blacklist_entry(
        "employee", "seed-emp-2", "Zeeshan Haider", 2, 5,
        "Five discrepancy events within 30 days, pattern of under dispensing.")
    rows["blacklist"]["seed-bl-seed-emp-3"] = blacklist_entry(
        "employee", "seed-emp-3", "Naveed Iqbal", 3, 7,
        "Seven discrepancy events within 30 days, suspended pending investigation.")
    rows["blacklist"]["seed-bl-seed-station-a"] = blacklist_entry(
        "station", "seed-station-a", "Blue Area Fuel Station", 2, 5,
        "Highest fraud frequency in the network with five verified over reports in 30 days.")
    rows["blacklist"]["seed-bl-seed-cust-1"] = blacklist_entry(
        "customer", "seed-cust-1", "Hamza Tariq", 1, 3,
        "Three invalid under dispensing claims within 30 days.")
    rows["blacklist"]["seed-bl-card"] = blacklist_entry(
        "payment_method", "card", "Card", 1, 3,
        "Repeated chargeback activity on card payments.")

    # Honest network traffic so the dashboard looks alive ---------------------------
    for i in range(24):
        days_ago = (i * 2) % CUTOFF_DAYS
        tx_id = f"seed-tx-{tx_no:03d}"
        tx_no += 1
        litres = round(8.0 + (i % 7) * 2.5, 2)
        price = 268.63
        method = ["cash", "card", "wallet", "qr_pay"][i % 4]
        rows["sessions"][f"seed-sess-{tx_id}"] = {
            "id": f"seed-sess-{tx_id}", "nozzle_id": "seed-nz-4", "user_id": None,
            "status": "completed", "qr_data": None,
            "started_at": utc(days_ago, 2), "ended_at": utc(days_ago, 2),
            "expires_at": None, "total_litres": litres,
            "total_amount": round(litres * price, 2), "transaction_id": tx_id,
            "seeded": True, "created_at": utc(days_ago, 2),
        }
        rows["transactions"][tx_id] = {
            "id": tx_id, "session_id": f"seed-sess-{tx_id}", "nozzle_id": "seed-nz-4",
            "user_id": None, "vehicle_id": None,
            "fuel_type": "diesel" if i % 3 == 0 else "petrol",
            "litres_dispensed": litres, "price_per_litre": price,
            "total_amount": round(litres * price, 2), "payment_method": method,
            "status": "completed", "employee_id": "seed-emp-4", "station_id": "seed-station-b",
            "receipt_url": None, "evidence_url": None, "is_flagged": False,
            "disputed": False, "seeded": True, "created_at": utc(days_ago, 2),
        }

    # IoT control plane demo ---------------------------------------------------
    demo_key = "fgk_demo-device-key-2026"
    rows["iot_devices"]["seed-dev-1"] = {
        "id": "seed-dev-1", "device_id": "FG-ESP32-0001", "device_type": "esp32",
        "nozzle_id": "seed-nz-1", "station_id": "seed-station-a",
        "firmware_version": "1.4.2", "ip_address": None,
        "device_key_hash": hashlib.sha256(demo_key.encode("utf-8")).hexdigest(),
        "status": "offline", "last_ping": utc(0, 2),
        "firmware_update_status": "idle",
        "seeded": True, "created_at": utc(50),
    }
    rows["iot_devices"]["seed-dev-2"] = {
        "id": "seed-dev-2", "device_id": "FG-CAM-0002", "device_type": "camera",
        "nozzle_id": "seed-nz-2", "station_id": "seed-station-a",
        "firmware_version": "2.0.1", "ip_address": None,
        "device_key_hash": hashlib.sha256(demo_key.encode("utf-8")).hexdigest(),
        "status": "offline", "last_ping": utc(3, 1),
        "seeded": True, "created_at": utc(45),
    }
    rows["iot_firmware"]["seed-fw-1"] = {
        "id": "seed-fw-1", "version": "1.4.2",
        "release_notes": "Tamper latch fix and command polling.",
        "is_mandatory": False, "file_size_bytes": 786432,
        "download_url": "https://example.com/fw-1.4.2.bin",
        "seeded": True, "created_at": utc(20),
    }
    rows["iot_firmware"]["seed-fw-2"] = {
        "id": "seed-fw-2", "version": "1.3.0",
        "release_notes": "Initial dual sensor support.",
        "is_mandatory": True, "file_size_bytes": 765952,
        "download_url": "https://example.com/fw-1.3.0.bin",
        "seeded": True, "created_at": utc(60),
    }
    rows["device_commands"]["seed-cmd-1"] = {
        "id": "seed-cmd-1", "device_id": "FG-ESP32-0001",
        "command": "flow_meter_calibrate",
        "payload": {"calibration_factor": 7.5, "offset": 0},
        "status": "complete", "delivered_at": utc(1), "acked_at": utc(1),
        "seeded": True, "created_at": utc(1, 1),
    }
    rows["device_commands"]["seed-cmd-2"] = {
        "id": "seed-cmd-2", "device_id": "FG-ESP32-0001",
        "command": "camera_capture", "payload": {"reason": "session_end"},
        "status": "complete", "delivered_at": utc(0, 3), "acked_at": utc(0, 3),
        "seeded": True, "created_at": utc(0, 4),
    }
    rows["device_commands"]["seed-cmd-3"] = {
        "id": "seed-cmd-3", "device_id": "FG-CAM-0002",
        "command": "camera_capture", "payload": {"reason": "tamper"},
        "status": "failed", "error": "device unreachable",
        "acked_at": utc(3), "seeded": True, "created_at": utc(3, 1),
    }
    for i in range(6):
        rows["telemetry_logs"][f"seed-tel-{i}"] = {
            "device_id": "FG-ESP32-0001", "nozzle_id": "seed-nz-1",
            "flow_rate": round(38.0 + i * 1.4, 1),
            "total_dispensed": round(4.2 + i * 2.6, 2),
            "tamper_detected": False, "temperature": 36 + i,
            "signal_strength": -52 - i,
            "seeded": True, "received_at": utc(0, 5 - i * 0.02),
        }

    rows["rules"] = rules
    rows["demo_device_key"] = demo_key
    return rows


# ── Writers ───────────────────────────────────────────────────────────────────

def write_all(db: firestore.Client, rows: dict, dry_run: bool) -> None:
    collections = [
        ("flagging_rules", rows["rules"], ["rules"]),
        ("users", rows["users"], []),
        ("stations", rows["stations"], []),
        ("nozzles", rows["nozzles"], []),
        ("prices", rows["prices"], []),
        ("sessions", rows["sessions"], []),
        ("transactions", rows["transactions"], []),
        ("fraud_alerts", rows["fraud_alerts"], []),
        ("flags", rows["flags"], []),
        ("blacklist", rows["blacklist"], []),
        ("complaints", rows["complaints"], []),
        ("iot_devices", rows["iot_devices"], []),
        ("iot_firmware", rows["iot_firmware"], []),
        ("device_commands", rows["device_commands"], []),
        ("telemetry_logs", rows["telemetry_logs"], []),
    ]
    for name, docs, _ in collections:
        if name == "flagging_rules":
            print(f"  flagging_rules: 1 doc")
            if not dry_run:
                db.collection("flagging_rules").document("default").set(docs)
            continue
        print(f"  {name}: {len(docs)} docs")
        if dry_run:
            continue
        for doc_id, data in docs.items():
            db.collection(name).document(doc_id).set(data)


def purge_all(db: firestore.Client, dry_run: bool) -> None:
    rows = build_rows()
    groups = [
        ("users", list(rows["users"].keys())),
        ("stations", list(rows["stations"].keys())),
        ("nozzles", list(rows["nozzles"].keys())),
        ("prices", list(rows["prices"].keys())),
        ("sessions", list(rows["sessions"].keys())),
        ("transactions", list(rows["transactions"].keys())),
        ("fraud_alerts", list(rows["fraud_alerts"].keys())),
        ("flags", list(rows["flags"].keys())),
        ("blacklist", list(rows["blacklist"].keys())),
        ("complaints", list(rows["complaints"].keys())),
        ("iot_devices", list(rows["iot_devices"].keys())),
        ("iot_firmware", list(rows["iot_firmware"].keys())),
        ("device_commands", list(rows["device_commands"].keys())),
        ("telemetry_logs", list(rows["telemetry_logs"].keys())),
    ]
    print("Purging seeded flagging scenario...")
    for name, ids in groups:
        print(f"  {name}: {len(ids)} docs")
        if dry_run:
            continue
        for doc_id in ids:
            db.collection(name).document(doc_id).delete()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the flagging and blacklisting scenario")
    parser.add_argument("--key", help="Path to the Firebase service account key")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"Firebase project id (default {DEFAULT_PROJECT})")
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing")
    parser.add_argument("--purge", action="store_true", help="Delete every seeded document, then exit")
    args = parser.parse_args()

    db = connect(find_key(args.key), args.project)

    if args.purge:
        purge_all(db, args.dry_run)
        print("Purge complete." if not args.dry_run else "Dry run, nothing deleted.")
        return 0

    rows = build_rows()
    total = sum(len(v) for k, v in rows.items() if isinstance(v, dict))
    print(f"Seeding flagging scenario into {args.project} "
          f"({'dry run' if args.dry_run else 'live'}) — {total} docs")
    write_all(db, rows, args.dry_run)

    print()
    print("Scenario seeded:")
    print("  Attendants  Imran Shah (level 1) / Zeeshan Haider (level 2) / Naveed Iqbal (level 3)")
    print("  Station     Blue Area Fuel Station blacklisted, badge shows in the app")
    print("  Customer    Hamza Tariq blacklisted for repeated invalid claims")
    print("  Payment     card on the chargeback watchlist")
    print("  IoT         FG-ESP32-0001 device + calibration/OTA commands + telemetry")
    print()
    print("Panel login:")
    print(f"  {PANEL_ADMIN_EMAIL} / {PANEL_ADMIN_PASSWORD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
