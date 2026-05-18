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
    Service role client — for DB queries only.
    NEVER call any auth functions (signIn, signUp, etc.) on this client.
    Auth functions return a user session that overwrites the service role
    Authorization header, breaking RLS bypass for all subsequent queries.
    """
    from config import settings
    from supabase import create_client, Client

    url = settings.supabase_url.strip()
    key = settings.supabase_service_key.strip()

    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env")

    client: Client = create_client(url, key)
    return client


@lru_cache(maxsize=1)
def get_supabase_auth():
    """
    Separate client for auth operations (login, signup, etc.)
    Kept isolated so user sessions never bleed into the admin client.
    """
    from config import settings
    from supabase import create_client, Client

    url = settings.supabase_url.strip()
    key = settings.supabase_anon_key.strip()  # anon key is correct for auth

    if not url or not key:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_ANON_KEY in .env")

    client: Client = create_client(url, key)
    return client