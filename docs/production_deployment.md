# Hikvision Listening Server - Production Deployment Guide

## Overview

This document outlines the considerations and steps for migrating the Hikvision listening server from a local LAN environment to a production server with a public IP address.

## Architecture

### Current Setup (LAN)
- **Device → Server (Events)**: Hikvision devices send HTTP POST requests to the listening server
- **Server → Device (Provisioning)**: Server sends ISAPI requests to provision users to devices
- Both server and devices are on the same local network (192.168.x.x)

### Production Setup
- **Device → Server (Events)**: Devices connect to server's public IP over the internet
- **Server → Device (Provisioning)**: Requires additional network configuration (VPN, port forwarding, etc.)

## Device → Server Communication (Event Reception)

### Requirements

For device-to-server event reception, a **public IP address on the server is sufficient**.

#### How It Works
1. The Hikvision device initiates an **outbound** HTTP POST connection to the server
2. The device can be behind NAT/firewall - this is fine since it's an outbound connection
3. The server receives the connection on its public IP address

#### What You Need
- ✅ Server has a public IP address (or domain name)
- ✅ Server firewall allows inbound connections on the listening port
- ✅ Device is configured with the server's public IP/domain
- ✅ Device network allows outbound HTTP connections (usually enabled by default)
- ✅ FastAPI server listens on `0.0.0.0` (all interfaces), not just `127.0.0.1`

### Device Configuration Options

#### Option 1: Using Domain Name (Recommended)
```
Server URL: https://hikvision.yourdomain.com/hikvision/events
Port: 443 (HTTPS) or 80 (HTTP)
```

**Advantages:**
- More flexible (IP can change without device reconfiguration)
- SSL certificates work properly
- Professional appearance

#### Option 2: Using IP Address Only
```
Server IP: 203.0.113.45 (your public IP)
Port: 80 (if behind nginx) or 8000 (direct access)
Path: /hikvision/events
Full URL: http://203.0.113.45/hikvision/events
```

**Considerations:**
- Works perfectly fine for functionality
- Less flexible if IP changes
- HTTPS requires IP-based SSL certificate (less common)
- HTTP is typically sufficient for device communication

## Cloud Panel Setup

### Prerequisites
- Server with Cloud Panel installed
- Public IP address assigned to server
- Existing nginx configuration for other sites

### Step 1: Create New Python Website

1. **Log into Cloud Panel**
2. **Create a new Python website/app** with the following settings:
   - **Domain/Subdomain**: Use a subdomain (e.g., `hikvision.yourdomain.com`) or dedicated domain
   - **Python Version**: Python 3.9+ (3.10 or 3.11 recommended)
   - **Application Type**: FastAPI (or Python/WSGI if FastAPI option not available)
   - **Working Directory**: Directory containing `main.py`, `config/`, `faces/`, `templates/`

### Step 2: Application Configuration

**Startup File/Entry Point:**
- Point to `main.py`

**Port:**
- Cloud Panel will assign a port (e.g., 8000, 8001)
- nginx will automatically proxy requests to this port

**Dependencies:**
- Cloud Panel should auto-detect `requirements.txt` and install:
  - `fastapi>=0.104.0`
  - `uvicorn[standard]>=0.24.0`
  - `python-multipart>=0.0.6`
  - `jinja2>=3.1.2`
  - `requests>=2.31.0`

**Startup Command** (if Cloud Panel requires it):
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```
(Adjust port number to what Cloud Panel assigns)

### Step 3: Path Routing

The application uses a catch-all POST route (`/{full_path:path}`), which means:
- ✅ `/hikvision/events` - Works
- ✅ `/api/hikvision/events` - Works
- ✅ Any path the device sends - Works

Cloud Panel's nginx will automatically proxy all requests to your FastAPI application.

### Step 4: Static Files

The `/faces/` directory is handled by FastAPI's StaticFiles mount. Cloud Panel's nginx configuration should automatically handle this through the proxy.

## Device Configuration

### If Device Supports Domain Names
```
Server URL: https://hikvision.yourdomain.com/hikvision/events
```

### If Device Only Accepts IP Address

**Through nginx (Standard Setup):**
```
Server IP: 203.0.113.45
Port: 80 (or leave blank if default)
Path: /hikvision/events
Full URL: http://203.0.113.45/hikvision/events
```

**Direct Port Access (if Cloud Panel exposes specific port):**
```
Server IP: 203.0.113.45
Port: 8000 (or whatever Cloud Panel assigns)
Path: /hikvision/events
Full URL: http://203.0.113.45:8000/hikvision/events
```

## Testing

### Verify Server is Running
```bash
curl -X POST http://your-server-ip/hikvision/events
```

### Check Logs
- Event logs: `hikvision_events_{date}.log`
- Access logs: `Access Log {date}.log`

## Important Considerations

### 1. Firewall Configuration
- Ensure server firewall allows inbound connections on:
  - Port 80 (HTTP) - if using nginx
  - Port 443 (HTTPS) - if using SSL
  - Port 8000+ (direct FastAPI access) - if not using nginx proxy

### 2. SSL/HTTPS
- **With Domain Name**: Standard SSL certificates work (Let's Encrypt, etc.)
- **With IP Address Only**: Requires IP-based SSL certificate (less common) or use HTTP
- HTTP is typically sufficient for device communication

### 3. IP Address Stability
- **Static IP**: Ideal for production
- **Dynamic IP**: May require device reconfiguration if IP changes

### 4. Network Reliability
- Internet connections are less stable than LAN
- Consider increasing timeout values if needed (currently 10 seconds for provisioning)

### 5. Security Considerations
- The catch-all POST route accepts any path
- Consider adding:
  - Path validation for known Hikvision paths
  - Authentication/authorization for event endpoints
  - Rate limiting to prevent abuse

## Server → Device Communication (Provisioning)

**Note**: This document focuses on device → server communication (event reception). 

For server → device communication (user provisioning), additional network configuration is required:
- **VPN**: Connect PROD server to device network
- **Port Forwarding**: Forward public ports to device private IPs
- **Alternative Architecture**: Device-initiated provisioning pull

## Troubleshooting

### Device Cannot Connect
1. Verify server's public IP is correct
2. Check firewall allows inbound connections
3. Verify FastAPI is listening on `0.0.0.0`, not `127.0.0.1`
4. Test with `curl` from external network

### Events Not Received
1. Check application logs
2. Verify device configuration (IP, port, path)
3. Check nginx proxy configuration
4. Verify Cloud Panel Python app is running

### SSL Certificate Issues
- If using IP address, consider using HTTP instead of HTTPS
- If using domain name, ensure SSL certificate is properly configured

## Summary

✅ **For Device → Server (Events)**: Public IP on server is sufficient  
✅ **Works with Cloud Panel**: Standard Python website setup  
✅ **IP Address Only**: Fully supported, just less flexible than domain names  
✅ **Path Routing**: Catch-all route handles any path automatically  

The migration is straightforward - configure the device with the server's public IP and ensure firewall rules allow inbound connections.

