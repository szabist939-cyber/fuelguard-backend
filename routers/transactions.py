from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from core.cache import TAG_DASHBOARD, TAG_NOZZLES, TAG_TRANSACTIONS, invalidate
from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, get_db
from models.transaction import (
    PriceUpdateRequest,
    TransactionCreate,
    TransactionResponse,
)
from services import flagging_service
from services.report_service import generate_receipt_pdf

router = APIRouter()


@router.post("", response_model=TransactionResponse, status_code=status.HTTP_201_CREATED)
async def create_transaction(
    payload: TransactionCreate,
    current_user: dict = Depends(get_current_user),
):
    db = get_db()

    session_doc = db.collection(Collections.SESSIONS).document(payload.session_id).get()
    if not session_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    session = session_doc.to_dict()
    if session["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session is not active",
        )

    # Blacklist enforcement runs before anything is written. A level 3
    # attendant is suspended and cannot dispense; lower levels travel as
    # stamps the admin panel can filter on.
    enforcement = flagging_service.enforce_transaction_blacklists(
        db,
        employee_id=payload.employee_id,
        customer_id=payload.user_id,
        payment_method=payload.payment_method.value,
    )
    if enforcement["rejected"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=enforcement["rejected"],
        )

    total_amount = round(payload.litres_dispensed * payload.price_per_litre, 2)
    if total_amount <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Transaction amount must be greater than zero",
        )

    tx_id = str(uuid.uuid4())
    data  = {
        "id": tx_id,
        "session_id": payload.session_id,
        "nozzle_id": payload.nozzle_id,
        "user_id": payload.user_id,
        "vehicle_id": payload.vehicle_id,
        "fuel_type": payload.fuel_type,
        "litres_dispensed": payload.litres_dispensed,
        "price_per_litre": payload.price_per_litre,
        "total_amount": total_amount,
        "payment_method": payload.payment_method.value,
        "status": "completed",
        "employee_id": payload.employee_id,
        "station_id": session.get("station_id"),
        "receipt_url": None,
        "evidence_url": None,
        "is_flagged": False,
        "disputed": False,
        **enforcement["stamps"],
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.TRANSACTIONS).document(tx_id).set(data)
    invalidate(TAG_TRANSACTIONS, TAG_DASHBOARD, TAG_NOZZLES)

    # The customer's notification feed is real data, not decoration: every
    # completed fill lands here with the verified numbers.
    db.collection(Collections.NOTIFICATIONS).add({
        "id": str(uuid.uuid4()),
        "type": "transaction_completed",
        "audience": "user",
        "user_id": payload.user_id,
        "title": "Transaction completed",
        "body": (
            f"{payload.litres_dispensed:.1f} L of {payload.fuel_type} verified"
            f" for PKR {total_amount:,.0f}."
        ),
        "transaction_id": tx_id,
        "is_read": False,
        "created_at": datetime.now(timezone.utc),
    })

    # Close the session atomically after the transaction is recorded
    db.collection(Collections.SESSIONS).document(payload.session_id).update({
        "status": "completed",
        "ended_at": datetime.now(timezone.utc),
        "transaction_id": tx_id,
        "total_litres": payload.litres_dispensed,
        "total_amount": total_amount,
    })

    return TransactionResponse(**data)


