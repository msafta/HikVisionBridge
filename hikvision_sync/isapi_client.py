"""ISAPI client functions for Hikvision device communication."""

import json
import requests
from typing import Optional
from .models import SyncResult, SyncResultStatus


def _build_person_payload(angajat: dict) -> dict:
    """
    Build ISAPI Person creation payload from angajat data.
    Args:
        angajat: Dict with angajat data including biometrie.employee_no, nume, prenume, status
    Returns:
        JSON payload dict for ISAPI Person creation
    """
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    
    if not employee_no:
        raise ValueError("employee_no is required for sync")
    
    # Format name: "Nume Prenume" or fallback to nume_complet
    nume = angajat.get("nume", "").strip()
    prenume = angajat.get("prenume", "").strip()
    if nume and prenume:
        name = f"{nume} {prenume}"
    else:
        name = angajat.get("nume_complet", "").strip() or "Unknown"
    
    # Determine if employee is active
    status = angajat.get("status", "").lower()
    is_active = status == "activ"
    
    return {
        "UserInfo": {
            "employeeNo": str(employee_no),  # Device expects string format
            "name": name,
            "userType": "normal",
            "Valid": {
                "enable": is_active,
                "beginTime": "2025-10-10T00:00:00",
                "endTime": "2037-12-31T23:59:59"
            },
            "doorRight": "1",
            "RightPlan": [
                {
                    "doorNo": 1,
                    "plamTemplateNo": "1"
                }
            ]
        }
    }


def _build_face_image_payload(angajat: dict, supabase_url: Optional[str] = None) -> dict:
    """
    Build ISAPI Face Image payload from angajat data.
    Args:
        angajat: Dict with angajat data including biometrie.employee_no and foto_fata_url
        supabase_url: Optional Supabase URL (e.g., "https://xxx.supabase.co") to construct full URL if foto_fata_url is just a filename
    Returns:
        JSON payload dict for ISAPI Face Image addition
    """
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    foto_fata_url = biometrie.get("foto_fata_url")
    
    if not employee_no:
        raise ValueError("employee_no is required for face image sync")
    if not foto_fata_url:
        raise ValueError("foto_fata_url is required for face image sync")
    
    # If foto_fata_url is just a filename (no http:// or https://), construct full Supabase Storage URL
    if not foto_fata_url.startswith("http://") and not foto_fata_url.startswith("https://"):
        if supabase_url:
            # Construct full URL: https://xxx.supabase.co/storage/v1/object/public/pontaj-photos/{filename}
            foto_fata_url = f"{supabase_url}/storage/v1/object/public/pontaj-photos/{foto_fata_url}"
            print(f"  INFO: Constructed full Supabase Storage URL from filename")
        else:
            raise ValueError(f"foto_fata_url is just a filename ('{foto_fata_url}') but supabase_url not provided to construct full URL")
    
    # Ensure HTTPS is used for Supabase storage URLs (devices need HTTPS for external URLs)
    if foto_fata_url.startswith("http://") and ".supabase.co" in foto_fata_url:
        foto_fata_url = foto_fata_url.replace("http://", "https://", 1)
        print(f"  INFO: Converted HTTP to HTTPS for Supabase URL")
    
    return {
        "faceLibType": "blackFD",
        "FDID": "1",
        "FPID": str(employee_no),  # Numeric employee number as string
        "faceURL": foto_fata_url
    }


