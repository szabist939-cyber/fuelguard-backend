from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.cache import TAG_STATIONS, cache, invalidate
from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, get_db
from models.flagging import FraudRiskStatus
from models.station import StationCreate, StationResponse, StationUpdate
from services import flagging_service

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


def _reduced_visibility_enabled(db) -> bool:
    return flagging_service.get_rules(db).reduced_visibility_enabled


def _sort_with_risk(items: list[dict], distance_key: str, enabled: bool) -> None:
    """
    Blacklisted stations sink below every honest one; within each group the
    original distance ordering is preserved.
    """
    if enabled:
        items.sort(key=lambda x: (x["fraud_risk"]["is_blacklisted"], x[distance_key]))
    else:
        items.sort(key=lambda x: x[distance_key])


# ── Static / user-scoped routes MUST come before parameterized /{station_id} ──

@router.get("/nearby")
async def nearby_stations(
    latitude: float = Query(...),
    longitude: float = Query(...),
    radius_km: float = Query(10.0),
    fuel_type: str | None = Query(None),
    _: dict = Depends(get_current_user),
):
    async def _load():
        db      = get_db()
        docs    = db.collection(Collections.STATIONS).where("is_active", "==", True).get()
        results = []
        for d in docs:
            data = d.to_dict()
            dist = _haversine(latitude, longitude, data["latitude"], data["longitude"])
            if dist > radius_km:
                continue
            if fuel_type and fuel_type not in data.get("fuel_types_available", []):
                continue
            results.append({**data, "id": d.id, "distance_km": round(dist, 2)})

        risk_map = flagging_service.get_fraud_risk_map(db, [r["id"] for r in results])
        for r in results:
            r["fraud_risk"] = (risk_map.get(r["id"]) or FraudRiskStatus.clean()).model_dump(mode="json")
        _sort_with_risk(results, "distance_km", _reduced_visibility_enabled(db))
        return results

    key = f"stations:nearby:{latitude:.4f}:{longitude:.4f}:{radius_km}:{fuel_type or 'any'}"
    results = await cache.get_or_set(key, 10, _load, tag=TAG_STATIONS)
    return results


@router.get("/route")
async def route_based_stations(
    origin_lat: float = Query(...),
    origin_lng: float = Query(...),
    dest_lat: float = Query(...),
    dest_lng: float = Query(...),
    fuel_type: str | None = Query(None),
    _: dict = Depends(get_current_user),
):
    """Stations within a buffer of the straight-line route midpoint."""
    mid_lat       = (origin_lat + dest_lat) / 2
    mid_lng       = (origin_lng + dest_lng) / 2
    route_len_km  = _haversine(origin_lat, origin_lng, dest_lat, dest_lng)
    buffer_km     = min(route_len_km / 2, 10.0)

    async def _load():
        db   = get_db()
        docs = db.collection(Collections.STATIONS).where("is_active", "==", True).get()
        results = []
        for d in docs:
            data         = d.to_dict()
            dist_to_mid  = _haversine(mid_lat, mid_lng, data["latitude"], data["longitude"])
            if dist_to_mid > buffer_km:
                continue
            if fuel_type and fuel_type not in data.get("fuel_types_available", []):
                continue
            results.append({**data, "id": d.id, "distance_from_route_km": round(dist_to_mid, 2)})

        risk_map = flagging_service.get_fraud_risk_map(db, [r["id"] for r in results])
        for r in results:
            r["fraud_risk"] = (risk_map.get(r["id"]) or FraudRiskStatus.clean()).model_dump(mode="json")
        _sort_with_risk(results, "distance_from_route_km", _reduced_visibility_enabled(db))
        return results

    key = f"stations:route:{origin_lat:.4f}:{origin_lng:.4f}:{dest_lat:.4f}:{dest_lng:.4f}:{fuel_type or 'any'}"
    results = await cache.get_or_set(key, 10, _load, tag=TAG_STATIONS)
    return results


@router.get("/me/favorites")
async def get_favorites(current_user: dict = Depends(get_current_user)):
    db         = get_db()
    docs       = db.collection(Collections.FAVORITES).where("user_id", "==", current_user["uid"]).get()
    station_ids = [d.to_dict().get("station_id") for d in docs]
    stations   = []
    for sid in station_ids:
        doc = db.collection(Collections.STATIONS).document(sid).get()
        if doc.exists:
            stations.append({"id": doc.id, **doc.to_dict()})
    return stations


