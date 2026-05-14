# routers/auth.py
#
# Phase 2 — Backend: Authentication System
#
# This router is the entry-point for all auth flows. It does NOT use
# resolve_tenant as a dependency — it IS the dependency that creates
# tenant context for the first time.
#
# Endpoints:
#   POST /auth/admin/signup          — Create admin account + tenant (trial)
#   POST /auth/admin/login           — Admin login → Supabase JWT session
#   POST /auth/mobile/signup         — Employee signup via company join code
#   POST /auth/mobile/login          — Mobile login → Supabase JWT session
#   POST /auth/refresh               — Refresh an expired access token
#
# Join-code management lives in routers/admin.py (GET + POST /admin/join-code/*)
# because those routes require an authenticated admin context.

from __future__ import annotations

import random
import re
import string

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr

from services.supabase_client import get_supabase_admin
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Request / Response schemas ────────────────────────────────────────────────

class AdminSignupRequest(BaseModel):
    email:        EmailStr
    password:     str
    company_name: str


class AdminLoginRequest(BaseModel):
    email:    EmailStr
    password: str


class MobileSignupRequest(BaseModel):
    email:     EmailStr
    password:  str
    join_code: str


class RefreshRequest(BaseModel):
    refresh_token: str


# ── Helpers ───────────────────────────────────────────────────────────────────

# Predefined word list for memorable join codes  e.g. "SHIP-4829"
_JOIN_WORDS: list[str] = [
    "SHIP", "DOCK", "CREW", "MAST", "SAIL", "PORT", "HULL", "DECK",
    "KEEL", "HELM", "TIDE", "WAVE", "REEF", "BUOY", "LANE", "WIND",
    "BOLT", "CRANE", "LOCK", "PIER", "ROPE", "TANK", "YARD", "STAR",
    "GULF", "CAPE", "COVE", "ISLE", "QUAY", "BRIG",
]


def _slugify(name: str) -> str:
    """Convert a company name into a safe, lowercase slug.

    "Acme Shipping Ltd." → "acme_shipping_ltd"
    """
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:40] or "tenant"


def _gen_join_code() -> str:
    """Generate a human-friendly join code like SHIP-4829."""
    word   = random.choice(_JOIN_WORDS)
    digits = random.randint(1000, 9999)
    return f"{word}-{digits}"


def _ensure_unique_slug(sb, base_slug: str) -> str:
    """Return base_slug if available, otherwise append a numeric suffix."""
    slug = base_slug
    attempt = 0
    while True:
        result = (
            sb.table("tenants")
            .select("id")
            .eq("slug", slug)
            .execute()
        )
        if not result.data:
            return slug
        attempt += 1
        suffix = "".join(random.choices(string.digits, k=3))
        slug   = f"{base_slug}_{suffix}"


def _ensure_unique_join_code(sb) -> str:
    """Generate a join code that does not already exist in the DB."""
    for _ in range(20):          # 20 attempts before giving up
        code   = _gen_join_code()
        result = (
            sb.table("tenants")
            .select("id")
            .eq("join_code", code)
            .execute()
        )
        if not result.data:
            return code
    # Fallback: 8-char random alphanum (very unlikely collision)
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


def _get_starter_plan_id(sb) -> str:
    """Fetch the 'Starter' plan UUID from the plans table."""
    result = (
        sb.table("plans")
        .select("id")
        .eq("name", "Starter")
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Starter plan not found. Run database seed SQL first.",
        )
    return result.data["id"]


def _extract_session(auth_response) -> dict:
    """Extract access_token / refresh_token from a Supabase auth response."""
    session = getattr(auth_response, "session", None)
    if session is None:
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Authentication failed — no session returned.",
        )
    return {
        "access_token" : session.access_token,
        "refresh_token": session.refresh_token,
        "token_type"   : "bearer",
    }


# ── POST /auth/admin/signup ───────────────────────────────────────────────────

