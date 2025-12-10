"""Supabase client helpers for fetching data via Edge Function."""

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
        # Hardcoded endpoint URL and API key for pontaj event saving
        endpoint_url = "https://xnjbjmbjyyuqjcflzigg.supabase.co/functions/v1/pontaj-adauga-eveniment"
        api_key = "sk_pontaj_prod_LsoJQrkbi40ov3dWhLqDHKZeOkAK4MBX"
        
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint_url,
                    headers=headers,
                    json=event_data,
                    timeout=10.0,
                )
                response.raise_for_status()
                result = response.json()
                return {"status": "success", "data": result}
        except httpx.HTTPStatusError as exc:
            # HTTP error (4xx, 5xx)
            return {
                "status": "error",
                "error_type": "HTTPStatusError",
                "status_code": exc.response.status_code,
                "error": str(exc),
                "response_text": exc.response.text if exc.response else None,
            }
        except httpx.RequestError as exc:
            # Network error (timeout, connection error, etc.)
            return {
                "status": "error",
                "error_type": "RequestError",
                "error": str(exc),
            }
        except Exception as exc:
            # Any other unexpected error
            return {
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

