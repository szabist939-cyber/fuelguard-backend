from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.firebase import Collections

STATION_ID = "STATION001"
NOZZLE_ID = "NZ001"
PRICE_ID = "petrol_global"


def _create_if_missing(collection, doc_id: str, data: dict[str, Any]) -> dict[str, str]:
    ref = collection.document(doc_id)
    if ref.get().exists:
        return {"id": doc_id, "status": "already_exists"}
    ref.set(data)
    return {"id": doc_id, "status": "created"}


def seed_test_device(db) -> dict[str, dict[str, str]]:
    now = datetime.now(timezone.utc)

    station = _create_if_missing(
        db.collection(Collections.STATIONS),
        STATION_ID,
        {
            "id": STATION_ID,
            "name": "Test Fuel Station",
            "address": "123 Test Road",
            "city": "Karachi",
            "latitude": 24.8607,
            "longitude": 67.0011,
            "is_active": True,
            "created_at": now,
        },
    )

    nozzle = _create_if_missing(
        db.collection(Collections.NOZZLES),
        NOZZLE_ID,
        {
            "id": NOZZLE_ID,
            "name": "Nozzle 1",
            "station_id": STATION_ID,
            "fuel_type": "petrol",
            "ble_uuid": "NZ001-BLE-UUID",
            "ble_device_name": "FuelMonitor",
            "hardware_serial": NOZZLE_ID,
            "flow_meter_model": "YF-S201",
            "max_flow_rate": 60.0,
            "status": "idle",
            "is_active": True,
            "tamper_detected": False,
            "current_flow_rate": None,
            "total_litres_today": 0.0,
            "last_seen": None,
            "created_at": now,
        },
    )

    price = _create_if_missing(
        db.collection(Collections.PRICES),
        PRICE_ID,
        {
            "fuel_type": "petrol",
            "price_per_litre": 282.76,
            "station_id": None,
            "effective_from": now.isoformat(),
            "updated_at": now,
        },
    )

    return {"station": station, "nozzle": nozzle, "price": price}
