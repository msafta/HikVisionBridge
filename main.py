import asyncio
import base64
import json
import logging
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

import httpx
import jwt
import requests
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jwt import PyJWKClient
from requests.auth import HTTPDigestAuth

from hikvision_sync.supabase_client import SupabaseClient
from hikvision_sync.isapi_client import create_person_on_device, add_face_image_to_device, rate_limit_delay
from hikvision_sync.orchestration import sync_angajat_to_device, sync_photo_only_to_device, sync_photo_only_to_device

app = FastAPI()

_ROOT_DIR = Path(__file__).resolve().parent
_LOG_DIR = _ROOT_DIR
_CONFIG_PATH = _ROOT_DIR / "config" / "devices.json"
_APP_CONFIG_PATH = _ROOT_DIR / "config" / "app_settings.json"
_FACE_DIR = _ROOT_DIR / "faces"
_FACE_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(_ROOT_DIR / "templates"))
app.mount("/faces", StaticFiles(directory=str(_FACE_DIR)), name="faces")

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Initialize Supabase client
_SUPABASE_CLIENT = SupabaseClient(_SUPABASE_URL, "hikvision-sync-2024") if _SUPABASE_URL else None


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


@app.get("/api/health-auth")
async def auth_health_check(_: dict = Depends(require_auth)):
    return {"status": "ok", "auth": "passed"}


@app.get("/api/test-supabase/config")
async def test_supabase_config():
    """Test endpoint to verify Supabase Edge Function config is loaded."""
    if not _SUPABASE_CLIENT:
        return {"error": "Supabase client not initialized"}
    return {
        "supabase_url": _SUPABASE_URL,
        "edge_function_url": _SUPABASE_CLIENT.edge_function_url,
        "edge_function_api_key": _SUPABASE_CLIENT.api_key,
        "headers": _SUPABASE_CLIENT._get_headers(),
    }


@app.get("/api/test-supabase/devices")
async def test_supabase_devices():
    """Test endpoint to verify Supabase device fetching works via Edge Function."""
    try:
        devices = await get_active_devices()
        return {"status": "ok", "count": len(devices), "devices": devices}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "error_type": type(exc).__name__}


@app.get("/api/test-supabase/angajati")
async def test_supabase_angajati():
    """Test endpoint to verify Supabase angajati fetching works."""
    try:
        angajati = await get_all_active_angajati_with_biometrie()
        return {"status": "ok", "count": len(angajati), "angajati": angajati[:5]}  # Return first 5 for testing
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@app.get("/api/test-supabase/angajat/{angajat_id}")
async def test_supabase_angajat(angajat_id: str):
    """Test endpoint to verify fetching a single angajat with biometrie works."""
    try:
        angajat = await get_angajat_with_biometrie(angajat_id)
        if angajat:
            return {"status": "ok", "angajat": angajat}
        else:
            return {"status": "not_found", "message": f"Angajat with id {angajat_id} not found"}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "error_type": type(exc).__name__}


# Phase 3 Test Endpoints - ISAPI Client Functions
from hikvision_sync.isapi_client import create_person_on_device, add_face_image_to_device
from fastapi import Query


@app.post("/api/test-isapi/create-person")
async def test_create_person(angajat_id: str = Query(...), device_id: str = Query(None)):
    """
    Test endpoint for create_person_on_device ISAPI function.
    Query params:
        angajat_id: UUID of angajat to sync (required)
        device_id: Optional device ID (if not provided, uses first active device)
    """
    if not _SUPABASE_CLIENT:
        return {"status": "error", "error": "Supabase client not initialized"}
    
    try:
        # Fetch angajat
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {"status": "error", "error": f"Angajat {angajat_id} not found"}
        
        # Check if employee_no exists
        biometrie = angajat.get("biometrie", {})
        if not biometrie.get("employee_no"):
            return {"status": "error", "error": "Angajat missing employee_no"}
        
        # Fetch devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
        if not devices:
            return {"status": "error", "error": "No active devices found"}
        
        # Select device
        if device_id:
            device = next((d for d in devices if d.get("id") == device_id), None)
            if not device:
                return {"status": "error", "error": f"Device {device_id} not found"}
        else:
            device = devices[0]  # Use first device
        
        # Test create_person_on_device
        result = await create_person_on_device(device, angajat)
        
        return {
            "status": "ok",
            "result": result.to_dict(),
            "device": {"id": device.get("id"), "ip_address": device.get("ip_address")},
            "angajat": {"id": angajat.get("id"), "name": f"{angajat.get('nume', '')} {angajat.get('prenume', '')}".strip()},
        }
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }


