"""Orchestration logic for syncing Angajati to Hikvision devices."""

from typing import Optional
from .models import SyncResult, SyncResultStatus
from .isapi_client import create_person_on_device, add_face_image_to_device


async def sync_angajat_to_device(
    angajat: dict,
    device: dict,
    supabase_url: Optional[str] = None
) -> SyncResult:
    """
    Sync one Angajat to one Hikvision device.
    
    Steps:
    1. Validate employee_no exists (skip if missing)
    2. Create/update Person on device
    3. Add face image if foto_fata_url exists
    
    Args:
        angajat: Angajat dict with biometrie data (must include employee_no)
        device: Device dict with ip_address, port, username, password_encrypted
        supabase_url: Optional Supabase URL for constructing full image URLs
    
    Returns:
        SyncResult with status:
        - SUCCESS: Person created and photo added (or photo skipped if no URL)
        - PARTIAL: Person created but photo failed
        - SKIPPED: Missing employee_no (non-fatal, continue)
        - FATAL: Device error (auth/network/timeout) - stop bulk sync
    """
    # Step 1: Validate employee_no exists
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    
    if not employee_no:
        return SyncResult(
            SyncResultStatus.SKIPPED,
            "Missing employee_no - cannot sync without employee number",
            "validation"
        )
    
    # Step 2: Create/update Person on device
    person_result = await create_person_on_device(device, angajat)
    
    # If person creation failed fatally, return immediately (stop sync)
    if person_result.status == SyncResultStatus.FATAL:
        return person_result
    
    # If person creation was skipped (shouldn't happen here, but handle it)
    if person_result.status == SyncResultStatus.SKIPPED:
        return person_result
    
    # Check if person already existed on device (skip photo if so)
    person_already_exists = "already exists" in person_result.message.lower() or "already exist" in person_result.message.lower()
    
    if person_already_exists:
        # Person already exists - skip photo addition
        return SyncResult(
            SyncResultStatus.SUCCESS,
            f"{person_result.message}. Photo step skipped (person already exists on device)",
            "person"
        )
    
    # Step 3: Add face image if foto_fata_url exists (only if person was newly created)
    foto_fata_url = biometrie.get("foto_fata_url")
    
    if not foto_fata_url:
        # No photo URL - this is still success (person was created)
        return SyncResult(
            SyncResultStatus.SUCCESS,
            f"Person created successfully. No photo URL available - photo step skipped",
            "person"
        )
    
    # Attempt to add face image (person was newly created)
    photo_result = await add_face_image_to_device(device, angajat, supabase_url)
    
    # Determine final status:
    # - If person succeeded and photo succeeded → SUCCESS
    # - If person succeeded but photo failed → PARTIAL
    # - Person failures already handled above (FATAL returned)
    
    if photo_result.status == SyncResultStatus.SUCCESS:
        return SyncResult(
            SyncResultStatus.SUCCESS,
            "Person and photo synced successfully",
            "complete"
        )
    else:
        # Photo failed but person succeeded → PARTIAL
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Person created successfully, but photo sync failed: {photo_result.message}",
            "photo"
        )


async def sync_photo_only_to_device(
    angajat: dict,
    device: dict,
    supabase_url: Optional[str] = None
) -> SyncResult:
    """
    Sync ONLY the face photo to one Hikvision device (skips person creation).
    
    This assumes the person already exists on the device.
    Use this when you only want to update/add the photo without touching person data.
    
    Steps:
    1. Validate employee_no exists
    2. Validate foto_fata_url exists
    3. Add face image to device
    
    Args:
        angajat: Angajat dict with biometrie data (must include employee_no and foto_fata_url)
        device: Device dict with ip_address, port, username, password_encrypted
        supabase_url: Optional Supabase URL for constructing full image URLs
    
    Returns:
        SyncResult with status:
        - SUCCESS: Photo added successfully
        - SKIPPED: Missing employee_no or foto_fata_url
        - PARTIAL: Photo sync failed (non-fatal)
        - FATAL: Device error (auth/network/timeout)
    """
    # Step 1: Validate employee_no exists
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    
    if not employee_no:
        return SyncResult(
            SyncResultStatus.SKIPPED,
            "Missing employee_no - cannot sync photo without employee number",
            "validation"
        )
    
    # Step 2: Validate foto_fata_url exists
    foto_fata_url = biometrie.get("foto_fata_url")
    
    if not foto_fata_url:
        return SyncResult(
            SyncResultStatus.SKIPPED,
            "Missing foto_fata_url - cannot sync photo without photo URL",
            "validation"
        )
    
    # Step 3: Add face image (skip person creation)
    photo_result = await add_face_image_to_device(device, angajat, supabase_url)
    
    # Return the photo result directly
    return photo_result

