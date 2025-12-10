# Implementation Plan: Delete User from HikVision Devices

## Overview
Add functionality to delete a user from HikVision devices using the ISAPI UserInfoDetail/Delete endpoint. This follows the same pattern as other user management features (add user, update photo).

## Requirements
- **ISAPI Endpoint**: `http://{device_ip}:{port}/ISAPI/AccessControl/UserInfoDetail/Delete?format=json`
- **HTTP Method**: POST (with JSON body)
- **Data Source**: Retrieve `employee_no` from Supabase user info (same as add user flow)
- **New API Endpoint**: Create endpoint callable from React app to delete user from all active devices

## Implementation Structure

Following the same structure as the existing user sync functionality (`sync-angajat-all-devices` endpoint).

---

## 1. ISAPI Client Layer (`hikvision_sync/isapi_client.py`)

### 1.1 Add `_build_delete_user_payload()` function
- **Purpose**: Build payload for DELETE request to remove user from device
- **Input**: `angajat` dict (must contain `biometrie.employee_no`)
- **Output**: JSON payload dict
- **Payload Structure**:
  ```json
  {
    "UserInfoDetail": {
      "mode": "byEmployeeNo",
      "EmployeeNoList": [
        {
          "employeeNo": "{employee_no}"  // From biometrie.employee_no (as string)
        }
      ],
      "operateType": "byTerminal",
      "terminalNoList": [1]  // Default to terminal 1
    }
  }
  ```
- **Notes**:
  - `employeeNo` must be a string (convert from int if needed)
  - `mode` is always `"byEmployeeNo"` (delete by employee number)
  - `operateType` is always `"byTerminal"` (operate on terminals)
  - `terminalNoList` defaults to `[1]` (can be made configurable later if needed)
  - Validate that `employee_no` exists before building payload

### 1.2 Add `delete_user_from_device()` function
- **Purpose**: Delete user from device via ISAPI DELETE endpoint
- **Signature**: `async def delete_user_from_device(device: dict, angajat: dict) -> SyncResult`
- **Flow**:
  1. Validate `employee_no` exists in `angajat.biometrie` → raise `ValueError` if missing
  2. Build payload using `_build_delete_user_payload()`
  3. Construct URL: `http://{ip}:{port}/ISAPI/AccessControl/UserInfoDetail/Delete?format=json`
  4. Make POST request with Digest Auth (despite endpoint name "Delete", ISAPI uses POST)
  5. Parse response:
     - Success: `statusCode: 1, statusString: "OK", subStatusCode: "ok"` → `SyncResult(SUCCESS, ...)`
     - User not found: Handle gracefully (may return different statusCode) → `SyncResult(SUCCESS, ...)` (idempotent - user already deleted)
     - Error: Parse error message → `SyncResult(FATAL, ...)` for auth/network errors, `SyncResult(PARTIAL, ...)` for other errors
  6. Handle exceptions (timeout, connection error) → `SyncResult(FATAL, ...)`
- **Error Handling**:
  - Auth failures (401) → `FATAL` (stop bulk sync)
  - Network/timeout errors → `FATAL` (stop bulk sync)
  - User not found → `SUCCESS` (idempotent operation)
  - Other ISAPI errors → `PARTIAL` (non-fatal, continue with other devices)
- **Similar to**: `create_person_on_device()` but for deletion

