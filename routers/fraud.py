from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, get_db
from models.fraud import (
    BlacklistEntry,
    BlacklistResponse,
    FraudAlertResolve,
    FraudFlagRequest,
    FraudStats,
)
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
    return {"message": "Alert updated"}


@router.post("/analyze", status_code=status.HTTP_200_OK)
async def analyze_fraud(transaction_id: str, _: dict = Depends(require_admin)):
    db     = get_db()
    tx_doc = db.collection(Collections.TRANSACTIONS).document(transaction_id).get()
    if not tx_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    alerts = analyze_transaction_for_fraud(db, tx_doc.to_dict())
    return {"transaction_id": transaction_id, "alerts_generated": len(alerts), "alerts": alerts}


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
    db = get_db()
    existing = (
        db.collection(Collections.BLACKLIST)
        .where("entity_id", "==", payload.entity_id)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Entity is already blacklisted",
        )

    entry_id = str(uuid.uuid4())
    data = {
        "id": entry_id,
        **payload.model_dump(),
        "entity_type": payload.entity_type.value,
        "flagged_by": payload.flagged_by or current_user["uid"],
        "created_at": datetime.now(timezone.utc),
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
    return {"alert_id": alert_id, "message": "Transaction flagged"}
