import asyncio
import base64
import json
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

import requests
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from requests.auth import HTTPDigestAuth

app = FastAPI()

_ROOT_DIR = Path(__file__).resolve().parent
_LOG_DIR = _ROOT_DIR
_CONFIG_PATH = _ROOT_DIR / "config" / "devices.json"
_FACE_DIR = _ROOT_DIR / "faces"
_FACE_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(_ROOT_DIR / "templates"))
app.mount("/faces", StaticFiles(directory=str(_FACE_DIR)), name="faces")


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
