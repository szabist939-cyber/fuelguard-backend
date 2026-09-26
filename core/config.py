from __future__ import annotations

import logging
from typing import List

from pydantic import Field, field_validator

logger = logging.getLogger(__name__)
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Fuel Guard API"
    APP_ENV: str  = "development"
    API_V1_PREFIX: str = "/api/v1"

    # Comma-separated list of allowed CORS origins.
    # Override in production: ALLOWED_ORIGINS="https://admin.fuelguard.com,https://app.fuelguard.com"
    # Stored as a raw string (not List[str]) because pydantic-settings tries to
    # JSON-decode list-typed env values, which breaks on a plain comma-separated string.
    ALLOWED_ORIGINS: str = "http://localhost:3000,http://localhost:8080"

    @property
    def allowed_origins(self) -> List[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    # ── Firebase ──────────────────────────────────────────────────────────────
    # For local dev: path to serviceAccountKey.json
    # For Railway/production: set FIREBASE_CREDENTIALS_JSON instead (see firebase.py)
    FIREBASE_CREDENTIALS_PATH: str = "serviceAccountKey.json"
    FIREBASE_PROJECT_ID: str = "fuelguard-f3112"
    # Web API key — used only for the Identity Toolkit REST API (password-reset email).
    # Find it in: Firebase Console → Project Settings → General → Web API Key
    FIREBASE_API_KEY: str = ""

    # ── JWT ───────────────────────────────────────────────────────────────────
    # MUST be overridden in production via environment variable.
    # Generate a strong key: python -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET_KEY: str = Field(default="change-me-in-production")
    JWT_ALGORITHM: str  = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int   = 30

    # ── Email (SMTP) ──────────────────────────────────────────────────────────
    SMTP_HOST: str     = "smtp.gmail.com"
    SMTP_PORT: int     = 587
    SMTP_USER: str     = ""
    SMTP_PASSWORD: str = ""
    EMAIL_FROM: str    = "noreply@fuelguard.com"

    # ── OTP ───────────────────────────────────────────────────────────────────
    OTP_EXPIRE_MINUTES: int = 10

    # ── AI ────────────────────────────────────────────────────────────────────
    GROQ_API_KEY: str = ""

    # ── Cloudinary (evidence photo storage) ──────────────────────────────────
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str    = ""
    CLOUDINARY_API_SECRET: str = ""

    # ── Evidence retention ────────────────────────────────────────────────────
    EVIDENCE_RETENTION_DAYS: int = 90

    # ── Rate limiting (per client IP, sliding one-minute windows) ─────────────
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = 120
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_IOT_PER_MINUTE: int = 600
    RATE_LIMIT_WRITE_PER_MINUTE: int = 60

    # ── IoT ───────────────────────────────────────────────────────────────────
    TELEMETRY_RETENTION_DAYS: int = 30

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def jwt_secret_must_be_strong(cls, v: str) -> str:
        # Hard validation deferred to validate_production_settings() at startup
        return v

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()


def validate_production_settings() -> None:
    """
    Called at startup. Raises ValueError for any unsafe production config.
    Safe to call in development — checks are gated on APP_ENV=production.
    """
    if not settings.is_production:
        return

    if settings.JWT_SECRET_KEY in ("change-me-in-production", "change-me", ""):
        raise ValueError(
            "JWT_SECRET_KEY must be set to a strong random value in production. "
            "Run: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(settings.JWT_SECRET_KEY) < 32:
        raise ValueError("JWT_SECRET_KEY must be at least 32 characters long in production.")
    if not settings.FIREBASE_PROJECT_ID:
        raise ValueError("FIREBASE_PROJECT_ID is required in production.")
    if not settings.SMTP_USER:
        logger.warning(
            "SMTP_USER is not set — email features (SMTP) will be unavailable. "
            "Password reset uses Firebase Auth and does not require SMTP."
        )
    if not settings.FIREBASE_API_KEY:
        logger.warning(
            "FIREBASE_API_KEY is not set — the forgot-password endpoint will fail at runtime. "
            "Find it in: Firebase Console → Project Settings → General → Web API Key."
        )
