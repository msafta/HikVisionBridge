# Angajati to Device Synchronization

## Overview

This document outlines the implementation plan for synchronizing Angajati (employees) data from the Asphalt OS database to Hikvision devices. The synchronization process involves creating "Person" records on each device and associating face images with those persons.

## Context

- **Angajat** in the database corresponds to **Person** on the Hikvision device
- Each device maintains its own user database
- Synchronization must be performed for each device individually
- Two ISAPI calls are required per Angajat per device:
  1. Create/Update Person record
  2. Add Face Image to Person

## Architecture Decision: FastAPI Server on VPN

**Important:** 
- Devices are located behind a VPN and are not accessible from Supabase Edge Functions (cloud infrastructure)
- The application will be hosted separately from the API server
- The FastAPI server will be on the same VPN as the devices (PROD via VPN, DEV on LAN)

**Solution:** Sync operations will be performed via **FastAPI endpoints** on the server that has VPN/LAN reachability to the devices.

**Architecture:**
```
┌─────────────┐
│   Browser   │ (React Frontend)
│   (User)    │
└──────┬──────┘
       │ HTTP Request
       ▼
┌─────────────┐
│  FastAPI    │ (Python server on VPN/LAN)
│  Server     │
└──────┬──────┘
       │ ISAPI Calls
       ▼
┌─────────────┐
│  Hikvision  │
│  Devices    │
│  (VPN/LAN)  │
└─────────────┘
```

**Rationale:**
- Edge Functions run on Supabase's cloud servers and cannot reach devices on private VPNs
- FastAPI server is on the VPN/LAN and can reach devices directly
- Device credentials stay on the server (not exposed to browser)
- Single source of truth for sync operations and logging
- Works for DEV (LAN) and PROD (VPN) with the same codepath

**Security Considerations:**
- Device credentials stay on the FastAPI server (never exposed to browser)
- FastAPI endpoints should authenticate requests (e.g., Supabase JWT validation)
- Network-level security: server must be on VPN/LAN to access devices
- Consider role-based access control for sync operations
- Credentials are stored in Supabase and fetched by the server

**Implementation:**
- Extend the existing FastAPI listener to add sync endpoints
- Use `requests` + `HTTPDigestAuth` or `httpx` + digest auth for ISAPI calls
- FastAPI fetches device credentials and employee data from Supabase (service role key)
- FastAPI makes ISAPI calls directly to devices and writes results back to Supabase
- React frontend calls FastAPI endpoints (instead of Edge Functions) and shows results

**Alternative Approaches Considered:**
1. **Edge Functions** - Not feasible (Supabase Edge Functions cannot connect to customer VPNs)
2. **Client-side sync** - Less secure (credentials in browser), harder error handling
3. **Local proxy service** - Equivalent to using the FastAPI server already available

## Database Schema

### Source Tables

#### `angajati` Table
- `id` (UUID) - Primary key, used as `employeeNo` on device
- `nume` (text) - Last name
- `prenume` (text) - First name
- `nume_complet` (text) - Full name (fallback)
- `status` (text) - Employee status ('activ', 'suspendat', 'concediu', etc.)

#### `angajati_biometrie` Table
- `id` (UUID) - Primary key
- `angajat_id` (UUID) - Foreign key to `angajati.id`
- `foto_fata_url` (text) - URL of the face photo
- `hikvision_employee_no` (text) - Optional Hikvision employee number
- `employee_no` (INTEGER) - **NEW:** Numeric employee number for device synchronization (unique, auto-assigned)

#### `dispozitive_pontaj` Table (Devices)
- `id` (UUID) - Primary key
- `ip_address` (text) - Device IP address
- `port` (integer) - Device port (default: 80)
- `username` (text) - Device username for authentication
- `password_encrypted` (text) - Device password for authentication
- `status` (text) - Device status ('activ', 'inactiv', 'eroare')
- `santier_id` (UUID) - Associated construction site

## ISAPI Calls

### 1. Create/Update Person Record

**Endpoint:**
```
POST http://{device_ip}:{port}/ISAPI/AccessControl/UserInfo/Record?format=json
```

**Authentication:**
- Type: **Digest Authentication**
- Username: From `dispozitive_pontaj.username`
- Password: From `dispozitive_pontaj.password_encrypted` (decrypted)

**Request Headers:**
```
Content-Type: application/json
```

**Request Body:**
```json
{
  "UserInfo": {
    "employeeNo": "10",
    "name": "Andreea Test",
    "userType": "normal",
    "Valid": {
      "enable": true,
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
```

**Field Mapping:**
- `employeeNo`: **Numeric employee number** from `angajati_biometrie.employee_no` (see Employee Number Assignment section)
- `name`: **Nume + Prenume** from `angajati` table (formatted as "Nume Prenume")
- `userType`: Always `"normal"`
- `Valid.enable`: `true` for active employees, `false` for inactive
- `Valid.beginTime`: Start date (e.g., `"2025-10-10T00:00:00"`)
- `Valid.endTime`: End date (e.g., `"2037-12-31T23:59:59"`)
- `doorRight`: `"1"` (access granted)
- `RightPlan`: Array with door access plan (default: door 1, template 1)

**Response Handling:**

The ISAPI endpoint returns different responses based on the operation result:

