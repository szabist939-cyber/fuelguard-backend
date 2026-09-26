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
    vehicle_data = doc.to_dict()
    if vehicle_data.get("owner_uid") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    expenses = (
        db.collection(Collections.FLEET_EXPENSES)
        .where("vehicle_id", "==", vehicle_id)
        .where("category", "==", "fuel")
        .get()
    )

    # Odometer segments: each reading measures the distance since the previous
    # one, and the segment belongs to the month of the later fill. Fills
    # without an odometer contribute litres and cost but no distance.
    fills = sorted(
        (
            {
                "date": e.to_dict().get("expense_date"),
                "litres": float(e.to_dict().get("litres") or 0.0),
                "amount": float(e.to_dict().get("amount") or 0.0),
                "odometer": float(e.to_dict().get("odometer_km") or 0.0) or None,
            }
            for e in expenses
        ),
        key=lambda f: f["date"] or datetime.min.replace(tzinfo=timezone.utc),
    )

    monthly: dict[str, dict] = {}
    latest_odometer: float | None = None
    previous_odometer: float | None = None
    total_distance = 0.0
    total_litres_all = 0.0

    def _bucket(month_key: str) -> dict:
        if month_key not in monthly:
            monthly[month_key] = {
                "litres": 0.0, "amount": 0.0, "distance_km": 0.0,
                "km_per_litre": None, "cost_per_km": None,
            }
        return monthly[month_key]

    for fill in fills:
        edate = fill["date"]
        key = str(edate)[:7] if edate else "unknown"
        bucket = _bucket(key)
        bucket["litres"] += fill["litres"]
        bucket["amount"] += fill["amount"]
        total_litres_all += fill["litres"]

        if fill["odometer"]:
            latest_odometer = fill["odometer"]
            if previous_odometer is not None and fill["odometer"] > previous_odometer:
                segment = fill["odometer"] - previous_odometer
                bucket["distance_km"] += segment
                total_distance += segment
            previous_odometer = fill["odometer"]

    for entry in monthly.values():
        if entry["litres"] > 0 and entry["distance_km"] > 0:
            entry["km_per_litre"] = round(entry["distance_km"] / entry["litres"], 2)
            entry["cost_per_km"] = round(entry["amount"] / entry["distance_km"], 2)
        entry["litres"] = round(entry["litres"], 2)
        entry["amount"] = round(entry["amount"], 2)
        entry["distance_km"] = round(entry["distance_km"], 2)

    return {
        "vehicle_id": vehicle_id,
        "registration_number": vehicle_data.get("registration_number"),
        "total_fuel_consumed": vehicle_data.get("total_fuel_consumed", 0.0),
        "odometer_latest": latest_odometer,
        "total_distance_km": round(total_distance, 2),
        "overall_km_per_litre": (
            round(total_distance / total_litres_all, 2)
            if total_litres_all > 0 and total_distance > 0 else None
        ),
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
    db = get_db()

    # Expenses mutate vehicle totals, so the caller must own the vehicle.
    vehicle_doc = db.collection(Collections.FLEET_VEHICLES).document(payload.vehicle_id).get()
    if not vehicle_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
    if vehicle_doc.to_dict().get("owner_uid") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

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

    # A fuel fill with an odometer reading advances the vehicle odometer so the
    # consumption analytics always see the latest distance marker.
    vehicle_updates: dict = {}
    if payload.category.value == "fuel" and payload.litres:
        vehicle_updates["total_fuel_consumed"] = Increment(payload.litres)
        vehicle_updates["total_expense"] = Increment(payload.amount)
    else:
        vehicle_updates["total_expense"] = Increment(payload.amount)
    if payload.odometer_km:
        current_odo = vehicle_doc.to_dict().get("odometer_km") or 0.0
        if payload.odometer_km > current_odo:
            vehicle_updates["odometer_km"] = payload.odometer_km
    db.collection(Collections.FLEET_VEHICLES).document(payload.vehicle_id).update(vehicle_updates)

    return ExpenseResponse(**{**data, "expense_date": expense_day})


@router.delete("/expenses/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_expense(expense_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    ref = db.collection(Collections.FLEET_EXPENSES).document(expense_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")

    data = doc.to_dict()
    if data.get("user_id") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Roll the vehicle totals back so sums stay consistent after a delete.
    amount = float(data.get("amount") or 0.0)
    litres = float(data.get("litres") or 0.0)
    vehicle_id = data.get("vehicle_id")
    if vehicle_id:
        vehicle_ref = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id)
        updates: dict = {"total_expense": Increment(-amount)}
        if data.get("category") == "fuel" and litres:
            updates["total_fuel_consumed"] = Increment(-litres)
        vehicle_ref.update(updates)

    ref.delete()


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
            "over_budget": False,
            "percentage": 0.0,
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

    budget_amount = float(data.get("budget_amount", 0) or 0)
    data["spent_amount"] = round(spent, 2)
    data["remaining"] = round(budget_amount - spent, 2)
    # Server-authoritative over-budget signal — clients no longer have to
    # infer it from a negative remaining value.
    data["over_budget"] = budget_amount > 0 and spent > budget_amount
    data["percentage"] = round(spent / budget_amount * 100, 1) if budget_amount > 0 else 0.0
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
async def list_drivers(current_user: dict = Depends(get_current_user)):
    db   = get_db()
    docs = db.collection(Collections.FLEET_DRIVERS).get()
    items = []
    for d in docs:
        data = {"id": d.id, **d.to_dict()}
        # Drivers created before ownership scoping have no owner_uid; they stay
        # visible so legacy demo data is not silently hidden.
        owner = data.get("owner_uid")
        if current_user["role"] not in ("admin", "super_admin") and owner not in (current_user["uid"], None):
            continue
        items.append(data)
    return items


