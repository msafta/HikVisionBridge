"""Orchestration logic for syncing Angajati to Hikvision devices."""

from typing import Optional
from .models import SyncResult, SyncResultStatus
from .isapi_client import create_person_on_device, add_face_image_to_device, update_face_image_to_device, delete_user_from_device as delete_user_from_device_isapi


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


async def update_photo_to_device(
    angajat: dict,
    device: dict,
    supabase_url: Optional[str] = None
) -> SyncResult:
    """
    Update face photo on one Hikvision device with PUT fallback to POST.
    
    This attempts to update an existing face image using PUT request.
    If PUT fails, it falls back to POST (create) to ensure the photo is synced.
    Assumes the person already exists on the device.
    
    Steps:
    1. Validate employee_no exists
    2. Validate foto_fata_url exists
    3. Attempt PUT update via update_face_image_to_device()
    4. If PUT succeeds → return SUCCESS
    5. If PUT fails → fallback to POST via add_face_image_to_device()
    
    Args:
        angajat: Angajat dict with biometrie data (must include employee_no and foto_fata_url)
        device: Device dict with ip_address, port, username, password_encrypted
        supabase_url: Optional Supabase URL for constructing full image URLs
    
    Returns:
        SyncResult with status:
        - SUCCESS: Photo updated/created successfully (PUT succeeded OR POST fallback succeeded)
        - SKIPPED: Missing employee_no or foto_fata_url
        - PARTIAL: Both PUT and POST failed (non-fatal)
        - FATAL: Device error (auth/network/timeout) - should not occur but handle if needed
    """
    # Step 1: Validate employee_no exists
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    
    if not employee_no:
        return SyncResult(
            SyncResultStatus.SKIPPED,
            "Missing employee_no - cannot update photo without employee number",
            "validation"
        )
    
    # Step 2: Validate foto_fata_url exists
    foto_fata_url = biometrie.get("foto_fata_url")
    
    if not foto_fata_url:
        return SyncResult(
            SyncResultStatus.SKIPPED,
            "Missing foto_fata_url - cannot update photo without photo URL",
            "validation"
        )
    
    # Step 3: Attempt PUT update
    print(f"  [UPDATE_PHOTO] Attempting PUT update for employee_no={biometrie.get('employee_no')}")
    put_result = await update_face_image_to_device(device, angajat, supabase_url)
    print(f"  [UPDATE_PHOTO] PUT result: status={put_result.status.value}, message={put_result.message}")
    
    # Step 4: If PUT succeeded, return SUCCESS
    if put_result.status == SyncResultStatus.SUCCESS:
        return SyncResult(
            SyncResultStatus.SUCCESS,
            "Face image updated successfully via PUT",
            "photo"
        )
    
    # Step 5: PUT failed - fallback to POST (create)
    # Note: PUT failures return PARTIAL status, so we fallback to POST
    print(f"  [UPDATE_PHOTO] PUT failed, falling back to POST for employee_no={biometrie.get('employee_no')}")
    post_result = await add_face_image_to_device(device, angajat, supabase_url)
    print(f"  [UPDATE_PHOTO] POST result: status={post_result.status.value}, message={post_result.message}")
    
    # Return POST result (SUCCESS if fallback worked, PARTIAL if both failed)
    if post_result.status == SyncResultStatus.SUCCESS:
        return SyncResult(
            SyncResultStatus.SUCCESS,
            f"PUT update failed, but photo created successfully via POST fallback. PUT error: {put_result.message}",
            "photo"
        )
    else:
        # Both PUT and POST failed
        return SyncResult(
            SyncResultStatus.PARTIAL,
            f"Both PUT update and POST create failed. PUT error: {put_result.message}. POST error: {post_result.message}",
            "photo"
        )


async def delete_user_from_device(
    angajat: dict,
    device: dict
) -> SyncResult:
    """
    Delete user from one Hikvision device.
    
    This function orchestrates user deletion from a single device.
    The API endpoint will call this function for each device in the list.
    
    Steps:
    1. Validate employee_no exists (skip if missing)
    2. Call ISAPI delete_user_from_device() function
    
    Args:
        angajat: Angajat dict with biometrie data (must include employee_no)
        device: Device dict with ip_address, port, username, password_encrypted
    
    Returns:
        SyncResult with status:
        - SUCCESS: User deleted successfully (or user not found - idempotent)
        - SKIPPED: Missing employee_no (non-fatal, continue with other devices)
        - PARTIAL: Non-fatal ISAPI error (continue with other devices)
        - FATAL: Device error (auth/network/timeout) - stop bulk sync
    """
    # Step 1: Validate employee_no exists
    biometrie = angajat.get("biometrie", {})
    employee_no = biometrie.get("employee_no")
    
    if not employee_no:
        return SyncResult(
            SyncResultStatus.SKIPPED,
            "Missing employee_no - cannot delete user without employee number",
            "validation"
        )
    
    # Step 2: Call ISAPI delete_user_from_device() function
    # Note: The ISAPI function handles validation and will return SKIPPED if employee_no is missing,
    # but we check here first to avoid unnecessary API calls
    result = await delete_user_from_device_isapi(device, angajat)
    
    # Return result directly (no additional steps needed for deletion)
    return result