1. **Success Response:**
   ```json
   {
     "statusCode": 1,
     "statusString": "OK",
     "subStatusCode": "ok"
   }
   ```
   - HTTP 200 with JSON success message
   - Person created/updated successfully
   - Continue with next step (add face image)

2. **User Already Exists Response:**
   ```json
   {
     "statusCode": 6,
     "statusString": "Invalid Content",
     "subStatusCode": "employeeNoAlreadyExist",
     "errorCode": 1610637344,
     "errorMsg": "checkUser"
   }
   ```
   - **Handling:** Treat as success - user already exists on device
   - **Action:** Skip Person creation, continue with next step (add face image if needed)
   - **Note:** No error should be displayed, sync should continue normally
   - **Database:** Mark as "sincronizat" in sync tracking

3. **Authentication Error Response:**
   - HTTP 401 with XML response:
   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <userCheck version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
     <statusValue>401</statusValue>
     <statusString>Unauthorized</statusString>
     <isActivated>true</isActivated>
     <lockStatus>unlock</lockStatus>
     <unlockTime>0</unlockTime>
     <retryLoginTime>4</retryLoginTime>
   </userCheck>
   ```
   - **Handling:** Treat as fatal error
   - **Action:** Stop sync, display error, return

4. **Other Error Responses:**
   - Examples: Request timeout, network errors, device unreachable
   - **Handling:** Treat as fatal error
   - **Action:** 
     - Display error message to user
     - **Stop syncing remaining Angajati** (they will likely fail with same error)
     - Log error details for troubleshooting
     - Return error status

**Response Processing Logic:**
```
IF response.statusCode == 6 AND response.subStatusCode == "employeeNoAlreadyExist":
    → User exists, treat as success, continue
ELSE IF response.statusCode == 1 AND response.subStatusCode == "ok":
    → Success, continue
ELSE IF HTTP status == 401:
    → Authentication error, fatal, stop sync
ELSE:
    → Fatal error, stop sync, display error
```

**Notes:**
- The `employeeNo` must be a **number** (not a string)
- The `employeeNo` must be unique per device
- Date format: ISO 8601 (`YYYY-MM-DDTHH:mm:ss`)
- If user already exists, no update is performed (device keeps existing data)
- Fatal errors (timeout, auth failure) indicate device/network issues that will affect all subsequent syncs

---

### 2. Add Face Image to Person

**Endpoint:**
```
POST http://{device_ip}:{port}/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json
```

**Authentication:**
- Type: **Digest Authentication**
- Username: From `dispozitive_pontaj.username`
- Password: From `dispozitive_pontaj.password_encrypted` (decrypted)

**Request Headers:**
```
Content-Type: application/json
```

**Request Body:**
```json
{
  "faceLibType": "blackFD",
  "FDID": "1",
  "FPID": "10",
  "faceURL": "http://192.168.1.232:8080/zzz.jpg"
}
```

**Field Mapping:**
- `faceLibType`: Always `"blackFD"` (blacklist face database)
- `FDID`: Face Database ID (default: `"1"`)
- `FPID`: Face Person ID - **Numeric employee number** (same as `employeeNo` from Person creation)
- `faceURL`: **URL of the image** from `angajati_biometrie.foto_fata_url`

**Response:**
- Success: HTTP 200 with JSON response
- Error: HTTP 4xx/5xx with error details

**Error Handling:**
- If face image addition fails but Person creation succeeded:
  - **Action:** Continue and mark as partial success
  - Continue with next employee
  - Log error for this specific employee/device combination
  - Include in error summary at end
- **UI Consideration:** May need separate "Sync Photo" button in Biometrie tab for retrying failed photo syncs

**Notes:**
- `FPID` must match the `employeeNo` used in Person creation
- `faceURL` must be accessible from the device's network (use public Supabase Storage URLs)
- If face already exists for this `FPID`, it may be updated or an error may occur (device-dependent)

---

## Synchronization Flow

### High-Level Process

1. **Query Active Angajati**
   - Fetch all `angajati` with `status = 'activ'`
   - Join with `angajati_biometrie` to get face photo URLs
   - Filter employees that should be synced to devices

2. **Get All Active Devices**
   - Fetch all devices from `dispozitive_pontaj` with `status = 'activ'`
   - Extract device IP, port, username, password for each device

3. **Processing Order:**
   - Process Employee 1 to all devices, then Employee 2 to all devices, etc.
   - For each employee, sync to all devices sequentially

4. **For Each Angajat:**
   - **Step 0:** Verify `employee_no` is assigned
     - Check if `angajati_biometrie.employee_no` exists
     - For existing employees: Should already be manually assigned
     - For new employees: Trigger automatically assigns on insert
     - If NULL, skip sync and log error (employee number required)
   - **Step 1:** Create/Update Person on device
     - Use `employee_no` as numeric `employeeNo`
     - Use "Nume Prenume" as `name`
     - Set validity dates based on employee status
     - **Response Handling:**
       - If `statusCode: 6` and `subStatusCode: "employeeNoAlreadyExist"` → Treat as success, continue
       - If HTTP 200 success → Continue
       - If fatal error (timeout, auth failure, network error) → **STOP SYNC**, display error, return
   - **Step 2:** Add Face Image (if available)
     - Use same `employee_no` as numeric `FPID`
     - Use `foto_fata_url` as `faceURL`
     - Ensure URL is accessible from device network
     - If Person creation was skipped (already exists), still attempt to add/update face image

4. **Error Handling:**
   - **Fatal Errors (Stop Sync):**
     - Request timeout, invalid credentials, network errors
     - Display error message immediately
     - Stop processing remaining Angajati
     - Return error status
   - **Non-Fatal Errors (Continue):**
     - Missing employee number → Skip silently, continue with next employee
     - Missing photo → Skip face image step, continue
     - Face image sync failure → Mark as partial success, continue with next employee
     - Log failures per device/employee combination
   - Track sync status in database

5. **Status Tracking:**
   - Update sync status per device
   - Record last sync timestamp
   - Log errors for troubleshooting
   - Include summary: X successful, Y skipped (already exists), Z failed

## Implementation Considerations

### Supabase Integration (FastAPI)

- Env vars (FastAPI):
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_ROLE_KEY` (server-side only, never to clients)
  - (Optional for JWT validation) JWKS/JWT secret from Supabase to validate client tokens if you verify JWTs server-side.
