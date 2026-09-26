from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.firebase import Collections, ensure_utc, get_db
from models.flagging import FlaggingRules, FraudRiskStatus

logger = logging.getLogger(__name__)

LEVEL_LABELS = {1: "Level 1 Yellow", 2: "Level 2 Orange", 3: "Level 3 Red"}

_DEFAULT_RULES = FlaggingRules()

_FLAGGING_RULES_DOC = "default"

# Threshold lookup per entity type — payment methods use their own knob.
_THRESHOLD_KEY = {
    "employee":       "employee_flags_to_blacklist",
    "station":        "station_flags_to_blacklist",
    "customer":       "customer_flags_to_blacklist",
    "payment_method": "payment_flags_to_flag",
}


# ── Rules ─────────────────────────────────────────────────────────────────────

def get_rules(db=None) -> FlaggingRules:
    """
    Load thresholds from the flagging_rules config document. Every escalation
    decision flows through this — nothing is hardcoded at call sites. Falls
    back to safe defaults when the doc has not been seeded yet.
    """
    db = db or get_db()
    try:
        doc = db.collection(Collections.FLAGGING_RULES).document(_FLAGGING_RULES_DOC).get()
        if doc.exists:
            return FlaggingRules(**{**_DEFAULT_RULES.model_dump(exclude_none=True),
                                    **{k: v for k, v in doc.to_dict().items() if v is not None}})
    except Exception:
        logger.exception("Failed to load flagging rules — using defaults")
    return _DEFAULT_RULES


def save_rules(db, update: dict, updated_by: str) -> FlaggingRules:
    current = get_rules(db).model_dump(exclude_none=True)
    current.update({k: v for k, v in update.items() if v is not None})
    current["updated_by"] = updated_by
    current["updated_at"] = datetime.now(timezone.utc)
    db.collection(Collections.FLAGGING_RULES).document(_FLAGGING_RULES_DOC).set(current)
    return FlaggingRules(**current)


# ── Entity resolution ─────────────────────────────────────────────────────────

def resolve_station_id(db, transaction: dict) -> Optional[str]:
    station_id = transaction.get("station_id")
    if station_id:
        return station_id
    nozzle_id = transaction.get("nozzle_id")
    if not nozzle_id:
        return None
    try:
        nozzle = db.collection(Collections.NOZZLES).document(nozzle_id).get()
        if nozzle.exists:
            return nozzle.to_dict().get("station_id")
    except Exception:
        logger.exception("Station resolution failed for nozzle %s", nozzle_id)
    return None


def _display_name(db, entity_type: str, entity_id: str) -> Optional[str]:
    try:
        if entity_type == "employee":
            doc = db.collection(Collections.USERS).document(entity_id).get()
            if doc.exists:
                return doc.to_dict().get("full_name")
        elif entity_type == "customer":
            doc = db.collection(Collections.USERS).document(entity_id).get()
            if doc.exists:
                return doc.to_dict().get("full_name")
        elif entity_type == "station":
            doc = db.collection(Collections.STATIONS).document(entity_id).get()
            if doc.exists:
                return doc.to_dict().get("name")
        elif entity_type == "payment_method":
            return entity_id.replace("_", " ").title()
    except Exception:
        logger.exception("Name lookup failed for %s/%s", entity_type, entity_id)
    return None


# ── Restrictions ──────────────────────────────────────────────────────────────

def derive_restrictions(entity_type: str, level: int) -> Dict[str, bool]:
    """
    Restriction ladder per entity type, matching the client's blacklist action
    matrix. Levels are cumulative — a level 2 attendant carries level 1 rules.
    """
    if entity_type == "employee":
        r: Dict[str, bool] = {
            "supervisor_approval_required": True,
            "additional_monitoring": True,
            "daily_performance_review": True,
        }
        if level >= 2:
            r |= {"supervised_hours_only": True,
                  "manual_transaction_verification": True,
                  "restricted_nozzle_access": True}
        if level >= 3:
            r |= {"suspended_pending_investigation": True,
                  "management_investigation": True}
        return r

    if entity_type == "station":
        return {
            "warning_badge": True,
            "reduced_visibility": True,
            "excluded_from_recommendations": True,
            "customer_visit_warning": True,
        }

    if entity_type == "customer":
        r = {"manual_dispute_review": True}
        if level >= 2:
            r |= {"evidence_required_for_complaints": True}
        if level >= 3:
            r |= {"verification_deposit_required": True}
        return r

    # payment_method
    r = {"manual_review_transactions": True, "chargeback_watchlist": True}
    if level >= 3:
        r |= {"flag_new_transactions": True}
    return r


