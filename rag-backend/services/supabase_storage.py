# services/supabase_storage.py
#
# Phase 3 — Plan & Usage Enforcement
#
# CHANGES vs Phase 1 version:
#   - upload_pdf_to_supabase() now accepts `tenant_slug` as a required parameter.
#   - Storage path changes from pdfs/{filename} to pdfs/{tenant_slug}/{filename}.
#   - delete_pdf_from_supabase() updated to accept an optional `tenant_slug`
#     so the correct scoped path is used for deletion.
#
# WHY:
#   Multi-tenant isolation in Supabase Storage requires that each tenant's PDFs
#   live in their own namespace. Using pdfs/{tenant_slug}/{filename} gives:
#   - Visual separation in the Supabase Storage UI.
#   - Correct RLS policies can be applied per folder in the future.
#   - Prevents filename collisions across tenants (two tenants can both have
#     "engine_manual.pdf" without conflict).
#
# BACKWARD COMPATIBILITY:
#   delete_pdf_from_supabase() has tenant_slug as an optional parameter
#   (defaults to None) so internal callers that don't yet pass it continue to
#   work — they will attempt to delete at the legacy pdfs/{filename} path.
#
# All other behaviour (upload logic, headers, error handling) is UNCHANGED.

from pathlib import Path

import requests


def supabase_enabled() -> bool:
    """Return True if Supabase Storage is configured."""
    from config import settings
    return bool(
        settings.supabase_url.strip()
        and settings.supabase_service_key.strip()
        and settings.supabase_bucket.strip()
    )


def upload_pdf_to_supabase(
    file_path   : str,
    tenant_slug : str,          # PHASE 3: now required for path scoping
) -> str | None:
    """
    Upload a PDF to Supabase Storage under the tenant's namespace.

    PHASE 3 CHANGE: Storage path is now pdfs/{tenant_slug}/{filename}
    instead of the previous pdfs/{filename}.

    Args:
        file_path:   Local path to the PDF file.
        tenant_slug: Tenant slug used to namespace the upload path.

    Returns:
        Public URL of the uploaded file (str), or None on failure.

    Supabase Storage REST API:
        POST /storage/v1/object/{bucket}/{path}
        Headers:
            Authorization: Bearer {service_role_key}
            apikey:        {service_role_key}   (Supabase routing header)
            Content-Type:  application/octet-stream
            x-upsert:      true                 (overwrite if exists)
    """
    if not supabase_enabled():
        return None   # no-op — not configured

    from config import settings

    service_key  = settings.supabase_service_key.strip()
    base_url     = settings.supabase_url.rstrip("/")
    bucket       = settings.supabase_bucket.strip()

    file_path_obj = Path(file_path)
    filename      = file_path_obj.name

    # PHASE 3: scoped path — pdfs/{tenant_slug}/{filename}
    storage_path = f"pdfs/{tenant_slug}/{filename}"
    upload_url   = f"{base_url}/storage/v1/object/{bucket}/{storage_path}"

    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey"       : service_key,
        "Content-Type" : "application/octet-stream",
        "x-upsert"     : "true",
    }

    try:
        print(f"  [SUPABASE] Uploading '{filename}' → {upload_url}")

        with open(file_path_obj, "rb") as fh:
            response = requests.post(
                upload_url,
                headers = headers,
                data    = fh,
                timeout = 120,   # large PDFs may take a while
            )

        if response.status_code in (200, 201):
            public_url = (
                f"{base_url}/storage/v1/object/public/{bucket}/{storage_path}"
            )
            print(f"  [SUPABASE] ✅ Uploaded '{filename}' → {public_url}")
            return public_url
        else:
            print(
                f"  [SUPABASE] ❌ Upload failed for '{filename}': "
                f"HTTP {response.status_code} — {response.text[:500]}"
            )
            if response.status_code in (401, 403):
                print(
                    f"  [SUPABASE] 💡 Auth hint: check that SUPABASE_SERVICE_KEY "
                    f"is the 'service_role' key (not 'anon'), and that it has no "
                    f"leading/trailing whitespace in your .env file."
                )
            elif response.status_code == 404:
                print(
                    f"  [SUPABASE] 💡 Bucket hint: make sure the bucket '{bucket}' "
                    f"exists in Supabase Storage and is set to PUBLIC."
                )
            return None

    except Exception as exc:
        print(f"  [SUPABASE] ❌ Upload exception for '{filename}': {exc}")
        return None


def download_pdf_from_url(url: str, dest_path: str) -> bool:
    """
    Stream-download a PDF from `url` and save it to `dest_path`.

    Used by the sync engine to download PDFs referenced in chunk metadata
    (source_url) to the local data/pdfs/ directory for the offline viewer.

    The download is streamed in 8 KB chunks so large PDFs don't exhaust RAM.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        print(f"  [SUPABASE] Downloading PDF from {url}")
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)

        print(f"  [SUPABASE] ✅ Saved to {dest}")
        return True

    except Exception as exc:
        print(f"  [SUPABASE] ❌ Download failed from {url}: {exc}")
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        return False


def delete_pdf_from_supabase(
    filename    : str,
    tenant_slug : str = None,   # PHASE 3: optional; uses scoped path when provided
) -> bool:
    """
    Delete a PDF object from Supabase Storage.

    PHASE 3 CHANGE:
        When tenant_slug is provided, deletes from pdfs/{tenant_slug}/{filename}.
        When None (legacy callers), falls back to pdfs/{filename}.

    Args:
        filename:    The filename as stored in the bucket (e.g. "manual.pdf").
        tenant_slug: Tenant slug (optional — for path scoping).

    Returns:
        True  — object deleted successfully (or Supabase not configured).
        False — delete failed (logged, but caller continues regardless).

    Supabase Storage REST API:
        DELETE /storage/v1/object/{bucket}/{path}
        Requires Authorization + apikey headers.
        Returns 200 on success.
    """
    if not supabase_enabled():
        return True   # no-op — not configured

    from config import settings

    service_key = settings.supabase_service_key.strip()
    base_url    = settings.supabase_url.rstrip("/")
    bucket      = settings.supabase_bucket.strip()

    # PHASE 3: use scoped path if tenant_slug provided
    if tenant_slug:
        storage_path = f"pdfs/{tenant_slug}/{filename}"
    else:
        storage_path = f"pdfs/{filename}"   # legacy fallback

    delete_url = f"{base_url}/storage/v1/object/{bucket}/{storage_path}"

    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey"       : service_key,
    }

    try:
        print(f"  [SUPABASE] Deleting '{storage_path}' from bucket '{bucket}'…")
        response = requests.delete(delete_url, headers=headers, timeout=30)

        if response.status_code in (200, 204):
            print(f"  [SUPABASE] ✅ Deleted '{storage_path}' from Supabase Storage")
            return True
        else:
            print(
                f"  [SUPABASE] ❌ Delete failed for '{storage_path}': "
                f"HTTP {response.status_code} — {response.text[:300]}"
            )
            return False

    except Exception as exc:
        print(f"  [SUPABASE] ❌ Delete exception for '{storage_path}': {exc}")
        return False


__all__ = [
    "supabase_enabled",
    "upload_pdf_to_supabase",
    "download_pdf_from_url",
    "delete_pdf_from_supabase",
]