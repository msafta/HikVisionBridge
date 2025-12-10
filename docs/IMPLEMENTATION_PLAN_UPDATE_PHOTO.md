# Implementation Plan: Update Person's Image on Device

## Overview
Add functionality to update a person's face image on HikVision devices using PUT request, with fallback to POST (create) if PUT fails.

## Requirements
- **PUT Endpoint**: `http://{device_ip}:{port}/ISAPI/Intelligent/FDLib/FDModify?format=json`
- **Fallback**: If PUT fails, attempt POST to create the image (using existing `add_face_image_to_device`)
- **Data Source**: Retrieve FPID (employee_no) and image URL from Supabase (same as POST flow)
- **New API Endpoint**: Create endpoint callable from React app

## Implementation Structure

Following the same structure as the existing photo sync functionality (`sync-angajat-photo-only` endpoint).

---

## 1. ISAPI Client Layer (`hikvision_sync/isapi_client.py`)

### 1.1 Add `_build_face_image_update_payload()` function
- **Purpose**: Build payload for PUT request to update face image
- **Input**: `angajat` dict, `supabase_url` (optional)
- **Output**: JSON payload dict
- **Payload Structure**:
  ```json
  {
    "faceLibType": "blackFD",
    "FDID": "1",
    "FPID": "{employee_no}",  // From biometrie.employee_no
    "faceID": "1",            // Default to "1"
    "faceURL": "{foto_fata_url}"  // From biometrie.foto_fata_url (full URL)
  }
  ```
- **Notes**:
  - Reuse URL construction logic from `_build_face_image_payload()` (handle relative URLs, HTTPS conversion)
  - `faceID` defaults to "1" (can be made configurable later if needed)
  - `FPID` must match `employee_no` from Supabase

### 1.2 Add `update_face_image_to_device()` function
- **Purpose**: Update face image on device via PUT request
- **Signature**: `async def update_face_image_to_device(device: dict, angajat: dict, supabase_url: Optional[str] = None) -> SyncResult`
- **Flow**:
  1. Build payload using `_build_face_image_update_payload()`
  2. Construct URL: `http://{ip}:{port}/ISAPI/Intelligent/FDLib/FDModify?format=json`
  3. Make PUT request with Digest Auth
  4. Parse response:
     - Success: `statusCode: 1, statusString: "OK", subStatusCode: "ok"` → `SyncResult(SUCCESS, ...)`
     - Error: Parse error message → `SyncResult(PARTIAL, ...)` (non-fatal, will fallback to POST)
  5. Handle exceptions (timeout, connection error) → `SyncResult(PARTIAL, ...)`
- **Error Handling**:
  - All errors return `PARTIAL` status (non-fatal) to allow fallback to POST
  - Log errors for debugging
- **Similar to**: `add_face_image_to_device()` but uses PUT instead of POST

---

## 2. Orchestration Layer (`hikvision_sync/orchestration.py`)

### 2.1 Add `update_photo_to_device()` function
- **Purpose**: Orchestrate photo update with PUT fallback to POST
- **Signature**: `async def update_photo_to_device(angajat: dict, device: dict, supabase_url: Optional[str] = None) -> SyncResult`
- **Flow**:
  1. Validate `employee_no` exists → return `SKIPPED` if missing
  2. Validate `foto_fata_url` exists → return `SKIPPED` if missing
  3. Attempt PUT update via `update_face_image_to_device()`
  4. If PUT succeeds → return `SUCCESS`
  5. If PUT fails (returns `PARTIAL` or other error):
     - Fallback to POST via `add_face_image_to_device()` (existing function)
     - Return result from POST attempt
- **Returns**: `SyncResult` with appropriate status
- **Similar to**: `sync_photo_only_to_device()` but adds PUT attempt before POST fallback

---

## 3. API Endpoint Layer (`main.py`)

### 3.1 Add `/api/hikvision/update-angajat-photo` endpoint
- **Method**: `POST`
- **Purpose**: Update face photo for one Angajat to all active devices
- **Request Body**:
  ```json
  {
    "angajat_id": "<uuid>"  // Required: UUID of the angajat to update photo for
  }
  ```
- **Flow**:
  1. Validate `angajat_id` in request body
  2. Fetch angajat from Supabase via `get_angajat_with_biometrie()`
  3. Fetch all active devices via `get_active_devices()`
  4. For each device:
     - Call `update_photo_to_device()` (orchestration function)
     - Record result per device
     - Add rate limit delay between devices
  5. Aggregate results and return summary
