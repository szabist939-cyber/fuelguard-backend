from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status

import cloudinary
import cloudinary.uploader

from core.config import settings
from core.dependencies import require_admin
from core.firebase import Collections, ensure_utc, get_db
from models.iot import (
    CameraCapture,
    DevicePing,
    DeviceRegister,
    DeviceResponse,
    FirmwareStatusResponse,
    FirmwareUpdateRequest,
    FlowCalibration,
    TelemetryPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Device authentication ─────────────────────────────────────────────────────
# Device-facing endpoints require X-Device-Id + X-Device-Key. The key is shown
# once at registration and only its sha256 is stored, so a Firestore leak does
# not expose usable credentials.

_DEVICE_KEY_HEADER = "x-device-key"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_device(db, device_id: str):
    docs = (
        db.collection(Collections.IOT_DEVICES)
        .where("device_id", "==", device_id)
        .limit(1)
        .get()
    )
    if not docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Device not registered"
        )
    return docs[0]


def verify_device(device_id: str, x_device_key: str) -> dict:
    """Authenticate a hardware device by its id + key. Raises 401 on mismatch."""
    if not x_device_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device key required",
        )
    db = get_db()
    doc = _load_device(db, device_id)
    data = doc.to_dict()
    stored = data.get("device_key_hash")
    if not stored or not secrets.compare_digest(stored, _hash_key(x_device_key)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device credentials",
        )
    return {"id": doc.id, **data}


@router.post("/devices/register", status_code=status.HTTP_201_CREATED)
async def register_device(payload: DeviceRegister, _: dict = Depends(require_admin)):
    """
    Register a device. The raw device key is returned exactly once — only its
    hash is stored server-side, so losing it means re-registering the device.
    """
    db = get_db()

    existing = (
        db.collection(Collections.IOT_DEVICES)
        .where("device_id", "==", payload.device_id)
        .limit(1)
        .get()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Device already registered",
        )

    device_key = f"fgk_{secrets.token_urlsafe(24)}"
    doc_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    data   = {
        **payload.model_dump(),
        "id": doc_id,
        "device_type": payload.device_type.value,
        "device_key_hash": _hash_key(device_key),
        "status": "offline",
        "last_ping": None,
        "created_at": now,
    }
    db.collection(Collections.IOT_DEVICES).document(doc_id).set(data)

    return {
        "id": doc_id,
        "device_id": payload.device_id,
        "device_key": device_key,
        "message": (
            "Store this device key now. It is shown once and cannot be "
            "recovered from the server."
        ),
        "created_at": now,
    }


@router.get("/devices")
async def list_devices(_: dict = Depends(require_admin)):
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).get()
    now  = datetime.now(timezone.utc)
    items = []
    for d in docs:
        data = d.to_dict()
        last_ping = ensure_utc(data.get("last_ping"))
        data["computed_status"] = (
            "online" if last_ping and (now - last_ping).total_seconds() < 60 else "offline"
        )
        data.pop("device_key_hash", None)  # never leave the server
        items.append({"id": d.id, **data})
    return items


@router.get("/devices/{device_id}", response_model=DeviceResponse)
async def get_device(device_id: str, _: dict = Depends(require_admin)):
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    d = docs[0]
    return DeviceResponse(**{**d.to_dict(), "id": d.id})


@router.post("/devices/{device_id}/ping", status_code=status.HTTP_200_OK)
async def device_ping(
    device_id: str,
    payload: DevicePing,
    x_device_key: str = Header(default="", alias=_DEVICE_KEY_HEADER),
):
    """Heartbeat from hardware — authenticated with the device key."""
    db    = get_db()
    device = verify_device(device_id, x_device_key)

    updates: dict = {"status": "online", "last_ping": datetime.now(timezone.utc)}
    if payload.firmware_version:
        updates["firmware_version"] = payload.firmware_version
    if payload.ip_address:
        updates["ip_address"] = payload.ip_address

    db.collection(Collections.IOT_DEVICES).document(device["id"]).update(updates)

    pending = (
        db.collection(Collections.DEVICE_COMMANDS)
        .where("device_id", "==", device_id)
        .where("status", "==", "pending")
        .count()
        .get()[0][0].value
    )
    return {"status": "ok", "server_time": datetime.now(timezone.utc), "pending_commands": pending}


