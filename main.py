import asyncio
import ipaddress
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from jwt import PyJWKClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from hikvision_sync.photo_url import PhotoResolutionConfig
from hikvision_sync.supabase_client import SupabaseClient
from hikvision_sync.isapi_client import create_person_on_device, add_face_image_to_device, rate_limit_delay
from hikvision_sync.orchestration import (
    sync_angajat_to_device_with_data,
    sync_photo_only_to_device_with_data,
    update_photo_to_device_with_data,
    delete_user_from_device
)
from hikvision_sync.events import DailyLogger, process_event_request

# Load environment variables from .env file
load_dotenv()

# Configure logging to output to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()  # Output to console
    ]
)

app = FastAPI()

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


_ROOT_DIR = Path(__file__).resolve().parent
_LOG_DIR = _ROOT_DIR / "logs"
_APP_CONFIG_PATH = _ROOT_DIR / "config" / "app_settings.json"

def _load_app_config() -> dict:
    if not _APP_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_APP_CONFIG = _load_app_config()


def _get_config_value(env_key: str, config_key: str, default=None):
    env_val = os.getenv(env_key)
    if env_val:
        return env_val
    return _APP_CONFIG.get(config_key, default)


_SUPABASE_JWKS_URL = _get_config_value("SUPABASE_JWKS_URL", "supabase_jwks_url")
_SUPABASE_JWT_SECRET = _get_config_value("SUPABASE_JWT_SECRET", "supabase_jwt_secret")
_SUPABASE_URL = _get_config_value("SUPABASE_URL", "supabase_url")
_SUPABASE_SERVICE_ROLE_KEY = _get_config_value("SUPABASE_SERVICE_ROLE_KEY", "supabase_service_role_key")
_ALLOWED_ORIGINS = _get_config_value("ALLOWED_ORIGINS", "allowed_origins", [])
if isinstance(_ALLOWED_ORIGINS, str):
    _ALLOWED_ORIGINS = [origin.strip() for origin in _ALLOWED_ORIGINS.split(",") if origin.strip()]
if not _ALLOWED_ORIGINS:
    _ALLOWED_ORIGINS = ["http://localhost:3000"]

# VPN/IP Whitelist configuration for device events
_VPN_SUBNET = _get_config_value("VPN_SUBNET", "vpn_subnet", "")
_ALLOWED_EVENT_IPS = _get_config_value("ALLOWED_EVENT_IPS", "allowed_event_ips", "")

# Parse allowed IP ranges
_ALLOWED_IP_NETWORKS = []
if _VPN_SUBNET:
    try:
        _ALLOWED_IP_NETWORKS.append(ipaddress.ip_network(_VPN_SUBNET, strict=False))
    except ValueError:
        logging.warning(f"Invalid VPN_SUBNET: {_VPN_SUBNET}")

if _ALLOWED_EVENT_IPS:
    for ip_range in _ALLOWED_EVENT_IPS.split(","):
        ip_range = ip_range.strip()
        if ip_range:
            try:
                _ALLOWED_IP_NETWORKS.append(ipaddress.ip_network(ip_range, strict=False))
            except ValueError:
                logging.warning(f"Invalid IP range in ALLOWED_EVENT_IPS: {ip_range}")


def _is_ip_whitelisted(client_ip: str) -> bool:
    """
    Check if client IP is in the whitelisted VPN subnet or allowed IP ranges.
    
    Args:
        client_ip: Client IP address as string
        
    Returns:
        True if IP is whitelisted, False otherwise
    """
    if not _ALLOWED_IP_NETWORKS:
        # If no whitelist configured, allow all (backward compatible for dev)
        return True
    
    try:
        ip = ipaddress.ip_address(client_ip)
        for network in _ALLOWED_IP_NETWORKS:
            if ip in network:
                return True
    except ValueError:
        # Invalid IP address
        return False
    
    return False


def _get_client_ip(request: Request) -> str:
    """
    Extract client IP from request, handling X-Forwarded-For header if behind proxy.
    
    Args:
        request: FastAPI Request object
        
    Returns:
        Client IP address as string
    """
    # Check X-Forwarded-For header (if behind proxy/load balancer)
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # X-Forwarded-For can contain multiple IPs, take the first one
        client_ip = forwarded_for.split(",")[0].strip()
        return client_ip
    
    # Fall back to direct client IP
    return request.client.host if request.client else "unknown"


