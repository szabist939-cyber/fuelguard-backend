from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Entity vocabulary ─────────────────────────────────────────────────────────

class FlagEntityType(str, Enum):
    EMPLOYEE       = "employee"
    STATION        = "station"
    CUSTOMER       = "customer"
    PAYMENT_METHOD = "payment_method"


class FlagSource(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL    = "manual"
    DISPUTE   = "dispute"


class FlagTrigger(str, Enum):
    FLOW_DISCREPANCY       = "flow_discrepancy"
    TAMPER_DETECTED        = "tamper_detected"
    UNDER_DISPENSING       = "under_dispensing_pattern"
    INVALID_CLAIM          = "invalid_claim"
    DISPUTED_VALID_TXN     = "disputed_valid_transaction"
    CHARGEBACK_PATTERN     = "chargeback_pattern"
    PRICE_MANIPULATION     = "price_mismatch"
    MANUAL_REVIEW          = "manual_review"


class FlagStatus(str, Enum):
    ACTIVE    = "active"
    DISMISSED = "dismissed"


class BlacklistStatus(str, Enum):
    ACTIVE       = "active"
    UNDER_REVIEW = "under_review"
    REMOVED      = "removed"


# ── Rules (the single source of truth for every threshold) ────────────────────

class FlaggingRules(BaseModel):
    discrepancy_threshold_percent: float = Field(default=5.0, gt=0, le=100)
    flag_window_days: int                = Field(default=30, ge=1, le=365)
    employee_flags_to_blacklist: int     = Field(default=3, ge=1)
    station_flags_to_blacklist: int      = Field(default=4, ge=1)
    customer_flags_to_blacklist: int     = Field(default=3, ge=1)
    payment_flags_to_flag: int           = Field(default=3, ge=1)
    level_2_flags: int                   = Field(default=5, ge=1)
    level_3_flags: int                   = Field(default=7, ge=1)
    auto_blacklist_enabled: bool         = True
    reduced_visibility_enabled: bool     = True
    updated_by: Optional[str]            = None
    updated_at: Optional[datetime]       = None


class FlaggingRulesUpdate(BaseModel):
    discrepancy_threshold_percent: Optional[float] = Field(default=None, gt=0, le=100)
    flag_window_days: Optional[int]                = Field(default=None, ge=1, le=365)
    employee_flags_to_blacklist: Optional[int]     = Field(default=None, ge=1)
    station_flags_to_blacklist: Optional[int]      = Field(default=None, ge=1)
    customer_flags_to_blacklist: Optional[int]     = Field(default=None, ge=1)
    payment_flags_to_flag: Optional[int]           = Field(default=None, ge=1)
    level_2_flags: Optional[int]                   = Field(default=None, ge=1)
    level_3_flags: Optional[int]                   = Field(default=None, ge=1)
    auto_blacklist_enabled: Optional[bool]         = None
    reduced_visibility_enabled: Optional[bool]     = None


# ── Flags ─────────────────────────────────────────────────────────────────────

class ManualFlagRequest(BaseModel):
    entity_type: FlagEntityType
    entity_id: str
    trigger_type: FlagTrigger = FlagTrigger.MANUAL_REVIEW
    description: str          = Field(min_length=4, max_length=1000)
    severity: str             = "medium"
    transaction_id: Optional[str] = None
    station_id: Optional[str]     = None
    nozzle_id: Optional[str]      = None


class DisputeRequest(BaseModel):
    """Records a payment dispute or chargeback against a transaction."""
    transaction_id: str
    reason: str            = Field(min_length=4, max_length=1000)
    is_chargeback: bool    = False


class FlagDismissRequest(BaseModel):
    note: str = Field(min_length=4, max_length=1000)


class FlagResponse(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    entity_name: Optional[str] = None
    source: str
    trigger_type: str
    severity: str
    description: str
    status: str
    transaction_id: Optional[str] = None
    nozzle_id: Optional[str]      = None
    station_id: Optional[str]     = None
    discrepancy_percent: Optional[float] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    dismissed_by: Optional[str] = None
    dismissed_at: Optional[datetime] = None
    dismissal_note: Optional[str] = None


# ── Blacklist ─────────────────────────────────────────────────────────────────

class ManualBlacklistRequest(BaseModel):
    entity_type: FlagEntityType
    entity_id: str
    level: int = Field(ge=1, le=3)
    reason: str = Field(min_length=4, max_length=1000)
    station_id: Optional[str] = None


class BlacklistUpdateRequest(BaseModel):
    level: Optional[int]              = Field(default=None, ge=1, le=3)
    status: Optional[BlacklistStatus] = None
    note: Optional[str]               = Field(default=None, max_length=1000)


class BlacklistResponse(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    entity_name: Optional[str] = None
    level: int
    status: str
    reason: Optional[str] = None
    restrictions: Optional[Dict[str, Any]] = None
    flags_count: int = 0
    window_days: int = 30
    auto_escalated: bool = False
    flagged_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    removed_by: Optional[str] = None
    removed_at: Optional[datetime] = None
    removal_note: Optional[str] = None


# ── Read models ───────────────────────────────────────────────────────────────

class EntityHistoryResponse(BaseModel):
    entity_type: str
    entity_id: str
    entity_name: Optional[str] = None
    flags: List[FlagResponse]
    flags_in_window: int
    blacklist: Optional[BlacklistResponse] = None


class FlaggingStats(BaseModel):
    total_active_flags: int
    flags_last_30d: int
    dismissals_total: int
    escalations_last_30d: int
    active_blacklist: Dict[str, int]  # {"employee": n, "station": n, ...}
    by_level: Dict[str, int]          # {"level_1": n, "level_2": n, "level_3": n}
    by_trigger: Dict[str, int]
    by_entity_type: Dict[str, int]


class FraudRiskStatus(BaseModel):
    """Embedded into station payloads consumed by the mobile app."""
    is_blacklisted: bool = False
    level: Optional[int] = None
    level_label: Optional[str] = None
    warning_message: Optional[str] = None

    @classmethod
    def clean(cls) -> "FraudRiskStatus":
        return cls()

    @classmethod
    def flagged(cls, level: int) -> "FraudRiskStatus":
        labels  = {1: "Watchlisted", 2: "High Fraud Risk", 3: "High Fraud Risk"}
        message = {
            1: "This station is under monitoring for fraud related activity.",
            2: "Customers have reported fraud at this station. Consider an alternative.",
            3: "This station is blacklisted for repeated fraud. FuelGuard does not recommend fueling here.",
        }
        return cls(
            is_blacklisted=True,
            level=level,
            level_label=labels.get(level, "High Fraud Risk"),
            warning_message=message.get(level),
        )
