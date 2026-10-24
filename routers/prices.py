from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.dependencies import get_current_user
from core.firebase import Collections, get_db
from models.price import PriceAlertCreate, PriceAlertResponse

router = APIRouter()


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two lat/lon points."""
    R    = 6371
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── Static routes first ────────────────────────────────────────────────────────

@router.get("/compare")
async def compare_prices(
    fuel_type: str = Query(...),
    latitude: float | None = Query(None),
    longitude: float | None = Query(None),
    radius_km: float = Query(20.0),
    _: dict = Depends(get_current_user),
):
    db          = get_db()
    price_docs  = db.collection(Collections.PRICES).where("fuel_type", "==", fuel_type).get()
    results     = []
    filter_geo  = latitude is not None and longitude is not None

    for pd in price_docs:
        price_data = pd.to_dict()
        station_id = price_data.get("station_id")
        if not station_id:
            continue
        station_doc = db.collection(Collections.STATIONS).document(station_id).get()
        if not station_doc.exists:
            continue
        station = station_doc.to_dict()
        dist    = None
        if filter_geo:
            dist = round(_haversine(latitude, longitude, station["latitude"], station["longitude"]), 2)
            if dist > radius_km:
                continue
        results.append({
            "station_id":     station_id,
            "station_name":   station.get("name"),
            "fuel_type":      fuel_type,
            "price_per_litre": price_data.get("price_per_litre"),
            "distance_km":    dist,
            "last_updated":   price_data.get("effective_from"),
            "is_sample":      price_data.get("is_sample", False),
        })
    results.sort(key=lambda x: x["price_per_litre"])
    return results


@router.get("/cheapest")
async def cheapest_fuel(
    fuel_type: str = Query(...),
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_km: float = Query(20.0),
    _: dict = Depends(get_current_user),
):
    db         = get_db()
    price_docs = db.collection(Collections.PRICES).where("fuel_type", "==", fuel_type).get()
    cheapest: dict | None = None
    cheapest_price        = float("inf")

    for pd in price_docs:
        price_data = pd.to_dict()
        station_id = price_data.get("station_id")
        if not station_id:
            continue
        station_doc = db.collection(Collections.STATIONS).document(station_id).get()
        if not station_doc.exists:
            continue
        station = station_doc.to_dict()
        dist    = _haversine(latitude, longitude, station["latitude"], station["longitude"])
        if dist > radius_km:
            continue
        p = price_data.get("price_per_litre", float("inf"))
        if p < cheapest_price:
            cheapest_price = p
            cheapest = {
                "station_id":     station_id,
                "station_name":   station.get("name"),
                "fuel_type":      fuel_type,
                "price_per_litre": p,
                "distance_km":    round(dist, 2),
                "address":        station.get("address"),
                "is_sample":      price_data.get("is_sample", False),
            }

    if not cheapest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No stations found within radius",
        )
    return cheapest


@router.get("/alerts")
async def list_price_alerts(current_user: dict = Depends(get_current_user)):
    db   = get_db()
    docs = db.collection(Collections.PRICE_ALERTS).where("user_id", "==", current_user["uid"]).get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.post("/alerts", response_model=PriceAlertResponse, status_code=status.HTTP_201_CREATED)
async def create_price_alert(payload: PriceAlertCreate, current_user: dict = Depends(get_current_user)):
    db       = get_db()
    alert_id = str(uuid.uuid4())
    data     = {
        "id":         alert_id,
        "user_id":    current_user["uid"],
        **payload.model_dump(),
        "is_active":  True,
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.PRICE_ALERTS).document(alert_id).set(data)
    return PriceAlertResponse(**data)


@router.delete("/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_price_alert(alert_id: str, current_user: dict = Depends(get_current_user)):
    db  = get_db()
    ref = db.collection(Collections.PRICE_ALERTS).document(alert_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    if (
        doc.to_dict()["user_id"] != current_user["uid"]
        and current_user["role"] not in ("admin", "super_admin")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    ref.delete()


# ── Parameterized routes ───────────────────────────────────────────────────────

@router.get("/{station_id}/history")
async def station_price_history(
    station_id: str,
    fuel_type: str | None = Query(None),
    _: dict = Depends(get_current_user),
):
    db    = get_db()
    query = db.collection(Collections.PRICE_HISTORY).where("station_id", "==", station_id)
    if fuel_type:
        query = query.where("fuel_type", "==", fuel_type)
    docs = query.order_by("created_at", direction="DESCENDING").limit(100).get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.post("/{station_id}/update", status_code=status.HTTP_200_OK)
async def update_station_price(
    station_id: str,
    fuel_type: str,
    price_per_litre: float,
    current_user: dict = Depends(get_current_user),
):
    if current_user["role"] not in ("admin", "super_admin", "employee"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")
    if price_per_litre <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="price_per_litre must be greater than zero",
        )

    db          = get_db()
    station_doc = db.collection(Collections.STATIONS).document(station_id).get()
    if not station_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")

    price_id = f"{fuel_type}_{station_id}"
    now      = datetime.now(timezone.utc)

    # Archive the current price before overwriting
    old_price_doc = db.collection(Collections.PRICES).document(price_id).get()
    if old_price_doc.exists:
        db.collection(Collections.PRICE_HISTORY).add({**old_price_doc.to_dict(), "archived_at": now})

    db.collection(Collections.PRICES).document(price_id).set({
        "station_id":     station_id,
        "fuel_type":      fuel_type,
        "price_per_litre": price_per_litre,
        "updated_by":     current_user["uid"],
        "effective_from": now,
    })
    return {"message": "Price updated", "fuel_type": fuel_type, "price_per_litre": price_per_litre}