- Tables used:
  - `angajati` (filter `status = 'activ'`)
  - `angajati_biometrie` (`employee_no`, `foto_fata_url`)
  - `dispozitive_pontaj` (`ip_address`, `port`, `username`, `password_encrypted`, `status = 'activ'`)
  - `angajati_biometrie_dispozitive` (sync tracking per device/employee)
- Storage:
  - Public bucket: `pontaj-photos`
  - Face URL pattern: `https://<project>.supabase.co/storage/v1/object/public/pontaj-photos/<file>.jpg`
  - Devices must be able to reach this URL (internet/VPN). If not, provide a LAN-accessible URL for dev.
- Access from FastAPI:
  - Use `supabase-py` or REST via `httpx` with `Authorization: Bearer <SUPABASE_SERVICE_ROLE_KEY>`

### Digest Authentication (Python/FastAPI)

Digest Authentication requires:
1. Initial request without credentials
2. Server responds with 401 and `WWW-Authenticate` header
3. Client calculates digest response using:
   - Username / Password
   - Realm / Nonce (from server)
   - Request method / Request URI
   - Request body hash (if applicable)
4. Resend request with `Authorization` header containing digest

**Implementation Note (Python):**
- Use `requests` + `HTTPDigestAuth` (sync) or `httpx` with digest auth (async).
- Set timeouts (e.g., 10–15s) and handle fatal errors (timeout/auth/network).
- Keep 1s rate limit between device calls.

### Employee Number Assignment (Option 1: Sequential Auto-Increment)

**Decision:** Use sequential auto-increment numeric assignment per Angajat.

The `employeeNo` field on the device **must be a number**. Since Angajat IDs are UUIDs, we need a stable numeric mapping.

#### Implementation Approach

**Database Schema Changes:**

1. Add `employee_no` column to `angajati_biometrie` table:
   ```sql
   ALTER TABLE angajati_biometrie 
   ADD COLUMN employee_no INTEGER UNIQUE;
   ```

2. Create a sequence for auto-incrementing employee numbers (starting from 1000):
   ```sql
   CREATE SEQUENCE angajat_employee_no_seq START 1000;
   ```

3. Create a function to auto-assign employee numbers:
   ```sql
   CREATE OR REPLACE FUNCTION assign_employee_no()
   RETURNS TRIGGER AS $$
   BEGIN
     IF NEW.employee_no IS NULL THEN
       NEW.employee_no := nextval('angajat_employee_no_seq');
     END IF;
     RETURN NEW;
   END;
   $$ LANGUAGE plpgsql;
   ```

4. Create trigger to auto-assign on insert (for NEW records only):
   ```sql
   CREATE TRIGGER assign_employee_no_trigger
   BEFORE INSERT ON angajati_biometrie
   FOR EACH ROW
   EXECUTE FUNCTION assign_employee_no();
   ```

5. **Manual assignment for existing records:**
   - Existing employees in `angajati_biometrie` will have `employee_no` manually assigned
   - Numbers should start from 1000 and increment sequentially
   - After manual assignment is complete, update the sequence to continue from the highest assigned number:
     ```sql
     -- Example: If highest manually assigned number is 1050, set sequence to start from 1051
     SELECT setval('angajat_employee_no_seq', (SELECT MAX(employee_no) FROM angajati_biometrie));
     ```
   - This ensures new records continue from where manual assignments ended

#### Usage in Sync Process

1. **Before syncing to device:**
   - For **existing employees:** Ensure `employee_no` has been manually assigned (should already be set)
   - For **new employees:** Trigger automatically assigns `employee_no` on insert
   - If `employee_no` is NULL, sync should fail with error (employee number required)

2. **When creating Person on device:**
   - Use `employee_no` as numeric value: `employeeNo: biometrie.employee_no`
   - Store in `hikvision_employee_no` for backward compatibility (as string): `hikvision_employee_no: biometrie.employee_no.toString()`

3. **When processing events from devices:**
   - Events arrive with numeric `employeeNo`
   - Lookup Angajat by: `SELECT * FROM angajati_biometrie WHERE employee_no = {event_employeeNo}`
   - This provides fast reverse lookup for event processing

#### Benefits

