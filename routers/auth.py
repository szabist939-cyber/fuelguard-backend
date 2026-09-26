from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import httpx
from firebase_admin import auth as firebase_auth
from fastapi import APIRouter, Depends, HTTPException, status

from core.config import settings
from core.dependencies import get_current_user
from core.firebase import Collections, get_db
from core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from models.user import (
    ChatbotMessage,
    ChatbotResponse,
    ForgotPasswordRequest,
    PasswordChange,
    PasswordReset,
    RefreshTokenRequest,
    TokenResponse,
    UserCreate,
    UserCreateWithFirebase,
    UserLogin,
    UserLoginWithFirebase,
)
from services.auth_service import (
    create_firebase_auth_user,
    get_firebase_user_email,
    query_chatbot,
    send_firebase_password_reset,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _verify_via_firebase(email: str, password: str) -> bool:
    """
    Verify email+password against Firebase Auth using the Identity Toolkit REST API.
    Used as a fallback when the Firestore bcrypt hash is stale (e.g. after a
    browser-based Firebase password reset that didn't sync back to Firestore).
    Returns False instead of raising so the caller can return a clean 401.
    """
    if not settings.FIREBASE_API_KEY:
        return False
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
                f"?key={settings.FIREBASE_API_KEY}",
                json={"email": email, "password": password, "returnSecureToken": False},
            )
            return resp.status_code == 200
    except Exception:
        logger.exception("Firebase fallback auth check failed for %s", email)
        return False


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def signup(payload: UserCreateWithFirebase):
    """
    Flutter creates the Firebase Auth account first (client-side), then sends the
    resulting ID token here. The backend verifies the token, creates the Firestore
    profile using the Firebase UID as the document key, and returns a JWT pair.
    No raw password ever reaches the backend.
    """
    try:
        decoded = firebase_auth.verify_id_token(payload.firebase_id_token)
    except Exception as exc:
        logger.warning("Signup: invalid Firebase ID token — %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired Firebase token. Please try again.",
        )

    uid   = decoded["uid"]
    email = decoded.get("email", "")

    db = get_db()

    # Idempotent — if the profile already exists just return fresh tokens.
    existing_doc = db.collection(Collections.USERS).document(uid).get()
    if existing_doc.exists:
        user = existing_doc.to_dict()
        token_payload = {"uid": uid, "role": user.get("role", "customer")}
        return TokenResponse(
            access_token=create_access_token(token_payload),
            refresh_token=create_refresh_token(token_payload),
            uid=uid,
            role=user.get("role", "customer"),
        )

    user_data = {
        "uid":            uid,
        "email":          email,
        "full_name":      payload.full_name,
        "phone":          payload.phone,
        "role":           payload.role.value,
        "avatar_url":     None,
        "is_active":      True,
        "email_verified": decoded.get("email_verified", False),
        "created_at":     datetime.now(timezone.utc),
    }
    db.collection(Collections.USERS).document(uid).set(user_data)

    token_payload = {"uid": uid, "role": payload.role.value}
    return TokenResponse(
        access_token=create_access_token(token_payload),
        refresh_token=create_refresh_token(token_payload),
        uid=uid,
        role=payload.role.value,
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: UserLoginWithFirebase):
    """
    Flutter signs into Firebase Auth client-side and sends the ID token here.
    The backend verifies the token and looks up the user profile by UID (direct
    document fetch — no collection query needed).
    """
    try:
        decoded = firebase_auth.verify_id_token(payload.firebase_id_token)
    except Exception as exc:
        logger.warning("Login: invalid Firebase ID token — %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please sign in again.",
        )

    uid = decoded["uid"]

    db  = get_db()
    doc = db.collection(Collections.USERS).document(uid).get()
    if not doc.exists:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account not found. Please sign up first.",
        )

    user = doc.to_dict()
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact support.",
        )

    token_payload = {"uid": uid, "role": user["role"]}
    return TokenResponse(
        access_token=create_access_token(token_payload),
        refresh_token=create_refresh_token(token_payload),
        uid=uid,
        role=user["role"],
    )


