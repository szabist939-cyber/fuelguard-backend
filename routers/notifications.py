from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.dependencies import get_current_user
from core.firebase import Collections, ensure_utc, get_db

router = APIRouter()


@router.get("")
async def list_my_notifications(
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    """
    The caller's own notifications, newest first. The flagging engine writes
    here on blacklist escalations; transaction events write here on completion.
    """
    db = get_db()
    docs = (
        db.collection(Collections.NOTIFICATIONS)
        .where("audience", "==", "user")
        .where("user_id", "==", current_user["uid"])
        .limit(limit)
        .get()
    )
    items = sorted(
        ({**d.to_dict(), "id": d.id} for d in docs),
        key=lambda n: ensure_utc(n.get("created_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {"total": len(items), "items": items}


@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    current_user: dict = Depends(get_current_user),
):
    db = get_db()
    ref = db.collection(Collections.NOTIFICATIONS).document(notification_id)
    doc = ref.get()
    if not doc.exists:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    if doc.to_dict().get("user_id") != current_user["uid"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your notification")
    ref.update({"is_read": True, "read_at": datetime.now(timezone.utc)})
    return {"message": "Marked read"}
