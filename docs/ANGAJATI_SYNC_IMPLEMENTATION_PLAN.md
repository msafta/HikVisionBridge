# Angajati → Hikvision Device Sync – Implementation Plan (FastAPI + Supabase)

This plan focuses only on pushing Angajati to devices. Event ingestion will be handled later.

## Scope & Assumptions
- FastAPI service lives in `main.py`; we extend it with authenticated sync endpoints.
- Supabase JWT validation required (access tokens only). Service role key is used **only** after auth, for server→Supabase calls.
- Device creds in Supabase: `username`, `password_encrypted` (plain text despite name). No decryption.
- Transport: HTTP to devices for now.
- Execution: Sequential per device (no per-device parallelization). Add ~1s delay between device calls.
- Fatal errors (auth/network/timeout) stop bulk sync. Data issues per employee are non-fatal (skip/continue).
- CORS: allow React app origin and localhost dev origins.
- For now, return results to frontend; no structured DB logging beyond what already exists.

## Data Inputs (Supabase)
- `angajati`: filter `status = 'activ'`; name = `nume + " " + prenume` (fallback `nume_complet`).
- `angajati_biometrie`: `employee_no` (int, required), `foto_fata_url` (optional for photo step), `hikvision_employee_no` legacy string (can mirror `employee_no`).
- `dispozitive_pontaj`: only `status = 'activ'`; fields `ip_address`, `port` (default 80), `username`, `password_encrypted`.

## API Endpoints (FastAPI)
All endpoints require Supabase JWT (validated via JWKS/JWT secret).
- `POST /api/hikvision/sync-angajat-all-devices`
  - Body: `{ "angajat_id": "<uuid>" }`
  - Action: Sync one Angajat to all active devices (person + photo).
- `POST /api/hikvision/sync-all-to-all-devices`
  - Body: `{}` (no filters for now)
  - Action: Sync all active Angajati to all active devices.
- `POST /api/hikvision/sync-angajat-photo-only`
  - Body: `{ "angajat_id": "<uuid>" }`
  - Action: Photo only, all active devices (assumes person exists).

Response: return per-device results and summary to frontend; no DB writes for tracking in this phase.

## Auth & CORS
- Add middleware to validate Supabase JWT:
  - Prefer JWKS from Supabase; fallback to JWT secret env if needed.
  - On success, attach user info to request state.
  - On failure, 401/403.
- CORS: allow production app origin and localhost dev (`http://localhost:3000` etc.); allow Authorization header and POST.

## Device Client Helpers (Python, requests + HTTPDigestAuth)
- Common options: timeout (e.g., 10–15s), digest auth with `username` + `password_encrypted`, headers per endpoint.
- Rate limit: `await asyncio.sleep(1)` between device calls.

### Person (Create/Update) – ISAPI
- Endpoint: `POST http://{ip}:{port}/ISAPI/AccessControl/UserInfo/Record?format=json`
- Payload:
  - `employeeNo`: numeric `employee_no`
  - `name`: `nume prenume` (fallback `nume_complet`)
  - `userType`: `"normal"`
  - `Valid`: `enable=true`, `beginTime`/`endTime` static range
  - `doorRight`: `"1"`, `RightPlan`: default door plan (door 1/template 1)
- Handling:
  - Success: `statusCode=1 && subStatusCode="ok"` → success
  - Already exists: `statusCode=6 && subStatusCode="employeeNoAlreadyExist"` → treat as success
  - HTTP 401/timeout/network → fatal; stop bulk
  - Other errors → fatal

### Face Image
- Endpoint: `POST http://{ip}:{port}/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json`
- Payload:
  - `faceLibType`: `"blackFD"`
  - `FDID`: `"1"`
  - `FPID`: numeric `employee_no`
  - `faceURL`: `foto_fata_url`
- Handling:
  - Success: OK
  - Failure: partial error; continue with next employee/device

## Orchestration Logic
- `sync_angajat_to_device(angajat, device)`:
  1) Ensure `employee_no` exists; if missing → skip (non-fatal, mark skipped).
  2) Call person endpoint; handle already-exists as success.
  3) If photo exists, call face endpoint; photo failure marked partial.
  4) Return result: success | skipped | partial | fatal (with message).
- `sync_angajat_to_all_devices(angajat_id)`:
  - Fetch angajat + biometrie.
  - Iterate active devices sequentially; 1s delay between devices.
  - Stop on first fatal device error; otherwise collect per-device results.
- `sync_all_angajati_to_all_devices()`:
  - Fetch all active angajati + biometrie.
  - For each angajat (sequence): run `sync_angajat_to_all_devices` but do not parallelize across employees; stop entire run on fatal device error.
  - Skip employees without `employee_no`; photo-only step skipped if no photo.