@app.post("/api/test-isapi/add-face-image")
async def test_add_face_image(angajat_id: str = Query(...), device_id: str = Query(None)):
    """
    Test endpoint for add_face_image_to_device ISAPI function.
    Query params:
        angajat_id: UUID of angajat to sync photo for (required)
        device_id: Optional device ID (if not provided, uses first active device)
    """
    if not _SUPABASE_CLIENT:
        return {"status": "error", "error": "Supabase client not initialized"}
    
    try:
        # Fetch angajat
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {"status": "error", "error": f"Angajat {angajat_id} not found"}
        
        # Check if employee_no and foto_fata_url exist
        biometrie = angajat.get("biometrie", {})
        if not biometrie.get("employee_no"):
            return {"status": "error", "error": "Angajat missing employee_no"}
        if not biometrie.get("foto_fata_url"):
            return {"status": "error", "error": "Angajat missing foto_fata_url"}
        
        # Fetch devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
        if not devices:
            return {"status": "error", "error": "No active devices found"}
        
        # Select device
        if device_id:
            device = next((d for d in devices if d.get("id") == device_id), None)
            if not device:
                return {"status": "error", "error": f"Device {device_id} not found"}
        else:
            device = devices[0]  # Use first device
        
        # Test add_face_image_to_device
        supabase_url = _APP_CONFIG.get("supabase_url")
        result = await add_face_image_to_device(device, angajat, supabase_url)
        
        return {
            "status": "ok",
            "result": result.to_dict(),
            "device": {"id": device.get("id"), "ip_address": device.get("ip_address")},
            "angajat": {"id": angajat.get("id"), "name": f"{angajat.get('nume', '')} {angajat.get('prenume', '')}".strip()},
        }
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }


@app.post("/api/test-isapi/sync-angajat-to-device")
async def test_sync_angajat_to_device(angajat_id: str = Query(...), device_id: str = Query(None)):
    """
    Test endpoint for sync_angajat_to_device orchestration function.
    This tests the full sync flow: person creation + photo addition.
    Query params:
        angajat_id: UUID of angajat to sync (required)
        device_id: Optional device ID (if not provided, uses first active device)
    """
    if not _SUPABASE_CLIENT:
        return {"status": "error", "error": "Supabase client not initialized"}
    
    try:
        # Fetch angajat
        angajat = await _SUPABASE_CLIENT.get_angajat_with_biometrie(angajat_id)
        if not angajat:
            return {"status": "error", "error": f"Angajat {angajat_id} not found"}
        
        # Fetch devices
        devices = await _SUPABASE_CLIENT.get_active_devices()
        if not devices:
            return {"status": "error", "error": "No active devices found"}
        
        # Select device
        if device_id:
            device = next((d for d in devices if d.get("id") == device_id), None)
            if not device:
                return {"status": "error", "error": f"Device {device_id} not found"}
        else:
            device = devices[0]  # Use first device
        
        # Test sync_angajat_to_device (full orchestration)
        supabase_url = _APP_CONFIG.get("supabase_url")
        result = await sync_angajat_to_device(angajat, device, supabase_url)
        
        return {
            "status": "ok",
            "result": result.to_dict(),
            "device": {"id": device.get("id"), "ip_address": device.get("ip_address")},
            "angajat": {"id": angajat.get("id"), "name": f"{angajat.get('nume', '')} {angajat.get('prenume', '')}".strip()},
        }
    except Exception as exc:
        import traceback
        return {
            "status": "error",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc(),
        }


