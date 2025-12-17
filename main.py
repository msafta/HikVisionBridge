import asyncio
import ipaddress
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

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


# Initialize Supabase client
_SUPABASE_EDGE_FUNCTION_API_KEY = _get_config_value("SUPABASE_EDGE_FUNCTION_API_KEY", "supabase_edge_function_api_key", "hikvision-sync-2024")
_SUPABASE_CLIENT = SupabaseClient(_SUPABASE_URL, _SUPABASE_EDGE_FUNCTION_API_KEY) if _SUPABASE_URL else None


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


@app.get("/env-test")
def env_test():
    """Test endpoint to verify environment variables are loaded correctly."""
    return {
        "env": os.getenv("APP_ENV"),
        "vpn_subnet": os.getenv("VPN_SUBNET"),
        "allowed_origins": os.getenv("ALLOWED_ORIGINS"),
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
        "angajat_id": "<uuid>"  # Required: UUID of the angajat to sync
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
    
    if not _SUPABASE_CLIENT:
        return {
            "status": "error",
            "error": "Supabase client not initialized"
        }
    
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    
    try:
        # Fetch angajat with biometrie data
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
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
        
        # Get Supabase URL for constructing image URLs (from .env, not config file)
        supabase_url = _get_config_value("SUPABASE_URL", "supabase_url")
        
        # Sync to each device sequentially (using existing sync_angajat_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Sync to this device using direct image data functionality
            result = await sync_angajat_to_device_with_data(angajat, device, supabase_url)
            
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
        "angajat_id": "<uuid>"  # Required: UUID of the angajat to delete
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
    if not _SUPABASE_CLIENT:
        return {
            "status": "error",
            "error": "Supabase client not initialized"
        }
    
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    
    try:
        # Fetch angajat with biometrie data
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
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
    {}  # No parameters required
    
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
    if not _SUPABASE_CLIENT:
        return {
            "status": "error",
            "error": "Supabase client not initialized"
        }
    
    try:
        # Fetch all active angajati with biometrie data
        angajati = await _SUPABASE_CLIENT.get_all_active_angajati_with_biometrie()
        if not angajati:
            return {
                "status": "error",
                "error": "No active angajati found"
            }
        
        # Fetch all active devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
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
        
        # Get Supabase URL for constructing image URLs (from .env, not config file)
        supabase_url = _get_config_value("SUPABASE_URL", "supabase_url")
        
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
                result = await sync_angajat_to_device_with_data(angajat, device, supabase_url)
                
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
        "angajat_id": "<uuid>"  # Required: UUID of the angajat to sync photo for
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
    if not _SUPABASE_CLIENT:
        return {
            "status": "error",
            "error": "Supabase client not initialized"
        }
    
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    
    try:
        # Fetch angajat with biometrie data
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
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
        
        # Get Supabase URL for constructing image URLs (from .env, not config file)
        supabase_url = _get_config_value("SUPABASE_URL", "supabase_url")
        
        # Sync photo to each device sequentially (using sync_photo_only_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Sync photo only to this device using direct image data (skips person creation)
            result = await sync_photo_only_to_device_with_data(angajat, device, supabase_url)
            
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
        "angajat_id": "<uuid>"  # Required: UUID of the angajat to update photo for
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
    if not _SUPABASE_CLIENT:
        return {
            "status": "error",
            "error": "Supabase client not initialized"
        }
    
    # Validate request body
    angajat_id = body.get("angajat_id")
    if not angajat_id:
        return {
            "status": "error",
            "error": "Missing required field: angajat_id"
        }
    
    try:
        # Fetch angajat with biometrie data
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {
                "status": "error",
                "error": f"Angajat {angajat_id} not found"
            }
        
        # Fetch all active devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
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
        
        # Get Supabase URL for constructing image URLs (from .env, not config file)
        supabase_url = _get_config_value("SUPABASE_URL", "supabase_url")
        
        # Update photo to each device sequentially (using update_photo_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Update photo to this device using direct image data (PUT with POST fallback)
            result = await update_photo_to_device_with_data(angajat, device, supabase_url)
            
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
    
    # Process event using events module
    await process_event_request(
        body=body,
        content_type=content_type,
        path=full_path,
        event_logger=_EVENT_LOGGER.get(),
        access_logger=_ACCESS_LOGGER.get(),
        supabase_client=_SUPABASE_CLIENT
    )
    
    return PlainTextResponse("OK")
