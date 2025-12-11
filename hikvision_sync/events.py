"""Event handling for Hikvision device events."""

import json
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .supabase_client import SupabaseClient


class DailyLogger:
    """Logger that rotates log files daily."""
    
    def __init__(self, name: str, filename_template: str, log_dir: Path, subfolder: str = ""):
        """
        Initialize daily rotating logger.
        
        Args:
            name: Logger name
            filename_template: Template for log filename (e.g., "hikvision_events_{date}.log")
            log_dir: Base log directory (e.g., Path("logs"))
            subfolder: Subfolder name within log_dir (e.g., "hikvision_events")
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.filename_template = filename_template
        # Create subfolder path: log_dir / subfolder
        self.log_dir = log_dir / subfolder if subfolder else log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_date = None

    def get(self) -> logging.Logger:
        today = date.today()
        today_iso = today.isoformat()
        if self.current_date != today_iso:
            for handler in list(self.logger.handlers):
                self.logger.removeHandler(handler)
                handler.close()
            
            # Create year/month subdirectory structure: YYYY/MM/
            year_month_dir = self.log_dir / str(today.year) / f"{today.month:02d}"
            year_month_dir.mkdir(parents=True, exist_ok=True)
            
            # Place log file in year/month directory
            log_file = year_month_dir / self.filename_template.format(date=today_iso)
            handler = logging.FileHandler(log_file)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            self.logger.addHandler(handler)
            self.current_date = today_iso
        return self.logger


def extract_event(parsed):
    """Extract AccessControllerEvent from parsed request body."""
    if isinstance(parsed, dict):
        event = parsed.get("AccessControllerEvent")
        if isinstance(event, dict):
            return event
    elif isinstance(parsed, ET.Element):
        event = parsed.find("AccessControllerEvent")
        if event is not None:
            return {child.tag: child.text for child in event}
    return None


def is_access_event(parsed) -> bool:
    """Check if parsed event is an access control event (major=5, sub=75 or 76)."""
    event = extract_event(parsed)
    if not event:
        return False
    try:
        major = int(event.get("majorEventType"))
        sub = int(event.get("subEventType"))
    except (TypeError, ValueError):
        return False
    return major == 5 and sub in (75, 76)


def extract_boundary(content_type: str) -> Optional[str]:
    """Extract boundary parameter from multipart content-type header."""
    parts = [p.strip() for p in content_type.split(";")]
    for part in parts:
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1]
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]
            return boundary
    return None


def parse_multipart_event(body: bytes, content_type: str, logger: logging.Logger):
    """Parse multipart/form-data event body and extract event_log JSON."""
    boundary = extract_boundary(content_type)
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


def parse_request_body(body: bytes, content_type: str, logger: logging.Logger):
    """Parse request body based on content type (multipart, JSON, or XML)."""
    if "multipart/form-data" in content_type:
        return parse_multipart_event(body, content_type, logger)
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


async def process_event_request(
    body: bytes,
    content_type: str,
    path: str,
    event_logger: logging.Logger,
    access_logger: Optional[logging.Logger] = None,
    supabase_client: Optional["SupabaseClient"] = None
) -> dict:
    """
    Process incoming event request from Hikvision device.
    
    Args:
        body: Raw request body bytes
        content_type: Content-Type header value
        path: Request path
        event_logger: Logger for all events
        access_logger: Optional logger for access events only
        supabase_client: Optional Supabase client for saving access events
    
    Returns:
        dict with processing result including save_status
    """
    # Decode path if percent-encoded
    decoded_path = urllib.parse.unquote(path)
    event_logger.info(f"Received POST to path: {decoded_path}")
    
    decoded_body = body.decode(errors="ignore")
    event_logger.info(f"Content-Type: {content_type}")
    event_logger.info(decoded_body)
    
    # Parse request body
    parsed = parse_request_body(body, content_type, event_logger)
    
    # Check if it's an access event
    is_access = parsed and is_access_event(parsed) if parsed else False
    save_status = None
    
    if is_access:
        # Log to access logger if provided
        if access_logger:
            access_logger.info(f"Received POST to path: {decoded_path}")
            access_logger.info(f"Content-Type: {content_type}")
            access_logger.info(decoded_body)
        
        # Save to Supabase if client is provided
        if supabase_client and parsed:
            try:
                result = await supabase_client.save_access_event(parsed)
                if result.get("status") == "success":
                    save_status = "success"
                    event_logger.info(f"Successfully saved access event to Supabase")
                else:
                    save_status = "error"
                    error_type = result.get("error_type", "Unknown")
                    error_msg = result.get("error", "Unknown error")
                    response_text = result.get("response_text", "")
                    status_code = result.get("status_code", "")
                    event_logger.error(
                        f"Failed to save access event to Supabase: {error_type} - {error_msg}"
                        f"{f' (Status: {status_code})' if status_code else ''}"
                        f"{f'. Response: {response_text[:200]}' if response_text else ''}. "
                        f"Event data: {json.dumps(parsed, default=str)[:500]}"
                    )
            except Exception as exc:
                save_status = "error"
                event_logger.error(
                    f"Unexpected error saving access event to Supabase: {exc}. "
                    f"Event data: {json.dumps(parsed, default=str)[:500]}"
                )
    
    return {
        "parsed": parsed,
        "is_access_event": is_access,
        "save_status": save_status
    }