- ✅ **Stable mapping:** Same Angajat always gets same number
- ✅ **Human-readable:** Sequential numbers are easy to understand and debug
- ✅ **Fast lookup:** Indexed integer column enables efficient event-to-employee mapping
- ✅ **Unique constraint:** Database enforces uniqueness automatically
- ✅ **Backward compatible:** Can still use `hikvision_employee_no` for existing code

#### Considerations

- **Number range:** Starting from 1000 avoids conflicts with system numbers (0-999)
- **Uniqueness:** Database UNIQUE constraint ensures no duplicates
- **Persistence:** Once assigned, number never changes for that Angajat
- **Manual assignment:** Existing employees require manual assignment of `employee_no` starting from 1000
- **Sequence alignment:** After manual assignment, sequence must be updated to continue from highest assigned number
- **New records:** Automatically assigned via trigger when new `angajati_biometrie` records are created
- **Event lookup:** When events arrive with numeric `employeeNo`, simple WHERE clause lookup

### Name Format

- Primary: `nume + " " + prenume` (e.g., "Popescu Ion")
- Fallback: Use `nume_complet` if `nume` or `prenume` is null

### Image URL Accessibility

The `faceURL` must be accessible from the device's network.

**Implementation:**
- Use public Supabase Storage URLs directly
- Format: `https://{project-ref}.supabase.co/storage/v1/object/public/pontaj-photos/{filename}`
- Example: `https://xnjbjmbjyyuqjcflzigg.supabase.co/storage/v1/object/public/pontaj-photos/81c2897e-9eff-4ab9-aeca-549522f5631d-1765009445688.jpg`
- No signed URLs needed - public bucket URLs are sufficient
- URLs are accessible from device network as long as device has internet access

### Validity Dates

- `beginTime`: Use example date format: `"2025-10-10T00:00:00"` (dates not critical)
- `endTime`: Use far future date: `"2037-12-31T23:59:59"`
- Dates are not critical for functionality - use standard values as shown in examples

### Error Handling Strategy

1. **User Already Exists (employeeNoAlreadyExist):**
   - **Status:** Not an error - treat as success
   - **Action:** Continue sync process (skip Person creation, proceed to face image)
   - **No error displayed to user**

2. **Fatal Errors (Stop Sync):**
   - **Request Timeout:** Device not responding
   - **Invalid Credentials:** Authentication failure (401/403)
   - **Network Errors:** Device unreachable, connection refused
   - **Action:** 
     - Display error message to user
     - **Stop syncing remaining Angajati** (they will fail with same error)
     - Log error details
     - Return error status immediately

3. **Non-Fatal Errors (Continue Sync):**
   - **Missing Required Fields:** Employee missing name or photo
   - **Invalid Data Format:** Data validation errors
   - **Action:** 
     - Log error for specific employee
     - Continue with next employee
     - Include in error summary at end

4. **Device-Specific Errors:**
   - **Device Storage Full:** Log error, skip this device, continue with other devices
   - **Device API Version Incompatible:** Log error, skip this device, continue with other devices
   - **Device Offline:** Treat as fatal error if syncing to single device, otherwise skip and continue with other devices

### Performance Considerations

1. **Rate Limiting:**
   - Add **1 second delay** between API calls to avoid overwhelming devices
   - Global configuration (same delay for all devices)
   - Can be adjusted later based on testing

2. **Batch Processing:**
   - Process employees in batches
   - Allow partial success (some employees succeed, others fail)

3. **Parallel Processing:**
   - Process multiple devices in parallel (if supported)
   - Process employees sequentially per device (to avoid conflicts)

4. **Incremental Sync:**
   - Track which employees are already synced
   - Only sync new/changed employees
   - Support full resync option

## Database Tracking

### Sync Status Table

Track sync status in `angajati_biometrie_dispozitive` table:
- `angajat_biometrie_id` - Reference to employee biometric record
- `dispozitiv_id` - Reference to device
- `status_sincronizare` - Status: 'pending', 'sincronizat', 'eroare'
- `sincronizat_la` - Timestamp of last successful sync
- `eroare_detalii` - Error message if sync failed

**Update Logic:**
- Update **only on success/failure** (not on "already exists" responses)
- Treat "already exists" (`employeeNoAlreadyExist`) as success → mark as 'sincronizat'
- Do not update for skipped employees (missing employee_no or photo)

### Device Sync Tracking

Update `dispozitive_pontaj` table:
- `ultima_sincronizare` - Last sync timestamp
- `status` - Update if sync fails repeatedly

## UI Integration Points

### Sync Button Locations

There will be two ways to trigger synchronization from the UI:

#### 1. Sync All Angajati to All Devices

**Location:** Angajati page (`src/pages/Angajati.tsx`)

**Button Placement:**
- Located in the header section of the Angajati page
- Positioned **left of the "Template Excel" button**
- Should be visible alongside other action buttons (Template Excel, Import Excel, etc.)

**Button Details:**
- Button label: "Sincronizează toți Angajații"
- Icon: Sync/Refresh icon (suggested)
- Action: Triggers sync of all active Angajati to all active devices
- Should show loading state during sync
- Should display success/error notifications

**Implementation:**
- Calls FastAPI endpoint: `POST /api/hikvision/sync-all-to-all-devices`
- Should filter to only sync active employees (`status = 'activ'`)
- Should only sync to active devices (`status = 'activ'`)
- Should verify `employee_no` is assigned before syncing

