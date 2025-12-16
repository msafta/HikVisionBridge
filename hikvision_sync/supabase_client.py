"""Supabase client helpers for fetching data via Edge Function."""

import json
import logging
import os
import httpx
from typing import List, Optional


class SupabaseClient:
    """Client for Supabase Edge Function API."""
    
    def __init__(self, supabase_url: str, api_key: str):
        self.edge_function_url = f"{supabase_url}/functions/v1/external-api-proxy"
        self.api_key = api_key
    
    def _get_headers(self) -> dict:
        """Get headers for Edge Function calls."""
        return {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        }
    
    async def get_active_devices(self) -> List[dict]:
        """
        Fetch all active devices from dispozitive_pontaj table via Edge Function.
        Returns list of devices with: id, ip_address, port, username, password_encrypted, santier_id
        """
        headers = self._get_headers()
        url = f"{self.edge_function_url}?action=get-active-devices"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("data", [])
    
    async def get_angajat_with_biometrie(self, angajat_id: str) -> Optional[dict]:
        """
        Fetch a single angajat with biometrie data via Edge Function.
        Returns dict with: id, nume, prenume, nume_complet, status, biometrie (with employee_no, foto_fata_url)
        """
        headers = self._get_headers()
        url = f"{self.edge_function_url}?action=get-angajat&angajat_id={angajat_id}"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers,
                timeout=30.0,  # Increased timeout for Edge Function calls
            )
            response.raise_for_status()
            result = response.json()
            data = result.get("data")
            return data if data else None
    
    async def get_all_active_angajati_with_biometrie(self) -> List[dict]:
        """
        Fetch all active angajati with biometrie data via Edge Function.
        Returns list of angajati with same structure as get_angajat_with_biometrie.
        Filters to status='activ' and includes only those with biometrie records.
        """
        headers = self._get_headers()
        url = f"{self.edge_function_url}?action=get-angajati-biometrie"
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers=headers,
                timeout=30.0,  # Increased timeout for Edge Function calls
            )
            response.raise_for_status()
            result = response.json()
            return result.get("data", [])
    
    async def save_pontaj_event(self, angajat_id: str, dispozitiv_id: str, event_time: str) -> dict:
        """
        Save a pontaj event via Edge Function.
        Args:
            angajat_id: UUID of the employee
            dispozitiv_id: UUID of the device
            event_time: ISO 8601 timestamp (e.g., "2025-01-01T08:00:00Z")
        Returns:
            dict with the saved event data
        """
        headers = self._get_headers()
        url = f"{self.edge_function_url}?action=save-pontaj-event"
        payload = {
            "angajat_id": angajat_id,
            "dispozitiv_id": dispozitiv_id,
            "event_time": event_time,
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            response.raise_for_status()
            result = response.json()
            return result.get("data", result)
    
    async def save_access_event(self, event_data: dict) -> dict:
        """
        Save access event to Supabase via Edge Function.
        
        Args:
            event_data: Complete event JSON dict (as parsed from device request)
        
        Returns:
            dict with response data from Edge Function, or error information if failed
        
        Note:
            This method uses a different Edge Function endpoint than other methods.
            Errors are caught and returned as dict (non-blocking) rather than raised.
        """
        # Load endpoint URL and API key from environment variables
        endpoint_url = os.getenv(
            "SUPABASE_EVENT_FUNCTION_URL",
            ""
        )
        api_key = os.getenv(
            "SUPABASE_EVENT_FUNCTION_API_KEY",
            ""
        )
        
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        
        # Console output for testing
        api_key_masked = f"{api_key[:10]}...{api_key[-4:]}" if len(api_key) > 14 else "***"
        print(f"INFO:     [SUPABASE EVENT CALL] POST {endpoint_url}")
        print(f"INFO:     [SUPABASE EVENT CALL] Event data: {json.dumps(event_data, default=str)[:200]}")
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint_url,
                    headers=headers,
                    json=event_data,
                    timeout=10.0,
                )
                print(f"INFO:     [SUPABASE EVENT CALL] Response: {response.status_code} {response.reason_phrase}")
                print(f"INFO:     [SUPABASE EVENT CALL] Response body: {response.text[:200]}")
                response.raise_for_status()
                result = response.json()
                print(f"INFO:     [SUPABASE EVENT CALL] Success - Event saved to database")
                return {"status": "success", "data": result}
        except httpx.HTTPStatusError as exc:
            # HTTP error (4xx, 5xx)
            print(f"ERROR:    [SUPABASE EVENT CALL] HTTP Error {exc.response.status_code}: {exc}")
            print(f"ERROR:    [SUPABASE EVENT CALL] Response: {exc.response.text[:200] if exc.response else 'No response'}")
            return {
                "status": "error",
                "error_type": "HTTPStatusError",
                "status_code": exc.response.status_code,
                "error": str(exc),
                "response_text": exc.response.text if exc.response else None,
            }
        except httpx.RequestError as exc:
            # Network error (timeout, connection error, etc.)
            print(f"ERROR:    [SUPABASE EVENT CALL] Request Error: {exc}")
            return {
                "status": "error",
                "error_type": "RequestError",
                "error": str(exc),
            }
        except Exception as exc:
            # Any other unexpected error
            print(f"ERROR:    [SUPABASE EVENT CALL] Unexpected Error: {exc}")
            return {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