app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _build_supabase_client_for_env(env_name: str) -> Optional[SupabaseClient]:
    env_upper = env_name.upper()
    env_lower = env_name.lower()

    url = _get_config_value(f"SUPABASE_{env_upper}_URL", f"supabase_{env_lower}_url")
    edge_key = _get_config_value(
        f"SUPABASE_{env_upper}_EDGE_FUNCTION_API_KEY",
        f"supabase_{env_lower}_edge_function_api_key",
        "hikvision-sync-2024",
    )
    event_url = _get_config_value(
        f"SUPABASE_{env_upper}_EVENT_FUNCTION_URL",
        f"supabase_{env_lower}_event_function_url",
    )
    event_api_key = _get_config_value(
        f"SUPABASE_{env_upper}_EVENT_FUNCTION_API_KEY",
        f"supabase_{env_lower}_event_function_api_key",
    )

    # Backward compatibility: DEV can use legacy env names.
    if env_lower == "dev":
        url = url or _SUPABASE_URL
        edge_key = edge_key or _get_config_value("SUPABASE_EDGE_FUNCTION_API_KEY", "supabase_edge_function_api_key", "hikvision-sync-2024")
        event_url = event_url or _get_config_value("SUPABASE_EVENT_FUNCTION_URL", "supabase_event_function_url", "")
        event_api_key = event_api_key or _get_config_value("SUPABASE_EVENT_FUNCTION_API_KEY", "supabase_event_function_api_key", "")

    if not url:
        return None

    return SupabaseClient(
        supabase_url=url,
        api_key=edge_key,
        event_function_url=event_url or "",
        event_function_api_key=event_api_key or "",
    )


_SUPABASE_CLIENTS: Dict[str, SupabaseClient] = {}
for _env_name in ("dev", "test", "prod"):
    _client = _build_supabase_client_for_env(_env_name)
    if _client:
        _SUPABASE_CLIENTS[_env_name] = _client

_SUPABASE_CLIENT = _SUPABASE_CLIENTS.get("dev")
if _SUPABASE_CLIENT is None:
    # Keep current behavior for single-env installs.
    _SUPABASE_EDGE_FUNCTION_API_KEY = _get_config_value("SUPABASE_EDGE_FUNCTION_API_KEY", "supabase_edge_function_api_key", "hikvision-sync-2024")
    _SUPABASE_CLIENT = SupabaseClient(_SUPABASE_URL, _SUPABASE_EDGE_FUNCTION_API_KEY) if _SUPABASE_URL else None

if _SUPABASE_CLIENT and "dev" not in _SUPABASE_CLIENTS:
    _SUPABASE_CLIENTS["dev"] = _SUPABASE_CLIENT


_DEVICE_MEDIU_CACHE: Dict[str, str] = {}
_DEVICE_CACHE_LAST_REFRESH = 0.0
_DEVICE_CACHE_TTL_SECONDS = 120
_DEVICE_CACHE_LOCK = asyncio.Lock()


def _normalize_ip(ip_value: Optional[str]) -> str:
    return str(ip_value or "").strip()


def _extract_event_device_ip(parsed_event: Optional[dict]) -> Optional[str]:
    if not isinstance(parsed_event, dict):
        return None
    ip_val = parsed_event.get("ipAddress")
    normalized = _normalize_ip(ip_val)
    return normalized or None


async def _refresh_device_mediu_cache(event_logger: logging.Logger, reason: str = "ttl_refresh") -> bool:
    global _DEVICE_MEDIU_CACHE, _DEVICE_CACHE_LAST_REFRESH
    dev_client = _SUPABASE_CLIENTS.get("dev")
    if not dev_client:
        event_logger.error("Cannot refresh device mediu cache: DEV Supabase client not configured.")
        return False

    try:
        devices = await dev_client.get_active_devices()
    except Exception as exc:
        event_logger.error("Failed refreshing device mediu cache from Supabase (%s): %s", reason, exc)
        return False

    new_cache: Dict[str, str] = {}
    for device in devices:
        ip_value = _normalize_ip(device.get("ip_address") or device.get("ipAddress"))
        mediu_value = str(device.get("mediu") or "").strip().lower()
        if not ip_value:
            continue
        if mediu_value not in ("dev", "test", "prod"):
            event_logger.warning(
                "Skipping device with invalid mediu value ip=%s mediu=%s",
                ip_value,
                device.get("mediu"),
            )
            continue
        new_cache[ip_value] = mediu_value

    _DEVICE_MEDIU_CACHE = new_cache
    _DEVICE_CACHE_LAST_REFRESH = time.time()
    event_logger.info(
        "Device mediu cache refreshed reason=%s size=%s",
        reason,
        len(_DEVICE_MEDIU_CACHE),
    )
    return True


async def _ensure_device_cache_fresh(event_logger: logging.Logger, force: bool = False, reason: str = "ttl_check") -> bool:
    now = time.time()
    if not force and _DEVICE_MEDIU_CACHE and (now - _DEVICE_CACHE_LAST_REFRESH) < _DEVICE_CACHE_TTL_SECONDS:
        return True

    async with _DEVICE_CACHE_LOCK:
        now = time.time()
        if not force and _DEVICE_MEDIU_CACHE and (now - _DEVICE_CACHE_LAST_REFRESH) < _DEVICE_CACHE_TTL_SECONDS:
            return True

        refreshed = await _refresh_device_mediu_cache(event_logger=event_logger, reason=reason)
        if refreshed:
            return True

        if _DEVICE_MEDIU_CACHE:
            event_logger.warning("Using stale device mediu cache after refresh failure.")
            return True
        return False