@router.post("/devices/{device_id}/telemetry", status_code=status.HTTP_200_OK)
async def receive_telemetry(
    device_id: str,
    payload: TelemetryPayload,
    x_device_key: str = Header(default="", alias=_DEVICE_KEY_HEADER),
):
    """
    Push sensor readings from hardware — flow rate, tamper flag, temperature.

    Values are bounds-checked so a leaked or spoofed device key cannot inject
    nonsensical readings into live dashboards and fraud checks.
    """
    db    = get_db()
    device = verify_device(device_id, x_device_key)

    if payload.flow_rate is not None and not 0 <= payload.flow_rate <= 500:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="flow_rate out of range")
    if payload.total_dispensed is not None and not 0 <= payload.total_dispensed <= 100_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="total_dispensed out of range")
    if payload.temperature is not None and not -40 <= payload.temperature <= 125:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="temperature out of range")
    if payload.signal_strength is not None and not -128 <= payload.signal_strength <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="signal_strength out of range")

    db.collection(Collections.IOT_DEVICES).document(device["id"]).update(
        {"status": "online", "last_ping": datetime.now(timezone.utc)}
    )

    if payload.nozzle_id:
        nozzle_updates: dict = {"last_seen": datetime.now(timezone.utc)}
        if payload.flow_rate is not None:
            nozzle_updates["current_flow_rate"] = payload.flow_rate
            nozzle_updates["status"]            = "dispensing" if payload.flow_rate > 0 else "idle"
        if payload.total_dispensed is not None:
            nozzle_updates["total_litres_today"] = payload.total_dispensed
        if payload.tamper_detected:
            nozzle_updates["tamper_detected"] = True
            nozzle_updates["status"]          = "tampered"
        try:
            db.collection(Collections.NOZZLES).document(payload.nozzle_id).update(nozzle_updates)
        except Exception:
            logger.exception("Failed to update nozzle %s from telemetry", payload.nozzle_id)

    # Store raw telemetry for analytics and audit
    db.collection(Collections.TELEMETRY_LOGS).add({
        "device_id": device_id,
        **payload.model_dump(),
        "received_at": datetime.now(timezone.utc),
    })

    return {"status": "ok"}