@router.post("/admin/signup", status_code=status.HTTP_201_CREATED)
async def admin_signup(body: AdminSignupRequest):
    """
    Create a new admin account and an associated tenant (status=trial, plan=Starter).

    Steps:
      1. Create Supabase Auth user (email verify required).
      2. Derive unique tenant slug from company name.
      3. Generate a unique join code for employees to use.
      4. Insert tenant row (trial, Starter plan).
      5. Insert tenant_usage row (zeros).
      6. Insert tenant_members row (role=admin).

    Returns a message asking the user to check their email.
    JWT is NOT returned — user must verify email then call /auth/admin/login.
    """
    sb = get_supabase_admin()

    # ── 1. Create Supabase Auth user ──────────────────────────────────────────
    try:
        auth_response = sb.auth.admin.create_user(
            {
                "email"        : body.email,
                "password"     : body.password,
                "email_confirm": True,   # Supabase sends verification email
            }
        )
        user = getattr(auth_response, "user", None)
        if user is None:
            raise HTTPException(
                status_code = status.HTTP_400_BAD_REQUEST,
                detail      = "Failed to create account. Check email and password.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        msg = str(exc)
        logger.error("[AUTH/ADMIN/SIGNUP] Supabase create_user error: %s", msg)
        if "already registered" in msg.lower() or "email address is already" in msg.lower():
            raise HTTPException(
                status_code = status.HTTP_409_CONFLICT,
                detail      = "An account with this email already exists.",
            )
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Could not create account: {msg}",
        )

    user_id = user.id
    logger.info("[AUTH/ADMIN/SIGNUP] Supabase user created: %s", user_id)

    # ── 2. Derive unique slug ─────────────────────────────────────────────────
    base_slug = _slugify(body.company_name)
    slug      = _ensure_unique_slug(sb, base_slug)

    # ── 3. Generate unique join code ──────────────────────────────────────────
    join_code = _ensure_unique_join_code(sb)

    # ── 4. Fetch Starter plan ID ──────────────────────────────────────────────
    plan_id = _get_starter_plan_id(sb)

    # ── 5. Insert tenant ──────────────────────────────────────────────────────
    try:
        tenant_result = (
            sb.table("tenants")
            .insert(
                {
                    "slug"        : slug,
                    "display_name": body.company_name,
                    "plan_id"     : plan_id,
                    "status"      : "trial",
                    "join_code"   : join_code,
                }
            )
            .execute()
        )
        tenant_id = tenant_result.data[0]["id"]
        logger.info(
            "[AUTH/ADMIN/SIGNUP] Tenant created: slug=%s  id=%s  join_code=%s",
            slug, tenant_id, join_code,
        )
    except Exception as exc:
        # Roll back user creation so the email isn't orphaned
        logger.error("[AUTH/ADMIN/SIGNUP] Tenant insert failed: %s", exc)
        try:
            sb.auth.admin.delete_user(user_id)
        except Exception:
            pass
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Failed to create tenant. Please try again.",
        )

    # ── 6. Insert tenant_usage ────────────────────────────────────────────────
    try:
        sb.table("tenant_usage").insert(
            {
                "tenant_id"  : tenant_id,
                "vector_count": 0,
                "user_count"  : 1,   # the admin counts as 1 user
            }
        ).execute()
    except Exception as exc:
        logger.warning("[AUTH/ADMIN/SIGNUP] tenant_usage insert failed: %s", exc)

    # ── 7. Insert tenant_members ──────────────────────────────────────────────
    try:
        sb.table("tenant_members").insert(
            {
                "tenant_id": tenant_id,
                "user_id"  : user_id,
                "role"     : "admin",
            }
        ).execute()
        logger.info(
            "[AUTH/ADMIN/SIGNUP] tenant_members row created — user=%s  role=admin",
            user_id,
        )
    except Exception as exc:
        logger.error("[AUTH/ADMIN/SIGNUP] tenant_members insert failed: %s", exc)
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Account created but membership setup failed. Contact support.",
        )

    logger.info(
        "[AUTH/ADMIN/SIGNUP] ✅ Admin signup complete — email=%s  slug=%s",
        body.email, slug,
    )

    return {
        "message"  : "Account created. Check your email to verify before logging in.",
        "join_code": join_code,   # returned so admin can share it with employees
        "slug"     : slug,
    }