def _resolve_target_envs(mediu_raw: Optional[str]) -> List[str]:
    normalized = (mediu_raw or "").strip().lower()
    if normalized in ("dev", "test", "prod"):
        return [normalized]
    return []


def _parse_sync_mediu(body: dict) -> Tuple[Optional[str], Optional[str]]:
    """Read mediu from request body. Returns (mediu_or_none, error_message)."""
    raw = body.get("mediu")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = body.get("environment")
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return "dev", None
    normalized = str(raw).strip().lower()
    if normalized in ("dev", "test", "prod"):
        return normalized, None
    return None, f"Invalid mediu: {raw!r}; expected dev, test, or prod"


def _get_supabase_public_base_url_for_env(env_name: str) -> Optional[str]:
    """Resolve public Supabase base URL using same rules as client construction."""
    env_upper = env_name.upper()
    env_lower = env_name.lower()
    url = _get_config_value(f"SUPABASE_{env_upper}_URL", f"supabase_{env_lower}_url")
    if env_lower == "dev":
        url = url or _SUPABASE_URL
    return url if url else None


def _get_supabase_anon_key_for_env(env_name: str) -> Optional[str]:
    """Public anon key for Edge/Storage calls (optional; adds apikey + Authorization when set)."""
    env_upper = env_name.upper()
    env_lower = env_name.lower()
    key = _get_config_value(f"SUPABASE_{env_upper}_ANON_KEY", f"supabase_{env_lower}_anon_key")
    if env_lower == "dev":
        key = key or _get_config_value("SUPABASE_ANON_KEY", "supabase_anon_key")
    return key if key else None


def _photo_request_from_body(body: dict) -> dict:
    """Subset of bridge JSON forwarded to photo resolution (signed URL + bulk callback mode)."""
    out: dict = {}
    if "foto_fata_signed_url" in body and body.get("foto_fata_signed_url") is not None:
        out["foto_fata_signed_url"] = body.get("foto_fata_signed_url")
    pr = body.get("photo_resolver")
    if isinstance(pr, dict):
        out["photo_resolver"] = pr
    return out


def _photo_resolution_from_body(body: dict, sb_client: SupabaseClient, supabase_url: str) -> Tuple[dict, PhotoResolutionConfig]:
    """Build (photo_request, PhotoResolutionConfig) for orchestration/isapi."""
    mediu, _ = _parse_sync_mediu(body)
    anon = _get_supabase_anon_key_for_env(mediu)
    photo_request = _photo_request_from_body(body)
    photo_config = PhotoResolutionConfig(
        supabase_url=supabase_url or "",
        edge_api_key=sb_client.api_key,
        anon_key=anon,
    )
    return photo_request, photo_config


def _get_supabase_client_for_sync(mediu: str) -> Optional[SupabaseClient]:
    return _SUPABASE_CLIENTS.get(mediu)


def _resolve_sync_supabase(body: dict) -> Tuple[Optional[SupabaseClient], Optional[str], Optional[str]]:
    """
    Resolve Supabase client and public URL for Hikvision sync endpoints.
    Returns (client, supabase_public_url, error_message).
    """
    mediu, parse_err = _parse_sync_mediu(body)
    if parse_err:
        return None, None, parse_err

    assert mediu is not None
    client = _get_supabase_client_for_sync(mediu)
    if not client:
        return None, None, (
            f"Supabase client for mediu={mediu!r} is not configured on this bridge. "
            f"Set SUPABASE_{mediu.upper()}_URL and SUPABASE_{mediu.upper()}_EDGE_FUNCTION_API_KEY "
            "(for dev, SUPABASE_URL / SUPABASE_EDGE_FUNCTION_API_KEY are also accepted)."
        )

    base_url = _get_supabase_public_base_url_for_env(mediu)
    if not base_url:
        return None, None, f"Supabase public URL for mediu={mediu!r} is not configured."
    return client, base_url, None


async def _save_access_event_to_targets(parsed_event: dict, target_envs: List[str], event_logger: logging.Logger) -> Dict[str, str]:
    tasks = []
    task_envs = []
    for env_name in target_envs:
        client = _SUPABASE_CLIENTS.get(env_name)
        if not client:
            event_logger.error("Missing Supabase client for target environment '%s'", env_name)
            continue
        task_envs.append(env_name)
        tasks.append(client.save_access_event(parsed_event))

    if not tasks:
        return {}

    results = await asyncio.gather(*tasks, return_exceptions=True)
    statuses: Dict[str, str] = {}

    for env_name, result in zip(task_envs, results):
        if isinstance(result, Exception):
            statuses[env_name] = "error"
            event_logger.error("Unexpected error saving event to %s: %s", env_name.upper(), result)
            continue

        if result.get("status") == "success":
            statuses[env_name] = "success"
            event_logger.info("Successfully saved access event to %s", env_name.upper())
        else:
            statuses[env_name] = "error"
            error_type = result.get("error_type", "Unknown")
            error_msg = result.get("error", "Unknown error")
            status_code = result.get("status_code", "")
            event_logger.error(
                "Failed saving access event to %s: %s - %s%s",
                env_name.upper(),
                error_type,
                error_msg,
                f" (Status: {status_code})" if status_code else "",
            )

    return statuses


