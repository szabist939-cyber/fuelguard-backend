from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel


class FraudAlertType(str, Enum):
    FLOW_DISCREPANCY  = "flow_discrepancy"
    TAMPER_DETECTED   = "tamper_detected"
    DUPLICATE_SESSION = "duplicate_session"
    PRICE_MISMATCH    = "price_mismatch"
    UNUSUAL_VOLUME    = "unusual_volume"
    BLACKLISTED_USER  = "blacklisted_user"
    PATTERN_ANOMALY   = "pattern_anomaly"
    MANUAL_FLAG       = "manual_flag"
    BLACKLIST_ESCALATION = "blacklist_escalation"


class FraudAlertStatus(str, Enum):
    OPEN           = "open"
    REVIEWED       = "reviewed"
    RESOLVED       = "resolved"
    FALSE_POSITIVE = "false_positive"


class FraudSeverity(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class BlacklistEntityType(str, Enum):
    USER    = "user"
    VEHICLE = "vehicle"
    DEVICE  = "device"


class FraudAlertResponse(BaseModel):
    id: str
    alert_type: str
    transaction_id: Optional[str] = None
    nozzle_id: Optional[str] = None
    user_id: Optional[str] = None
    description: str
    severity: str
    status: str
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


class FraudAlertResolve(BaseModel):
    status: FraudAlertStatus
    resolution_note: str


class FraudFlagRequest(BaseModel):
    transaction_id: str
    reason: str
    severity: FraudSeverity = FraudSeverity.MEDIUM
    # Sensor readings from the hardware session. When supplied, the flagging
    # engine compares the discrepancy against the configured percentage
    # threshold and flags the attendant and station behind the transaction.
    dispenser_litres: Optional[float] = None
    flowmeter_litres: Optional[float] = None
    discrepancy_percent: Optional[float] = None


class BlacklistEntry(BaseModel):
    entity_type: BlacklistEntityType
    entity_id: str
    reason: str
    flagged_by: Optional[str] = None


class BlacklistResponse(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    reason: str
    flagged_by: Optional[str] = None
    created_at: Optional[datetime] = None


class FraudStats(BaseModel):
    total_alerts: int
    open_alerts: int
    resolved_this_week: int
    by_type: dict
    by_severity: dict