---

#### 2. Sync Single Employee to All Devices

**Location:** Angajat Edit Dialog - Biometrie Tab (`src/pages/Angajati.tsx`)

**Button Placement:**
- Located in the **Biometrie tab** when editing an Angajat
- Should be visible in the Biometrie tab content area
- Positioned near the photo upload component (`FotoUpload`)

**Button Details:**
- Button label: "Sincronizează pe toate dispozitivele"
- Icon: Sync/Upload icon (suggested)
- Action: Triggers sync of the current Angajat to all active devices
- **Enablement Logic:**
  - Button should be **disabled** if:
    - Angajat does not have `employee_no` assigned
    - Angajat does not have `foto_fata_url` (face photo) available
  - Show small message/tooltip indicating why button is disabled
  - Example: "Employee number required" or "Face photo required"
- Should show loading state during sync
- Should display success/error notifications per device

**Implementation:**
- Calls FastAPI endpoint: `POST /api/hikvision/sync-angajat-all-devices`
- Body: `{ angajat_id: <current_angajat_id> }`
- Should verify `employee_no` exists before allowing sync
- Should sync to all active devices (`status = 'activ'`)

---

#### 3. Sync Photo Only (Retry Failed Photo Sync)

**Location:** Angajat Edit Dialog - Biometrie Tab (`src/pages/Angajati.tsx`)

**Button Placement:**
- Located in the **Biometrie tab** when editing an Angajat
- Should be visible in the Biometrie tab content area
- Positioned near the "Sincronizează pe toate dispozitivele" button
- Only visible when photo exists but sync may have failed

**Button Details:**
- Button label: "Sincronizează Foto"
- Icon: Image/Photo icon (suggested)
- Action: Triggers sync of face photo only (skips Person creation) to all active devices
- **Enablement Logic:**
  - Button should be **enabled** if:
    - Angajat has `employee_no` assigned
    - Angajat has `foto_fata_url` (face photo) available
  - Button should be **disabled** if:
    - Missing `employee_no` or `foto_fata_url`
  - Show small message/tooltip indicating why button is disabled
- Should show loading state during sync
- Should display success/error notifications per device

**Use Cases:**
- Retry failed photo syncs after Person was successfully created
- Update photo on devices when photo was changed in database
- Sync photo to new devices added after initial sync

**Implementation:**
- Calls FastAPI endpoint: `POST /api/hikvision/sync-angajat-photo-only`
- Body: `{ angajat_id: <current_angajat_id> }`
- Should verify `employee_no` and `foto_fata_url` exist before allowing sync
- Should sync photo to all active devices (`status = 'activ'`)
- Skips Person creation step (assumes Person already exists on devices)

---

### UI Considerations

1. **Loading States:**
   - Show spinner/loading indicator during sync operations
   - Disable button while sync is in progress
   - Show progress for bulk operations (X of Y employees synced)

2. **Success/Error Display (Toast Notifications):**
   - Use toast notifications from `@/hooks/use-toast` (existing pattern in app)
   - **Success Toast:**
     ```typescript
     toast({
       title: "✅ Sincronizare finalizată",
       description: `${successCount} angajați sincronizați cu succes`,
       variant: "default"
     });
     ```
   - **Partial Success Toast:**
     ```typescript
     toast({
       title: "⚠️ Sincronizare parțială",
       description: `
         ${successCount} angajați sincronizați cu succes
         ${skippedCount > 0 ? `\n${skippedCount} deja existenți` : ''}
         ${failedCount > 0 ? `\n❌ ${failedCount} erori` : ''}
       `.trim(),
       variant: failedCount > 0 ? "destructive" : "default"
     });
     ```
   - **Fatal Error Toast:**
     ```typescript
     toast({
       title: "❌ Sincronizare eșuată",
       description: errorMessage,
       variant: "destructive"
     });
     ```
   - **Error Details Toast (if multiple errors):**
     ```typescript
     toast({
       title: "⚠️ Detalii Erori",
       description: errorDetails.slice(0, 3).map(e => `• ${e}`).join('\n'),
       variant: "destructive"
     });
     ```

3. **Partial Success Handling:**
   - If Person synced but face image failed:
     - Include in success count but note in description
     - Show warning icon (⚠️) in toast title
     - User can use "Sincronizează Foto" button in Biometrie tab to retry photo sync

4. **Permissions:**
   - Ensure only authorized users can trigger sync
   - Consider role-based access control

## API Endpoint Design

### Required Endpoints

1. **Sync Single Employee to All Devices** ⭐ (For Biometrie tab button)
   ```
   POST /api/hikvision/sync-angajat-all-devices
   Body: { angajat_id }
   ```
   - Syncs one Angajat to all active devices
   - Returns per-device sync results

2. **Sync All Employees to All Devices** ⭐ (For Angajati page button)
   ```
   POST /api/hikvision/sync-all-to-all-devices
   Body: {}
   ```
   - Syncs all active Angajati to all active devices
   - Returns summary of sync results

3. **Sync Photo Only** ⭐ (For Biometrie tab "Sync Photo" button)
   ```
   POST /api/hikvision/sync-angajat-photo-only
   Body: { angajat_id }
   ```
   - Syncs face photo only (skips Person creation) to all active devices
   - Assumes Person already exists on devices
   - Returns per-device sync results