# Legacy function wrappers for backward compatibility (if needed)
async def get_active_devices() -> List[dict]:
    """Wrapper for SupabaseClient.get_active_devices()."""
    if not _SUPABASE_CLIENT:
        raise ValueError("SUPABASE_URL not configured")
    return await _SUPABASE_CLIENT.get_active_devices()


async def get_angajat_with_biometrie(angajat_id: str) -> Optional[dict]:
    """Wrapper for SupabaseClient.get_angajat_with_biometrie()."""
    if not _SUPABASE_CLIENT:
        raise ValueError("SUPABASE_URL not configured")
    return await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)


async def get_all_active_angajati_with_biometrie() -> List[dict]:
    """Wrapper for SupabaseClient.get_all_active_angajati_with_biometrie()."""
    if not _SUPABASE_CLIENT:
        raise ValueError("SUPABASE_URL not configured")
    return await _SUPABASE_CLIENT.get_all_active_angajati_with_biometrie()


async def save_pontaj_event(angajat_id: str, dispozitiv_id: str, event_time: str) -> dict:
    """Wrapper for SupabaseClient.save_pontaj_event()."""
    if not _SUPABASE_CLIENT:
        raise ValueError("SUPABASE_URL not configured")
    return await _SUPABASE_CLIENT.save_pontaj_event(angajat_id, dispozitiv_id, event_time)


class _AuthVerifier:
    def __init__(self, jwks_url: Optional[str], jwt_secret: Optional[str]):
        self.jwks_client = PyJWKClient(jwks_url) if jwks_url and jwks_url.strip() else None
        self.jwt_secret = jwt_secret

    def verify(self, token: str) -> dict:
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
        
        # Try JWKS first (for RS256 user tokens)
        if self.jwks_client:
            try:
                signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
                return jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256"],
                    options={"verify_aud": False},
                )
            except Exception as jwks_exc:
                # JWKS failed, fall through to JWT secret fallback
                # Log the error for debugging (can be removed later)
                import logging
                logger = logging.getLogger(__name__)
                logger.debug(f"JWKS verification failed: {jwks_exc}, trying JWT secret fallback")
        
        # Fall back to JWT secret (for HS256 tokens)
        if self.jwt_secret:
            try:
                return jwt.decode(
                    token,
                    self.jwt_secret,
                    algorithms=["HS256"],
                    options={"verify_aud": False},
                )
            except Exception as exc:
                # If both failed, provide helpful error message
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Invalid token: {exc}. Check JWT secret matches Supabase JWT secret."
                ) from exc

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT verification is not configured (set SUPABASE_JWKS_URL or SUPABASE_JWT_SECRET)",
        )


_AUTH_VERIFIER = _AuthVerifier(_SUPABASE_JWKS_URL, _SUPABASE_JWT_SECRET)
_REQUEST_LOGGER = logging.getLogger("hikvision.request")


def _extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
    if not auth_header:
        return None
    if not auth_header.lower().startswith("bearer "):
        return None
    return auth_header.split(" ", 1)[1].strip()


async def require_auth(request: Request):
    token = _extract_bearer_token(request.headers.get("Authorization"))
    payload = _AUTH_VERIFIER.verify(token)
    request.state.user = payload
    return payload


def _request_id_for(request: Request) -> str:
    """Use caller-provided request id if present, otherwise generate one."""
    header_val = request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")
    if header_val and header_val.strip():
        return header_val.strip()
    return str(uuid.uuid4())[:8]


@app.get("/env-test")
def env_test():
    """Test endpoint to verify environment variables are loaded correctly."""
    active_clients = sorted(_SUPABASE_CLIENTS.keys())
    cache_age_seconds = int(time.time() - _DEVICE_CACHE_LAST_REFRESH) if _DEVICE_CACHE_LAST_REFRESH else None
    return {
        "env": os.getenv("APP_ENV"),
        "vpn_subnet": os.getenv("VPN_SUBNET"),
        "allowed_origins": os.getenv("ALLOWED_ORIGINS"),
        "supabase_clients_active": active_clients,
        "routing_ready": {
            "dev": "dev" in _SUPABASE_CLIENTS,
            "test": "test" in _SUPABASE_CLIENTS,
            "prod": "prod" in _SUPABASE_CLIENTS,
        },
        "device_cache": {
            "ttl_seconds": _DEVICE_CACHE_TTL_SECONDS,
            "size": len(_DEVICE_MEDIU_CACHE),
            "last_refresh_epoch": _DEVICE_CACHE_LAST_REFRESH if _DEVICE_CACHE_LAST_REFRESH else None,
            "age_seconds": cache_age_seconds,
        },
    }