def _build_face_image_update_payload(angajat: dict, supabase_url: Optional[str] = None) -> dict:
    """
    Build ISAPI Face Image update payload from angajat data (for PUT request).
    Args:
        angajat: Dict with angajat data including biometrie.employee_no and foto_fata_url
        supabase_url: Optional Supabase URL (e.g., "https://xxx.supabase.co") to construct full URL if foto_fata_url is just a filename
    Returns:
        JSON payload dict for ISAPI Face Image update (PUT request)
    """
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    foto_fata_url = biometrie.get("foto_fata_url")
    
    if not employee_no:
        raise ValueError("employee_no is required for face image update")
    if not foto_fata_url:
        raise ValueError("foto_fata_url is required for face image update")
    
    # If foto_fata_url is just a filename (no http:// or https://), construct full Supabase Storage URL
    if not foto_fata_url.startswith("http://") and not foto_fata_url.startswith("https://"):
        if supabase_url:
            # Construct full URL: https://xxx.supabase.co/storage/v1/object/public/pontaj-photos/{filename}
            foto_fata_url = f"{supabase_url}/storage/v1/object/public/pontaj-photos/{foto_fata_url}"
            print(f"  INFO: Constructed full Supabase Storage URL from filename")
        else:
            raise ValueError(f"foto_fata_url is just a filename ('{foto_fata_url}') but supabase_url not provided to construct full URL")
    
    # Ensure HTTPS is used for Supabase storage URLs (devices need HTTPS for external URLs)
    if foto_fata_url.startswith("http://") and ".supabase.co" in foto_fata_url:
        foto_fata_url = foto_fata_url.replace("http://", "https://", 1)
        print(f"  INFO: Converted HTTP to HTTPS for Supabase URL")
    
    return {
        "faceLibType": "blackFD",
        "FDID": "1",
        "FPID": str(employee_no),  # Numeric employee number as string
        "faceID": "1",  # Default face ID for update
        "faceURL": foto_fata_url
    }


def _classify_person_response(response) -> SyncResult:
    """
    Classify ISAPI Person creation response.
    Returns SyncResult with status and message.
    """
    # Auth failure is always fatal
    if response.status_code == 401:
        return SyncResult(
            SyncResultStatus.FATAL,
            "Authentication failed - invalid device credentials",
            "person"
        )
    
    # Try to parse JSON body even for non-200 responses
    # Some devices return HTTP 400 with "employeeNoAlreadyExist" in the body
    try:
        data = response.json()
        status_code = data.get("statusCode")
        sub_status_code = data.get("subStatusCode", "")
        
        # Success (HTTP 200)
        if status_code == 1 and sub_status_code == "ok":
            return SyncResult(SyncResultStatus.SUCCESS, "Person created/updated successfully", "person")
        
        # Already exists - treat as success (can be HTTP 200 or HTTP 400)
        if status_code == 6 and sub_status_code == "employeeNoAlreadyExist":
            return SyncResult(SyncResultStatus.SUCCESS, "Person already exists on device", "person")
        
        # Other ISAPI error (even if HTTP 200, but statusCode indicates error)
        error_msg = data.get("errorMsg", "") or data.get("statusString", "")
        return SyncResult(
            SyncResultStatus.FATAL,
            f"ISAPI error: statusCode={status_code}, subStatusCode={sub_status_code}, errorMsg={error_msg}",
            "person"
        )
    except Exception:
        # If we can't parse JSON, treat non-200 as fatal
        if response.status_code != 200:
            return SyncResult(
                SyncResultStatus.FATAL,
                f"HTTP {response.status_code}: {response.text[:200]}",
                "person"
            )
        # HTTP 200 but couldn't parse JSON - unexpected
        return SyncResult(
            SyncResultStatus.FATAL,
            f"HTTP 200 but failed to parse response: {response.text[:200]}",
            "person"
        )