@router.get("/my")
async def my_transactions(
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    db   = get_db()
    docs = (
        db.collection(Collections.TRANSACTIONS)
        .where("user_id", "==", current_user["uid"])
        .get()
    )
    # Sort in Python — avoids requiring a Firestore composite index on
    # (user_id, created_at) which may not be deployed yet.
    raw_items = sorted(
        [{"id": d.id, **d.to_dict()} for d in docs],
        key=lambda x: x.get("created_at") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    # Run each item through TransactionResponse so:
    #  - all missing fields get their Pydantic defaults (e.g. is_flagged=False)
    #  - datetime objects are serialised to ISO-8601 strings
    #  - any document that is genuinely malformed is skipped rather than crashing
    validated = []
    for item in raw_items:
        try:
            validated.append(TransactionResponse(**item).model_dump(mode="json"))
        except Exception:
            # Keep the raw item so at least partial data is returned
            validated.append(item)
    return {"total": len(validated), "items": validated[offset: offset + limit]}


@router.get("/prices/current")
async def get_current_prices(
    station_id: str | None = Query(None),
    _: dict = Depends(get_current_user),
):
    db    = get_db()
    query = db.collection(Collections.PRICES)
    if station_id:
        query = query.where("station_id", "==", station_id)
    docs = query.get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.put("/prices/current")
async def update_prices(payload: PriceUpdateRequest, _: dict = Depends(require_admin)):
    if payload.price_per_litre <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Price per litre must be greater than zero",
        )
    db       = get_db()
    price_id = f"{payload.fuel_type}_{payload.station_id or 'global'}"
    now      = datetime.now(timezone.utc)

    # Archive the previous price before overwriting
    old = db.collection(Collections.PRICES).document(price_id).get()
    if old.exists:
        db.collection(Collections.PRICE_HISTORY).add({**old.to_dict(), "archived_at": now})

    db.collection(Collections.PRICES).document(price_id).set({
        "fuel_type": payload.fuel_type,
        "price_per_litre": payload.price_per_litre,
        "station_id": payload.station_id,
        "effective_from": now,
    })
    return {"message": "Price updated"}


@router.get("/prices/history")
async def price_history(
    fuel_type: str | None = Query(None),
    station_id: str | None = Query(None),
    _: dict = Depends(get_current_user),
):
    db    = get_db()
    query = db.collection(Collections.PRICE_HISTORY)
    if fuel_type:
        query = query.where("fuel_type", "==", fuel_type)
    if station_id:
        query = query.where("station_id", "==", station_id)
    docs = query.order_by("created_at", direction="DESCENDING").limit(100).get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.get("/{transaction_id}/receipt")
async def get_receipt(transaction_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    doc = db.collection(Collections.TRANSACTIONS).document(transaction_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    tx = doc.to_dict()
    if tx["user_id"] != current_user["uid"] and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    pdf_bytes = generate_receipt_pdf(tx)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=receipt_{transaction_id}.pdf"},
    )


@router.get("/{transaction_id}", response_model=TransactionResponse)
async def get_transaction(transaction_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    doc = db.collection(Collections.TRANSACTIONS).document(transaction_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    tx = doc.to_dict()
    if tx["user_id"] != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin", "employee"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return TransactionResponse(**{**tx, "id": doc.id})


@router.get("")
async def list_transactions(
    nozzle_id: str | None = Query(None),
    user_id: str | None = Query(None),
    station_id: str | None = Query(None),
    fuel_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
):
    db    = get_db()
    query = db.collection(Collections.TRANSACTIONS)
    if nozzle_id:
        query = query.where("nozzle_id", "==", nozzle_id)
    if user_id:
        query = query.where("user_id", "==", user_id)
    if station_id:
        query = query.where("station_id", "==", station_id)
    if fuel_type:
        query = query.where("fuel_type", "==", fuel_type)

    docs  = query.order_by("created_at", direction="DESCENDING").get()
    items = [{"id": d.id, **d.to_dict()} for d in docs]
    return {"total": len(items), "items": items[offset: offset + limit]}


@router.post("/{transaction_id}/price-update", status_code=status.HTTP_200_OK)
async def apply_price_update(
    transaction_id: str,
    payload: PriceUpdateRequest,
    _: dict = Depends(require_admin),
):
    if payload.price_per_litre <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Price per litre must be greater than zero",
        )

    db  = get_db()
    ref = db.collection(Collections.TRANSACTIONS).document(transaction_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    tx        = doc.to_dict()
    new_total = round(tx["litres_dispensed"] * payload.price_per_litre, 2)
    ref.update({
        "price_per_litre": payload.price_per_litre,
        "total_amount": new_total,
        "price_updated_reason": payload.reason,
    })
    invalidate(TAG_TRANSACTIONS, TAG_DASHBOARD)
    return {"message": "Price updated", "new_total_amount": new_total}
