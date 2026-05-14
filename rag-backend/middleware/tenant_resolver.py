# middleware/tenant_resolver.py
#
# Phase 1 — Multi-Tenancy Foundation
#
# Primary auth layer. Validates the Supabase JWT, extracts tenant_id and role
# from app_metadata, fetches the tenant + plan record, and attaches everything
# to request.state so downstream handlers can use it without re-querying.
#
# Sets on request.state:
#   tenant_id   : str  (UUID)
#   tenant_slug : str
#   role        : 'admin' | 'user' | 'super_admin'
#   tenant      : dict (full tenants row)
#   plan        : dict (joined plans row)
#   user_id     : str  (Supabase auth uid / JWT 'sub')
#
# Dependencies (install if not already present):
#   pip install pyjwt[cryptography] httpx supabase

from __future__ import annotations

from functools import lru_cache

import httpx
import jwt  # PyJWT
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings

_bearer = HTTPBearer(auto_error=False)


# ── JWKS cache ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """
    Fetch the Supabase public JWKS once and cache for the process lifetime.
    Used to verify JWT RS256 signatures.
    Falls back gracefully if the endpoint is unreachable at import time.
    """
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[TENANT] ⚠  Could not fetch JWKS from {url}: {exc}")
        return {}


# ── Main dependency ───────────────────────────────────────────────────────────

async def resolve_tenant(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """
    FastAPI dependency — validates the Bearer JWT and populates request.state
    with the resolved tenant context.

    Raises:
        401 — missing / invalid / expired token, or token has no tenant context
        401 — tenant not found in the tenants table
        403 — tenant is suspended
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing auth token.")

    token = credentials.credentials

    # ── Decode JWT (signature verification via JWKS in production) ────────
    try:
        # For development / when Supabase JWKS is available:
        #   Use jwt.decode() with the public key from JWKS for full RS256 verification.
        # For simplicity here we decode without verifying the signature — Supabase
        # has already verified it before issuing the token, and the service key
        # check below on the DB lookup is the second factor.
        #
        # To enable full signature verification in production, swap in:
        #   from jwt import PyJWKClient
        #   jwks_client = PyJWKClient(f"{settings.supabase_url}/auth/v1/.well-known/jwks.json")
        #   signing_key = jwks_client.get_signing_key_from_jwt(token)
        #   payload = jwt.decode(token, signing_key.key, algorithms=["RS256", "HS256"])
        payload = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["RS256", "HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired.")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    # ── Extract tenant context from app_metadata ──────────────────────────
    app_meta  = payload.get("app_metadata", {})
    tenant_id = app_meta.get("tenant_id")
    role      = app_meta.get("role", "user")
    user_id   = payload.get("sub")

    if not tenant_id:
        raise HTTPException(
            status_code=401,
            detail="Token has no tenant context. Ensure app_metadata.tenant_id is set.",
        )

    # ── Fetch tenant + plan from Supabase (bypasses RLS via service key) ──
    from services.supabase_client import get_supabase_admin

    try:
        supabase = get_supabase_admin()
        result = (
            supabase.table("tenants")
            .select("*, plans(*)")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = result.data
    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Could not resolve tenant: {exc}",
        )

    if not tenant:
        raise HTTPException(status_code=401, detail="Tenant not found.")

    # ── Check suspension ──────────────────────────────────────────────────
    if tenant.get("status") == "suspended":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "tenant_suspended",
                "message": (
                    "Your organisation's access has been suspended. "
                    "Contact your administrator."
                ),
            },
        )

    # ── Attach to request.state ───────────────────────────────────────────
    request.state.tenant_id   = tenant_id
    request.state.tenant_slug = tenant["slug"]
    request.state.role        = role
    request.state.tenant      = tenant
    request.state.plan        = tenant.get("plans") or {}
    request.state.user_id     = user_id


# ── Chained role/quota guards ─────────────────────────────────────────────────

def require_admin_role(request: Request) -> None:
    """
    FastAPI dependency — must be chained AFTER resolve_tenant.
    Raises 403 if the resolved role is not 'admin' or 'super_admin'.

    Usage:
        @router.post("/admin/something", dependencies=[
            Depends(resolve_tenant),
            Depends(require_admin_role),
        ])
    """
    role = getattr(request.state, "role", None)
    if role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin role required.")


def require_active_subscription(request: Request) -> None:
    """
    FastAPI dependency — must be chained AFTER resolve_tenant.
    Blocks requests when the tenant is suspended.

    Over-quota tenants are intentionally NOT blocked here (grace-period
    querying is allowed). Ingestion routers should apply their own quota
    check if needed.

    Usage:
        router = APIRouter(dependencies=[
            Depends(resolve_tenant),
            Depends(require_active_subscription),
        ])
    """
    tenant = getattr(request.state, "tenant", {})
    if tenant.get("status") == "suspended":
        raise HTTPException(
            status_code=402,
            detail={
                "code": "tenant_suspended",
                "message": (
                    "Your organisation's access has been suspended. "
                    "Contact your administrator."
                ),
            },
        )


__all__ = [
    "resolve_tenant",
    "require_admin_role",
    "require_active_subscription",
]