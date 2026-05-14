# services/supabase_client.py
#
# Phase 1 — Multi-Tenancy Foundation
#
# Centralises the Supabase Python client so it is created once (lru_cache)
# and reused across all service calls.
#
# Uses the service_role key so that Row Level Security (RLS) is bypassed for
# server-side operations (tenant lookup, metadata reads, storage management).
# Never expose this client to the browser or return its key to the frontend.
#
# Dependency:
#   pip install supabase
#
# Usage:
#   from services.supabase_client import get_supabase_admin
#   sb = get_supabase_admin()
#   result = sb.table("tenants").select("*").eq("id", tenant_id).single().execute()

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def get_supabase_admin():
    """
    Return a cached Supabase client initialised with the service_role key.

    The client is constructed once per process lifetime (lru_cache maxsize=1).
    Thread-safe because lru_cache is thread-safe in CPython.

    Returns:
        supabase.Client — authenticated as service_role (bypasses RLS).

    Raises:
        RuntimeError — if SUPABASE_URL or SUPABASE_SERVICE_KEY are not set.
    """
    from config import settings
    from supabase import create_client, Client  # pip install supabase

    url = settings.supabase_url.strip()
    key = settings.supabase_service_key.strip()

    if not url or not key:
        raise RuntimeError(
            "Supabase is not configured. "
            "Set SUPABASE_URL and SUPABASE_SERVICE_KEY in your .env file."
        )

    client: Client = create_client(url, key)
    print(f"  [SUPABASE_CLIENT] ✅ Admin client initialised for {url}")
    return client


__all__ = ["get_supabase_admin"]