### Optional Endpoints (for future use)

3. **Sync Single Employee to Single Device** (optional/future)
   ```
   POST /api/hikvision/sync-angajat-device
   Body: { angajat_id, dispozitiv_id }
   ```

4. **Sync All Employees to Single Device** (optional/future)
   ```
   POST /api/hikvision/sync-all-to-device
   Body: { dispozitiv_id }
   ```

## Testing Requirements

### Unit Tests
- Digest authentication implementation
- Request payload construction
- Error handling logic
- Employee number assignment logic
- Data transformation (employee_no to numeric employeeNo, name formatting)

### Integration Tests
- Successful person creation
- Successful face image addition
- Error scenarios (invalid credentials, device offline, etc.)
- Concurrent sync operations

### End-to-End Tests
- Full sync flow for single employee to single device
- Bulk sync for multiple employees
- Error recovery and retry logic

## Security Considerations

1. **Password Storage:**
   - Passwords stored in `password_encrypted` field (field name suggests encryption but stored as plain text)
   - Current implementation uses password directly: `btoa(\`${device.username}:${device.password_encrypted}\`)`
   - Use password value directly from database (no decryption needed)
   - **Note:** Field is named "encrypted" but stores plain password - use as-is

2. **Network Security:**
   - Use HTTPS if device supports it
   - Validate device certificates if using HTTPS

3. **Access Control:**
   - Ensure only authorized users can trigger sync
   - Log all sync operations for audit

4. **Data Privacy:**
   - Handle employee data securely
   - Ensure image URLs don't expose sensitive information

## Open Questions / Decisions Needed

1. ✅ **Employee ID Format:** ~~How should UUID be converted to `employeeNo`?~~ **RESOLVED:** Use sequential auto-increment numeric assignment (Option 1)
2. ✅ **Validity Dates:** ~~What dates should be used for `Valid.beginTime` and `Valid.endTime`?~~ **RESOLVED:** Use example dates (not critical)
3. ✅ **Image URL:** ~~How to ensure images are accessible from device network?~~ **RESOLVED:** Use public Supabase Storage URLs directly
4. ✅ **Sync Processing Order:** ~~Process per-device or per-employee?~~ **RESOLVED:** Process Employee 1 to all devices, then Employee 2 to all devices
5. ✅ **Rate Limiting:** ~~What delay between API calls?~~ **RESOLVED:** 1 second global delay
6. ✅ **Button Enablement:** ~~How to handle missing employee_no or photo?~~ **RESOLVED:** Disable button with message
7. ✅ **Database Tracking:** ~~When to update sync status?~~ **RESOLVED:** Only on success/failure, treat "already exists" as success
8. ✅ **Missing Employee Number:** ~~How to handle during bulk sync?~~ **RESOLVED:** Skip silently and continue
9. ✅ **Digest Auth Library:** ~~Which library/method to use for Digest Authentication?~~ **RESOLVED:** Use Python `requests` + `HTTPDigestAuth` (or `httpx` with digest auth)
10. ✅ **Face Image Error UI:** ~~How to display partial success?~~ **RESOLVED:** Use toast notifications with warning variant, show counts. Add "Sync Photo" button for retrying
11. ✅ **Success/Failure Display:** ~~How to display in UI?~~ **RESOLVED:** Use toast notifications with summary counts (success, skipped, failed)
12. ✅ **Sync Photo Button:** ~~Add separate button for retrying photo sync?~~ **RESOLVED:** Add "Sincronizează Foto" button in Biometrie tab
12. ⏳ **Sync Trigger:** Manual, scheduled, or event-driven? (Currently manual via UI buttons)
13. ⏳ **Incremental vs Full Sync:** Should we track what's already synced? (Currently full sync each time)

## Implementation Plan

This implementation plan is organized into distinct phases that can be tackled sequentially. Each phase builds upon the previous one and has clear deliverables.

---

### Phase 1: Database Setup and Schema Changes

**Objective:** Prepare database schema for employee number assignment and sync tracking.

**Tasks:**
1. Create database migration file:
   - Add `employee_no` INTEGER UNIQUE column to `angajati_biometrie` table
   - Create sequence `angajat_employee_no_seq` starting at 1000
   - Create trigger function `assign_employee_no()` for auto-assignment
   - Create trigger `assign_employee_no_trigger` on `angajati_biometrie` INSERT
   - Add index on `employee_no` for performance

2. Manual data migration:
   - Manually assign `employee_no` to existing employees in `angajati_biometrie` (starting from 1000)
   - Update sequence to continue from highest manually assigned number

3. Verify:
   - Test trigger works for new `angajati_biometrie` records
   - Verify sequence continues correctly after manual assignment
   - Check uniqueness constraint works

**Deliverables:**
- ✅ Database migration file created and applied
- ✅ All existing employees have `employee_no` assigned
- ✅ Sequence configured correctly
- ✅ Trigger working for new records

**Dependencies:** None

**Estimated Time:** 1-2 hours

---

### Phase 2: Core Sync Infrastructure

**Objective:** Implement Digest Authentication and ISAPI call functions (Python/FastAPI compatible).