@app.post("/api/hikvision/sync-angajat-all-devices")
@limiter.limit("60/minute")
async def sync_angajat_all_devices(
    request: Request
):
    """
    Sync one Angajat to all active devices.
    
    This endpoint syncs a single employee (angajat) to all active Hikvision devices.
    It creates/updates the person record and adds the face photo if available.
    Uses the same functionality as the test endpoint but for production use.
    
    Request body:
    {
        "angajat_id": "<uuid>",   # Required: UUID of the angajat to sync
        "mediu": "dev|test|prod", # Optional: defaults to "dev"
        "foto_fata_signed_url": "<https...>",  # Optional: signed GET for private bucket
        "photo_resolver": {"mode": "callback"}  # Optional: bulk-style; per-angajat get-photo-url
    }
    
    Returns:
    {
        "status": "ok",
        "summary": {
            "success": 2,    # Number of devices synced successfully
            "partial": 1,    # Number of devices with partial success (person ok, photo failed)
            "skipped": 0,    # Number of devices skipped (missing employee_no)
            "fatal": 0       # Number of devices with fatal errors
        },
        "per_device": [
            {
                "device_id": "...",
                "device_ip": "192.168.1.100",
                "status": "success",
                "message": "Person and photo synced successfully",
                "step": "complete"
            },
            ...
        ]
    }
    """
    # Read and print raw request body
    body = await request.body()
    print("RAW BODY:", body)
    
    # Parse JSON body
    try:
        body = json.loads(body)
    except json.JSONDecodeError:
        return {
            "status": "error",
            "error": "Invalid JSON in request body"
        }
    request_id = _request_id_for(request)
    
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    _REQUEST_LOGGER.info(
        "request_id=%s endpoint=sync-angajat-all-devices angajat_id=%s mediu=%s",
        request_id,
        angajat_id,
        body.get("mediu") or body.get("environment") or "dev",
    )

    sb_client, supabase_url, sync_err = _resolve_sync_supabase(body)
    if sync_err:
        return {
            "status": "error",
            "error": sync_err,
        }

    photo_request, photo_config = _photo_resolution_from_body(body, sb_client, supabase_url or "")
    
    try:
        # Fetch angajat with biometrie data
        angajat = await sb_client.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await sb_client.get_active_devices()
        if not devices:
            return {
                "status": "error",
                "error": "No active devices found"
            }
        
        # Initialize result aggregation
        per_device_results = []
        summary = {
            "success": 0,
            "partial": 0,
            "skipped": 0,
            "fatal": 0
        }
        
        # Sync to each device sequentially (using existing sync_angajat_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Sync to this device using direct image data functionality
            result = await sync_angajat_to_device_with_data(
                angajat,
                device,
                supabase_url,
                photo_request=photo_request,
                photo_config=photo_config,
            )
            
            # Record result
            per_device_results.append({
                "device_id": device_id,
                "device_ip": device_ip,
                "status": result.status.value,
                "message": result.message,
                "step": result.step
            })
            
            # Update summary counts
            summary[result.status.value] = summary.get(result.status.value, 0) + 1
            
            # Add delay between devices (except after the last one)
            if device != devices[-1]:
                await rate_limit_delay(1.0)
        
        # Return structured result (always 200, status in payload)
        return {
            "status": "ok",
            "summary": summary,
            "per_device": per_device_results
        }
        
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()
        }


@app.post("/api/hikvision/delete-user")
@limiter.limit("60/minute")
async def delete_user(
    request: Request,
    body: dict
):
    """
    Delete user from all active devices.
    
    This endpoint deletes a single employee (angajat) from all active Hikvision devices.
    It follows the same pattern as sync-angajat-all-devices but for deletion.
    
    Request body:
    {
        "angajat_id": "<uuid>",   # Required: UUID of the angajat to delete
        "mediu": "dev|test|prod"  # Optional: defaults to "dev"
    }
    
    Returns:
    {
        "status": "ok",
        "summary": {
            "success": 2,    # Number of devices where user was deleted successfully
            "partial": 0,    # Number of devices with partial success (non-fatal errors)
            "skipped": 1,    # Number of devices skipped (missing employee_no)
            "fatal": 0       # Number of devices with fatal errors (auth/network)
        },
        "per_device": [
            {
                "device_id": "...",
                "device_ip": "192.168.1.100",
                "status": "success",
                "message": "User deleted successfully",
                "step": "delete"
            },
            ...
        ]
    }
    """
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    request_id = _request_id_for(request)
    _REQUEST_LOGGER.info(
        "request_id=%s endpoint=delete-user angajat_id=%s mediu=%s",
        request_id,
        angajat_id,
        body.get("mediu") or body.get("environment") or "dev",
    )

    sb_client, _, sync_err = _resolve_sync_supabase(body)
    if sync_err:
        return {
            "status": "error",
            "error": sync_err,
        }
    
    try:
        # Fetch angajat with biometrie data
        angajat = await sb_client.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await sb_client.get_active_devices()
        if not devices:
            return {
                "status": "error",
                "error": "No active devices found"
            }
        
        # Initialize result aggregation
        per_device_results = []
        summary = {
            "success": 0,
            "partial": 0,
            "skipped": 0,
            "fatal": 0
        }
        
        # Delete from each device sequentially (using delete_user_from_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Delete from this device using orchestration function
            result = await delete_user_from_device(angajat, device)
            
            # Record result
            per_device_results.append({
                "device_id": device_id,
                "device_ip": device_ip,
                "status": result.status.value,
                "message": result.message,
                "step": result.step
            })
            
            # Update summary counts
            summary[result.status.value] = summary.get(result.status.value, 0) + 1
            
            # Add delay between devices (except after the last one)
            if device != devices[-1]:
                await rate_limit_delay(1.0)
        
        # Return structured result (always 200, status in payload)
        return {
            "status": "ok",
            "summary": summary,
            "per_device": per_device_results
        }
        
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()
        }


