from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.cache import TAG_DASHBOARD, TAG_FRAUD, invalidate
from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, get_db
from models.fraud import (
    BlacklistEntry,
    BlacklistResponse,
    FraudAlertResolve,
    FraudFlagRequest,
    FraudStats,
)
from services import flagging_service
from services.fraud_service import analyze_transaction_for_fraud

router = APIRouter()


@router.get("/alerts")
async def list_fraud_alerts(
    alert_type: str | None = Query(None),
    alert_status: str | None = Query(None, alias="status"),
    severity: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
):
    db    = get_db()
    query = db.collection(Collections.FRAUD_ALERTS)
    if alert_type:
        query = query.where("alert_type", "==", alert_type)
    if alert_status:
        query = query.where("status", "==", alert_status)
    if severity:
        query = query.where("severity", "==", severity)

    docs  = query.order_by("created_at", direction="DESCENDING").get()
    items = [{"id": d.id, **d.to_dict()} for d in docs]
    return {"total": len(items), "items": items[offset: offset + limit]}


@router.get("/alerts/{alert_id}")
async def get_fraud_alert(alert_id: str, _: dict = Depends(require_admin)):
    db  = get_db()
    doc = db.collection(Collections.FRAUD_ALERTS).document(alert_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    return {"id": doc.id, **doc.to_dict()}


@router.post("/alerts/{alert_id}/resolve")
async def resolve_fraud_alert(
    alert_id: str,
    payload: FraudAlertResolve,
    current_user: dict = Depends(require_admin),
):
    db  = get_db()
    ref = db.collection(Collections.FRAUD_ALERTS).document(alert_id)
    if not ref.get().exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    ref.update({
        "status": payload.status.value,
        "resolution_note": payload.resolution_note,
        "resolved_by": current_user["uid"],
        "resolved_at": datetime.now(timezone.utc),
    })
    invalidate(TAG_FRAUD, TAG_DASHBOARD)
    return {"message": "Alert updated"}


@router.post("/analyze", status_code=status.HTTP_200_OK)
async def analyze_fraud(transaction_id: str, _: dict = Depends(require_admin)):
    db     = get_db()
    tx_doc = db.collection(Collections.TRANSACTIONS).document(transaction_id).get()
    if not tx_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    tx     = tx_doc.to_dict()
    alerts = analyze_transaction_for_fraud(db, tx)

    # Check outcomes that point at the attendant or the station also flow into
    # the flagging engine. record_flag deduplicates on transaction + trigger,
    # so re-running the analysis cannot inflate an entity's count.
    flagging: dict = {"flags": [], "escalated": False, "level": None}
    trigger_map = {
        "price_mismatch":  ("price_mismatch", "high"),
        "unusual_volume":  ("under_dispensing_pattern", "high"),
        "tamper_detected": ("tamper_detected", "critical"),
    }
    for alert in alerts:
        trigger = trigger_map.get(alert["alert_type"])
        if not trigger:
            continue
        res = flagging_service.flag_transaction_entities(
            db, tx,
            trigger_type=trigger[0], severity=trigger[1],
            description=alert["description"],
        )
        flagging["flags"].extend(res["flags"])
        if res["escalated"] and not flagging["escalated"]:
            flagging.update({"escalated": True, "level": res["level"], "blacklist": res["blacklist"]})

    return {
        "transaction_id": transaction_id,
        "alerts_generated": len(alerts),
        "alerts": alerts,
        "flagging": {
            "flags_recorded": len(flagging["flags"]),
            "escalated": flagging["escalated"],
            "level": flagging["level"],
            "blacklist": flagging.get("blacklist"),
        },
    }


@router.get("/patterns")
async def get_fraud_patterns(_: dict = Depends(require_admin)):
    db   = get_db()
    docs = (
        db.collection(Collections.FRAUD_ALERTS)
        .order_by("created_at", direction="DESCENDING")
        .limit(500)
        .get()
    )
    patterns: dict[str, int] = {}
    for d in docs:
        nozzle = d.to_dict().get("nozzle_id", "unknown")
        patterns[nozzle] = patterns.get(nozzle, 0) + 1

    sorted_patterns = sorted(patterns.items(), key=lambda x: x[1], reverse=True)
    return {"patterns": [{"nozzle_id": k, "alert_count": v} for k, v in sorted_patterns]}

# asd
@router.get("/stats", response_model=FraudStats)
async def fraud_stats(_: dict = Depends(require_admin)):
    db         = get_db()
    docs       = db.collection(Collections.FRAUD_ALERTS).get()
    all_alerts = [d.to_dict() for d in docs]

    open_count = sum(1 for a in all_alerts if a.get("status") == "open")
    by_type: dict[str, int]     = {}
    by_severity: dict[str, int] = {}

    for a in all_alerts:
        t = a.get("alert_type", "unknown")
        s = a.get("severity", "unknown")
        by_type[t]     = by_type.get(t, 0) + 1
        by_severity[s] = by_severity.get(s, 0) + 1

    return FraudStats(
        total_alerts=len(all_alerts),
        open_alerts=open_count,
        resolved_this_week=0,
        by_type=by_type,
        by_severity=by_severity,
    )


@router.get("/blacklist")
async def list_blacklist(
    entity_type: str | None = Query(None),
    _: dict = Depends(require_admin),
):
    db    = get_db()
    query = db.collection(Collections.BLACKLIST)
    if entity_type:
        query = query.where("entity_type", "==", entity_type)
    docs = query.get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.post("/blacklist", response_model=BlacklistResponse, status_code=status.HTTP_201_CREATED)
async def add_to_blacklist(
    payload: BlacklistEntry,
    current_user: dict = Depends(require_admin),
):
    """
    Legacy blacklist write kept for API compatibility. Entries are created in
    the full flagging schema (level 1, active) so the escalation engine and
    the station risk lookups treat them identically.
    """
    db = get_db()
    existing = (
        db.collection(Collections.BLACKLIST)
        .where("entity_type", "==", payload.entity_type.value)
        .where("entity_id", "==", payload.entity_id)
        .where("status", "in", ["active", "under_review"])
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Entity is already blacklisted",
        )

    now = datetime.now(timezone.utc)
    entry_id = str(uuid.uuid4())
    data = {
        "id": entry_id,
        **payload.model_dump(),
        "entity_type": payload.entity_type.value,
        "level": 1,
        "status": "active",
        "restrictions": flagging_service.derive_restrictions(payload.entity_type.value, 1),
        "flags_count": 0,
        "window_days": flagging_service.get_rules(db).flag_window_days,
        "auto_escalated": False,
        "entity_name": flagging_service._display_name(db, payload.entity_type.value, payload.entity_id),
        "flagged_by": payload.flagged_by or current_user["uid"],
        "removed_by": None,
        "removed_at": None,
        "removal_note": None,
        "created_at": now,
        "updated_at": now,
    }
    db.collection(Collections.BLACKLIST).document(entry_id).set(data)
    return BlacklistResponse(**data)


@router.delete("/blacklist/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_blacklist(entry_id: str, _: dict = Depends(require_admin)):
    db  = get_db()
    ref = db.collection(Collections.BLACKLIST).document(entry_id)
    if not ref.get().exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blacklist entry not found")
    ref.delete()


@router.post("/flag", status_code=status.HTTP_201_CREATED)
async def flag_transaction(
    payload: FraudFlagRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Flag a transaction from the mobile app. The fraud alert always lands in
    the alerts feed; the entity flagging engine additionally fires when the
    reported discrepancy crosses the configured percentage threshold.
    """
    db     = get_db()
    tx_doc = db.collection(Collections.TRANSACTIONS).document(payload.transaction_id).get()
    if not tx_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    tx       = tx_doc.to_dict()
    alert_id = str(uuid.uuid4())
    data = {
        "id": alert_id,
        "alert_type": "manual_flag",
        "transaction_id": payload.transaction_id,
        "nozzle_id": tx.get("nozzle_id"),
        "user_id": tx.get("user_id"),
        "description": payload.reason,
        "severity": payload.severity.value,
        "status": "open",
        "resolved_by": None,
        "resolution_note": None,
        "flagged_by": current_user["uid"],
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.FRAUD_ALERTS).document(alert_id).set(data)
    db.collection(Collections.TRANSACTIONS).document(payload.transaction_id).update(
        {"is_flagged": True}
    )
    invalidate(TAG_FRAUD, TAG_DASHBOARD)

    # Discrepancy percentage from explicit reading, or derived from the pair.
    discrepancy_percent = payload.discrepancy_percent
    if discrepancy_percent is None and payload.flowmeter_litres:
        discrepancy_percent = round(
            (payload.dispenser_litres or tx.get("litres_dispensed") or 0)
            / payload.flowmeter_litres * 100 - 100, 2
        )

    flagging_result = None
    rules = flagging_service.get_rules(db)
    if discrepancy_percent is not None and discrepancy_percent >= rules.discrepancy_threshold_percent:
        res = flagging_service.flag_transaction_entities(
            db, tx,
            trigger_type="flow_discrepancy",
            severity=payload.severity.value,
            description=payload.reason,
            source="automatic",
            discrepancy_percent=discrepancy_percent,
            created_by=current_user["uid"],
        )
        flagging_result = {
            "flags_recorded": len(res["flags"]),
            "escalated": res["escalated"],
            "level": res["level"],
            "blacklist": res["blacklist"],
        }

    return {
        "alert_id": alert_id,
        "message": "Transaction flagged",
        "flagging": flagging_result,
    }