- `sync_photo_only_to_all_devices(angajat_id)`:
  - Like single sync but skip person step; require `employee_no` and `foto_fata_url`.

## Error & Result Model (returned to frontend)
- Per device: `{ device_id, status: "success" | "partial" | "skipped" | "fatal", message?, step? }`
- Summary: counts for success, partial, skipped, failed/fatal; first fatal error message if any.

## Configuration & Env
- Env vars:
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_ROLE_KEY` (server→Supabase)
  - `SUPABASE_JWKS_URL` or `SUPABASE_JWT_SECRET` (for JWT verification)
  - CORS allowed origins list (prod + localhost)
- Device fetch: from Supabase (`dispozitive_pontaj` where status='activ').

## Testing Checklist
- Unit: JWT verification helper; ISAPI payload builders; response classifiers.
- Integration (with a test device or mocked ISAPI):
  - Person success, already-exists, auth failure, timeout.
  - Photo success/failure handling.
  - Bulk fatal-stop behavior.
  - Skips when `employee_no` missing; photo skipped when URL missing.
- CORS preflight from localhost and prod origins.

## Phased Delivery (do these one by one)

### Phase 1 – Auth + CORS groundwork
- Add CORS allowing prod app origin + localhost dev (include port 3000).
- Add JWT verification middleware:
  - Fetch Supabase JWKS (preferred) or use JWT secret env fallback.
  - Validate access token; reject on expired/invalid; attach user info to request state.
- Add small dependency/helper to enforce auth on the new endpoints.
- Smoke test with a sample signed token (unit test + manual call).

### Phase 2 – Supabase data access helpers
- Create a thin Supabase client using `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`.
- Helpers (server-side only):
  - Fetch active devices (`dispozitive_pontaj` where status='activ'), return `id/ip_address/port/username/password_encrypted`.
  - Fetch a single angajat + biometrie by id (include `employee_no`, `foto_fata_url`, `nume/prenume/nume_complet`, status).
  - Fetch all active angajati with biometrie (same fields), filtered to `status='activ'`.
- Basic validation of required fields (e.g., numeric `employee_no`).
- Unit tests for shape/field presence (can mock Supabase REST responses if needed).

### Phase 3 – ISAPI client functions
- Implement `create_person_on_device(device, angajat)`:
  - Build payload per doc; send with `requests` + `HTTPDigestAuth`; timeout ~10–15s.
  - Classify responses: success, already-exists (treat as success), fatal (auth/network/timeout/other).
- Implement `add_face_image_to_device(device, angajat)`:
  - Build payload with `FPID=employee_no`, `faceURL=foto_fata_url`.
  - Return success or error (non-fatal; photo failure is partial).
- Shared utilities:
  - 1s rate-limit helper (async sleep).
  - Error classification helpers.
- Unit tests for payload builders and response classification (mock requests).

### Phase 4 – Orchestration logic
- `sync_angajat_to_device`: person step → photo step (if photo present); return status `success|partial|skipped|fatal` with message.
- `sync_angajat_to_all_devices`: fetch angajat + devices; iterate devices sequentially with 1s delay; stop on first fatal; aggregate results.
- `sync_all_angajati_to_all_devices`: fetch list; for each angajat run above (still sequential); stop entire run on first fatal device error.
- `sync_photo_only_to_all_devices`: skip person step; require `employee_no` + `foto_fata_url`.
- Summaries: counts for success/partial/skipped/fatal plus per-device details.
- Unit tests for orchestration with mocked ISAPI helpers.

### Phase 5 – FastAPI endpoints
- Wire endpoints:
  - `POST /api/hikvision/sync-angajat-all-devices { angajat_id }`
  - `POST /api/hikvision/sync-all-to-all-devices {}`
  - `POST /api/hikvision/sync-angajat-photo-only { angajat_id }`
- Apply auth dependency; parse/validate body.
- Call orchestration; return structured result (per-device + summary); map fatal to 502/500? (or always 200 with status payload—pick and document).
- Add minimal OpenAPI descriptions.

### Phase 6 – Smoke tests & hardening
- Local smoke:
  - Call endpoints with a valid Supabase token.
  - Hit a real or mocked device for person success and already-exists paths.
  - Simulate auth failure/timeout to confirm fatal-stop behavior.
- CORS preflight check from localhost and prod origin.
- Log key steps (start/stop sync, device outcomes, fatal errors).
- Optional: tighten timeouts/rate-limit constants; add small retry for transient network if desired.

### Phase 7 – Deployment notes
- Ensure env vars set: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWKS_URL`/`SUPABASE_JWT_SECRET`, allowed origins.
- Run with HTTPS front (reverse proxy) so React calls `https://api.example.com`; enable CORS for app + localhost.
- Monitor logs for ISAPI failures; adjust delay/timeouts if devices are sensitive.

