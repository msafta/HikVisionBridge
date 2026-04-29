"""Resolve downloadable face image URLs (signed URL, Edge callback, or legacy public Storage).

external-api-proxy action=get-photo-url (callback / bulk):
  - Request: GET {SUPABASE_*_URL}/functions/v1/external-api-proxy?action=get-photo-url&angajat_id=...
  - Headers: x-api-key (same edge key as get-angajat); if SUPABASE_*_ANON_KEY is set, also apikey and
    Authorization: Bearer <anon> (Supabase REST convention).
  - Response JSON: signed URL read from signed_url, signedUrl, or url (top-level or under data).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


def has_face_photo_source(angajat: dict, photo_request: Optional[dict]) -> bool:
    """True if we can attempt a face download (DB path, inline signed URL, or callback resolver)."""
    pr = photo_request if isinstance(photo_request, dict) else {}
    if (angajat.get("biometrie") or {}).get("foto_fata_url"):
        return True
    signed = pr.get("foto_fata_signed_url")
    if signed is not None and str(signed).strip():
        return True
    res = pr.get("photo_resolver")
    return isinstance(res, dict) and res.get("mode") == "callback"


# Bulk callback cache: avoid hammering external-api-proxy (~50 min TTL, under typical 1h signed URLs)
_CALLBACK_CACHE_TTL_SEC = 50 * 60
_callback_signed_url_cache: Dict[str, tuple[str, float]] = {}


@dataclass
class PhotoResolutionConfig:
    """Supabase project credentials for resolving photos for one mediu."""

    supabase_url: str
    edge_api_key: str
    anon_key: Optional[str] = None


def _normalize_https_supabase(url: str) -> str:
    u = url.strip()
    if u.startswith("http://") and ".supabase.co" in u:
        return "https://" + u[len("http://") :]
    return u


def _signed_url_from_proxy_json(payload: Any) -> Optional[str]:
    """Parse external-api-proxy JSON for a signed image URL (flexible keys)."""
    if not isinstance(payload, dict):
        return None
    for key in ("signed_url", "signedUrl", "url"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("signed_url", "signedUrl", "url"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _callback_request_headers(edge_api_key: str, anon_key: Optional[str]) -> Dict[str, str]:
    """Headers for GET external-api-proxy (align with SupabaseClient + optional anon)."""
    headers: Dict[str, str] = {
        "x-api-key": edge_api_key,
        "Content-Type": "application/json",
    }
    if anon_key and anon_key.strip():
        ak = anon_key.strip()
        headers["apikey"] = ak
        headers["Authorization"] = f"Bearer {ak}"
    return headers


async def _http_get_photo_signed_url(
    base_url: str,
    angajat_id: str,
    edge_api_key: str,
    anon_key: Optional[str],
) -> Optional[str]:
    """GET get-photo-url from Edge; one retry on 5xx; timeout 10s."""
    url = f"{base_url.rstrip('/')}/functions/v1/external-api-proxy"
    params = {"action": "get-photo-url", "angajat_id": angajat_id}
    headers = _callback_request_headers(edge_api_key, anon_key)

    last_status: Optional[int] = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params, headers=headers)
                last_status = response.status_code
                if response.status_code >= 500 and attempt == 0:
                    logger.warning(
                        "get-photo-url 5xx for angajat_id=%s status=%s, retrying once",
                        angajat_id,
                        response.status_code,
                    )
                    continue
                if not response.is_success:
                    logger.warning(
                        "get-photo-url failed for angajat_id=%s: HTTP %s %s",
                        angajat_id,
                        response.status_code,
                        (response.text or "")[:200],
                    )
                    return None
                try:
                    body = response.json()
                except Exception as exc:
                    logger.warning("get-photo-url invalid JSON for angajat_id=%s: %s", angajat_id, exc)
                    return None
                signed = _signed_url_from_proxy_json(body)
                if not signed:
                    logger.warning(
                        "get-photo-url response missing signed URL for angajat_id=%s keys=%s",
                        angajat_id,
                        list(body.keys()) if isinstance(body, dict) else type(body),
                    )
                return signed
        except httpx.TimeoutException as exc:
            logger.warning("get-photo-url timeout for angajat_id=%s: %s", angajat_id, exc)
            return None
        except Exception as exc:
            logger.warning("get-photo-url error for angajat_id=%s: %s", angajat_id, exc)
            return None
    logger.warning("get-photo-url exhausted retries for angajat_id=%s last_status=%s", angajat_id, last_status)
    return None


async def resolve_downloadable_face_url(
    photo_request: Optional[dict],
    angajat: dict,
    config: PhotoResolutionConfig,
) -> Optional[str]:
    """
    Return a URL the bridge can GET to obtain face image bytes.

    Priority:
      1. foto_fata_signed_url on photo_request (single-sync from client).
      2. photo_resolver.mode == callback → GET get-photo-url (bulk); optional cache.
      3. Legacy public Storage URL from biometrie.foto_fata_url + config.supabase_url.
    """
    pr = photo_request if isinstance(photo_request, dict) else {}

    # 1) Inline signed URL
    signed_inline = pr.get("foto_fata_signed_url")
    if signed_inline is not None:
        s = str(signed_inline).strip()
        if s:
            return _normalize_https_supabase(s)

    # 2) Callback
    resolver = pr.get("photo_resolver") if isinstance(pr.get("photo_resolver"), dict) else {}
    if (resolver or {}).get("mode") == "callback":
        angajat_id = angajat.get("id")
        if not angajat_id:
            logger.warning("photo_resolver callback but angajat has no id")
            return None
        aid = str(angajat_id)
        if not (config.edge_api_key or "").strip():
            logger.warning("photo_resolver callback but edge_api_key is empty; cannot call get-photo-url")
            return None

        now = time.monotonic()
        cached = _callback_signed_url_cache.get(aid)
        if cached:
            url_cached, exp = cached
            if now < exp:
                logger.debug("get-photo-url cache hit angajat_id=%s", aid)
                return url_cached
            del _callback_signed_url_cache[aid]

        signed = await _http_get_photo_signed_url(
            config.supabase_url,
            aid,
            config.edge_api_key.strip(),
            config.anon_key,
        )
        if signed:
            signed = _normalize_https_supabase(signed)
            _callback_signed_url_cache[aid] = (signed, now + _CALLBACK_CACHE_TTL_SEC)
        return signed

    # 3) Legacy public URL
    biometrie = angajat.get("biometrie") or {}
    foto = biometrie.get("foto_fata_url")
    if not foto:
        return None
    foto = str(foto).strip()
    if not foto:
        return None
    if foto.startswith("http://") or foto.startswith("https://"):
        return _normalize_https_supabase(foto)
    base = (config.supabase_url or "").strip()
    if not base:
        return None
    return f"{base.rstrip('/')}/storage/v1/object/public/pontaj-photos/{foto}"