**Tasks:**
1. Set up Digest Authentication (Python):
   - Choose HTTP client: `requests` + `HTTPDigestAuth` (sync) or `httpx` + digest auth (async)
   - Create helper to perform authenticated ISAPI requests with timeouts
   - Test Digest Auth against a device

2. Implement Person creation function:
   - Create `create_person_on_device()` function (Python)
   - Handle ISAPI Person creation endpoint
   - Implement response parsing (success, already exists, errors)
   - Add error handling for fatal errors (timeout, auth failure)

3. Implement Face Image addition function:
   - Create `add_face_image_to_device()` function (Python)
   - Handle ISAPI Face Image endpoint
   - Implement response parsing and error handling
   - Handle partial success scenarios

4. Create shared utilities:
   - Device connection helper (build URL, get credentials)
   - Response parser for ISAPI responses
   - Error classifier (fatal vs non-fatal)
   - Rate limiting helper (1 second delay)

**Deliverables:**
- ✅ Digest Authentication working (Python client)
- ✅ `create_person_on_device()` implemented and tested
- ✅ `add_face_image_to_device()` implemented and tested
- ✅ Shared utilities created
- ✅ Unit tests for core functions

**Dependencies:** Phase 1 (need `employee_no` field)

**Estimated Time:** 4-6 hours

---

### Phase 3: Sync Orchestration Logic

**Objective:** Create the core sync logic that orchestrates Person and Photo sync.

**Tasks:**
1. Implement single employee sync function:
   - Create `syncAngajatToDevice()` function
   - Handle Person creation (with "already exists" logic)
   - Handle Face Image addition (with partial success handling)
   - Return detailed sync result per device

2. Implement sync to all devices:
   - Create `syncAngajatToAllDevices()` function
   - Iterate through all active devices
   - Handle fatal errors (stop sync)
   - Collect results per device
   - Return summary

3. Implement bulk sync function:
   - Create `syncAllAngajatiToAllDevices()` function
   - Process Employee 1 to all devices, then Employee 2, etc.
   - Handle missing `employee_no` (skip silently)
   - Handle fatal errors (stop remaining syncs)
   - Collect summary statistics
   - Return comprehensive results

4. Implement photo-only sync:
   - Create `syncPhotoOnlyToAllDevices()` function
   - Skip Person creation step
   - Only sync face image
   - Return per-device results

5. Add database tracking:
   - Update `angajati_biometrie_dispozitive` table on success/failure
   - Mark "already exists" as success
   - Update `dispozitive_pontaj.ultima_sincronizare` timestamp
   - Log errors to sync tracking table

**Deliverables:**
- ✅ `syncAngajatToDevice()` function
- ✅ `syncAngajatToAllDevices()` function
- ✅ `syncAllAngajatiToAllDevices()` function
- ✅ `syncPhotoOnlyToAllDevices()` function
- ✅ Database tracking implemented
- ✅ Error handling and logging complete

**Dependencies:** Phase 2 (core sync infrastructure)

**Estimated Time:** 6-8 hours

---

### Phase 4: FastAPI Server Implementation

**Objective:** Extend the existing Python/FastAPI server (on VPN/LAN) with sync endpoints that can reach devices.

**Tasks:**
1. Set up FastAPI sync module:
   - Add `app/hikvision_sync/` (or similar) in the FastAPI project
   - Configure CORS for the React frontend
   - Load Supabase service role key and Supabase URL from env vars
   - Add Supabase client (e.g., `supabase-py`) or REST calls via `httpx`

2. Port sync utilities to Python:
   - Recreate ISAPI helpers (`createPersonOnDevice`, `addFaceImageToDevice`) in Python
   - Use Digest Auth via `requests` + `HTTPDigestAuth` (or `httpx` with digest auth)
   - Implement response parsing and error classification (fatal vs non-fatal)
   - Implement rate limiting (e.g., `asyncio.sleep(1)`)

3. Implement sync orchestration in Python:
   - `sync_angajat_to_device`
   - `sync_angajat_to_all_devices`
   - `sync_all_angajati_to_all_devices`
   - `sync_photo_only_to_all_devices`
   - Handle skips (missing employee_no/photo), partials (photo fail), and fatal errors

4. Create API endpoints in FastAPI:
   - `POST /api/hikvision/sync-angajat-all-devices`
     - Accept `{ angajat_id }` in body
     - Authenticate request (Supabase JWT validation middleware)
     - Call `sync_angajat_to_all_devices`
     - Return per-device results
   
   - `POST /api/hikvision/sync-all-to-all-devices`
     - Optional filters (future)
     - Authenticate request
     - Call `sync_all_angajati_to_all_devices`
     - Return summary response
   
   - `POST /api/hikvision/sync-angajat-photo-only`
     - Accept `{ angajat_id }`
     - Authenticate request
     - Call `sync_photo_only_to_all_devices`
     - Return per-device results

5. Add authentication middleware:
   - Validate Supabase JWT tokens (or reuse existing FastAPI auth)
   - Optionally enforce role-based access for sync actions

6. Add error handling and logging:
   - Structured logging for ISAPI requests/responses
   - Clear fatal vs non-fatal error responses
   - Request/response logging for debugging

**Deliverables:**
- ✅ FastAPI sync module added
- ✅ Authentication middleware working
- ✅ All three sync API endpoints implemented
- ✅ Sync utilities ported to Python
- ✅ Error handling and logging complete
- ✅ Server runs on VPN/LAN and can reach devices