@app.post("/api/hikvision/sync-all-to-all-devices")
@limiter.limit("60/minute")
async def sync_all_to_all_devices(
    request: Request,
    body: dict
):
    """
    Sync all active Angajati to all active devices (bulk sync).
    
    This endpoint syncs all active employees to all active Hikvision devices.
    It creates/updates person records and adds face photos if available.
    Continues syncing even if some angajati don't have photos uploaded yet.
    
    Request body:
    {
        "mediu": "dev|test|prod",  # Optional: defaults to "dev"
        "foto_fata_signed_url": "<https...>",  # Optional
        "photo_resolver": {"mode": "callback"}  # Optional: fetch signed URL per angajat via Edge
    }
    
    Returns:
    {
        "status": "ok",
        "summary": {
            "total_angajati": 10,
            "total_devices": 3,
            "success": 25,      # Total successful syncs (angajat+device combinations)
            "partial": 2,        # Total partial syncs (person ok, photo failed)
            "skipped": 3,        # Total skipped (missing employee_no)
            "fatal": 0           # Total fatal errors
        },
        "employeeResults": [
            {
                "angajatId": "...",
                "angajatName": "John Doe",
                "success": true,      # true if at least one device succeeded and no fatal errors
                "skipped": false,     # true if all devices were skipped
                "error": null,        # null if success, otherwise first error message
                "deviceResults": [
                    {
                        "deviceId": "uuid1",
                        "success": true,
                        "error": null
                    },
                    {
                        "deviceId": "uuid2",
                        "success": false,
                        "error": "Connection timeout"
                    }
                ]
            },
            ...
        ]
    }
    """
    sb_client, supabase_url, sync_err = _resolve_sync_supabase(body)
    if sync_err:
        return {
            "status": "error",
            "error": sync_err,
        }
    request_id = _request_id_for(request)
    _REQUEST_LOGGER.info(
        "request_id=%s endpoint=sync-all-to-all-devices mediu=%s",
        request_id,
        body.get("mediu") or body.get("environment") or "dev",
    )

    photo_request, photo_config = _photo_resolution_from_body(body, sb_client, supabase_url or "")

    try:
        # Fetch all active angajati with biometrie data
        angajati = await sb_client.get_all_active_angajati_with_biometrie()
        if not angajati:
            return {
                "status": "error",
                "error": "No active angajati found"
            }
        
        # Fetch all active devices
        devices = await sb_client.get_active_devices()
        if not devices:
            return {
                "status": "error",
                "error": "No active devices found"
            }
        
        # Initialize result aggregation
        employee_results = []
        total_summary = {
            "total_angajati": len(angajati),
            "total_devices": len(devices),
            "success": 0,
            "partial": 0,
            "skipped": 0,
            "fatal": 0
        }
        
        # Sync each angajat to all devices sequentially
        for angajat in angajati:
            angajat_id = angajat.get("id", "unknown")
            angajat_name = f"{angajat.get('nume', '')} {angajat.get('prenume', '')}".strip() or angajat.get("nume_complet", "Unknown")
            
            # Initialize per-angajat result
            device_results = []
            angajat_summary = {
                "success": 0,
                "partial": 0,
                "skipped": 0,
                "fatal": 0
            }
            
            # Sync to each device sequentially
            for device in devices:
                device_id = device.get("id", "unknown")
                device_ip = device.get("ip_address", "unknown")
                
                # Sync to this device using direct image data (handles missing photo URLs automatically)
                result = await sync_angajat_to_device_with_data(
                    angajat,
                    device,
                    supabase_url,
                    photo_request=photo_request,
                    photo_config=photo_config,
                )
                
                # Record device result in new format
                device_success = result.status.value in ("success", "partial")
                device_error = None if device_success else result.message
                
                device_results.append({
                    "deviceId": device_id,
                    "success": device_success,
                    "error": device_error
                })
                
                # Update per-angajat summary
                angajat_summary[result.status.value] = angajat_summary.get(result.status.value, 0) + 1
                
                # Update total summary
                total_summary[result.status.value] = total_summary.get(result.status.value, 0) + 1
                
                # Add delay between devices (except after the last device for this angajat)
                if device != devices[-1]:
                    await rate_limit_delay(1.0)
            
            # Determine overall employee result status
            # success: true if at least one device succeeded and no fatal errors
            # skipped: true if all devices were skipped
            # error: first error message if any failures occurred
            has_success = angajat_summary.get("success", 0) > 0 or angajat_summary.get("partial", 0) > 0
            has_fatal = angajat_summary.get("fatal", 0) > 0
            all_skipped = angajat_summary.get("skipped", 0) == len(devices)
            
            employee_success = has_success and not has_fatal
            employee_skipped = all_skipped
            
            # Find first error message from device results
            employee_error = None
            if not employee_success and not employee_skipped:
                for dr in device_results:
                    if dr["error"]:
                        employee_error = dr["error"]
                        break
            
            # Record employee result in new format
            employee_results.append({
                "angajatId": angajat_id,
                "angajatName": angajat_name,
                "success": employee_success,
                "skipped": employee_skipped,
                "error": employee_error,
                "deviceResults": device_results
            })
            
            # Add delay between angajati (except after the last one)
            if angajat != angajati[-1]:
                await rate_limit_delay(1.0)
        
        # Return structured result (always 200, status in payload)
        return {
            "status": "ok",
            "summary": total_summary,
            "employeeResults": employee_results
        }
        
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()
        }


