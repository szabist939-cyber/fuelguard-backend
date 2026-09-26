from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, ensure_utc, get_db
from models.flagging import (
    BlacklistResponse,
    BlacklistStatus,
    BlacklistUpdateRequest,
    DisputeRequest,
    EntityHistoryResponse,
    FlagDismissRequest,
    FlagResponse,
    FlaggingRules,
    FlaggingRulesUpdate,
    FlaggingStats,
    FlagEntityType,
    ManualBlacklistRequest,
    ManualFlagRequest,
)
from services import flagging_service
from services.flagging_service import (
    get_active_blacklist_entry,
    get_rules,
    record_flag,
    save_rules,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_ENTITY_TYPES = {t.value for t in FlagEntityType}


# ── Rules ─────────────────────────────────────────────────────────────────────

@router.get("/rules", response_model=FlaggingRules)
async def read_rules(_: dict = Depends(require_admin)):
    return get_rules(get_db())


@router.put("/rules", response_model=FlaggingRules)
async def update_rules(payload: FlaggingRulesUpdate, current_user: dict = Depends(require_admin)):
    db = get_db()
    update = payload.model_dump(exclude_none=True)

    rules = {**get_rules(db).model_dump(exclude_none=True), **update}
    if rules["level_2_flags"] >= rules["level_3_flags"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Level 3 threshold must be higher than the level 2 threshold.",
        )

    return save_rules(db, update, updated_by=current_user["uid"])


# ── Flags ─────────────────────────────────────────────────────────────────────

@router.get("/flags")
async def list_flags(
    entity_type: str | None = Query(None),
    entity_id: str | None = Query(None),
    flag_status: str | None = Query(None, alias="status"),
    source: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
):
    db = get_db()
    query = db.collection(Collections.FLAGS)
    if entity_type:
        query = query.where("entity_type", "==", entity_type)
    if entity_id:
        query = query.where("entity_id", "==", entity_id)
    if flag_status:
        query = query.where("status", "==", flag_status)
    if source:
        query = query.where("source", "==", source)

    docs  = query.get()
    items = sorted(
        ({**d.to_dict(), "id": d.id} for d in docs),
        key=lambda f: ensure_utc(f.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {"total": len(items), "items": items[offset: offset + limit]}


@router.post("/flags", status_code=status.HTTP_201_CREATED)
async def create_manual_flag(payload: ManualFlagRequest, current_user: dict = Depends(require_admin)):
    db = get_db()

    transaction = None
    if payload.transaction_id:
        tx_doc = db.collection(Collections.TRANSACTIONS).document(payload.transaction_id).get()
        if not tx_doc.exists:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
        transaction = tx_doc.to_dict()

    entity_id = payload.entity_id
    if payload.entity_type == FlagEntityType.CUSTOMER and transaction:
        entity_id = transaction.get("user_id") or entity_id
    elif payload.entity_type == FlagEntityType.EMPLOYEE and transaction:
        entity_id = transaction.get("employee_id") or entity_id
    elif payload.entity_type == FlagEntityType.PAYMENT_METHOD and transaction:
        entity_id = str(transaction.get("payment_method") or entity_id)

    if not entity_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Entity could not be resolved for the supplied entity type.",
        )

    result = record_flag(
        db,
        entity_type=payload.entity_type.value,
        entity_id=entity_id,
        trigger_type=payload.trigger_type.value,
        source="manual",
        description=payload.description,
        severity=payload.severity,
        transaction_id=payload.transaction_id,
        nozzle_id=payload.nozzle_id or (transaction or {}).get("nozzle_id"),
        station_id=payload.station_id or flagging_service.resolve_station_id(db, transaction or {}),
        created_by=current_user["uid"],
    )
    return {
        "flag": FlagResponse(**result["flag"]).model_dump(mode="json"),
        "blacklist": BlacklistResponse(**result["blacklist"]).model_dump(mode="json")
                     if result["blacklist"] else None,
        "escalated": result["escalated"],
        "level": result["level"],
    }


@router.patch("/flags/{flag_id}/dismiss")
async def dismiss_flag(flag_id: str, payload: FlagDismissRequest,
                       current_user: dict = Depends(require_admin)):
    db = get_db()
    ref = db.collection(Collections.FLAGS).document(flag_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flag not found")
    if doc.to_dict().get("status") == "dismissed":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Flag is already dismissed")

    ref.update({
        "status": "dismissed",
        "dismissed_by": current_user["uid"],
        "dismissed_at": datetime.now(timezone.utc),
        "dismissal_note": payload.note,
    })
    return {"message": "Flag dismissed"}


# ── Blacklist ─────────────────────────────────────────────────────────────────

@router.get("/blacklist", response_model=list[BlacklistResponse])
async def list_blacklist(
    entity_type: str | None = Query(None),
    level: int | None = Query(None, ge=1, le=3),
    entry_status: str | None = Query(None, alias="status"),
    _: dict = Depends(require_admin),
):
    db = get_db()
    query = db.collection(Collections.BLACKLIST)
    if entity_type:
        query = query.where("entity_type", "==", entity_type)
    if level:
        query = query.where("level", "==", level)
    if entry_status:
        query = query.where("status", "==", entry_status)

    entries = sorted(
        ({**d.to_dict(), "id": d.id} for d in query.get()),
        key=lambda b: ensure_utc(b.get("updated_at")) or ensure_utc(b.get("created_at"))
                      or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return [BlacklistResponse(**entry) for entry in entries]


@router.post("/blacklist", response_model=BlacklistResponse, status_code=status.HTTP_201_CREATED)
async def add_to_blacklist(payload: ManualBlacklistRequest,
                           current_user: dict = Depends(require_admin)):
    """Manual blacklisting — an admin decision that skips the flag ladder."""
    db = get_db()

    if get_active_blacklist_entry(db, payload.entity_type.value, payload.entity_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This entity is already on the blacklist.",
        )

    now = datetime.now(timezone.utc)
    rules = get_rules(db)
    entry_id = str(uuid.uuid4())
    entry = {
        "id": entry_id,
        "entity_type": payload.entity_type.value,
        "entity_id": payload.entity_id,
        "entity_name": flagging_service._display_name(db, payload.entity_type.value, payload.entity_id),
        "level": payload.level,
        "status": "active",
        "reason": payload.reason,
        "restrictions": flagging_service.derive_restrictions(payload.entity_type.value, payload.level),
        "flags_count": flagging_service.count_flags_in_window(
            db, payload.entity_type.value, payload.entity_id, rules.flag_window_days),
        "window_days": rules.flag_window_days,
        "auto_escalated": False,
        "flagged_by": current_user["uid"],
        "removed_by": None,
        "removed_at": None,
        "removal_note": None,
        "created_at": now,
        "updated_at": now,
    }
    db.collection(Collections.BLACKLIST).document(entry_id).set(entry)
    return BlacklistResponse(**entry)


@router.patch("/blacklist/{entry_id}", response_model=BlacklistResponse)
async def update_blacklist_entry(entry_id: str, payload: BlacklistUpdateRequest,
                                 current_user: dict = Depends(require_admin)):
    db = get_db()
    ref = db.collection(Collections.BLACKLIST).document(entry_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blacklist entry not found")

    entry  = doc.to_dict()
    update: dict = {"updated_at": datetime.now(timezone.utc)}

    if payload.level is not None and payload.level != entry.get("level"):
        update["level"] = payload.level
        update["restrictions"] = flagging_service.derive_restrictions(entry["entity_type"], payload.level)
    if payload.status is not None:
        update["status"] = payload.status.value
        if payload.status == BlacklistStatus.REMOVED:
            update["removed_by"] = current_user["uid"]
            update["removed_at"] = datetime.now(timezone.utc)
            update["removal_note"] = payload.note
    if payload.note and payload.status != BlacklistStatus.REMOVED:
        update["review_note"] = payload.note

    ref.update(update)
    return BlacklistResponse(**{**entry, **update, "id": entry_id})


# ── Entity history ────────────────────────────────────────────────────────────

@router.get("/entities/{entity_type}/{entity_id}/history", response_model=EntityHistoryResponse)
async def entity_history(entity_type: str, entity_id: str,
                         _: dict = Depends(require_admin)):
    if entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown entity type")

    db = get_db()
    rules = get_rules(db)

    flag_docs = (
        db.collection(Collections.FLAGS)
        .where("entity_type", "==", entity_type)
        .where("entity_id", "==", entity_id)
        .get()
    )
    flags = sorted(
        ({**d.to_dict(), "id": d.id} for d in flag_docs),
        key=lambda f: ensure_utc(f.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=rules.flag_window_days)
    in_window = sum(
        1 for f in flags
        if f.get("status") == "active" and ensure_utc(f.get("created_at")) >= cutoff
    )

    entry = get_active_blacklist_entry(db, entity_type, entity_id)
    return EntityHistoryResponse(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=flagging_service._display_name(db, entity_type, entity_id),
        flags=[FlagResponse(**f) for f in flags],
        flags_in_window=in_window,
        blacklist=BlacklistResponse(**entry) if entry else None,
    )


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=FlaggingStats)
async def flagging_stats(_: dict = Depends(require_admin)):
    db = get_db()
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)

    flags     = list(db.collection(Collections.FLAGS).get())
    blacklist = list(db.collection(Collections.BLACKLIST).get())

    active_flags = [f.to_dict() for f in flags if f.to_dict().get("status") == "active"]
    by_type: Dict[str, int] = {}
    by_trigger: Dict[str, int] = {}
    for f in active_flags:
        by_type[f.get("entity_type", "unknown")] = by_type.get(f.get("entity_type", "unknown"), 0) + 1
        by_trigger[f.get("trigger_type", "unknown")] = by_trigger.get(f.get("trigger_type", "unknown"), 0) + 1

    active_blacklist: Dict[str, int] = {}
    by_level = {"level_1": 0, "level_2": 0, "level_3": 0}
    escalations_30d = 0
    for b in blacklist:
        d = b.to_dict()
        if d.get("status") == "active":
            active_blacklist[d.get("entity_type", "unknown")] = \
                active_blacklist.get(d.get("entity_type", "unknown"), 0) + 1
            by_level[f"level_{d.get('level', 1)}"] += 1
        created = ensure_utc(d.get("created_at"))
        updated = ensure_utc(d.get("updated_at"))
        if created and created >= cutoff and (updated is None or updated == created):
            escalations_30d += 1

    return FlaggingStats(
        total_active_flags=len(active_flags),
        flags_last_30d=sum(
            1 for f in active_flags if ensure_utc(f.get("created_at")) >= cutoff),
        dismissals_total=sum(1 for f in (x.to_dict() for x in flags)
                             if f.get("status") == "dismissed"),
        escalations_last_30d=escalations_30d,
        active_blacklist=active_blacklist,
        by_level=by_level,
        by_trigger=by_trigger,
        by_entity_type=by_type,
    )


# ── Disputes ──────────────────────────────────────────────────────────────────

@router.post("/disputes", status_code=status.HTTP_201_CREATED)
async def record_dispute(payload: DisputeRequest, current_user: dict = Depends(require_admin)):
    """
    Records a payment dispute or chargeback against a transaction and flags
    both parties the client matrix calls out: the customer behind the claim
    and the payment method used.
    """
    db = get_db()
    tx_doc = db.collection(Collections.TRANSACTIONS).document(payload.transaction_id).get()
    if not tx_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    tx = tx_doc.to_dict()

    trigger = "chargeback_pattern" if payload.is_chargeback else "disputed_valid_transaction"
    severity = "high" if payload.is_chargeback else "medium"
    verb = "Chargeback recorded" if payload.is_chargeback else "Payment dispute recorded"

    customer_result = payment_result = None
    if tx.get("user_id"):
        customer_result = record_flag(
            db,
            entity_type="customer",
            entity_id=tx["user_id"],
            trigger_type=trigger,
            source="dispute",
            description=f"{verb}: {payload.reason}",
            severity=severity,
            transaction_id=tx["id"],
            nozzle_id=tx.get("nozzle_id"),
            station_id=flagging_service.resolve_station_id(db, tx),
            created_by=current_user["uid"],
        )
    if tx.get("payment_method"):
        payment_result = record_flag(
            db,
            entity_type="payment_method",
            entity_id=str(tx["payment_method"]),
            trigger_type=trigger,
            source="dispute",
            description=f"{verb}: {payload.reason}",
            severity=severity,
            transaction_id=tx["id"],
            created_by=current_user["uid"],
        )

    db.collection(Collections.TRANSACTIONS).document(tx["id"]).update({"disputed": True})

    def _shape(res):
        if not res:
            return None
        return {
            "flag": FlagResponse(**res["flag"]).model_dump(mode="json"),
            "blacklist": BlacklistResponse(**res["blacklist"]).model_dump(mode="json")
                         if res["blacklist"] else None,
            "escalated": res["escalated"],
            "level": res["level"],
        }

    return {
        "message": "Dispute recorded and entities flagged",
        "customer": _shape(customer_result),
        "payment_method": _shape(payment_result),
    }


# ── Customer self view ────────────────────────────────────────────────────────

@router.get("/me/status")
async def my_flagging_status(current_user: dict = Depends(get_current_user)):
    db = get_db()
    entry = get_active_blacklist_entry(db, "customer", current_user["uid"])
    rules = get_rules(db)
    return {
        "flagged": entry is not None,
        "level": int(entry["level"]) if entry else 0,
        "status": entry.get("status") if entry else None,
        "restrictions": entry.get("restrictions") if entry else {},
        "flags_in_window": flagging_service.count_flags_in_window(
            db, "customer", current_user["uid"], rules.flag_window_days),
    }


# ── Sweep (cron style, mirrors the evidence auto-delete pattern) ─────────────

@router.post("/escalation-sweep")
async def escalation_sweep():
    """
    Re-evaluates every active flag so escalations still land even if a flag
    was recorded while the rules were tighter, or an evaluation errored.
    Intended for a scheduled task, not end users.
    """
    db = get_db()
    rules = get_rules(db)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=rules.flag_window_days)

    flags = [
        f.to_dict() for f in db.collection(Collections.FLAGS).get()
        if f.to_dict().get("status") == "active"
    ]
    groups: dict[tuple[str, str], dict] = {}
    for f in flags:
        created = ensure_utc(f.get("created_at"))
        if created and created >= cutoff:
            groups.setdefault((f["entity_type"], f["entity_id"]), f)

    escalated = 0
    for (entity_type, entity_id), seed_flag in groups.items():
        result = flagging_service._evaluate_escalation(
            db, entity_type=entity_type, entity_id=entity_id,
            entity_name=seed_flag.get("entity_name"), trigger_flag=seed_flag,
        )
        if result.get("escalated"):
            escalated += 1

    return {"entities_reviewed": len(groups), "escalations": escalated}