# ── Lookups ───────────────────────────────────────────────────────────────────

def get_active_blacklist_entry(db, entity_type: str, entity_id: str) -> Optional[dict]:
    entries = (
        db.collection(Collections.BLACKLIST)
        .where("entity_type", "==", entity_type)
        .where("entity_id", "==", entity_id)
        .where("status", "in", ["active", "under_review"])
        .limit(1)
        .get()
    )
    return {**entries[0].to_dict(), "id": entries[0].id} if entries else None


def get_fraud_risk_map(db, station_ids: List[str]) -> Dict[str, FraudRiskStatus]:
    """Batch lookup powering station badges — one query for the whole page."""
    risk: Dict[str, FraudRiskStatus] = {}
    if not station_ids:
        return risk
    try:
        entries = (
            db.collection(Collections.BLACKLIST)
            .where("entity_type", "==", "station")
            .where("status", "==", "active")
            .get()
        )
        for e in entries:
            d = e.to_dict()
            sid = d.get("entity_id")
            if sid in station_ids and d.get("level"):
                risk[sid] = FraudRiskStatus.flagged(int(d["level"]))
    except Exception:
        logger.exception("Fraud risk map lookup failed")
    return risk


def count_flags_in_window(db, entity_type: str, entity_id: str, window_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    flags = (
        db.collection(Collections.FLAGS)
        .where("entity_type", "==", entity_type)
        .where("entity_id", "==", entity_id)
        .where("status", "==", "active")
        .where("created_at", ">=", cutoff)
        .get()
    )
    return len(flags)


# ── Escalation core ───────────────────────────────────────────────────────────

def record_flag(
    db,
    *,
    entity_type: str,
    entity_id: str,
    trigger_type: str,
    source: str,
    description: str,
    severity: str = "medium",
    entity_name: Optional[str] = None,
    transaction_id: Optional[str] = None,
    nozzle_id: Optional[str] = None,
    station_id: Optional[str] = None,
    discrepancy_percent: Optional[float] = None,
    created_by: str = "system",
) -> Dict[str, Any]:
    """
    Record one flag and run the escalation ladder:

        flag → count flags in window → threshold reached →
        blacklist create or upgrade → restrictions → alert + notification

    Returns {"flag": …, "blacklist": …|None, "escalated": bool, "level": int|None}.
    """
    now = datetime.now(timezone.utc)
    if entity_name is None:
        entity_name = _display_name(db, entity_type, entity_id)

    # Automatic sources re-run on every analysis — the same transaction must
    # never produce a second flag for the same entity and trigger.
    if transaction_id and source != "manual":
        duplicate = (
            db.collection(Collections.FLAGS)
            .where("entity_type", "==", entity_type)
            .where("entity_id", "==", entity_id)
            .where("transaction_id", "==", transaction_id)
            .where("trigger_type", "==", trigger_type)
            .where("status", "==", "active")
            .limit(1)
            .get()
        )
        if duplicate:
            return {"flag": duplicate[0].to_dict(), "blacklist": None,
                    "escalated": False, "level": None}

    flag_id = str(uuid.uuid4())
    flag = {
        "id": flag_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "source": source,
        "trigger_type": trigger_type,
        "severity": severity,
        "description": description,
        "status": "active",
        "transaction_id": transaction_id,
        "nozzle_id": nozzle_id,
        "station_id": station_id,
        "discrepancy_percent": discrepancy_percent,
        "created_by": created_by,
        "created_at": now,
        "dismissed_by": None,
        "dismissed_at": None,
        "dismissal_note": None,
    }
    db.collection(Collections.FLAGS).document(flag_id).set(flag)

    result: Dict[str, Any] = {"flag": flag, "blacklist": None, "escalated": False, "level": None}
    try:
        result.update(_evaluate_escalation(
            db, entity_type=entity_type, entity_id=entity_id, entity_name=entity_name,
            trigger_flag=flag,
        ))
    except Exception:
        logger.exception("Escalation evaluation failed for %s/%s", entity_type, entity_id)
    return result


def _evaluate_escalation(
    db,
    *,
    entity_type: str,
    entity_id: str,
    entity_name: Optional[str],
    trigger_flag: dict,
) -> Dict[str, Any]:
    rules = get_rules(db)
    flags_in_window = count_flags_in_window(db, entity_type, entity_id, rules.flag_window_days)

    threshold_key = _THRESHOLD_KEY.get(entity_type)
    if not threshold_key:
        return {"escalated": False, "level": None, "blacklist": None, "flags_in_window": flags_in_window}

    threshold = getattr(rules, threshold_key)
    out: Dict[str, Any] = {"escalated": False, "level": None, "blacklist": None,
                           "flags_in_window": flags_in_window}

    if not rules.auto_blacklist_enabled or flags_in_window < threshold:
        return out

    if flags_in_window >= rules.level_3_flags:
        level = 3
    elif flags_in_window >= rules.level_2_flags:
        level = 2
    else:
        level = 1

    existing = get_active_blacklist_entry(db, entity_type, entity_id)
    now = datetime.now(timezone.utc)

    if existing and existing.get("status") == "under_review":
        # Under review means a human is deciding — the engine never overrides it.
        out["blacklist"] = existing
        out["level"] = existing.get("level")
        return out

    if existing:
        entry_ref = db.collection(Collections.BLACKLIST).document(existing["id"])
        if int(existing.get("level") or 1) >= level:
            entry_ref.update({
                "flags_count": flags_in_window,
                "updated_at": now,
            })
            existing["flags_count"] = flags_in_window
            out.update({"escalated": False, "level": existing.get("level"), "blacklist": existing})
            return out

        # Upgrade only — the engine tightens but never loosens.
        previous_level = int(existing.get("level") or 1)
        entry_ref.update({
            "level": level,
            "reason": trigger_flag["description"],
            "flags_count": flags_in_window,
            "window_days": rules.flag_window_days,
            "restrictions": derive_restrictions(entity_type, level),
            "updated_at": now,
        })
        escalated_entry = {**existing, "level": level, "flags_count": flags_in_window}
        _record_escalation_event(db, escalated_entry, trigger_flag, flags_in_window,
                                 rules.flag_window_days, upgraded_from=previous_level)
        out.update({"escalated": True, "level": level, "blacklist": escalated_entry})
        return out

    entry_id = str(uuid.uuid4())
    entry = {
        "id": entry_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "level": level,
        "status": "active",
        "reason": trigger_flag["description"],
        "restrictions": derive_restrictions(entity_type, level),
        "flags_count": flags_in_window,
        "window_days": rules.flag_window_days,
        "auto_escalated": True,
        "flagged_by": "system",
        "removed_by": None,
        "removed_at": None,
        "removal_note": None,
        "created_at": now,
        "updated_at": now,
    }
    db.collection(Collections.BLACKLIST).document(entry_id).set(entry)
    _record_escalation_event(db, entry, trigger_flag, flags_in_window, rules.flag_window_days)
    out.update({"escalated": True, "level": level, "blacklist": entry})
    return out


def _record_escalation_event(
    db,
    entry: dict,
    trigger_flag: dict,
    flags_count: int,
    window_days: int,
    upgraded_from: Optional[int] = None,
) -> None:
    """
    Escalations surface in three places at once: the fraud alerts feed the
    admin panel already watches, the notifications collection for future app
    delivery, and the response payload of whichever endpoint triggered it.
    """
    now = datetime.now(timezone.utc)
    entity_type = entry["entity_type"]
    name = entry.get("entity_name") or entry["entity_id"]
    action = "upgraded to" if upgraded_from else "moved to"

    alert_id = str(uuid.uuid4())
    db.collection(Collections.FRAUD_ALERTS).document(alert_id).set({
        "id": alert_id,
        "alert_type": "blacklist_escalation",
        "transaction_id": trigger_flag.get("transaction_id"),
        "nozzle_id": trigger_flag.get("nozzle_id"),
        "user_id": entry["entity_id"] if entity_type == "customer" else None,
        "description": (
            f"{_entity_label(entity_type)} {name} {action} blacklist "
            f"{LEVEL_LABELS[entry['level']]} after {flags_count} flags "
            f"in {window_days} days."
        ),
        "severity": {1: "medium", 2: "high", 3: "critical"}[entry["level"]],
        "status": "open",
        "blacklist_id": entry["id"],
        "resolved_by": None,
        "resolution_note": None,
        "flagged_by": "system",
        "created_at": now,
    })

    notification = {
        "id": str(uuid.uuid4()),
        "type": "blacklist_escalation",
        "audience": "user" if entity_type == "customer" else "admin",
        "user_id": entry["entity_id"] if entity_type == "customer" else None,
        "title": "Blacklist updated",
        "body": (
            f"{_entity_label(entity_type)} {name} {action} "
            f"{LEVEL_LABELS[entry['level']]}."
        ),
        "entity_type": entity_type,
        "entity_id": entry["entity_id"],
        "level": entry["level"],
        "is_read": False,
        "created_at": now,
    }
    db.collection(Collections.NOTIFICATIONS).add(notification)


def _entity_label(entity_type: str) -> str:
    return {
        "employee": "Attendant", "station": "Station",
        "customer": "Customer", "payment_method": "Payment method",
    }.get(entity_type, entity_type.replace("_", " ").title())


# ── Convenience wrappers used by other routers ────────────────────────────────

def flag_transaction_entities(
    db,
    transaction: dict,
    *,
    trigger_type: str,
    description: str,
    severity: str,
    source: str = "automatic",
    discrepancy_percent: Optional[float] = None,
    targets: Optional[List[str]] = None,
    created_by: str = "system",
) -> Dict[str, Any]:
    """
    Flag the entities tied to a transaction. Defaults to attendant + station,
    the two parties positioned to manipulate a reading.
    """
    targets = targets or ["employee", "station"]
    station_id = resolve_station_id(db, transaction)
    employee_id = transaction.get("employee_id")
    outcome: Dict[str, Any] = {"flags": [], "blacklist": None, "escalated": False, "level": None}

    for target in targets:
        if target == "employee" and employee_id:
            res = record_flag(
                db, entity_type="employee", entity_id=employee_id,
                trigger_type=trigger_type, source=source, description=description,
                severity=severity, transaction_id=transaction.get("id"),
                nozzle_id=transaction.get("nozzle_id"), station_id=station_id,
                discrepancy_percent=discrepancy_percent, created_by=created_by,
            )
        elif target == "station" and station_id:
            res = record_flag(
                db, entity_type="station", entity_id=station_id,
                trigger_type=trigger_type, source=source, description=description,
                severity=severity, transaction_id=transaction.get("id"),
                nozzle_id=transaction.get("nozzle_id"), station_id=station_id,
                discrepancy_percent=discrepancy_percent, created_by=created_by,
            )
        else:
            continue
        outcome["flags"].append(res["flag"])
        if res["escalated"] and not outcome["blacklist"]:
            outcome.update({"blacklist": res["blacklist"], "escalated": True, "level": res["level"]})
    return outcome


def enforce_transaction_blacklists(db, *, employee_id: Optional[str],
                                   customer_id: Optional[str],
                                   payment_method: Optional[str]) -> Dict[str, Any]:
    """
    Active blacklist checks at transaction time.

    Returns enforcement stamps for the transaction document and, for level 3
    attendants, a rejection reason — a suspended attendant cannot dispense.
    """
    stamps: Dict[str, Any] = {}

    if employee_id:
        entry = get_active_blacklist_entry(db, "employee", employee_id)
        if entry and entry.get("status") == "active":
            level = int(entry.get("level") or 1)
            stamps["employee_blacklist_level"] = level
            if level >= 3:
                return {"rejected": "Attendant is suspended pending investigation.", "stamps": stamps}
            if level == 1:
                stamps["supervisor_approval_required"] = True
            if level >= 2:
                stamps["manual_verification_required"] = True

    if customer_id:
        entry = get_active_blacklist_entry(db, "customer", customer_id)
        if entry and entry.get("status") == "active":
            stamps["customer_blacklist_level"] = int(entry.get("level") or 1)
            if int(entry.get("level") or 1) >= 2:
                stamps["manual_dispute_review"] = True

    if payment_method:
        entry = get_active_blacklist_entry(db, "payment_method", str(payment_method))
        if entry and entry.get("status") == "active":
            stamps["payment_review_required"] = True

    return {"rejected": None, "stamps": stamps}