### 1.3 Add `_classify_delete_response()` helper function
- **Purpose**: Classify ISAPI delete response into SyncResult
- **Input**: `response` object from requests library
- **Output**: `SyncResult` with appropriate status
- **Response Classification**:
  - HTTP 401 → `FATAL` (authentication failed)
  - HTTP 200 with `statusCode: 1, subStatusCode: "ok"` → `SUCCESS`
  - HTTP 200 with other statusCode → Check if user not found (idempotent) → `SUCCESS`, otherwise → `PARTIAL`
  - HTTP non-200 → `FATAL` (unless it's a known non-fatal error)
- **Similar to**: `_classify_person_response()` function

---

## 2. Orchestration Layer (`hikvision_sync/orchestration.py`)

### 2.1 Add `delete_user_from_device()` function
- **Purpose**: Orchestrate user deletion from **a single device** (called once per device by the API endpoint)
- **Signature**: `async def delete_user_from_device(angajat: dict, device: dict) -> SyncResult`
- **Flow**:
  1. Validate `employee_no` exists → return `SKIPPED` if missing
  2. Call ISAPI `delete_user_from_device()` function
  3. Return result directly (no additional steps needed for deletion)
- **Returns**: `SyncResult` with appropriate status
- **Notes**:
  - Deletion is simpler than creation (no photo step)
  - Missing `employee_no` → `SKIPPED` (non-fatal, continue with other devices)
  - Device errors → `FATAL` or `PARTIAL` depending on error type
  - **This function handles ONE device** - the API endpoint calls it for each device in the list
- **Similar to**: `sync_angajat_to_device()` but simpler (single step)
  - **Note**: The API endpoint (`/api/hikvision/delete-user`) will iterate through all devices and call this function for each one, just like `sync-angajat-all-devices` calls `sync_angajat_to_device()` for each device

---

## 3. API Endpoint Layer (`main.py`)

### 3.1 Add `/api/hikvision/delete-user` endpoint
- **Method**: `POST`
- **Purpose**: Delete user from **ALL active devices** (same pattern as adding users to all devices)
- **Important**: This endpoint deletes the user from **every active device**, not just one device. It follows the exact same pattern as `/api/hikvision/sync-angajat-all-devices`.
- **Request Body**:
  ```json
  {
    "angajat_id": "<uuid>"  // Required: UUID of the angajat to delete
  }
  ```
- **Flow** (identical to `sync-angajat-all-devices` pattern):
  1. Validate `angajat_id` in request body
  2. Fetch angajat from Supabase via `get_angajat_with_biometrie()`
  3. Validate angajat exists → return error if not found
  4. Fetch **all active devices** via `get_active_devices()` (same as add user flow)
  5. **Iterate through ALL devices sequentially**:
     - For each device in the list:
       - Call `delete_user_from_device()` (orchestration function)
       - Record result per device
       - Add rate limit delay between devices (1 second, except after last device)
  6. Aggregate results and return summary with per-device breakdown
- **Response Format** (same structure as `sync-angajat-all-devices`):
  ```json
  {
    "status": "ok",
    "summary": {
      "success": 2,      // Number of devices where user was deleted successfully
      "partial": 0,      // Number of devices with partial success (non-fatal errors)
      "skipped": 1,      // Number of devices skipped (missing employee_no)
      "fatal": 0         // Number of devices with fatal errors (auth/network)
    },
    "per_device": [
      {
        "device_id": "...",
        "device_ip": "192.168.1.100",
        "status": "success",
        "message": "User deleted successfully",
        "step": "delete"
      },
      {
        "device_id": "...",
        "device_ip": "192.168.1.101",
        "status": "success",
        "message": "User deleted successfully",
        "step": "delete"
      },
      ...
    ]
  }
  ```
- **Similar to**: `/api/hikvision/sync-angajat-all-devices` endpoint (lines 514-633)
  - **Same pattern**: Fetch all devices → iterate through each → aggregate results
  - **Same rate limiting**: 1 second delay between devices
  - **Same error handling**: Continue with other devices even if one fails (unless fatal)
  - **Same response format**: Summary counts + per-device details
- **Error Handling**: 
  - Return structured error response if Supabase client not initialized
  - Return error if angajat not found
  - Return error if no active devices found
  - Always return 200 with status in payload (follow existing pattern)
  - **Continue deletion on other devices** even if one device fails (unless fatal auth/network error)

### 3.2 Add test endpoint `/api/test-isapi/delete-user` (Optional)
- **Method**: `POST`
- **Purpose**: Test deletion on a single device (for debugging)
- **Query Params**: 
  - `angajat_id`: Required UUID of angajat to delete
  - `device_id`: Optional device ID (uses first device if not provided)
- **Response**: Similar to other test endpoints, returns result for single device
- **Similar to**: `/api/test-isapi/create-person` endpoint (lines 239-290)

---

## 4. File Changes Summary

### Files to Modify:
1. **`hikvision_sync/isapi_client.py`**
   - Add `_build_delete_user_payload()` function
   - Add `_classify_delete_response()` helper function
   - Add `delete_user_from_device()` function
   - Export `delete_user_from_device` in module (add to `__init__.py` if needed)

2. **`hikvision_sync/orchestration.py`**
   - Add `delete_user_from_device()` function (orchestration wrapper)
   - Import `delete_user_from_device` from `isapi_client`

3. **`main.py`**
   - Add new endpoint `/api/hikvision/delete-user`
   - Import `delete_user_from_device` from `hikvision_sync.orchestration`
   - Optionally add test endpoint `/api/test-isapi/delete-user`

### Files NOT Modified:
- `hikvision_sync/models.py` - No new models needed (reuse `SyncResult`)
- `hikvision_sync/supabase_client.py` - No changes needed (reuse existing methods)

---

## 5. Implementation Details

### 5.1 DELETE Request Payload
- **mode**: Always `"byEmployeeNo"` (delete by employee number)
- **EmployeeNoList**: Array with single object containing `employeeNo` (string)
- **operateType**: Always `"byTerminal"` (operate on terminals)
- **terminalNoList**: Default to `[1]` (can be made configurable later)

### 5.2 Response Handling
- **Success Response**:
  ```json
  {
    "statusCode": 1,
    "statusString": "OK",
    "subStatusCode": "ok"
  }
  ```
- **User Not Found**: Should be handled gracefully (idempotent operation - user already deleted)
- **Error Response**: Parse `statusCode`, `statusString`, `subStatusCode` from JSON response
- **HTTP Method**: Despite endpoint name "Delete", ISAPI uses POST method with JSON body

### 5.3 Error Classification
- **SUCCESS**: User deleted successfully OR user not found (idempotent)
- **PARTIAL**: Non-fatal ISAPI error (continue with other devices)
- **SKIPPED**: Missing `employee_no` (non-fatal, continue)
- **FATAL**: Auth failure (401) or network/timeout errors (stop bulk sync)

### 5.4 Idempotency
- Deleting a user that doesn't exist should be treated as success (idempotent operation)
- This allows safe retries and bulk operations without errors

---

## 6. Testing Considerations

### Test Cases:
1. **Happy Path**: User exists → delete succeeds → return SUCCESS
2. **User Not Found**: User doesn't exist on device → return SUCCESS (idempotent)
3. **Missing employee_no**: Angajat missing `employee_no` → return SKIPPED
4. **Auth Failure**: Invalid credentials → return FATAL
5. **Network Error**: Device unreachable → return FATAL
6. **Multiple Devices**: Delete user from all active devices → aggregate results
7. **ISAPI Error**: Device returns error statusCode → return PARTIAL (non-fatal)

### Test Endpoints:
- `/api/test-isapi/delete-user` - Test deletion on single device
- `/api/hikvision/delete-user` - Production endpoint for all devices

---

## 7. Integration Points

### React App Integration:
- **Endpoint**: `POST /api/hikvision/delete-user`
- **Request**: `{ "angajat_id": "<uuid>" }`
- **Response**: Same format as `sync-angajat-all-devices` endpoint
- **Use Case**: When user is deleted/deactivated in React app, call this endpoint to **remove from ALL active devices**
- **Important**: This endpoint automatically handles deletion from all devices - no need to specify device IDs

### Supabase Integration:
- Fetch angajat using `get_angajat_with_biometrie()` (same as other endpoints)
- Extract `employee_no` from `biometrie.employee_no`
- No changes needed to Supabase client

---

## 8. Code Reuse

### Functions to Reuse:
- `get_angajat_with_biometrie()` → fetch angajat data
- `get_active_devices()` → fetch devices
- `rate_limit_delay()` → delay between devices
- `SyncResult` model → no changes needed
- Error handling patterns from `create_person_on_device()`

### Patterns to Follow:
- Same error classification logic as person creation
- Same rate limiting between devices (1 second delay)
- Same result aggregation format (summary + per_device array)
- Same API endpoint structure (iterate through all devices)
- **Key Pattern**: The API endpoint fetches ALL active devices and processes each one sequentially, exactly like `sync-angajat-all-devices` does

---

## 9. Implementation Order

1. **Step 1**: Implement `_build_delete_user_payload()` in `isapi_client.py`
2. **Step 2**: Implement `_classify_delete_response()` helper in `isapi_client.py`
3. **Step 3**: Implement `delete_user_from_device()` in `isapi_client.py`
4. **Step 4**: Implement `delete_user_from_device()` orchestration wrapper in `orchestration.py`
5. **Step 5**: Implement `/api/hikvision/delete-user` endpoint in `main.py`
   - **Important**: Follow the exact pattern from `sync-angajat-all-devices`:
     - Fetch all active devices
     - Iterate through each device sequentially
     - Call `delete_user_from_device()` for each device
     - Add rate limit delay between devices
     - Aggregate results (summary + per_device array)
6. **Step 6**: Optionally add `/api/test-isapi/delete-user` test endpoint (for single device testing)
7. **Step 7**: Test with single device, then **multiple devices** (verify deletion happens on all devices)
8. **Step 8**: Verify idempotency (delete non-existent user - should succeed on all devices)

---

## 10. Notes

- **All Devices Pattern**: The `/api/hikvision/delete-user` endpoint **automatically deletes from ALL active devices**, following the exact same pattern as `/api/hikvision/sync-angajat-all-devices`. The endpoint fetches all active devices and processes each one sequentially. This is intentional - when a user is deleted, they should be removed from all devices, not just one.
- **HTTP Method**: Despite endpoint name "Delete", ISAPI uses POST method (not HTTP DELETE)
- **Idempotency**: Deleting a non-existent user should be treated as success (safe to retry) - this applies to each device independently
- **Terminal List**: Default to `[1]` for now. If devices require different terminal numbers, make configurable later
- **Backward Compatibility**: No changes to existing endpoints
- **Consistency**: Follow same patterns as existing code (error handling, logging, response format)
- **Rate Limiting**: Use same `rate_limit_delay()` between devices as existing endpoints (1 second delay)
- **Error Handling**: Follow same classification as `create_person_on_device()`:
  - Auth failures → FATAL (stop bulk sync for that angajat)
  - Network/timeout → FATAL (stop bulk sync for that angajat)
  - Missing data → SKIPPED (continue with other devices)
  - Other errors → PARTIAL (continue with other devices)

---

## 11. Edge Cases

### User Already Deleted:
- If user doesn't exist on device, ISAPI may return different statusCode
- Treat as SUCCESS (idempotent operation)
- Log message indicating user was already deleted

### Missing employee_no:
- If `biometrie.employee_no` is missing, skip deletion
- Return SKIPPED status
- Continue with other devices/angajati

### Device Offline:
- Network errors/timeouts → FATAL status
- Stop bulk sync for that angajat
- Log error for debugging

### Partial Device Failure:
- If some devices succeed and others fail → **continue deletion on remaining devices**
- Aggregate results from all devices
- Return per-device status in response (one entry per device)
- Summary shows counts for each status type (success/partial/skipped/fatal)
- **Example**: If 3 devices exist and deletion succeeds on 2 but fails on 1, the response will show:
  - `summary.success = 2`
  - `summary.partial/fatal = 1` (depending on error type)
  - `per_device` array with 3 entries (one per device)

---

## 12. Security Considerations

- **Authentication**: Endpoint requires Supabase JWT (same as other endpoints)
- **Authorization**: Only authenticated users can delete users from devices
- **Validation**: Validate `angajat_id` exists before attempting deletion
- **Error Messages**: Don't expose sensitive device information in error messages

---

## 13. Future Enhancements (Out of Scope)

- **Selective Terminal Deletion**: Allow specifying which terminals to delete from
- **Bulk Deletion**: Delete multiple users in single request
- **Soft Delete**: Mark user as deleted in Supabase before removing from devices
- **Audit Logging**: Log deletion events to database for audit trail
- **Retry Logic**: Add retry mechanism for transient network errors