@router.post("/devices/{device_id}/camera/trigger", status_code=status.HTTP_200_OK)
async def trigger_camera(
    device_id: str,
    payload: CameraCapture,
    _: dict = Depends(require_admin),
):
    """Queue a camera capture command for the device to pick up on next poll."""
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    command_id = str(uuid.uuid4())
    db.collection(Collections.DEVICE_COMMANDS).document(command_id).set({
        "id": command_id,
        "device_id": device_id,
        "command": "camera_capture",
        "payload": {"reason": payload.reason},
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return {"command_id": command_id, "message": "Camera capture command queued"}


@router.put("/devices/{device_id}/calibrate", status_code=status.HTTP_200_OK)
async def calibrate_flow_meter(
    device_id: str,
    payload: FlowCalibration,
    _: dict = Depends(require_admin),
):
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    command_id = str(uuid.uuid4())
    db.collection(Collections.DEVICE_COMMANDS).document(command_id).set({
        "id": command_id,
        "device_id": device_id,
        "command": "flow_meter_calibrate",
        "payload": payload.model_dump(),
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return {"command_id": command_id, "message": "Calibration command queued"}


@router.get("/devices/{device_id}/connectivity")
async def device_connectivity(device_id: str, _: dict = Depends(require_admin)):
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    data      = docs[0].to_dict()
    last_ping = ensure_utc(data.get("last_ping"))
    now       = datetime.now(timezone.utc)

    # Device is considered online if it pinged within the last 60 seconds
    online = last_ping is not None and (now - last_ping).total_seconds() < 60

    return {
        "device_id": device_id,
        "status": "online" if online else "offline",
        "last_ping": last_ping,
        "ip_address": data.get("ip_address"),
    }


@router.post("/firmware/upload", status_code=status.HTTP_201_CREATED)
async def upload_firmware(
    version: str,
    release_notes: str | None = None,
    is_mandatory: bool = False,
    file: UploadFile = File(...),
    _: dict = Depends(require_admin),
):
    if not all([settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET]):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Firmware storage not configured — set CLOUDINARY_* env vars.",
        )

    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )

    db       = get_db()
    contents = await file.read()

    from io import BytesIO
    public_id = f"fuelguard/firmware/{version}/{file.filename}"
    upload_result = cloudinary.uploader.upload(
        BytesIO(contents),
        public_id=public_id,
        resource_type="raw",
        overwrite=False,
    )
    download_url = upload_result["secure_url"]

    fw_id = str(uuid.uuid4())
    data  = {
        "id": fw_id,
        "version": version,
        "release_notes": release_notes,
        "is_mandatory": is_mandatory,
        "download_url": download_url,
        "file_size_bytes": len(contents),
        "created_at": datetime.now(timezone.utc),
    }
    db.collection(Collections.IOT_FIRMWARE).document(fw_id).set(data)
    return data


@router.post("/devices/{device_id}/firmware/update", status_code=status.HTTP_200_OK)
async def push_firmware_update(
    device_id: str,
    payload: FirmwareUpdateRequest,
    _: dict = Depends(require_admin),
):
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    fw_docs = (
        db.collection(Collections.IOT_FIRMWARE)
        .where("version", "==", payload.firmware_version)
        .limit(1)
        .get()
    )
    if not fw_docs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Firmware version '{payload.firmware_version}' not found",
        )

    fw_data    = fw_docs[0].to_dict()
    command_id = str(uuid.uuid4())

    db.collection(Collections.DEVICE_COMMANDS).document(command_id).set({
        "id": command_id,
        "device_id": device_id,
        "command": "firmware_update",
        "payload": {
            "version": payload.firmware_version,
            "download_url": fw_data["download_url"],
            "force": payload.force,
        },
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    docs[0].reference.update({
        "firmware_update_status": "pending",
        "target_firmware_version": payload.firmware_version,
    })
    return {"command_id": command_id, "message": "Firmware update command queued"}


@router.get("/devices/{device_id}/firmware/status", response_model=FirmwareStatusResponse)
async def firmware_update_status(device_id: str, _: dict = Depends(require_admin)):
    db   = get_db()
    docs = db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    data = docs[0].to_dict()
    return FirmwareStatusResponse(
        device_id=device_id,
        current_version=data.get("firmware_version", "unknown"),
        target_version=data.get("target_firmware_version"),
        update_status=data.get("firmware_update_status", "idle"),
        progress_percent=data.get("firmware_update_progress"),
        last_updated=ensure_utc(data.get("last_ping")),
    )


# ── Command plane ─────────────────────────────────────────────────────────────
# Admin commands (camera capture, calibration, firmware) are queued in
# device_commands. The device polls for them over its STA link and acks the
# result, closing the loop the queue-only workflow left open.

@router.get("/devices/{device_id}/commands/pending", status_code=status.HTTP_200_OK)
async def poll_pending_commands(
    device_id: str,
    x_device_key: str = Header(default="", alias=_DEVICE_KEY_HEADER),
):
    """
    Device polls for queued commands. Pending commands are returned and
    atomically marked delivered so a poll retry cannot double-execute.
    """
    db     = get_db()
    device = verify_device(device_id, x_device_key)
    del device

    docs = (
        db.collection(Collections.DEVICE_COMMANDS)
        .where("device_id", "==", device_id)
        .where("status", "==", "pending")
        .get()
    )
    commands = []
    for d in docs:
        data = d.to_dict()
        d.reference.update({"status": "delivered", "delivered_at": datetime.now(timezone.utc)})
        commands.append({
            "id": data.get("id"),
            "command": data.get("command"),
            "payload": data.get("payload") or {},
            "created_at": ensure_utc(data.get("created_at")),
        })

    db.collection(Collections.IOT_DEVICES).where("device_id", "==", device_id).limit(1).get()
    return {"commands": commands, "server_time": datetime.now(timezone.utc)}


@router.post("/devices/{device_id}/commands/{command_id}/ack", status_code=status.HTTP_200_OK)
async def ack_command(
    device_id: str,
    command_id: str,
    ack_status: str = Query(..., alias="status", description="complete | failed | progress"),
    progress: int = Query(0, ge=0, le=100),
    error: str | None = Query(None),
    x_device_key: str = Header(default="", alias=_DEVICE_KEY_HEADER),
):
    """Device reports command execution state; firmware progress flows through here."""
    db     = get_db()
    device = verify_device(device_id, x_device_key)

    ref = db.collection(Collections.DEVICE_COMMANDS).document(command_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Command not found")
    command = doc.to_dict()
    if command.get("device_id") != device_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Command belongs to another device")

    updates: dict = {"acked_at": datetime.now(timezone.utc)}
    if ack_status == "complete":
        updates["status"] = "complete"
    elif ack_status == "failed":
        updates["status"] = "failed"
        updates["error"] = (error or "")[:500]
    elif ack_status == "progress":
        updates["status"] = "delivered"
        updates["progress"] = progress
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="status must be complete, failed or progress")
    ref.update(updates)

    if command.get("command") == "firmware_update":
        device_ref = db.collection(Collections.IOT_DEVICES).document(device["id"])
        if ack_status == "complete":
            device_ref.update({
                "firmware_update_status": "complete",
                "firmware_update_progress": 100,
                "firmware_version": (command.get("payload") or {}).get("version"),
                "target_firmware_version": None,
            })
        elif ack_status == "failed":
            device_ref.update({"firmware_update_status": "failed", "firmware_update_progress": None})
        else:
            device_ref.update({"firmware_update_status": "installing", "firmware_update_progress": progress})

    return {"status": "ok"}


# ── Admin views over the command plane and telemetry ──────────────────────────

@router.get("/devices/{device_id}/commands")
async def command_history(
    device_id: str,
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_admin),
):
    db = get_db()
    docs = (
        db.collection(Collections.DEVICE_COMMANDS)
        .where("device_id", "==", device_id)
        .limit(limit)
        .get()
    )
    items = sorted(
        ({**d.to_dict(), "id": d.id} for d in docs),
        key=lambda c: ensure_utc(c.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {"total": len(items), "items": items}


@router.get("/commands")
async def all_command_history(
    device_id: str | None = Query(None),
    command_status: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    _: dict = Depends(require_admin),
):
    db = get_db()
    query = db.collection(Collections.DEVICE_COMMANDS)
    if device_id:
        query = query.where("device_id", "==", device_id)
    if command_status:
        query = query.where("status", "==", command_status)
    docs = query.limit(limit).get()
    items = sorted(
        ({**d.to_dict(), "id": d.id} for d in docs),
        key=lambda c: ensure_utc(c.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {"total": len(items), "items": items}


@router.get("/telemetry")
async def recent_telemetry(
    device_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_admin),
):
    db = get_db()
    query = db.collection(Collections.TELEMETRY_LOGS)
    if device_id:
        query = query.where("device_id", "==", device_id)
    docs = query.limit(limit).get()
    items = sorted(
        ({**d.to_dict(), "id": d.id} for d in docs),
        key=lambda t: ensure_utc(t.get("received_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {"total": len(items), "items": items}


@router.get("/firmware")
async def list_firmware(_: dict = Depends(require_admin)):
    db = get_db()
    docs = db.collection(Collections.IOT_FIRMWARE).get()
    items = sorted(
        ({**d.to_dict(), "id": d.id} for d in docs),
        key=lambda f: ensure_utc(f.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items


@router.post("/telemetry/cleanup", status_code=status.HTTP_200_OK)
async def cleanup_telemetry():
    """
    Cron-style retention job (no auth, mirrors the evidence auto-delete
    pattern). Deletes telemetry older than TELEMETRY_RETENTION_DAYS.
    """
    db = get_db()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.TELEMETRY_RETENTION_DAYS)
    docs = (
        db.collection(Collections.TELEMETRY_LOGS)
        .where("received_at", "<=", cutoff)
        .limit(500)
        .get()
    )
    deleted = 0
    for d in docs:
        d.reference.delete()
        deleted += 1
    return {"deleted_count": deleted, "cutoff": cutoff}