async def create_person_on_device(device: dict, angajat: dict) -> SyncResult:
    """
    Create or update Person record on Hikvision device via ISAPI.
    Args:
        device: Device dict with ip_address, port, username, password_encrypted
        angajat: Angajat dict with biometrie data
    Returns:
        SyncResult with status and message
    """
    try:
        # Build payload
        payload = _build_person_payload(angajat)
        
        # Build URL - handle both Supabase format (ip_address) and legacy format (ip)
        ip = device.get("ip_address") or device.get("ip")
        port = device.get("port") or 80  # Default to 80 if port is None or 0
        if port == 8000:  # Likely wrong port - Hikvision devices typically use 80
            print(f"  WARNING: Port is 8000, but device info endpoint works on port 80. Using port 80 instead.")
            port = 80
        url = f"http://{ip}:{port}/ISAPI/AccessControl/UserInfo/Record?format=json"
        
        # Handle both Supabase format (username/password_encrypted) and legacy format (user/password)
        username = device.get("username") or device.get("user", "")
        password = device.get("password_encrypted") or device.get("password", "")  # Note: despite name, this is plain password
        
        # Debug logging
        print(f"DEBUG ISAPI Request:")
        print(f"  URL: {url}")
        print(f"  Username: {username}")
        print(f"  Password length: {len(password)}")
        print(f"  Password (first 3 chars): {password[:3] if password else 'None'}...")
        print(f"  Payload: {json.dumps(payload, indent=2)}")
        
        # Make request with Digest Auth
        # Note: Some devices may require User-Agent header
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Hikvision-ISAPI-Client/1.0",
        }
        
        print(f"  Headers: {headers}")
        
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            auth=requests.auth.HTTPDigestAuth(username, password),
            timeout=15.0,
            verify=False,  # Disable SSL verification if device uses self-signed cert
        )
        
        print(f"DEBUG Response:")
        print(f"  Status: {response.status_code}")
        print(f"  Headers: {dict(response.headers)}")
        print(f"  Body: {response.text[:500]}")
        
        return _classify_person_response(response)
        
    except requests.exceptions.Timeout as exc:
        print(f"DEBUG Timeout error: {exc}")
        return SyncResult(
            SyncResultStatus.FATAL,
            f"Request timeout - device {device.get('ip_address')} not responding: {exc}",
            "person"
        )
    except requests.exceptions.ConnectionError as exc:
        print(f"DEBUG Connection error: {exc}")
        print(f"DEBUG Connection error type: {type(exc)}")
        print(f"DEBUG Connection error args: {exc.args}")
        return SyncResult(
            SyncResultStatus.FATAL,
            f"Connection error - device {device.get('ip_address')} unreachable: {exc}",
            "person"
        )
    except Exception as exc:
        import traceback
        print(f"DEBUG Unexpected error: {exc}")
        print(f"DEBUG Traceback: {traceback.format_exc()}")
        return SyncResult(
            SyncResultStatus.FATAL,
            f"Unexpected error: {exc}",
            "person"
        )


async def add_face_image_to_device(device: dict, angajat: dict, supabase_url: Optional[str] = None) -> SyncResult:
    """
    Add face image to Person on Hikvision device via ISAPI.
    Args:
        device: Device dict with ip_address, port, username, password_encrypted
        angajat: Angajat dict with biometrie data including foto_fata_url
        supabase_url: Optional Supabase URL to construct full image URL if foto_fata_url is just a filename
    Returns:
        SyncResult with status and message (non-fatal errors return PARTIAL status)
    """
    try:
        # Build payload
        payload = _build_face_image_payload(angajat, supabase_url)
        
        # Build URL - handle both Supabase format (ip_address) and legacy format (ip)
        ip = device.get("ip_address") or device.get("ip")
        port = device.get("port") or 80  # Default to 80 if port is None or 0
        if port == 8000:  # Likely wrong port - Hikvision devices typically use 80
            print(f"  WARNING: Port is 8000, but device info endpoint works on port 80. Using port 80 instead.")
            port = 80
        url = f"http://{ip}:{port}/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json"
        
        # Handle both Supabase format (username/password_encrypted) and legacy format (user/password)
        username = device.get("username") or device.get("user", "")
        password = device.get("password_encrypted") or device.get("password", "")  # Note: despite name, this is plain password
        
        # Debug logging
        print("DEBUG ISAPI Request (Face):")
        print(f"  Device URL: {url}")
        print(f"  Username: {username}")
        print(f"  Password length: {len(password)}")
        print(f"  Password (first 3 chars): {password[:3] if password else 'None'}...")
        print(f"  Face Image URL (faceURL): {payload.get('faceURL', 'N/A')}")
        print(f"  Payload: {json.dumps(payload, indent=2)}")
        
        # Make request with Digest Auth
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Hikvision-ISAPI-Client/1.0",
        }
        print(f"  Headers: {headers}")
        
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            auth=requests.auth.HTTPDigestAuth(username, password),
            timeout=15.0,
        )
        
        print("DEBUG Response (Face):")
        print(f"  Status: {response.status_code}")
        print(f"  Headers: {dict(response.headers)}")
        print(f"  Body: {response.text[:500]}")
        
        if response.status_code == 200:
            return SyncResult(SyncResultStatus.SUCCESS, "Face image added successfully", "photo")
        else:
            # Photo failure is non-fatal (partial success)
            return SyncResult(
                SyncResultStatus.PARTIAL,
                f"Face image failed: HTTP {response.status_code} - {response.text[:200]}",
                "photo"
            )
        
    except requests.exceptions.Timeout:
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Request timeout - device {device.get('ip_address')} not responding",
            "photo"
        )
    except requests.exceptions.ConnectionError:
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Connection error - device {device.get('ip_address')} unreachable",
            "photo"
        )
    except Exception as exc:
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Unexpected error: {exc}",
            "photo"
        )


