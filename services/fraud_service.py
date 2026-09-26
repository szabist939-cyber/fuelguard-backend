from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from core.firebase import Collections, ensure_utc

logger = logging.getLogger(__name__)


def analyze_transaction_for_fraud(db, transaction: dict) -> list[dict[str, Any]]:
    """
    Run automated fraud checks against a completed transaction.

    Each check is independent — a failure in one does not abort the rest.
    All generated alerts are written to Firestore and returned to the caller.

    Checks performed:
      1. Unusual dispensed volume (> 200 L)
      2. Non-zero litres with zero amount charged
      3. Total amount inconsistent with litres × price
      4. Transaction by a blacklisted user
      5. Rapid consecutive transactions on the same nozzle (> 3 in 5 min)
      6. Transaction on a nozzle with an active tamper flag
    """
    alerts: list[dict[str, Any]] = []

    tx_id      = transaction.get("id")
    nozzle_id  = transaction.get("nozzle_id")
    user_id    = transaction.get("user_id")
    litres     = float(transaction.get("litres_dispensed", 0))
    amount     = float(transaction.get("total_amount", 0))
    price      = float(transaction.get("price_per_litre", 0))
    created_at = ensure_utc(transaction.get("created_at"))

    def _create_alert(alert_type: str, description: str, severity: str = "medium") -> dict:
        alert_id = str(uuid.uuid4())
        alert = {
            "id": alert_id,
            "alert_type": alert_type,
            "transaction_id": tx_id,
            "nozzle_id": nozzle_id,
            "user_id": user_id,
            "description": description,
            "severity": severity,
            "status": "open",
            "resolved_by": None,
            "resolution_note": None,
            "created_at": datetime.now(timezone.utc),
        }
        try:
            db.collection(Collections.FRAUD_ALERTS).document(alert_id).set(alert)
            alerts.append(alert)
        except Exception:
            logger.exception("Failed to write fraud alert %s for transaction %s", alert_id, tx_id)
        return alert

    # ── Check 1: Unusual volume ───────────────────────────────────────────────
    if litres > 200:
        _create_alert(
            "unusual_volume",
            f"Transaction dispensed {litres:.2f} L — exceeds 200 L threshold.",
            "high",
        )

    # ── Check 2: Litres dispensed but zero amount charged ────────────────────
    if litres > 0 and amount == 0:
        _create_alert(
            "price_mismatch",
            f"Transaction recorded {litres:.2f} L dispensed but PKR 0 charged.",
            "critical",
        )

    # ── Check 3: Amount inconsistent with litres × price ─────────────────────
    if price > 0 and litres > 0:
        expected = round(litres * price, 2)
        if abs(expected - amount) > 1.0:
            _create_alert(
                "price_mismatch",
                (
                    f"Expected PKR {expected:.2f} (= {litres} L × {price}/L) "
                    f"but recorded PKR {amount:.2f}."
                ),
                "high",
            )

    # ── Check 4: Blacklisted user ─────────────────────────────────────────────
    if user_id:
        try:
            blacklisted = (
                db.collection(Collections.BLACKLIST)
                .where("entity_id", "==", user_id)
                .where("entity_type", "in", ["user", "customer"])
                .where("status", "in", ["active", "under_review"])
                .limit(1)
                .get()
            )
            if blacklisted:
                _create_alert(
                    "blacklisted_user",
                    f"Transaction initiated by blacklisted user {user_id}.",
                    "critical",
                )
        except Exception:
            logger.exception("Blacklist check failed for user %s", user_id)

    # ── Check 5: Rapid consecutive transactions on same nozzle ───────────────
    if created_at and nozzle_id:
        window_start = created_at - timedelta(minutes=5)
        try:
            recent = (
                db.collection(Collections.TRANSACTIONS)
                .where("nozzle_id", "==", nozzle_id)
                .where("created_at", ">=", window_start)
                .where("created_at", "<", created_at)
                .get()
            )
            if len(list(recent)) > 3:
                _create_alert(
                    "pattern_anomaly",
                    f"Nozzle {nozzle_id} processed > 3 transactions in a 5-minute window.",
                    "high",
                )
        except Exception:
            logger.exception("Rapid-transaction check failed for nozzle %s", nozzle_id)

    # ── Check 6: Transaction on a tampered nozzle ────────────────────────────
    if nozzle_id:
        try:
            nozzle_doc = db.collection(Collections.NOZZLES).document(nozzle_id).get()
            if nozzle_doc.exists and nozzle_doc.to_dict().get("tamper_detected"):
                _create_alert(
                    "tamper_detected",
                    f"Transaction processed on nozzle {nozzle_id} which has an active tamper flag.",
                    "critical",
                )
        except Exception:
            logger.exception("Tamper check failed for nozzle %s", nozzle_id)

    return alerts