@app.post("/api/hikvision/sync-angajat-all-devices")
async def sync_angajat_all_devices(
    body: dict
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
        
        # Get Supabase URL for constructing image URLs
        supabase_url = _APP_CONFIG.get("supabase_url")
        
        # Sync to each device sequentially (using existing sync_angajat_to_device function)
        for device in devices:
            device_id = device.get("id", "unknown")
            device_ip = device.get("ip_address", "unknown")
            
            # Sync to this device using existing functionality
            result = await sync_angajat_to_device(angajat, device, supabase_url)
            
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


class _DailyLogger:
    def __init__(self, name: str, filename_template: str):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.filename_template = filename_template
        self.current_date = None

    def get(self) -> logging.Logger:
        today = date.today().isoformat()
        if self.current_date != today:
            for handler in list(self.logger.handlers):
                self.logger.removeHandler(handler)
                handler.close()
            log_file = _LOG_DIR / self.filename_template.format(date=today)
            handler = logging.FileHandler(log_file)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.logger.addHandler(handler)
            self.current_date = today
        return self.logger


_EVENT_LOGGER = _DailyLogger("hikvision_events", "hikvision_events_{date}.log")
_ACCESS_LOGGER = _DailyLogger("hikvision_access", "Access Log {date}.log")

_HEADERS = {"Content-Type": "application/xml"}
_device_cache: List[dict] = []
_device_cache_mtime: Optional[float] = None


def _load_devices() -> List[dict]:
    global _device_cache, _device_cache_mtime
    if not _CONFIG_PATH.exists():
        return []
    mtime = _CONFIG_PATH.stat().st_mtime
    if _device_cache and _device_cache_mtime == mtime:
        return _device_cache
    try:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _EVENT_LOGGER.get().error(f"Device config parse error: {exc}")
        return []
    devices = config.get("devices", [])
    _device_cache = devices
    _device_cache_mtime = mtime
    return devices


def _build_user_xml(user_id: str, name: str, face_image_bytes: bytes) -> str:
    """Build XML with raw face image data (base64 encoded)."""
    user_info = ET.Element("UserInfo")
    ET.SubElement(user_info, "userID").text = user_id
    ET.SubElement(user_info, "name").text = name
    
    # Valid element with enable and time range
    valid = ET.SubElement(user_info, "valid")
    ET.SubElement(valid, "enable").text = "true"
    ET.SubElement(valid, "beginTime").text = "2000-01-01T00:00:00"
    ET.SubElement(valid, "endTime").text = "2037-12-31T23:59:59"

    # Face recognition with raw data
    face = ET.SubElement(user_info, "face")
    ET.SubElement(face, "faceID").text = "1"
    ET.SubElement(face, "faceName").text = "Face1"
    ET.SubElement(face, "faceDataType").text = "raw"
    # Encode image as base64 (remove all newlines and whitespace)
    face_data = base64.b64encode(face_image_bytes).decode("utf-8").replace("\r\n", "").replace("\n", "").replace("\r", "").strip()
    face_data_elem = ET.SubElement(face, "faceData")
    face_data_elem.text = face_data
    ET.SubElement(face, "valid").text = "1"

    return ET.tostring(user_info, encoding="utf-8", xml_declaration=True).decode()


def _provision_device(device: dict, user_id: str, name: str, face_image_bytes: bytes) -> str:
    url = f"http://{device['ip']}:{device['port']}/ISAPI/AccessControl/UserInfo/Record"
    payload = _build_user_xml(user_id, name, face_image_bytes)
    
    # Console output
    image_size_kb = len(face_image_bytes) / 1024
    # Encode to base64 to show first 20 characters
    face_data_base64 = base64.b64encode(face_image_bytes).decode("utf-8").replace("\r\n", "").replace("\n", "").replace("\r", "").strip()
    
    # Verify JPEG magic bytes
    jpeg_magic_1 = b'\xff\xd8\xff\xe0'
    jpeg_magic_2 = b'\xff\xd8\xff\xe1'
    is_jpeg = face_image_bytes.startswith(jpeg_magic_1) or face_image_bytes.startswith(jpeg_magic_2)
    jpeg_status = "✓ Valid JPEG" if is_jpeg else "⚠ Not a valid JPEG"
    first_bytes_hex = face_image_bytes[:4].hex() if len(face_image_bytes) >= 4 else "N/A"
    
    print(f"\n{'='*60}")
    print(f"Provisioning user to device: {device.get('name', 'Unknown')} ({device['ip']})")
    print(f"User ID: {user_id}")
    print(f"Name: {name}")
    print(f"Face Image: {image_size_kb:.2f} KB (raw/base64)")
    print(f"Image Magic Bytes (hex): {first_bytes_hex} - {jpeg_status}")
    print(f"Base64 Encoded (first 20 chars): {face_data_base64[:20]}")
    print(f"ISAPI Endpoint: {url}")
    
    # Show full XML structure but replace base64 data with placeholder
    base64_length = 0
    match = re.search(r'<faceData>([A-Za-z0-9+/=\s]+)</faceData>', payload)
    if match:
        base64_length = len(match.group(1).strip())
    xml_for_display = re.sub(
        r'<faceData>[A-Za-z0-9+/=\s]+</faceData>',
        f'<faceData>[Base64 data: {base64_length} chars]</faceData>',
        payload
    )
    print(f"\nXML Payload Structure:")
    print(xml_for_display)
    print(f"{'='*60}\n")
    
    # Debug logging
    logger = _EVENT_LOGGER.get()
    logger.info(f"Provisioning user {user_id} to {device['ip']}")
    logger.info(f"Face image size: {image_size_kb:.2f} KB")
    logger.info(f"XML payload: {payload}")
    
    response = requests.post(
        url,
        data=payload,
        headers=_HEADERS,
        auth=HTTPDigestAuth(device["user"], device["password"]),
        timeout=10,
    )
    
    print(f"Response Status: {response.status_code}")
    if response.status_code not in (200, 201):
        print(f"Response Body: {response.text}")
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    else:
        print(f"Response Body: {response.text}")
        print(f"✅ Success!\n")
    
    return response.text


def _build_face_url(request: Request, filename: str) -> str:
    # Build URL that device can access - use the request's host
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/faces/{filename}"


def _persist_face_file(upload: UploadFile) -> str:
    suffix = Path(upload.filename or "").suffix.lower() or ".jpg"
    filename = f"{uuid4().hex}{suffix}"
    destination = _FACE_DIR / filename
    content = upload.file.read()
    destination.write_bytes(content)
    upload.file.close()
    return filename


def _render_form(
    request: Request,
    *,
    form_data: Optional[dict] = None,
    errors: Optional[List[str]] = None,
    results: Optional[List[dict]] = None,
):
    devices = _load_devices()
    form_defaults = {
        "user_id": "",
        "name": "",
        "device_ids": [],
    }
    if form_data:
        form_defaults.update(form_data)
    context = {
        "request": request,
        "devices": devices,
        "form": form_defaults,
        "errors": errors or [],
        "results": results or [],
    }
    return templates.TemplateResponse("provision.html", context)


def _extract_event(parsed):
    if isinstance(parsed, dict):
        event = parsed.get("AccessControllerEvent")
        if isinstance(event, dict):
            return event
    elif isinstance(parsed, ET.Element):
        event = parsed.find("AccessControllerEvent")
        if event is not None:
            return {child.tag: child.text for child in event}
    return None


def _is_access_event(parsed) -> bool:
    event = _extract_event(parsed)
    if not event:
        return False
    try:
        major = int(event.get("majorEventType"))
        sub = int(event.get("subEventType"))
    except (TypeError, ValueError):
        return False
    return major == 5 and sub == 75


def _extract_boundary(content_type: str) -> Optional[str]:
    parts = [p.strip() for p in content_type.split(";")]
    for part in parts:
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1]
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
            return boundary
    return None


