"""
Test script for _build_face_image_update_payload() function.

Run this script to test the payload building function:
    python test_build_face_image_update_payload.py
"""

import json
import sys
from pathlib import Path

# Add the project root to the path so we can import the module
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from hikvision_sync.isapi_client import _build_face_image_update_payload


def test_with_full_url():
    """Test with full HTTPS URL."""
    print("\n=== Test 1: Full HTTPS URL ===")
    angajat = {
        "biometrie": {
            "employee_no": 12,
            "foto_fata_url": "https://xnjbjmbjyyuqjcflzigg.supabase.co/storage/v1/object/public/pontaj-photos/test.jpg"
        }
    }
    
    payload = _build_face_image_update_payload(angajat)
    print("Input:", json.dumps(angajat, indent=2))
    print("Output:", json.dumps(payload, indent=2))
    
    assert payload["faceLibType"] == "blackFD"
    assert payload["FDID"] == "1"
    assert payload["FPID"] == "12"
    assert payload["faceID"] == "1"
    assert payload["faceURL"] == angajat["biometrie"]["foto_fata_url"]
    print("✅ Test 1 passed!")


def test_with_filename():
    """Test with just filename (requires supabase_url)."""
    print("\n=== Test 2: Filename only (with supabase_url) ===")
    angajat = {
        "biometrie": {
            "employee_no": 12,
            "foto_fata_url": "test-image.jpg"
        }
    }
    supabase_url = "https://xnjbjmbjyyuqjcflzigg.supabase.co"
    
    payload = _build_face_image_update_payload(angajat, supabase_url)
    print("Input:", json.dumps(angajat, indent=2))
    print("Supabase URL:", supabase_url)
    print("Output:", json.dumps(payload, indent=2))
    
    assert payload["faceLibType"] == "blackFD"
    assert payload["FDID"] == "1"
    assert payload["FPID"] == "12"
    assert payload["faceID"] == "1"
    expected_url = f"{supabase_url}/storage/v1/object/public/pontaj-photos/test-image.jpg"
    assert payload["faceURL"] == expected_url
    print("✅ Test 2 passed!")


def test_with_http_url():
    """Test with HTTP URL (should convert to HTTPS for Supabase)."""
    print("\n=== Test 3: HTTP URL (should convert to HTTPS) ===")
    angajat = {
        "biometrie": {
            "employee_no": 12,
            "foto_fata_url": "http://xnjbjmbjyyuqjcflzigg.supabase.co/storage/v1/object/public/pontaj-photos/test.jpg"
        }
    }
    
    payload = _build_face_image_update_payload(angajat)
    print("Input:", json.dumps(angajat, indent=2))
    print("Output:", json.dumps(payload, indent=2))
    
    assert payload["faceURL"].startswith("https://")
    assert "http://" not in payload["faceURL"]
    print("✅ Test 3 passed!")


def test_missing_employee_no():
    """Test error handling - missing employee_no."""
    print("\n=== Test 4: Missing employee_no (should raise ValueError) ===")
    angajat = {
        "biometrie": {
            "foto_fata_url": "https://example.com/image.jpg"
        }
    }
    
    try:
        _build_face_image_update_payload(angajat)
        print("❌ Test 4 failed - should have raised ValueError")
        assert False
    except ValueError as e:
        assert "employee_no" in str(e).lower()
        print(f"✅ Test 4 passed! Correctly raised: {e}")


def test_missing_foto_url():
    """Test error handling - missing foto_fata_url."""
    print("\n=== Test 5: Missing foto_fata_url (should raise ValueError) ===")
    angajat = {
        "biometrie": {
            "employee_no": 12
        }
    }
    
    try:
        _build_face_image_update_payload(angajat)
        print("❌ Test 5 failed - should have raised ValueError")
        assert False
    except ValueError as e:
        assert "foto_fata_url" in str(e).lower()
        print(f"✅ Test 5 passed! Correctly raised: {e}")


def test_filename_without_supabase_url():
    """Test error handling - filename without supabase_url."""
    print("\n=== Test 6: Filename without supabase_url (should raise ValueError) ===")
    angajat = {
        "biometrie": {
            "employee_no": 12,
            "foto_fata_url": "test-image.jpg"
        }
    }
    
    try:
        _build_face_image_update_payload(angajat)
        print("❌ Test 6 failed - should have raised ValueError")
        assert False
    except ValueError as e:
        assert "supabase_url" in str(e).lower() or "filename" in str(e).lower()
        print(f"✅ Test 6 passed! Correctly raised: {e}")


def test_faceid_field():
    """Test that faceID field is present and set to '1'."""
    print("\n=== Test 7: Verify faceID field ===")
    angajat = {
        "biometrie": {
            "employee_no": 12,
            "foto_fata_url": "https://example.com/image.jpg"
        }
    }
    
    payload = _build_face_image_update_payload(angajat)
    assert "faceID" in payload
    assert payload["faceID"] == "1"
    print("✅ Test 7 passed! faceID field is present and set to '1'")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing _build_face_image_update_payload() function")
    print("=" * 60)
    
    try:
        test_with_full_url()
        test_with_filename()
        test_with_http_url()
        test_missing_employee_no()
        test_missing_foto_url()
        test_filename_without_supabase_url()
        test_faceid_field()
        
        print("\n" + "=" * 60)
        print("✅ All tests passed!")
        print("=" * 60)
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        print("=" * 60)
        sys.exit(1)