@router.post("/drivers", response_model=DriverResponse, status_code=status.HTTP_201_CREATED)
async def add_driver(payload: DriverCreate, current_user: dict = Depends(get_current_user)):
    db        = get_db()
    driver_id = str(uuid.uuid4())
    data = {
        **payload.model_dump(),
        "id": driver_id,
        "owner_uid": current_user["uid"],
        "assigned_vehicle_id": None,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.FLEET_DRIVERS).document(driver_id).set(data)
    return DriverResponse(**data)


@router.put("/drivers/{driver_id}", response_model=DriverResponse)
async def update_driver(
    driver_id: str,
    payload: DriverCreate,
    current_user: dict = Depends(get_current_user),
):
    db  = get_db()
    ref = db.collection(Collections.FLEET_DRIVERS).document(driver_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Driver not found")
    if doc.to_dict().get("owner_uid") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    ref.update(payload.model_dump(exclude_none=True))
    return DriverResponse(**{**ref.get().to_dict(), "id": driver_id})


@router.patch("/drivers/{driver_id}/status")
async def set_driver_status(
    driver_id: str,
    is_active: bool = Query(...),
    current_user: dict = Depends(get_current_user),
):
    db  = get_db()
    ref = db.collection(Collections.FLEET_DRIVERS).document(driver_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Driver not found")
    if doc.to_dict().get("owner_uid") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    ref.update({"is_active": is_active})
    if not is_active:
        # An inactive driver cannot stay attached to a vehicle.
        data = doc.to_dict()
        assigned = data.get("assigned_vehicle_id")
        if assigned:
            db.collection(Collections.FLEET_VEHICLES).document(assigned).update(
                {"assigned_driver_uid": None}
            )
            ref.update({"assigned_vehicle_id": None})
    return {"message": "Driver status updated"}


@router.delete("/drivers/{driver_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_driver(driver_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    ref = db.collection(Collections.FLEET_DRIVERS).document(driver_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Driver not found")
    if doc.to_dict().get("owner_uid") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    assigned = doc.to_dict().get("assigned_vehicle_id")
    if assigned:
        db.collection(Collections.FLEET_VEHICLES).document(assigned).update(
            {"assigned_driver_uid": None}
        )
    ref.delete()


@router.put("/vehicles/{vehicle_id}/driver")
async def assign_driver(
    vehicle_id: str,
    payload: DriverAssign,
    current_user: dict = Depends(get_current_user),
):
    db  = get_db()
    ref = db.collection(Collections.FLEET_VEHICLES).document(vehicle_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
    if doc.to_dict().get("owner_uid") != current_user["uid"] and current_user["role"] not in (
        "admin", "super_admin"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    ref.update({"assigned_driver_uid": payload.driver_uid})

    # Drivers are matched by document id — the uid field is the owner's id and
    # is unrelated to which driver this is.
    previous = (
        db.collection(Collections.FLEET_DRIVERS)
        .where("assigned_vehicle_id", "==", vehicle_id)
        .get()
    )
    for d in previous:
        if not payload.driver_uid or d.id != payload.driver_uid:
            d.reference.update({"assigned_vehicle_id": None})

    if payload.driver_uid:
        driver_doc = db.collection(Collections.FLEET_DRIVERS).document(payload.driver_uid).get()
        if driver_doc.exists:
            driver_doc.reference.update({"assigned_vehicle_id": vehicle_id})

    return {"message": "Driver assigned to vehicle"}