# ── POST /auth/admin/login ────────────────────────────────────────────────────

@router.post("/auth/admin/login")
@router.post("/admin/login")
async def admin_login(body: AdminLoginRequest):
    """
    Admin login via email + password.

    Returns Supabase JWT session (access_token + refresh_token).
    The JWT's app_metadata carries tenant_id + role injected by the
    custom claims Edge Function hook on every sign-in.
    """
    sb = get_supabase_admin()

    try:
        auth_response = sb.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as exc:
        msg = str(exc).lower()
        logger.warning("[AUTH/ADMIN/LOGIN] Sign-in failed for %s: %s", body.email, msg)
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid email or password.",
        )

    logger.info("[AUTH/ADMIN/LOGIN] ✅ Admin signed in: %s", body.email)
    return _extract_session(auth_response)


# ── POST /auth/mobile/signup ──────────────────────────────────────────────────

@router.post("/mobile/signup", status_code=status.HTTP_201_CREATED)
async def mobile_signup(body: MobileSignupRequest):
    """
    Employee/mobile signup via join code.

    The join code is issued by the admin and ties this signup to a specific tenant.

    Steps:
      1. Look up tenant by join_code (404 if not found).
      2. Read plan limits — check user_count < plan.max_users (400 if full).
      3. Create Supabase Auth user (email_confirm=True — no email verify for mobile).
      4. Insert tenant_members row (role=user).
      5. Increment tenant_usage.user_count by 1.
      6. Sign in immediately to get a JWT with tenant claims.
      7. Return session (access_token + refresh_token).
    """
    sb = get_supabase_admin()

    # ── 1. Look up tenant by join_code ────────────────────────────────────────
    tenant_result = (
        sb.table("tenants")
        .select("*, plans(*)")
        .eq("join_code", body.join_code.strip().upper())
        .single()
        .execute()
    )

    if not tenant_result.data:
        logger.warning(
            "[AUTH/MOBILE/SIGNUP] Invalid join_code: %s", body.join_code
        )
        raise HTTPException(
            status_code = status.HTTP_404_NOT_FOUND,
            detail      = "Invalid join code. Ask your employer for the correct code.",
        )

    tenant    = tenant_result.data
    tenant_id = tenant["id"]
    plan      = tenant.get("plans", {}) or {}

    logger.info(
        "[AUTH/MOBILE/SIGNUP] Join code matched — tenant_id=%s  slug=%s",
        tenant_id, tenant.get("slug"),
    )

    # ── 2. Seat limit check ───────────────────────────────────────────────────
    usage_result = (
        sb.table("tenant_usage")
        .select("user_count")
        .eq("tenant_id", tenant_id)
        .single()
        .execute()
    )
    current_users = (usage_result.data or {}).get("user_count", 0)
    max_users     = plan.get("max_users", 5)

    if current_users >= max_users:
        logger.warning(
            "[AUTH/MOBILE/SIGNUP] Seat limit reached — tenant=%s  "
            "current=%d  max=%d",
            tenant_id, current_users, max_users,
        )
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = {
                "code"   : "seat_limit_reached",
                "message": (
                    "Your organisation has reached its user limit. "
                    "Contact your admin to upgrade the plan."
                ),
            },
        )

    # ── 3. Create Supabase Auth user (confirmed — no email verify for mobile) ─
    try:
        auth_response = sb.auth.admin.create_user(
            {
                "email"        : body.email,
                "password"     : body.password,
                "email_confirm": True,   # auto-confirm for mobile UX
            }
        )
        user = getattr(auth_response, "user", None)
        if user is None:
            raise ValueError("No user returned from create_user")
    except HTTPException:
        raise
    except Exception as exc:
        msg = str(exc)
        logger.error("[AUTH/MOBILE/SIGNUP] create_user error: %s", msg)
        if "already registered" in msg.lower() or "email address is already" in msg.lower():
            raise HTTPException(
                status_code = status.HTTP_409_CONFLICT,
                detail      = "An account with this email already exists.",
            )
        raise HTTPException(
            status_code = status.HTTP_400_BAD_REQUEST,
            detail      = f"Could not create account: {msg}",
        )

    user_id = user.id
    logger.info("[AUTH/MOBILE/SIGNUP] Supabase user created: %s", user_id)

    # ── 4. Insert tenant_members ──────────────────────────────────────────────
    try:
        sb.table("tenant_members").insert(
            {
                "tenant_id": tenant_id,
                "user_id"  : user_id,
                "role"     : "user",
            }
        ).execute()
    except Exception as exc:
        logger.error("[AUTH/MOBILE/SIGNUP] tenant_members insert failed: %s", exc)
        try:
            sb.auth.admin.delete_user(user_id)
        except Exception:
            pass
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail      = "Signup failed. Please try again.",
        )

    # ── 5. Increment tenant_usage.user_count ──────────────────────────────────
    try:
        sb.rpc(
            "increment_user_count",
            {"_tenant_id": tenant_id},
        ).execute()
    except Exception:
        # Fallback: manual upsert (RPC may not exist yet)
        try:
            sb.table("tenant_usage").upsert(
                {
                    "tenant_id" : tenant_id,
                    "user_count": current_users + 1,
                },
                on_conflict="tenant_id",
            ).execute()
        except Exception as exc2:
            logger.warning(
                "[AUTH/MOBILE/SIGNUP] user_count increment failed: %s", exc2
            )

    # ── 6. Sign in immediately so JWT has tenant claims ───────────────────────
    # The custom claims Edge Function fires on every sign-in and injects
    # tenant_id + role into app_metadata. We must sign in here (not just
    # return the user object) to get a JWT with the correct claims.
    try:
        sign_in_response = sb.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as exc:
        logger.error("[AUTH/MOBILE/SIGNUP] Auto sign-in failed: %s", exc)
        # Account was created — just can't auto sign-in. Ask user to log in.
        return {
            "message": (
                "Account created. Please log in with your credentials."
            )
        }

    logger.info(
        "[AUTH/MOBILE/SIGNUP] ✅ Mobile signup + sign-in complete — "
        "email=%s  tenant=%s",
        body.email, tenant.get("slug"),
    )

    session = _extract_session(sign_in_response)
    session["tenant_id"] = tenant_id
    return session