@router.post("/login-password", response_model=TokenResponse)
async def login_with_password(payload: UserLogin):
    """
    Email + password login for the web admin panel.

    Checks the bcrypt hash stored in Firestore, falling back to Firebase's
    Identity Toolkit REST API when the hash is stale (e.g. after a reset that
    only updated Firebase Auth). The Flutter app is unaffected — it keeps
    using Firebase ID tokens through /auth/login.
    """
    db  = get_db()
    docs = db.collection(Collections.USERS).where("email", "==", payload.email).limit(1).get()
    if not docs:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    user = docs[0].to_dict()

    password_ok = False
    if user.get("password_hash"):
        password_ok = verify_password(payload.password, user["password_hash"])
    if not password_ok:
        password_ok = await _verify_via_firebase(payload.email, payload.password)
    if not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact support.",
        )

    token_payload = {"uid": user["uid"], "role": user["role"]}
    return TokenResponse(
        access_token=create_access_token(token_payload),
        refresh_token=create_refresh_token(token_payload),
        uid=user["uid"],
        role=user["role"],
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(_: dict = Depends(get_current_user)):
    # Stateless JWT — the client discards its tokens.
    # For full revocation, add the jti claim to a Firestore denylist here.
    return {"message": "Logged out successfully"}


@router.post("/refresh-token", response_model=TokenResponse)
async def refresh_token(payload: RefreshTokenRequest):
    try:
        data = decode_token(payload.refresh_token)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    if data.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Provided token is not a refresh token",
        )

    uid  = data["uid"]
    role = data["role"]
    token_payload = {"uid": uid, "role": role}
    return TokenResponse(
        access_token=create_access_token(token_payload),
        refresh_token=create_refresh_token(token_payload),
        uid=uid,
        role=role,
    )


@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(payload: ForgotPasswordRequest):
    """
    Trigger a Firebase Auth password-reset email.

    Firebase delivers the email through Google's mail infrastructure — no SMTP
    is required.  The response is always identical regardless of whether the
    email is registered, preventing user-enumeration attacks.
    """
    db   = get_db()
    docs = db.collection(Collections.USERS).where("email", "==", payload.email).limit(1).get()

    if docs:
        try:
            await send_firebase_password_reset(payload.email)
        except httpx.HTTPStatusError as exc:
            # EMAIL_NOT_FOUND means the user exists in Firestore but not yet in
            # Firebase Auth (e.g. created before this feature was deployed).
            # In that case, silently skip — do not leak the account's existence.
            error_body = exc.response.json() if exc.response.content else {}
            firebase_error = error_body.get("error", {}).get("message", "")
            if firebase_error != "EMAIL_NOT_FOUND":
                logger.error(
                    "Firebase password-reset failed for %s: %s",
                    payload.email,
                    firebase_error,
                )
        except Exception:
            logger.exception("Unexpected error sending Firebase password-reset for %s", payload.email)

    return {"message": "If that email is registered, a password reset link has been sent"}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(payload: PasswordReset):
    """
    Sync a Firebase Auth password reset back to Firestore.

    Flow (mobile):
      1. User taps "Forgot password" → Firebase sends a reset-link email.
      2. User opens the link → Firebase's hosted UI → sets a new password.
      3. App signs the user in with Firebase Auth using the new password.
      4. App calls this endpoint with the resulting Firebase ID token and the
         new password so the backend can verify identity and update the bcrypt
         hash stored in Firestore.

    The Firebase ID token is verified server-side using the Admin SDK — the
    client cannot forge or tamper with it.
    """
    try:
        email = get_firebase_user_email(payload.firebase_id_token)
    except firebase_auth.InvalidIdTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Firebase ID token",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    db   = get_db()
    docs = db.collection(Collections.USERS).where("email", "==", email).limit(1).get()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    user = docs[0].to_dict()
    db.collection(Collections.USERS).document(user["uid"]).update(
        {"password_hash": hash_password(payload.new_password)}
    )

    # Return fresh tokens so the user is immediately logged in after resetting.
    token_payload = {"uid": user["uid"], "role": user["role"]}
    return {
        "message":       "Password reset successfully",
        "access_token":  create_access_token(token_payload),
        "refresh_token": create_refresh_token(token_payload),
        "uid":           user["uid"],
        "role":          user["role"],
        "token_type":    "bearer",
    }


@router.post("/chatbot", response_model=ChatbotResponse)
async def chatbot(payload: ChatbotMessage, current_user: dict = Depends(get_current_user)):
    reply = await query_chatbot(payload.message, current_user)
    return ChatbotResponse(reply=reply)
