# Implementation Plan: Save Access Events to Supabase

## Overview
Add functionality to automatically save access control events (major=5, sub=75) to Supabase database via Edge Function when they are received from Hikvision devices.

## Requirements
- **Trigger**: When an access event is detected and logged to Access Log
- **Endpoint**: `https://xnjbjmbjyyuqjcflzigg.supabase.co/functions/v1/pontaj-adauga-eveniment`
- **API Key**: `sk_pontaj_prod_LsoJQrkbi40ov3dWhLqDHKZeOkAK4MBX`
- **Headers**: 
  - `X-API-Key`: API key
  - `Content-Type`: `application/json`
- **Payload**: Entire event JSON (as parsed from device request)
- **Behavior**: Non-blocking - errors should not prevent event logging or response to device

## Implementation Structure

Following the same structure as existing sync functionality, with separation of concerns:
- **Event Processing Layer** (`hikvision_sync/events.py`): Detect access events and trigger save
- **Supabase Client Layer** (`hikvision_sync/supabase_client.py`): Handle Edge Function call
- **Main Application Layer** (`main.py`): Coordinate event processing

---

## 1. Supabase Client Layer (`hikvision_sync/supabase_client.py`)

### 1.1 Add `save_access_event()` method
- **Purpose**: Save access event to Supabase via Edge Function
- **Signature**: `async def save_access_event(self, event_data: dict) -> dict`
- **Input**: 
  - `event_data`: Complete event JSON dict (as parsed from device request)
- **Flow**:
  1. Construct Edge Function URL: `https://xnjbjmbjyyuqjcflzigg.supabase.co/functions/v1/pontaj-adauga-eveniment`
  2. Prepare headers:
     ```python
     {
         "X-API-Key": "sk_pontaj_prod_LsoJQrkbi40ov3dWhLqDHKZeOkAK4MBX",
         "Content-Type": "application/json"
     }
     ```
  3. Send POST request with `event_data` as JSON payload
  4. Handle response and return result
- **Error Handling**:
  - Catch `httpx.HTTPStatusError` and log error details
  - Catch `httpx.RequestError` (timeout, connection errors) and log
  - Return error information in structured format
  - **Non-blocking**: All errors should be logged but not raised (to avoid blocking event processing)
- **Returns**: 
  - Success: `dict` with response data from Edge Function
  - Error: `dict` with error information (for logging purposes)
- **Timeout**: Use 10.0 seconds (same as other Edge Function calls)

### 1.2 Configuration Considerations
- **Hardcoded Values**: For now, endpoint URL and API key are hardcoded
- **Future Enhancement**: Consider moving to config file or environment variables
- **Note**: This is a different endpoint than the existing `external-api-proxy` Edge Function, so it needs separate configuration

---

## 2. Event Processing Layer (`hikvision_sync/events.py`)