def _parse_multipart_event(body: bytes, content_type: str, logger: logging.Logger):
    boundary = _extract_boundary(content_type)
    if not boundary:
        logger.error("Multipart request missing boundary")
        return None
    delimiter = f"--{boundary}".encode()
    for raw_part in body.split(delimiter):
        part = raw_part.strip()
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2]
        header_blob, _, content = part.partition(b"\r\n\r\n")
        if b'name="event_log"' not in header_blob:
            continue
        payload = content.strip()
        try:
            return json.loads(payload)
        except Exception as exc:
            logger.error(f"Multipart event_log JSON parse error: {exc}")
            return None
    return None


def _parse_request_body(body: bytes, content_type: str, logger: logging.Logger):
    if "multipart/form-data" in content_type:
        return _parse_multipart_event(body, content_type, logger)
    if "application/json" in content_type:
        try:
            return json.loads(body)
        except Exception as exc:
            logger.error(f"JSON parse error: {exc}")
    elif "xml" in content_type or body.strip().startswith(b"<"):
        try:
            return ET.fromstring(body)
        except Exception as exc:
            logger.error(f"XML parse error: {exc}")
    return None


@app.get("/admin/provision", response_class=HTMLResponse)
async def provision_form(request: Request):
    return _render_form(request)


@app.post("/admin/provision", response_class=HTMLResponse)
async def provision_submit(
    request: Request,
    user_id: str = Form(...),
    name: str = Form(...),
    device_ids: List[str] = Form(default=[]),
    face: UploadFile = File(...),
):
    errors: List[str] = []
    devices = _load_devices()
    device_lookup = {device["id"]: device for device in devices if "id" in device}
    selected_devices = [device_lookup[device_id] for device_id in device_ids if device_id in device_lookup]

    if not selected_devices:
        errors.append("Select at least one device.")
    if not user_id.strip():
        errors.append("User ID is required.")
    if not name.strip():
        errors.append("Name is required.")
    if face is None or not face.filename:
        errors.append("Face image is required.")

    if errors:
        return _render_form(
            request,
            form_data={
                "user_id": user_id,
                "name": name,
                "device_ids": device_ids,
            },
            errors=errors,
        )

    # Read the image bytes directly for raw upload
    face_image_bytes = await face.read()
    
    # Optionally still save for reference (can remove if not needed)
    suffix = Path(face.filename or "").suffix.lower() or ".jpg"
    saved_filename = f"{uuid4().hex}{suffix}"
    (_FACE_DIR / saved_filename).write_bytes(face_image_bytes)

    async def _run(device: dict):
        try:
            await asyncio.to_thread(_provision_device, device, user_id, name, face_image_bytes)
            return {"device": device, "status": "success"}
        except Exception as exc:
            return {"device": device, "status": "error", "message": str(exc)}

    tasks = [_run(device) for device in selected_devices]
    results = await asyncio.gather(*tasks)

    return _render_form(
        request,
        form_data={
            "user_id": "",
            "name": "",
            "device_ids": [],
        },
        results=results,
    )


@app.post("/{full_path:path}")
async def catch_all_post(request: Request, full_path: str):
    logger = _EVENT_LOGGER.get()
    # decode the path if percent-encoded
    decoded_path = urllib.parse.unquote(full_path)
    logger.info(f"Received POST to path: {decoded_path}")
    
    body = await request.body()
    content_type = request.headers.get("content-type", "")
    decoded_body = body.decode(errors="ignore")
    logger.info(f"Content-Type: {content_type}")
    logger.info(decoded_body)
    
    parsed = _parse_request_body(body, content_type, logger)
    if parsed and _is_access_event(parsed):
        access_logger = _ACCESS_LOGGER.get()
        access_logger.info(f"Received POST to path: {decoded_path}")
        access_logger.info(f"Content-Type: {content_type}")
        access_logger.info(decoded_body)
    
    return PlainTextResponse("OK")
