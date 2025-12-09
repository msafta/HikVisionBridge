# Hikvision ACS User Provisioning via FastAPI

This document contains a Python FastAPI script to add users with face recognition to one or more Hikvision DS-K1T341AMF-S devices using ISAPI, along with explanatory comments.

---

```python
from fastapi import FastAPI, HTTPException
import requests
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET

# Initialize FastAPI app
app = FastAPI(title="Hikvision ACS User Provisioning")

# Configuration for multiple devices (IP, port, admin credentials)
DEVICES = [
    {"ip": "192.168.1.100", "port": 80, "user": "admin", "password": "1!to59MA72"},
    {"ip": "192.168.1.101", "port": 80, "user": "admin", "password": "yourpassword2"},
]

# Standard headers for ISAPI XML requests
HEADERS = {"Content-Type": "application/xml"}


def build_user_xml(user_id: str, name: str, face_url: str, door_no: int = 1) -> str:
    """Generate the XML payload for a user with face data."""
    user_info = ET.Element("UserInfo")
    
    ET.SubElement(user_info, "userID").text = user_id
    ET.SubElement(user_info, "name").text = name
    ET.SubElement(user_info, "enabled").text = "true"
    
    # Face recognition setup
    face = ET.SubElement(user_info, "face")
    ET.SubElement(face, "faceDataType").text = "url"  # Can also be 'raw' for binary upload
    ET.SubElement(face, "faceURL").text = face_url
    
    # Door access rights
    door_right = ET.SubElement(user_info, "doorRight")
    ET.SubElement(door_right, "doorNo").text = str(door_no)
    ET.SubElement(door_right, "rightPlan").text = "1"
    
    return ET.tostring(user_info, encoding="utf-8", xml_declaration=True).decode()


def add_user_to_device(device: dict, user_id: str, name: str, face_url: str):
    """Send the user XML to a single device via ISAPI."""
    url = f"http://{device['ip']}:{device['port']}/ISAPI/AccessControl/UserInfo/Record"
    xml_payload = build_user_xml(user_id, name, face_url)
    
    response = requests.post(
        url,
        data=xml_payload,
        headers=HEADERS,
        auth=HTTPDigestAuth(device['user'], device['password']),
        timeout=5
    )
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.text


@app.post("/add_user/")
def add_user(user_id: str, name: str, face_url: str):
    """FastAPI endpoint to add a user to all configured devices."""
    results = {}
    for device in DEVICES:
        try:
            result = add_user_to_device(device, user_id, name, face_url)
            results[device["ip"]] = "Success"
        except HTTPException as e:
            results[device["ip"]] = f"Failed: {e.detail}"
    return results
```

---

## **Usage**

### **Example POST request**

```bash
POST http://127.0.0.1:8000/add_user/
Content-Type: application/json

{
  "user_id": "1001",
  "name": "John Doe",
  "face_url": "http://myserver/faces/john_doe.jpg"
}
```

**Response:**

```json
{
  "192.168.1.100": "Success",
  "192.168.1.101": "Success"
}
```

---

## **Notes & Recommendations**

- `DEVICES`: List all target devices with IP, port, username, and password.
- `face_url`: Must be accessible from the device. For local images, consider hosting them via HTTP or uploading raw binary.
- `doorRight`: Assign the correct door number and access plan.
- This setup allows bulk provisioning of multiple users across multiple devices.
- For production, consider adding logging and error handling per device.
- Optional: Extend the script to upload raw face images if you do not want to host them externally.

---

This script is suitable for integration into a **timekeeping system** that uses Hikvision ACS face recognition devices.