- **Response Format**:
  ```json
  {
    "status": "ok",
    "summary": {
      "success": 2,      // Number of devices updated successfully
      "partial": 0,      // Number of devices with partial success (fallback to POST succeeded)
      "skipped": 1,      // Number of devices skipped (missing employee_no or foto_fata_url)
      "fatal": 0         // Number of devices with fatal errors
    },
    "per_device": [
      {
        "device_id": "...",
        "device_ip": "192.168.1.100",
        "status": "success",
        "message": "Face image updated successfully",
        "step": "photo"
      },
      ...
    ]
  }
  ```
- **Similar to**: `/api/hikvision/sync-angajat-photo-only` endpoint (lines 674-792)
- **Error Handling**: Return structured error response if Supabase client not initialized or angajat not found

---

## 4. File Changes Summary

### Files to Modify:
1. **`hikvision_sync/isapi_client.py`**
   - Add `_build_face_image_update_payload()` function
   - Add `update_face_image_to_device()` function
   - Export `update_face_image_to_device` in module

2. **`hikvision_sync/orchestration.py`**
   - Add `update_photo_to_device()` function
   - Import `update_face_image_to_device` from `isapi_client`

3. **`main.py`**
   - Add new endpoint `/api/hikvision/update-angajat-photo`
   - Import `update_photo_to_device` from `hikvision_sync.orchestration`

### Files NOT Modified:
- `hikvision_sync/models.py` - No new models needed (reuse `SyncResult`)
- `hikvision_sync/supabase_client.py` - No changes needed (reuse existing methods)

---

## 5. Implementation Details

### 5.1 PUT Request Payload
- **faceLibType**: Always `"blackFD"` (matches existing POST)
- **FDID**: Always `"1"` (matches existing POST)
- **FPID**: From `biometrie.employee_no` (same as POST)
- **faceID**: Default to `"1"` (new field for PUT)
- **faceURL**: From `biometrie.foto_fata_url` (same as POST, with URL construction)

### 5.2 Response Handling
- **Success Response**:
  ```json
  {
    "statusCode": 1,
    "statusString": "OK",
    "subStatusCode": "ok"
  }
  ```
- **Error Response**: Parse `statusCode`, `statusString`, `subStatusCode` from JSON response
- **Fallback Logic**: If PUT returns non-success, call existing `add_face_image_to_device()` function

### 5.3 Error Classification
- **SUCCESS**: PUT succeeded OR POST fallback succeeded
- **PARTIAL**: PUT failed but POST fallback succeeded (still counts as success for user)
- **SKIPPED**: Missing `employee_no` or `foto_fata_url`
- **FATAL**: Should not occur (both PUT and POST return PARTIAL for errors), but handle if needed

---

## 6. Testing Considerations

### Test Cases:
1. **Happy Path**: PUT succeeds → return SUCCESS
2. **PUT Fails, POST Succeeds**: PUT returns error → fallback to POST → return SUCCESS
3. **Both Fail**: PUT fails → POST fails → return PARTIAL
4. **Missing Data**: Missing `employee_no` or `foto_fata_url` → return SKIPPED
5. **Multiple Devices**: Update photo to all active devices → aggregate results

### Test Endpoints (Optional):
- Consider adding `/api/test-isapi/update-face-image` similar to existing test endpoints
- Useful for debugging PUT/POST fallback behavior

---

## 7. Integration Points

### React App Integration:
- **Endpoint**: `POST /api/hikvision/update-angajat-photo`
- **Request**: `{ "angajat_id": "<uuid>" }`
- **Response**: Same format as `sync-angajat-photo-only` endpoint
- **Use Case**: When user updates photo in React app, call this endpoint to sync to devices

---

## 8. Code Reuse

### Functions to Reuse:
- `_build_face_image_payload()` logic (URL construction) → extract to helper or reuse
- `add_face_image_to_device()` → called as fallback
- `get_angajat_with_biometrie()` → fetch angajat data
- `get_active_devices()` → fetch devices
- `rate_limit_delay()` → delay between devices
- `SyncResult` model → no changes needed

---

## 9. Implementation Order

1. **Step 1**: Implement `_build_face_image_update_payload()` in `isapi_client.py`
2. **Step 2**: Implement `update_face_image_to_device()` in `isapi_client.py`
3. **Step 3**: Implement `update_photo_to_device()` in `orchestration.py`
4. **Step 4**: Implement `/api/hikvision/update-angajat-photo` endpoint in `main.py`
5. **Step 5**: Test with single device, then multiple devices
6. **Step 6**: Verify PUT fallback to POST works correctly

---

## 10. Notes

- **faceID Field**: Default to "1" for now. If devices require different faceID values, we can make it configurable later.
- **Backward Compatibility**: Existing POST flow (`add_face_image_to_device`) remains unchanged.
- **Consistency**: Follow same patterns as existing code (error handling, logging, response format).
- **Rate Limiting**: Use same `rate_limit_delay()` between devices as existing endpoints.

