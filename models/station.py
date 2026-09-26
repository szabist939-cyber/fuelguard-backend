from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class StationCreate(BaseModel):
    name: str
    address: str
    city: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    contact_phone: Optional[str] = None
    manager_uid: Optional[str] = None
    fuel_types_available: List[str] = []
    operating_hours: Optional[str] = None


class StationUpdate(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    contact_phone: Optional[str] = None
    manager_uid: Optional[str] = None
    fuel_types_available: Optional[List[str]] = None
    operating_hours: Optional[str] = None
    is_active: Optional[bool] = None


class StationResponse(BaseModel):
    id: str
    name: str
    address: str
    city: str
    latitude: float
    longitude: float
    contact_phone: Optional[str] = None
    manager_uid: Optional[str] = None
    fuel_types_available: List[str] = []
    operating_hours: Optional[str] = None
    is_active: bool = True
    distance_km: Optional[float] = None  # populated in nearby searches
    current_prices: Optional[dict] = None
    fraud_risk: Optional[dict] = None    # blacklist status, attached by the stations router
    created_at: Optional[datetime] = None


class NearbyRequest(BaseModel):
    latitude: float
    longitude: float
    radius_km: float = 10.0
    fuel_type: Optional[str] = None


class RouteRequest(BaseModel):
    origin_lat: float
    origin_lng: float
    dest_lat: float
    dest_lng: float
    fuel_type: Optional[str] = None
