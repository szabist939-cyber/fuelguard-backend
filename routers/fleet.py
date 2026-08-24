from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, Increment, get_db
from models.fleet import (
    BudgetSet,
    CategoryBreakdown,
    ExpenseSummaryResponse,
    FuelCategory,
    DriverAssign,
    DriverCreate,
    DriverResponse,
    ExpenseCreate,
    ExpenseResponse,
    VehicleCreate,
    VehicleResponse,
    VehicleUpdate,
)

router = APIRouter()


def docs_iter(query):
    """Small indirection so query execution is in one place and easy to stub."""
    return query.get()


# ── Vehicles ──────────────────────────────────────────────────────────────────

@router.get("/vehicles")
async def list_vehicles(current_user: dict = Depends(get_current_user)):
    db    = get_db()
    query = db.collection(Collections.FLEET_VEHICLES)
    if current_user.get("role") not in ("admin", "super_admin"):
        query = query.where("owner_uid", "==", current_user["uid"])
    docs = query.get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.post("/vehicles", response_model=VehicleResponse, status_code=status.HTTP_201_CREATED)
async def add_vehicle(payload: VehicleCreate, current_user: dict = Depends(get_current_user)):
    db = get_db()
    existing = (
        db.collection(Collections.FLEET_VEHICLES)
        .where("registration_number", "==", payload.registration_number)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration number already registered",
        )

    vehicle_id = str(uuid.uuid4())
    data = {
        **payload.model_dump(),
        "id": vehicle_id,
        "owner_uid": payload.owner_uid or current_user["uid"],
        "assigned_driver_uid": None,
        "is_active": True,
        "total_fuel_consumed": 0.0,
        "total_expense": 0.0,
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.FLEET_VEHICLES).document(vehicle_id).set(data)
    return VehicleResponse(**data)