**Dependencies:** Phase 3 (sync orchestration logic - will be ported to Python)

**Estimated Time:** 6-8 hours (includes porting utilities)

---

### Phase 5: UI Implementation

**Objective:** Add sync buttons and UI feedback to the application.

**Tasks:**
1. Add "Sincronizează toți Angajații" button:
   - Location: Angajati page, left of Template Excel button
   - Implement button click handler
   - Call `POST /api/hikvision/sync-all-to-all-devices` endpoint (FastAPI server)
   - Include Supabase JWT token in Authorization header
   - Show loading state during sync
   - Display toast notification with results

2. Add "Sincronizează pe toate dispozitivele" button:
   - Location: Biometrie tab in Angajat Edit Dialog
   - Implement button with enablement logic:
     - Disabled if `employee_no` missing
     - Disabled if `foto_fata_url` missing
     - Show tooltip explaining why disabled
   - Call `POST /api/hikvision/sync-angajat-all-devices` endpoint (FastAPI server)
   - Include Supabase JWT token in Authorization header
   - Show loading state
   - Display toast notification

3. Add "Sincronizează Foto" button:
   - Location: Biometrie tab in Angajat Edit Dialog
   - Implement button with enablement logic (same as above)
   - Call `POST /api/hikvision/sync-angajat-photo-only` endpoint (FastAPI server)
   - Include Supabase JWT token in Authorization header
   - Show loading state
   - Display toast notification

4. Implement toast notifications:
   - Success toast (green)
   - Partial success toast (warning, with counts)
   - Fatal error toast (red)
   - Error details toast (if multiple errors)

5. Add loading states:
   - Disable buttons during sync
   - Show spinner/loading indicator
   - Show progress for bulk operations (optional)

**Deliverables:**
- ✅ All three buttons implemented
- ✅ Button enablement logic working
- ✅ Toast notifications displaying correctly
- ✅ Loading states implemented
- ✅ Error handling in UI

**Dependencies:** Phase 4 (FastAPI server implementation)

**Estimated Time:** 4-6 hours

---

### Phase 6: Testing and Refinement

**Objective:** Test the complete flow and refine based on real-world usage.

**Tasks:**
1. Unit testing:
   - Test Digest Auth implementation
   - Test ISAPI response parsing
   - Test error classification logic
   - Test sync orchestration functions

2. Integration testing:
   - Test with test device (if available)
   - Test Person creation
   - Test Face Image addition
   - Test "already exists" scenario
   - Test error scenarios (timeout, auth failure)

3. End-to-end testing:
   - Test full sync flow from UI button click
   - Test bulk sync with multiple employees
   - Test single employee sync
   - Test photo-only sync
   - Verify database tracking updates

4. Error scenario testing:
   - Test with device offline
   - Test with invalid credentials
   - Test with missing employee_no
   - Test with missing photo
   - Test with network timeout

5. Performance testing:
   - Test sync speed with multiple employees
   - Verify rate limiting works (1 second delay)
   - Check database query performance

6. Refinement:
   - Fix any bugs discovered
   - Optimize performance if needed
   - Improve error messages
   - Add additional logging if needed

**Deliverables:**
- ✅ All tests passing
- ✅ Tested with real devices
- ✅ Performance acceptable
- ✅ Error handling robust
- ✅ Ready for production

**Dependencies:** Phase 5 (UI implementation)

**Estimated Time:** 6-8 hours

---

## Implementation Summary

**Total Estimated Time:** 27-38 hours (updated for FastAPI implementation)

**Phase Order:**
1. Phase 1: Database Setup (1-2 hours)
2. Phase 2: Core Sync Infrastructure (4-6 hours) - **Note:** Will be ported to FastAPI in Phase 4
3. Phase 3: Sync Orchestration Logic (6-8 hours) - **Note:** Will be ported to FastAPI in Phase 4
4. Phase 4: FastAPI Server Implementation (6-8 hours)
5. Phase 5: UI Implementation (4-6 hours) - **Note:** Will call FastAPI endpoints instead of Edge Functions
6. Phase 6: Testing and Refinement (6-8 hours)

**Critical Path:** Phases must be completed in order (each depends on the previous).

**Parallel Work:** Within each phase, some tasks can be done in parallel (e.g., implementing multiple API endpoints).

**Milestones:**
- ✅ **Milestone 1:** Database ready (end of Phase 1)
- ✅ **Milestone 2:** Core sync functions working (end of Phase 2)
- ✅ **Milestone 3:** Full sync logic complete (end of Phase 3)
- ✅ **Milestone 4:** FastAPI sync API ready (end of Phase 4)
- ✅ **Milestone 5:** UI complete (end of Phase 5)
- ✅ **Milestone 6:** Production ready (end of Phase 6)

---

## Next Steps

1. ✅ Document ISAPI call details (this document)
2. ✅ Document Employee Number Assignment approach (Option 1)
3. ✅ Clarify manual assignment for existing employees vs auto-increment for new
4. ✅ Document UI integration points (sync button locations)
5. ✅ Create implementation plan with phases
6. ✅ Phase 1 completed (database migration applied, employee_no ready)
7. ⏳ **Start Phase 2: Core Sync Infrastructure (Python/FastAPI)**

