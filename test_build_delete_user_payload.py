"""
Test script for _build_delete_user_payload() function.

Run this script to test the payload building function:
    python test_build_delete_user_payload.py
"""

import json
import sys
from pathlib import Path

# Add the project root to the path so we can import the module
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from hikvision_sync.isapi_client import _build_delete_user_payload


def test_with_integer_employee_no():
    """Test with integer employee_no."""
    print("\n=== Test 1: Integer employee_no ===")
    angajat = {
        "biometrie": {
            "employee_no": 1000
        }
    }
    
    payload = _build_delete_user_payload(angajat)
    print("Input:", json.dumps(angajat, indent=2))
    print("Output:", json.dumps(payload, indent=2))
    
    # Verify payload structure
    assert "UserInfoDetail" in payload
    assert payload["UserInfoDetail"]["mode"] == "byEmployeeNo"
    assert payload["UserInfoDetail"]["operateType"] == "byTerminal"
    assert payload["UserInfoDetail"]["terminalNoList"] == [1]
    
    # Verify EmployeeNoList
    assert "EmployeeNoList" in payload["UserInfoDetail"]
    assert len(payload["UserInfoDetail"]["EmployeeNoList"]) == 1
    assert payload["UserInfoDetail"]["EmployeeNoList"][0]["employeeNo"] == "1000"  # Should be string
    print("✅ Test 1 passed!")


def test_with_string_employee_no():
    """Test with string employee_no (should still work)."""
    print("\n=== Test 2: String employee_no ===")
    angajat = {
        "biometrie": {
            "employee_no": "2000"
        }
    }
    
    payload = _build_delete_user_payload(angajat)
    print("Input:", json.dumps(angajat, indent=2))
    print("Output:", json.dumps(payload, indent=2))
    
    assert payload["UserInfoDetail"]["EmployeeNoList"][0]["employeeNo"] == "2000"
    print("✅ Test 2 passed!")


def test_with_different_employee_no():
    """Test with different employee number."""
    print("\n=== Test 3: Different employee_no ===")
    angajat = {
        "biometrie": {
            "employee_no": 42
        }
    }
    
    payload = _build_delete_user_payload(angajat)
    print("Input:", json.dumps(angajat, indent=2))
    print("Output:", json.dumps(payload, indent=2))
    
    assert payload["UserInfoDetail"]["EmployeeNoList"][0]["employeeNo"] == "42"
    print("✅ Test 3 passed!")


def test_missing_employee_no():
    """Test error handling - missing employee_no."""
    print("\n=== Test 4: Missing employee_no (should raise ValueError) ===")
    angajat = {
        "biometrie": {}
    }
    
    try:
        _build_delete_user_payload(angajat)
        print("❌ Test 4 failed - should have raised ValueError")
        assert False
    except ValueError as e:
        assert "employee_no" in str(e).lower()
        print(f"✅ Test 4 passed! Correctly raised: {e}")


def test_missing_biometrie():
    """Test error handling - missing biometrie dict."""
    print("\n=== Test 5: Missing biometrie dict (should raise ValueError) ===")
    angajat = {}
    
    try:
        _build_delete_user_payload(angajat)
        print("❌ Test 5 failed - should have raised ValueError")
        assert False
    except ValueError as e:
        assert "employee_no" in str(e).lower()
        print(f"✅ Test 5 passed! Correctly raised: {e}")


def test_payload_structure():
    """Test that payload structure matches ISAPI specification."""
    print("\n=== Test 6: Verify payload structure ===")
    angajat = {
        "biometrie": {
            "employee_no": 1234
        }
    }
    
    payload = _build_delete_user_payload(angajat)
    
    # Verify top-level structure
    assert isinstance(payload, dict)
    assert "UserInfoDetail" in payload
    
    user_info = payload["UserInfoDetail"]
    
    # Verify required fields
    assert user_info["mode"] == "byEmployeeNo"
    assert user_info["operateType"] == "byTerminal"
    assert isinstance(user_info["terminalNoList"], list)
    assert user_info["terminalNoList"] == [1]
    
    # Verify EmployeeNoList structure
    assert isinstance(user_info["EmployeeNoList"], list)
    assert len(user_info["EmployeeNoList"]) == 1
    assert isinstance(user_info["EmployeeNoList"][0], dict)
    assert "employeeNo" in user_info["EmployeeNoList"][0]
    assert isinstance(user_info["EmployeeNoList"][0]["employeeNo"], str)
    
    print("✅ Test 6 passed! Payload structure is correct")


def test_employee_no_conversion():
    """Test that employee_no is converted to string."""
    print("\n=== Test 7: Verify employee_no is converted to string ===")
    angajat = {
        "biometrie": {
            "employee_no": 9999  # Integer input
        }
    }
    
    payload = _build_delete_user_payload(angajat)
    employee_no = payload["UserInfoDetail"]["EmployeeNoList"][0]["employeeNo"]
    
    assert isinstance(employee_no, str), f"Expected string, got {type(employee_no)}"
    assert employee_no == "9999"
    print("✅ Test 7 passed! employee_no is correctly converted to string")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing _build_delete_user_payload() function")
    print("=" * 60)
    
    try:
        test_with_integer_employee_no()
        test_with_string_employee_no()
        test_with_different_employee_no()
        test_missing_employee_no()
        test_missing_biometrie()
        test_payload_structure()
        test_employee_no_conversion()
        
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