@app.post("/api/hikvision/sync-angajat-photo-only")
@limiter.limit("60/minute")
async def sync_angajat_photo_only(
    request: Request,
    body: dict
):
    """
    Sync only the face photo for one Angajat to all active devices (photo-only sync).
    
    This endpoint syncs ONLY the face photo to all active Hikvision devices.
    It assumes the person already exists on the devices and skips person creation.
    Use this when you only want to update/add photos without touching person data.
    
    Request body:
    {
        "angajat_id": "<uuid>",   # Required: UUID of the angajat to sync photo for
        "mediu": "dev|test|prod", # Optional: defaults to "dev"
        "foto_fata_signed_url": "<https...>",  # Optional: signed GET for private bucket
        "photo_resolver": {"mode": "callback"}  # Optional
    }
    
    Returns:
    {
        "status": "ok",
        "summary": {
            "success": 2,    # Number of devices where photo was added successfully
            "partial": 0,    # Number of devices with partial success (photo failed)
            "skipped": 1,    # Number of devices skipped (missing employee_no or foto_fata_url)
            "fatal": 0       # Number of devices with fatal errors
        },
        "per_device": [
            {
                "device_id": "...",
                "device_ip": "192.168.1.100",
                "status": "success",
                "message": "Face image added successfully",
                "step": "photo"
            },
            ...
        ]
    }
    """
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    request_id = _request_id_for(request)
    _REQUEST_LOGGER.info(
        "request_id=%s endpoint=sync-angajat-photo-only angajat_id=%s mediu=%s",
        request_id,
        angajat_id,
        body.get("mediu") or body.get("environment") or "dev",
    )

    sb_client, supabase_url, sync_err = _resolve_sync_supabase(body)
    if sync_err:
        return {
            "status": "error",
            "error": sync_err,
        }

    photo_request, photo_config = _photo_resolution_from_body(body, sb_client, supabase_url or "")
    
    try:
        # Fetch angajat with biometrie data
        angajat = await sb_client.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await sb_client.get_active_devices()
        if not devices:
            return {
                "status": "error",
                "error": "No active devices found"
            }
        
        # Initialize result aggregation
        per_device_results = []
        summary = {
            "success": 0,
            "partial": 0,
            "skipped": 0,
            "fatal": 0
        }
        
        # Sync photo to each device sequentially (using sync_photo_only_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Sync photo only to this device using direct image data (skips person creation)
            result = await sync_photo_only_to_device_with_data(
                angajat,
                device,
                supabase_url,
                photo_request=photo_request,
                photo_config=photo_config,
            )
            
            # Record result
            per_device_results.append({
                "device_id": device_id,
                "device_ip": device_ip,
                "status": result.status.value,
                "message": result.message,
                "step": result.step
            })
            
            # Update summary counts
            summary[result.status.value] = summary.get(result.status.value, 0) + 1
            
            # Add delay between devices (except after the last one)
            if device != devices[-1]:
                await rate_limit_delay(1.0)
        
        # Return structured result (always 200, status in payload)
        return {
            "status": "ok",
            "summary": summary,
            "per_device": per_device_results
        }
        
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()
        }