@router.post("/me/favorites/{station_id}", status_code=status.HTTP_201_CREATED)
async def add_favorite(station_id: str, current_user: dict = Depends(get_current_user)):
    db       = get_db()
    existing = (
        db.collection(Collections.FAVORITES)
        .where("user_id", "==", current_user["uid"])
        .where("station_id", "==", station_id)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Already in favorites",
        )
    fav_id = str(uuid.uuid4())
    db.collection(Collections.FAVORITES).document(fav_id).set({
        "id":         fav_id,
        "user_id":    current_user["uid"],
        "station_id": station_id,
        "created_at": datetime.now(timezone.utc),
    })
    return {"message": "Added to favorites"}


@router.delete("/me/favorites/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favorite(station_id: str, current_user: dict = Depends(get_current_user)):
    db   = get_db()
    docs = (
        db.collection(Collections.FAVORITES)
        .where("user_id", "==", current_user["uid"])
        .where("station_id", "==", station_id)
        .limit(1)
        .get()
    )
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Favorite not found")
    db.collection(Collections.FAVORITES).document(docs[0].id).delete()


# ── Collection-level routes ────────────────────────────────────────────────────

@router.get("", response_model=list[StationResponse])
async def list_stations(
    city: str | None = Query(None),
    fuel_type: str | None = Query(None),
    is_active: bool = Query(True),
    _: dict = Depends(get_current_user),
):
    db    = get_db()
    query = db.collection(Collections.STATIONS).where("is_active", "==", is_active)
    if city:
        query = query.where("city", "==", city)
    docs     = query.get()
    stations = []
    for d in docs:
        data = {**d.to_dict(), "id": d.id}
        if fuel_type and fuel_type not in data.get("fuel_types_available", []):
            continue
        stations.append(data)

    risk_map = flagging_service.get_fraud_risk_map(db, [s["id"] for s in stations])
    enriched = []
    for s in stations:
        s["fraud_risk"] = (risk_map.get(s["id"]) or FraudRiskStatus.clean()).model_dump(mode="json")
        enriched.append(StationResponse(**s))
    if _reduced_visibility_enabled(db):
        enriched.sort(key=lambda s: (s.fraud_risk["is_blacklisted"], s.name))
    return enriched


@router.post("", response_model=StationResponse, status_code=status.HTTP_201_CREATED)
async def create_station(payload: StationCreate, _: dict = Depends(require_admin)):
    db         = get_db()
    station_id = str(uuid.uuid4())
    data = {
        **payload.model_dump(),
        "id":         station_id,
        "is_active":  True,
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.STATIONS).document(station_id).set(data)
    invalidate(TAG_STATIONS)
    return StationResponse(**data)


# ── Document-level routes ──────────────────────────────────────────────────────

@router.get("/{station_id}", response_model=StationResponse)
async def get_station(station_id: str, _: dict = Depends(get_current_user)):
    db  = get_db()
    doc = db.collection(Collections.STATIONS).document(station_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    data = {**doc.to_dict(), "id": doc.id}
    entry = flagging_service.get_active_blacklist_entry(db, "station", station_id)
    if entry and entry.get("status") == "active":
        data["fraud_risk"] = FraudRiskStatus.flagged(int(entry.get("level") or 1)).model_dump(mode="json")
    else:
        data["fraud_risk"] = FraudRiskStatus.clean().model_dump(mode="json")
    return StationResponse(**data)


@router.put("/{station_id}", response_model=StationResponse)
async def update_station(
    station_id: str,
    payload: StationUpdate,
    _: dict = Depends(require_admin),
):
    db  = get_db()
    ref = db.collection(Collections.STATIONS).document(station_id)
    if not ref.get().exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    updates = payload.model_dump(exclude_none=True)
    ref.update(updates)
    invalidate(TAG_STATIONS)
    return StationResponse(**{**ref.get().to_dict(), "id": station_id})


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_station(station_id: str, _: dict = Depends(require_admin)):
    db  = get_db()
    ref = db.collection(Collections.STATIONS).document(station_id)
    if not ref.get().exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Station not found")
    ref.delete()
    invalidate(TAG_STATIONS)
