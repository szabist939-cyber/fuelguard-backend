from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from io import BytesIO
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.uploader
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from core.config import settings
from core.dependencies import get_current_user, require_admin
from core.firebase import Collections, get_db
from models.evidence import EvidenceResponse

logger = logging.getLogger(__name__)

router = APIRouter()
PKT = ZoneInfo("Asia/Karachi")


def _init_cloudinary() -> bool:
    """Configure Cloudinary from settings. Returns False if credentials are missing."""
    if not all([settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET]):
        logger.warning("Cloudinary credentials not configured — evidence upload will fail")
        return False
    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    return True


@router.post("", response_model=EvidenceResponse, status_code=status.HTTP_201_CREATED)
async def upload_evidence(
    transaction_id: str,
    nozzle_id: str = "manual",
    session_id: str | None = None,
    capture_trigger: str = "manual",
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    db = get_db()

    tx_doc = db.collection(Collections.TRANSACTIONS).document(transaction_id).get()
    if not tx_doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")

    if not _init_cloudinary():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Evidence storage not configured",
        )

    evidence_id = str(uuid.uuid4())
    captured_at = datetime.now(timezone.utc)
    contents = await file.read()

    # ── Upload original to Cloudinary ─────────────────────────────────────────
    public_id = f"fuelguard/evidence/{transaction_id}/{evidence_id}"
    upload_result = cloudinary.uploader.upload(
        BytesIO(contents),
        public_id=public_id,
        resource_type="image",
        overwrite=False,
        tags=[f"txn:{transaction_id}", f"nozzle:{nozzle_id}", capture_trigger],
    )
    image_url = upload_result["secure_url"]

    # ── Generate thumbnail URL via Cloudinary transformation (no extra upload) ─
    thumbnail_url: str | None = None
    try:
        thumbnail_url = cloudinary.CloudinaryImage(public_id).build_url(
            width=320, height=320, crop="limit", secure=True
        )
    except Exception:
        logger.exception("Thumbnail URL generation failed for evidence %s", evidence_id)

    # ── Save metadata to Firestore ─────────────────────────────────────────────
    delete_at = datetime.now(timezone.utc) + timedelta(days=settings.EVIDENCE_RETENTION_DAYS)
    watermark_txt = (
        f"TXN:{transaction_id} | "
        f"{captured_at.astimezone(PKT).strftime('%Y-%m-%d %H:%M:%S')} PKT"
    )

    doc_data = {
        "id":               evidence_id,
        "transaction_id":   transaction_id,
        "nozzle_id":        nozzle_id,
        "session_id":       session_id,
        "image_url":        image_url,
        "thumbnail_url":    thumbnail_url,
        "file_size_kb":     round(len(contents) / 1024, 2),
        "capture_trigger":  capture_trigger,
        "cloudinary_public_id": public_id,
        "metadata": {
            "uploaded_by":       current_user["uid"],
            "original_filename": file.filename,
            "capture_time_pkt":  captured_at.astimezone(PKT).isoformat(),
        },
        "watermark_text": watermark_txt,
        "delete_at":      delete_at,
        "created_at":     datetime.now(timezone.utc),
    }

    db.collection(Collections.EVIDENCE).document(evidence_id).set(doc_data)
    db.collection(Collections.TRANSACTIONS).document(transaction_id).update(
        {"evidence_url": image_url}
    )

    return EvidenceResponse(**doc_data)


@router.get("/{transaction_id}")
async def get_evidence_for_transaction(
    transaction_id: str,
    _: dict = Depends(get_current_user),
):
    db = get_db()
    docs = db.collection(Collections.EVIDENCE).where("transaction_id", "==", transaction_id).get()
    return [{"id": d.id, **d.to_dict()} for d in docs]


@router.get("/{evidence_id}/image")
async def get_evidence_image(evidence_id: str, _: dict = Depends(get_current_user)):
    db = get_db()
    doc = db.collection(Collections.EVIDENCE).document(evidence_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")
    data = doc.to_dict()
    return {"image_url": data.get("image_url"), "thumbnail_url": data.get("thumbnail_url")}


@router.delete("/{evidence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_evidence(evidence_id: str, _: dict = Depends(require_admin)):
    db = get_db()
    doc = db.collection(Collections.EVIDENCE).document(evidence_id).get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")

    data = doc.to_dict()
    public_id = data.get("cloudinary_public_id")

    # Delete from Cloudinary
    if public_id and _init_cloudinary():
        try:
            cloudinary.uploader.destroy(public_id, resource_type="image")
        except Exception:
            logger.exception("Failed to delete Cloudinary asset %s", public_id)

    db.collection(Collections.EVIDENCE).document(evidence_id).delete()


@router.post("/auto-delete", status_code=status.HTTP_200_OK)
async def auto_delete_expired_evidence():
    """Background job — enforces the evidence retention policy."""
    db = get_db()
    now = datetime.now(timezone.utc)
    docs = db.collection(Collections.EVIDENCE).where("delete_at", "<=", now).get()

    cloudinary_ready = _init_cloudinary()
    deleted = 0
    for doc in docs:
        data = doc.to_dict()
        public_id = data.get("cloudinary_public_id")
        if public_id and cloudinary_ready:
            try:
                cloudinary.uploader.destroy(public_id, resource_type="image")
            except Exception:
                logger.exception("Cloudinary delete failed for %s", public_id)
        db.collection(Collections.EVIDENCE).document(doc.id).delete()
        deleted += 1

    return {"deleted_count": deleted}


@router.get("")
async def list_all_evidence(
    nozzle_id: str | None = Query(None),
    capture_trigger: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(require_admin),
):
    db = get_db()
    query = db.collection(Collections.EVIDENCE)
    if nozzle_id:
        query = query.where("nozzle_id", "==", nozzle_id)
    if capture_trigger:
        query = query.where("capture_trigger", "==", capture_trigger)

    docs = query.order_by("created_at", direction="DESCENDING").get()
    items = [{"id": d.id, **d.to_dict()} for d in docs]
    return {"total": len(items), "items": items[offset: offset + limit]}