@router.get("/vehicles/{vehicle_id}", response_model=VehicleResponse)
async def get_vehicle(vehicle_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    doc = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
    data = doc.to_dict()
    if data["owner_uid"] != current_user["uid"] and current_user["role"] not in ("admin", "super_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return VehicleResponse(**{**data, "id": doc.id})


@router.put("/vehicles/{vehicle_id}", response_model=VehicleResponse)
async def update_vehicle(
    vehicle_id: str,
    payload: VehicleUpdate,
    current_user: dict = Depends(get_current_user),
):
    db  = get_db()
    ref = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
    if doc.to_dict()["owner_uid"] != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    updates = payload.model_dump(exclude_none=True)
    ref.update(updates)
    return VehicleResponse(**{**ref.get().to_dict(), "id": vehicle_id})


@router.delete("/vehicles/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vehicle(vehicle_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    ref = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
    if doc.to_dict()["owner_uid"] != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    ref.delete()


@router.get("/vehicles/{vehicle_id}/consumption")
async def fuel_consumption(vehicle_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    doc = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")

    expenses = (
        db.collection(Collections.FLEET_EXPENSES)
        .where("vehicle_id", "==", vehicle_id)
        .where("category", "==", "fuel")
        .get()
    )

    monthly: dict[str, dict] = {}
    for e in expenses:
        data  = e.to_dict()
        edate = data.get("expense_date")
        if not edate:
            continue
        key = str(edate)[:7]  # "YYYY-MM"
        if key not in monthly:
            monthly[key] = {"litres": 0.0, "amount": 0.0}
        monthly[key]["litres"] += data.get("litres", 0.0)
        monthly[key]["amount"] += data.get("amount", 0.0)

    vehicle_data = doc.to_dict()
    return {
        "vehicle_id": vehicle_id,
        "registration_number": vehicle_data.get("registration_number"),
        "total_fuel_consumed": vehicle_data.get("total_fuel_consumed", 0.0),
        "monthly_breakdown": monthly,
    }


# ── Expenses ──────────────────────────────────────────────────────────────────

@router.get("/expenses")
async def list_expenses(
    vehicle_id: str | None = Query(None),
    category: str | None = Query(None),
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    db    = get_db()
    query = db.collection(Collections.FLEET_EXPENSES)
    if vehicle_id:
        query = query.where("vehicle_id", "==", vehicle_id)
    if category:
        query = query.where("category", "==", category)
    if current_user["role"] not in ("admin", "super_admin"):
        query = query.where("user_id", "==", current_user["uid"])

    # Deliberately NOT using .order_by() here: combined with the user_id/vehicle_id
    # equality filters it would require a composite Firestore index, which fails
    # with FailedPrecondition (HTTP 500) until someone creates it by hand.
    # Sorting in Python keeps this working on a fresh project with zero setup.
    items = []
    for d in docs_iter(query):
        data = {"id": d.id, **d.to_dict()}
        edate = data.get("expense_date")
        if month and year:
            if edate and hasattr(edate, "month") and (edate.month != month or edate.year != year):
                continue
        # Firestore hands back a datetime; the clients want a plain date string.
        if edate is not None and hasattr(edate, "date"):
            data["expense_date"] = edate.date().isoformat()
        items.append(data)

    items.sort(key=lambda x: x.get("expense_date") or "", reverse=True)
    return {"total": len(items), "items": items}


@router.post("/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
async def log_expense(payload: ExpenseCreate, current_user: dict = Depends(get_current_user)):
    db         = get_db()
    expense_id = str(uuid.uuid4())
    # Firestore's Python SDK serialises datetime but NOT datetime.date — writing
    # a bare date raises and surfaces as a 500. Store midnight UTC instead and
    # hand the client back a plain date.
    expense_day = payload.expense_date or date.today()
    expense_dt  = datetime(
        expense_day.year, expense_day.month, expense_day.day, tzinfo=timezone.utc
    )

    data = {
        **payload.model_dump(),
        "id": expense_id,
        "user_id": current_user["uid"],
        "category": payload.category.value,
        "expense_date": expense_dt,
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.FLEET_EXPENSES).document(expense_id).set(data)

    # Increment vehicle totals — never overwrite
    if payload.category.value == "fuel" and payload.litres:
        db.collection(Collections.FLEET_VEHICLES).document(payload.vehicle_id).update({
            "total_fuel_consumed": Increment(payload.litres),
            "total_expense": Increment(payload.amount),
        })
    else:
        db.collection(Collections.FLEET_VEHICLES).document(payload.vehicle_id).update({
            "total_expense": Increment(payload.amount),
        })

    return ExpenseResponse(**{**data, "expense_date": expense_day})


@router.get("/expenses/summary", response_model=ExpenseSummaryResponse)
async def expense_summary(
    vehicle_id: str | None = Query(None),
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """
    Fleet-wide expense totals grouped by category.

    Mirrors the scoping rules of GET /expenses: non-admins only ever see their
    own rows. Aggregation happens here rather than in the client so the mobile
    app does not have to page the whole expense history just to draw a chart.
    """
    db    = get_db()
    query = db.collection(Collections.FLEET_EXPENSES)
    if vehicle_id:
        query = query.where("vehicle_id", "==", vehicle_id)
    if current_user["role"] not in ("admin", "super_admin"):
        query = query.where("user_id", "==", current_user["uid"])

    # Seed every category so the response shape is stable for charting.
    totals: dict[str, dict] = {
        c.value: {"amount": 0.0, "count": 0, "litres": 0.0} for c in FuelCategory
    }
    total_amount = 0.0
    total_count  = 0
    total_litres = 0.0

    for d in query.get():
        data = d.to_dict()

        if month and year:
            edate = data.get("expense_date")
            if edate and hasattr(edate, "month") and (edate.month != month or edate.year != year):
                continue

        category = data.get("category", FuelCategory.OTHER.value)
        if category not in totals:
            # Tolerate rows written before a category was added to the enum.
            category = FuelCategory.OTHER.value

        amount = float(data.get("amount") or 0.0)
        litres = float(data.get("litres") or 0.0)

        totals[category]["amount"] += amount
        totals[category]["count"]  += 1
        totals[category]["litres"] += litres
        total_amount += amount
        total_count  += 1
        total_litres += litres

    by_category = [
        CategoryBreakdown(
            category=FuelCategory(name),
            amount=round(v["amount"], 2),
            count=v["count"],
            # Guard against divide-by-zero on an empty fleet.
            percentage=round((v["amount"] / total_amount * 100), 2) if total_amount else 0.0,
            litres=round(v["litres"], 2) if v["litres"] else None,
        )
        for name, v in totals.items()
    ]
    by_category.sort(key=lambda c: c.amount, reverse=True)

    return ExpenseSummaryResponse(
        total_amount=round(total_amount, 2),
        total_count=total_count,
        total_litres=round(total_litres, 2),
        vehicle_id=vehicle_id,
        month=month,
        year=year,
        by_category=by_category,
    )


# ── Budget ────────────────────────────────────────────────────────────────────

@router.get("/budget")
async def get_budget(
    vehicle_id: str | None = Query(None),
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None),
    current_user: dict = Depends(get_current_user),
):
    db  = get_db()
    now = datetime.now(timezone.utc)
    m   = month or now.month
    y   = year  or now.year

    query = (
        db.collection(Collections.BUDGETS)
        .where("user_id", "==", current_user["uid"])
        .where("month", "==", m)
        .where("year", "==", y)
    )
    if vehicle_id:
        query = query.where("vehicle_id", "==", vehicle_id)

    docs = query.limit(1).get()
    if not docs:
        # Return a real, zeroed budget rather than {"message": ...}. The mobile
        # client deserialises this into a non-nullable model, so the old shape
        # threw a parse error instead of showing "no budget yet".
        return {
            "id": f"{current_user['uid']}_{vehicle_id or 'all'}_{y}_{m}",
            "user_id": current_user["uid"],
            "vehicle_id": vehicle_id,
            "month": m,
            "year": y,
            "budget_amount": 0.0,
            "spent_amount": 0.0,
            "remaining": 0.0,
        }

    data = docs[0].to_dict()

    # Calculate actual spend from expenses
    exp_query = db.collection(Collections.FLEET_EXPENSES).where("user_id", "==", current_user["uid"])
    if vehicle_id:
        exp_query = exp_query.where("vehicle_id", "==", vehicle_id)

    expenses = exp_query.get()
    spent = sum(
        e.to_dict().get("amount", 0)
        for e in expenses
        if (
            edate := e.to_dict().get("expense_date")
        ) and hasattr(edate, "month") and edate.month == m and edate.year == y
    )

    data["spent_amount"] = round(spent, 2)
    data["remaining"]    = round(data.get("budget_amount", 0) - spent, 2)
    return data


@router.put("/budget")
async def set_budget(payload: BudgetSet, current_user: dict = Depends(get_current_user)):
    db        = get_db()
    budget_id = (
        f"{current_user['uid']}_{payload.vehicle_id or 'all'}_{payload.year}_{payload.month}"
    )
    # BudgetSet calls the field `amount`, but get_budget and every client read
    # `budget_amount`. Writing model_dump() verbatim meant the budget was stored
    # under a key nothing ever read, so it always came back as 0.
    fields = payload.model_dump()
    fields["budget_amount"] = fields.pop("amount")

    data = {
        "id": budget_id,
        "user_id": current_user["uid"],
        **fields,
        "updated_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.BUDGETS).document(budget_id).set(data, merge=True)
    return {"message": "Budget set", "budget_id": budget_id}


# ── Drivers ───────────────────────────────────────────────────────────────────

@router.get("/drivers")
async def list_drivers(_: dict = Depends(get_current_user)):
    db   = get_db()
    docs = db.collection(Collections.FLEET_DRIVERS).get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.post("/drivers", response_model=DriverResponse, status_code=status.HTTP_201_CREATED)
async def add_driver(payload: DriverCreate, _: dict = Depends(get_current_user)):
    db        = get_db()
    driver_id = str(uuid.uuid4())
    data = {
        **payload.model_dump(),
        "id": driver_id,
        "assigned_vehicle_id": None,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.FLEET_DRIVERS).document(driver_id).set(data)
    return DriverResponse(**data)


@router.put("/vehicles/{vehicle_id}/driver")
async def assign_driver(
    vehicle_id: str,
    payload: DriverAssign,
    _: dict = Depends(get_current_user),
):
    db  = get_db()
    ref = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id)
    if not ref.get().exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")

    ref.update({"assigned_driver_uid": payload.driver_uid})

    driver_docs = (
        db.collection(Collections.FLEET_DRIVERS)
        .where("uid", "==", payload.driver_uid)
        .limit(1)
        .get()
    )
    if driver_docs:
        driver_docs[0].reference.update({"assigned_vehicle_id": vehicle_id})

    return {"message": "Driver assigned to vehicle"}
