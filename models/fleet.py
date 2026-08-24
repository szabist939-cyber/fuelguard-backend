from pydantic import BaseModel, Field
from typing import Optional, List
from enum import Enum
from datetime import datetime, date


class FuelCategory(str, Enum):
    FUEL        = "fuel"
    MAINTENANCE = "maintenance"
    TOLL        = "toll"
    PARKING     = "parking"
    OTHER       = "other"


class VehicleCreate(BaseModel):
    registration_number: str
    make: str
    model: str
    year: int
    fuel_type: str
    tank_capacity: float = Field(gt=0, description="Tank capacity in litres")
    owner_uid: Optional[str] = None


class VehicleUpdate(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    fuel_type: Optional[str] = None
    tank_capacity: Optional[float] = None
    is_active: Optional[bool] = None


class VehicleResponse(BaseModel):
    id: str
    registration_number: str
    make: str
    model: str
    year: int
    fuel_type: str
    tank_capacity: float
    owner_uid: Optional[str] = None
    assigned_driver_uid: Optional[str] = None
    is_active: bool = True
    total_fuel_consumed: float = 0.0
    total_expense: float = 0.0
    created_at: Optional[datetime] = None


class ExpenseCreate(BaseModel):
    vehicle_id: str
    category: FuelCategory = FuelCategory.FUEL
    amount: float = Field(gt=0)
    litres: Optional[float] = None
    station_id: Optional[str] = None
    description: Optional[str] = None
    expense_date: Optional[date] = None


class ExpenseResponse(BaseModel):
    id: str
    vehicle_id: str
    user_id: str
    category: FuelCategory
    amount: float
    litres: Optional[float] = None
    station_id: Optional[str] = None
    description: Optional[str] = None
    expense_date: Optional[date] = None
    created_at: Optional[datetime] = None


class CategoryBreakdown(BaseModel):
    """One row of the fleet-wide expense breakdown."""
    category: FuelCategory
    amount: float
    count: int
    percentage: float = Field(ge=0, le=100, description="Share of total_amount")
    litres: Optional[float] = None  # only meaningful for the fuel category


class ExpenseSummaryResponse(BaseModel):
    """
    Aggregate view across the whole fleet (or one vehicle when filtered).

    Every category in FuelCategory is always present — a zero row is more useful
    to a chart than a missing key, and it keeps the UI free of null checks.
    """
    total_amount: float
    total_count: int
    total_litres: float = 0.0
    vehicle_id: Optional[str] = None
    month: Optional[int] = None
    year: Optional[int] = None
    by_category: List[CategoryBreakdown] = []


class BudgetSet(BaseModel):
    vehicle_id: Optional[str] = None
    month: int = Field(ge=1, le=12)
    year: int
    amount: float = Field(gt=0)


class BudgetResponse(BaseModel):
    id: str
    user_id: str
    vehicle_id: Optional[str] = None
    month: int
    year: int
    budget_amount: float
    spent_amount: float = 0.0
    remaining: float = 0.0


class DriverCreate(BaseModel):
    full_name: str
    phone: str
    license_number: str
    uid: Optional[str] = None


class DriverResponse(BaseModel):
    id: str
    full_name: str
    phone: str
    license_number: str
    uid: Optional[str] = None
    assigned_vehicle_id: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None


class DriverAssign(BaseModel):
    driver_uid: str