### 2.1 Update `process_event_request()` function
- **Purpose**: Add Supabase save call when access event is detected
- **Current Behavior**: Detects access events and logs them
- **New Behavior**: Also saves access events to Supabase
- **Changes**:
  1. **Make function async**: Change from `def` to `async def` (since it's called from async endpoint)
  2. Add optional `supabase_client` parameter to function signature
  3. When access event is detected (`is_access_event(parsed) == True`):
     - Log to access logger (existing behavior)
     - **NEW**: Call `await supabase_client.save_access_event(parsed)` asynchronously
     - Handle save result (log success/error, but don't block)
- **Signature Update**:
  ```python
  async def process_event_request(
      body: bytes,
      content_type: str,
      path: str,
      event_logger: logging.Logger,
      access_logger: Optional[logging.Logger] = None,
      supabase_client: Optional[SupabaseClient] = None  # NEW
  ) -> dict:
  ```
- **Error Handling**:
  - Wrap Supabase call in try/except
  - Log errors to event_logger but don't raise exceptions
  - Return result dict includes save status (for debugging/monitoring)
- **Note**: Since this is called from async FastAPI endpoint, making it async allows proper await usage

### 2.3 Return Value Enhancement
- **Current**: Returns `{"parsed": parsed, "is_access_event": bool}`
- **New**: Add `"save_status"` field:
  ```python
  {
      "parsed": parsed,
      "is_access_event": bool,
      "save_status": "success" | "error" | "skipped" | None
  }
  ```
- **Note**: `save_status` is `None` if not an access event or if supabase_client not provided

---

## 3. Main Application Layer (`main.py`)

### 3.1 Update `catch_all_post()` endpoint
- **Purpose**: Pass Supabase client to event processing
- **Current**: Calls `process_event_request()` without Supabase client
- **Changes**:
  1. Check if `_SUPABASE_CLIENT` is initialized
  2. Pass `_SUPABASE_CLIENT` to `process_event_request()` if available
  3. Use `await` since `process_event_request()` is now async
  4. Handle any errors from event processing (shouldn't happen, but defensive)
- **Code Update**:
  ```python
  @app.post("/{full_path:path}")
  async def catch_all_post(request: Request, full_path: str):
      """Catch-all endpoint for receiving Hikvision device events."""
      body = await request.body()
      content_type = request.headers.get("content-type", "")
      
      # Process event using events module
      result = await process_event_request(
          body=body,
          content_type=content_type,
          path=full_path,
          event_logger=_EVENT_LOGGER.get(),
          access_logger=_ACCESS_LOGGER.get(),
          supabase_client=_SUPABASE_CLIENT  # NEW
      )
      
      return PlainTextResponse("OK")
  ```

### 3.2 Import Update
- **Add**: Import `SupabaseClient` type for type hints (optional but recommended)
- **Location**: At top of file with other imports

---

## 4. Error Handling Strategy

### 4.1 Non-Blocking Design
- **Principle**: Event saving failures should NOT prevent:
  - Event logging to files
  - Response to Hikvision device (always return "OK")
  - Processing of other events
- **Implementation**: All Supabase save errors are caught and logged, but exceptions are not raised

### 4.2 Error Logging
- **Location**: Log errors to `_EVENT_LOGGER` (not `_ACCESS_LOGGER`)
- **Format**: Include:
  - Error type and message
  - Event data (for debugging)
  - Timestamp (automatic via logger)
- **Example**:
  ```python
  event_logger.error(f"Failed to save access event to Supabase: {error}. Event: {event_data}")
  ```

### 4.3 Error Types to Handle
1. **HTTP Errors** (`httpx.HTTPStatusError`):
   - 4xx: Client errors (bad request, auth issues)
   - 5xx: Server errors (Edge Function issues)
2. **Network Errors** (`httpx.RequestError`):
   - Connection timeout
   - Connection refused
   - DNS resolution failures
3. **JSON Errors**:
   - Invalid event data structure
   - Serialization failures

---

## 5. Testing Considerations

### 5.1 Unit Tests (Future)
- Test `save_access_event()` with mock HTTP responses
- Test error handling for various HTTP status codes
- Test timeout scenarios

### 5.2 Integration Testing
- **Manual Test**: Send test access event from device
- **Verify**:
  1. Event is logged to Access Log file ✓
  2. Event is saved to Supabase database ✓
  3. Response to device is "OK" ✓
  4. Errors are logged appropriately ✓

### 5.3 Error Scenarios to Test
- Supabase Edge Function unavailable (timeout/connection error)
- Invalid API key (401 error)
- Invalid event data format
- Network interruption during save

---

## 6. Implementation Steps

### Step 1: Add `save_access_event()` to SupabaseClient
- **File**: `hikvision_sync/supabase_client.py`
- **Action**: Add new method with hardcoded endpoint URL and API key
- **Test**: Can be tested independently with sample event data

### Step 2: Update `process_event_request()` function
- **File**: `hikvision_sync/events.py`
- **Action**: 
  - **Make function async**: Change `def` to `async def`
  - Add `supabase_client` parameter
  - Add `await` call to `save_access_event()` when access event detected
  - Add error handling and logging
  - Update return value to include save status

### Step 3: Update `catch_all_post()` endpoint
- **File**: `main.py`
- **Action**: Pass `_SUPABASE_CLIENT` to `process_event_request()`
- **Test**: Verify endpoint still responds correctly

### Step 4: Testing
- **Action**: 
  - Trigger test access event from device
  - Verify event appears in Supabase database
  - Check logs for any errors
  - Verify device receives "OK" response

---

## 7. Future Enhancements

### 7.1 Configuration Management
- Move endpoint URL and API key to config file (`app_settings.json`)
- Add environment variable support
- Allow different endpoints for dev/staging/production

### 7.2 Retry Logic
- Add retry mechanism for transient failures
- Exponential backoff for retries
- Maximum retry attempts

### 7.3 Event Queue (Optional)
- For high-volume scenarios, consider queuing events
- Background worker to process queue
- Prevents blocking event reception

### 7.4 Monitoring & Metrics
- Track success/failure rates
- Monitor Edge Function response times
- Alert on high error rates

### 7.5 Event Filtering
- Option to filter which events are saved (currently saves all access events)
- Configurable filtering rules
- Support for different event types

---

## 8. Code Structure Summary

```
hikvision_sync/
├── supabase_client.py
│   └── SupabaseClient.save_access_event()  # NEW
│
├── events.py
│   └── process_event_request()  # MODIFIED
│
└── ...

main.py
└── catch_all_post()  # MODIFIED
```

---

## 9. Dependencies

- **Existing**: `httpx` (already used in `supabase_client.py`)
- **No new dependencies required**

---

## 10. Notes

- **Async/Await**: `process_event_request()` will be made async to properly await the Supabase call. This is the cleanest approach since it's called from an async FastAPI endpoint.

- **Event Data Format**: The entire parsed event JSON will be sent. The Edge Function will handle parsing and saving to database. If fields need to be filtered/removed later, that can be done in the Edge Function or added as a preprocessing step here.

- **Performance**: Saving to Supabase adds network latency. Since it's async and non-blocking, it shouldn't significantly impact event reception performance. Monitor response times in production.

- **Idempotency**: Consider if duplicate events need to be handled. Currently, each event will be saved. If devices resend events, duplicates may occur. This should be handled by the Edge Function or database constraints.