# ── POST /auth/mobile/login ───────────────────────────────────────────────────

@router.post("/mobile/login")
async def mobile_login(body: AdminLoginRequest):  # same fields as admin login
    """
    Mobile / employee login via email + password.

    Identical to admin login — sign_in_with_password + return session.
    The JWT carries tenant context from the custom claims hook.
    """
    sb = get_supabase_admin()

    try:
        auth_response = sb.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except Exception as exc:
        logger.warning(
            "[AUTH/MOBILE/LOGIN] Sign-in failed for %s: %s",
            body.email, exc,
        )
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Invalid email or password.",
        )

    logger.info("[AUTH/MOBILE/LOGIN] ✅ Mobile sign-in: %s", body.email)
    return _extract_session(auth_response)


# ── POST /auth/refresh ────────────────────────────────────────────────────────

@router.post("/refresh")
async def refresh_token(body: RefreshRequest):
    """
    Exchange a refresh_token for a new access_token + refresh_token pair.

    Called by clients when their access_token expires (typically after 1 hour).
    The new JWT will have up-to-date claims injected by the custom claims hook.
    """
    sb = get_supabase_admin()

    try:
        auth_response = sb.auth.refresh_session(body.refresh_token)
    except Exception as exc:
        logger.warning("[AUTH/REFRESH] Token refresh failed: %s", exc)
        raise HTTPException(
            status_code = status.HTTP_401_UNAUTHORIZED,
            detail      = "Session expired or invalid. Please log in again.",
        )

    logger.debug("[AUTH/REFRESH] ✅ Token refreshed")
    return _extract_session(auth_response)


__all__ = ["router"]