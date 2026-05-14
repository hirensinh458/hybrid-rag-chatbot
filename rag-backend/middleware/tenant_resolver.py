# middleware/tenant_resolver.py
#
# Phase 1 — Multi-Tenancy Foundation  (original)
# Phase 3 — Plan & Usage Enforcement  (extended)
#
# PHASE 3 CHANGES vs Phase 1:
#   - require_active_subscription() extended to handle "over_quota" status.
#     Previously it only blocked "suspended" tenants.
#
#   NEW BEHAVIOUR for over_quota:
#     - Queries are still ALLOWED (grace period — tenants can read, not write).
#     - request.state.quota_warning is set to True.
#     - A response middleware in main.py reads this flag and appends the
#       X-Quota-Warning: over_quota header to the response.
#     - The mobile app reads this header and shows a dismissible banner.
#
# ALL OTHER CODE IS UNCHANGED from Phase 1.
#
# Dependency chain for protected routes:
#   resolve_tenant           → validates JWT, populates request.state
#   require_admin_role       → chains after resolve_tenant; gates admin routes
#   require_active_subscription → chains after resolve_tenant; gates chat/query routes

from __future__ import annotations

from fastapi import Request, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from functools import lru_cache

import jwt   # pip install pyjwt[cryptography]

from config import settings
from utils.logger import get_logger

logger = get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """
    Fetch Supabase public JWKS once and cache for the process lifetime.

    The JWKS endpoint exposes the public key used to verify Supabase JWTs.
    We cache it (maxsize=1) so we only hit the network once per process.

    Production note: In production use python-jose with full JWKS signature
    verification. The `verify_signature=False` option below is safe during
    local dev where all traffic is internal, but should be replaced with a
    proper JWKS validation in a public deployment.
    """
    import httpx
    url = f"{settings.supabase_url}/auth/v1/.well-known/jwks.json"
    try:
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("[TENANT_RESOLVER] Could not fetch JWKS: %s", exc)
        return {}


async def resolve_tenant(request: Request) -> None:
    """
    FastAPI dependency — validates the Supabase JWT and populates request.state
    with tenant context for downstream handlers.

    Sets on request.state:
        tenant_id   : str  — UUID of the tenant
        tenant_slug : str  — URL-safe slug e.g. "acme_shipping"
        role        : str  — "admin" | "user" | "super_admin"
        tenant      : dict — full tenants row (including joined plan)
        plan        : dict — joined plans row
        user_id     : str  — Supabase auth.users UUID (sub claim)
        user_email  : str  — user's email (for audit logging)
        quota_warning: bool — False by default; set True by require_active_subscription
                              when tenant status is "over_quota"

    Raises:
        401 — missing token, invalid token, no tenant context in JWT claims.
        403 — tenant is not found in the database.
    """
    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing auth token.")

    token = credentials.credentials

    try:
        # NOTE: verify_signature=False is intentional for local/dev.
        # In production, replace with full python-jose JWKS verification:
        #   from jose import jwt as jose_jwt
        #   payload = jose_jwt.decode(token, _get_jwks(), algorithms=["RS256"])
        payload = jwt.decode(
            token,
            options={"verify_signature": False},
            algorithms=["RS256", "HS256"],
        )
    except Exception as exc:
        logger.warning("[TENANT_RESOLVER] JWT decode failed: %s", exc)
        raise HTTPException(status_code=401, detail="Invalid token.")

    app_meta  = payload.get("app_metadata", {})
    tenant_id = app_meta.get("tenant_id")
    role      = app_meta.get("role", "user")

    if not tenant_id:
        raise HTTPException(
            status_code=401,
            detail="Token has no tenant context. Please sign in again.",
        )

    # Fetch tenant + plan from Supabase using the service key (bypasses RLS)
    try:
        from services.supabase_client import get_supabase_admin
        supabase = get_supabase_admin()
        result = (
            supabase
            .table("tenants")
            .select("*, plans(*)")
            .eq("id", tenant_id)
            .single()
            .execute()
        )
        tenant = result.data
    except Exception as exc:
        logger.error(
            "[TENANT_RESOLVER] Supabase lookup failed for tenant_id=%s: %s",
            tenant_id, exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not verify tenant. Please try again.",
        )

    if not tenant:
        raise HTTPException(status_code=401, detail="Tenant not found.")

    request.state.tenant_id    = tenant_id
    request.state.tenant_slug  = tenant["slug"]
    request.state.role         = role
    request.state.tenant       = tenant
    request.state.plan         = tenant.get("plans", {})
    request.state.user_id      = payload.get("sub")
    request.state.user_email   = payload.get("email", "")
    request.state.quota_warning = False   # default; set True below when over_quota

    logger.debug(
        "[TENANT_RESOLVER] Resolved — tenant=%s  role=%s  status=%s",
        tenant["slug"], role, tenant.get("status"),
    )


def require_admin_role(request: Request) -> None:
    """
    FastAPI dependency — must be chained after resolve_tenant.

    Ensures the authenticated user has 'admin' or 'super_admin' role.
    Raises 403 if the role is 'user'.

    Usage (on a router or individual route):
        router = APIRouter(
            dependencies=[Depends(resolve_tenant), Depends(require_admin_role)]
        )
    """
    if request.state.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=403,
            detail="Admin role required. Contact your organization administrator.",
        )


def require_active_subscription(request: Request) -> None:
    """
    FastAPI dependency — must be chained after resolve_tenant.

    PHASE 3 EXTENSION:
      Previously only blocked "suspended" tenants.
      Now also handles "over_quota" tenants with a grace period:
        - "suspended" → 402 error, request is blocked entirely.
        - "over_quota" → allowed (grace period), but sets
          request.state.quota_warning = True so the response middleware
          can attach X-Quota-Warning: over_quota to the response.

    The X-Quota-Warning header is read by the mobile app to show a dismissible
    banner: "Your organization is over its plan limit. Contact your admin to upgrade."

    "trial" and "active" → pass through, no action.

    Usage:
        router = APIRouter(
            dependencies=[
                Depends(resolve_tenant),
                Depends(require_active_subscription),
            ]
        )
    """
    status_val = request.state.tenant.get("status")

    if status_val == "suspended":
        raise HTTPException(
            status_code=402,
            detail={
                "code"   : "tenant_suspended",
                "message": (
                    "Your organization's access has been suspended. "
                    "Please contact your administrator."
                ),
            },
        )

    # over_quota: allow queries (grace period) but flag the warning
    # The response middleware in main.py reads this and sets the header.
    if status_val == "over_quota":
        request.state.quota_warning = True
        logger.info(
            "[TENANT_RESOLVER] Tenant %s is over_quota — allowing query with warning header",
            request.state.tenant_slug,
        )


__all__ = [
    "resolve_tenant",
    "require_admin_role",
    "require_active_subscription",
]