async def update_face_image_to_device(device: dict, angajat: dict, supabase_url: Optional[str] = None) -> SyncResult:
    """
    Update face image on Hikvision device via ISAPI (PUT), fallback-ready.

    Args:
        device: Device dict with ip_address, port, username, password_encrypted
        angajat: Angajat dict with biometrie data including foto_fata_url
        supabase_url: Optional Supabase URL to construct full image URL if foto_fata_url is just a filename

    Returns:
        SyncResult with status and message (errors are PARTIAL to allow POST fallback)
    """
    try:
        # Build payload (includes faceID field)
        payload = _build_face_image_update_payload(angajat, supabase_url)

        # Build URL - handle both Supabase format (ip_address) and legacy format (ip)
        ip = device.get("ip_address") or device.get("ip")
        port = device.get("port") or 80  # Default to 80 if port is None or 0
        if port == 8000:  # Likely wrong port - Hikvision devices typically use 80
            print(f"  WARNING: Port is 8000, but device info endpoint works on port 80. Using port 80 instead.")
            port = 80
        url = f"http://{ip}:{port}/ISAPI/Intelligent/FDLib/FDModify?format=json"

        # Handle both Supabase format (username/password_encrypted) and legacy format (user/password)
        username = device.get("username") or device.get("user", "")
        password = device.get("password_encrypted") or device.get("password", "")  # Note: despite name, this is plain password

        # Debug logging
        print("DEBUG ISAPI Request (Face Update - PUT):")
        print(f"  Device URL: {url}")
        print(f"  Username: {username}")
        print(f"  Password length: {len(password)}")
        print(f"  Password (first 3 chars): {password[:3] if password else 'None'}...")
        print(f"  Face Image URL (faceURL): {payload.get('faceURL', 'N/A')}")
        print(f"  Payload: {json.dumps(payload, indent=2)}")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Hikvision-ISAPI-Client/1.0",
        }
        print(f"  Headers: {headers}")

        response = requests.put(
            url,
            json=payload,
            headers=headers,
            auth=requests.auth.HTTPDigestAuth(username, password),
            timeout=15.0,
        )

        print("DEBUG Response (Face Update - PUT):")
        print(f"  Status: {response.status_code}")
        print(f"  Headers: {dict(response.headers)}")
        print(f"  Body: {response.text[:500]}")

        # Parse response
        if response.status_code == 200:
            try:
                data = response.json()
                status_code = data.get("statusCode")
                status_string = data.get("statusString", "")
                sub_status = data.get("subStatusCode", "")
                if status_code == 1 and (sub_status == "ok" or status_string.lower() == "ok"):
                    return SyncResult(SyncResultStatus.SUCCESS, "Face image updated successfully (PUT)", "photo")
                # Non-OK JSON payload, treat as partial to allow fallback
                return SyncResult(
                    SyncResultStatus.PARTIAL,
                    f"Face image update failed: statusCode={status_code}, subStatusCode={sub_status}, statusString={status_string}",
                    "photo"
                )
            except Exception as exc:
                # JSON parse error on 200 - treat as partial to allow fallback
                return SyncResult(
                    SyncResultStatus.PARTIAL,
                    f"Face image update: HTTP 200 but failed to parse response: {exc}",
                    "photo"
                )

        # Non-200 HTTP codes - partial to allow fallback
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Face image update failed: HTTP {response.status_code} - {response.text[:200]}",
            "photo"
        )

    except requests.exceptions.Timeout:
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Request timeout - device {device.get('ip_address')} not responding",
            "photo"
        )
    except requests.exceptions.ConnectionError:
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Connection error - device {device.get('ip_address')} unreachable",
            "photo"
        )
    except Exception as exc:
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Unexpected error: {exc}",
            "photo"
        )


async def rate_limit_delay(seconds: float = 1.0):
    """Rate limiting helper - delay between device API calls."""
    import asyncio
    await asyncio.sleep(seconds)