@app.post("/api/hikvision/update-angajat-photo")
@limiter.limit("60/minute")
async def update_angajat_photo(
    request: Request,
    body: dict
):
    """
    Update face photo for one Angajat to all active devices (PUT with POST fallback).
    
    This endpoint updates the face photo on all active Hikvision devices.
    It attempts PUT request first to update existing photo, and falls back to POST (create) if PUT fails.
    Assumes the person already exists on the devices.
    Use this when you want to update an existing photo on devices.
    
    Request body:
    {
        "angajat_id": "<uuid>",   # Required: UUID of the angajat to update photo for
        "mediu": "dev|test|prod", # Optional: defaults to "dev"
        "foto_fata_signed_url": "<https...>",  # Optional: signed GET for private bucket
        "photo_resolver": {"mode": "callback"}  # Optional
    }
    
    Returns:
    {
        "status": "ok",
        "summary": {
            "success": 2,      # Number of devices updated successfully (PUT succeeded OR POST fallback succeeded)
            "partial": 0,      # Number of devices where both PUT and POST failed
            "skipped": 1,      # Number of devices skipped (missing employee_no or foto_fata_url)
            "fatal": 0         # Number of devices with fatal errors
        },
        "per_device": [
            {
                "device_id": "...",
                "device_ip": "192.168.1.100",
                "status": "success",
                "message": "Face image updated successfully via PUT",
                "step": "photo"
            },
            ...
        ]
    }
    """
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    request_id = _request_id_for(request)
    _REQUEST_LOGGER.info(
        "request_id=%s endpoint=update-angajat-photo angajat_id=%s mediu=%s",
        request_id,
        angajat_id,
        body.get("mediu") or body.get("environment") or "dev",
    )

    sb_client, supabase_url, sync_err = _resolve_sync_supabase(body)
    if sync_err:
        return {
            "status": "error",
            "error": sync_err,
        }

    photo_request, photo_config = _photo_resolution_from_body(body, sb_client, supabase_url or "")
    
    try:
        # Fetch angajat with biometrie data
        angajat = await sb_client.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await sb_client.get_active_devices()
        if not devices:
            return {
                "status": "error",
                "error": "No active devices found"
            }
        
        # Initialize result aggregation
        per_device_results = []
        summary = {
            "success": 0,
            "partial": 0,
            "skipped": 0,
            "fatal": 0
        }
        
        # Update photo to each device sequentially (using update_photo_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Update photo to this device using direct image data (PUT with POST fallback)
            result = await update_photo_to_device_with_data(
                angajat,
                device,
                supabase_url,
                photo_request=photo_request,
                photo_config=photo_config,
            )
            
            # Record result
            per_device_results.append({
                "device_id": device_id,
                "device_ip": device_ip,
                "status": result.status.value,
                "message": result.message,
                "step": result.step
            })
            
            # Update summary counts
            summary[result.status.value] = summary.get(result.status.value, 0) + 1
            
            # Add delay between devices (except after the last one)
            if device != devices[-1]:
                await rate_limit_delay(1.0)
        
        # Return structured result (always 200, status in payload)
        return {
            "status": "ok",
            "summary": summary,
            "per_device": per_device_results
        }
        
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()
        }


# Initialize event loggers
_EVENT_LOGGER = DailyLogger("hikvision_events", "hikvision_events_{date}.log", _LOG_DIR, subfolder="hikvision_events")
_ACCESS_LOGGER = DailyLogger("hikvision_access", "Access Log {date}.log", _LOG_DIR, subfolder="access")

@app.post("/{full_path:path}")
@limiter.limit("100/minute")
async def catch_all_post(request: Request, full_path: str):
    """Catch-all endpoint for receiving Hikvision device events."""
    # Check IP whitelist
    client_ip = _get_client_ip(request)
    if not _is_ip_whitelisted(client_ip):
        event_logger = _EVENT_LOGGER.get()
        event_logger.warning(f"Blocked device event from non-whitelisted IP: {client_ip}, path: {full_path}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: IP address not whitelisted"
        )
    
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    
    # Process event using events module (parse/log/classify only)
    process_result = await process_event_request(
        body=body,
        content_type=content_type,
        path=full_path,
        event_logger=_EVENT_LOGGER.get(),
        access_logger=_ACCESS_LOGGER.get(),
        supabase_client=None,
        save_to_supabase=False,
    )

    if process_result.get("is_access_event") and process_result.get("parsed"):
        event_logger = _EVENT_LOGGER.get()
        parsed_event = process_result["parsed"]
        event_device_ip = _extract_event_device_ip(parsed_event)
        if not event_device_ip:
            event_logger.error("Unable to route event: missing ipAddress in payload.")
            return PlainTextResponse("OK")

        cache_available = await _ensure_device_cache_fresh(event_logger=event_logger, reason="event_route")
        if not cache_available:
            event_logger.error("Unable to route event for device_ip=%s: device cache unavailable.", event_device_ip)
            return PlainTextResponse("OK")

        mediu = _DEVICE_MEDIU_CACHE.get(event_device_ip)
        cache_hit = mediu is not None
        if not mediu:
            await _ensure_device_cache_fresh(event_logger=event_logger, force=True, reason="cache_miss")
            mediu = _DEVICE_MEDIU_CACHE.get(event_device_ip)

        target_envs = _resolve_target_envs(mediu)
        if not target_envs:
            event_logger.error(
                "Unable to route event for device_ip=%s. mediu=%s. Event was not forwarded.",
                event_device_ip,
                mediu,
            )
        else:
            save_statuses = await _save_access_event_to_targets(
                parsed_event=parsed_event,
                target_envs=target_envs,
                event_logger=event_logger,
            )
            event_logger.info(
                "Access event routing device_ip=%s cache_hit=%s mediu=%s targets=%s statuses=%s",
                event_device_ip,
                cache_hit,
                mediu,
                target_envs,
                save_statuses,
            )
    
    return PlainTextResponse("OK")
