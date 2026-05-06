---
name: ring-api-connector
description: >
  Use when monitoring a Ring home security system — viewing devices, checking
  event history, managing motion settings, reviewing recordings, or controlling
  modes/sirens/floodlights via the Ring API HTTP endpoints.
---

# Ring API Connector

## Connection

| Variable | Purpose |
|----------|---------|
| `RING_API_URL` | Base URL for all API requests |

All paths below are relative to this URL.

## Endpoints

### Health

```
GET /health
```

### Devices

```
GET /clients_api/ring_devices
GET /clients_api/doorbots/{device_id}
GET /clients_api/doorbots/{device_id}/health
PUT /clients_api/doorbots/{device_id}/settings
```

**PUT body (update device settings):**

```json
{
  "motion_sensitivity": 9,
  "motion_detection_enabled": true,
  "people_detection_enabled": true,
  "package_detection_enabled": false,
  "led_status": "on",
  "light_schedule_enabled": true,
  "light_on_duration_seconds": 30
}
```

### Locations

```
GET /clients_api/locations/{location_id}
GET /clients_api/locations/{location_id}/devices
GET /clients_api/locations/{location_id}/mode
PUT /clients_api/locations/{location_id}/mode
```

**PUT body (set mode):**

```json
{
  "mode": "away"
}
```

Valid modes: `home`, `away`, `disarmed`.

### Active Dings

```
GET /clients_api/dings/active
```

### Event History

```
GET /clients_api/doorbots/{device_id}/history
GET /clients_api/dings/{event_id}
GET /clients_api/dings/{event_id}/recording
```

**Query params for GET history:**

| Parameter | Description |
|-----------|-------------|
| `kind` | Filter by event type: `motion`, `ding`, `person_detected`, `package_detected` |
| `date_from` | Filter from date (ISO format) |
| `date_to` | Filter to date (ISO format) |
| `limit` | Max results (1–100, default 20) |
| `offset` | Skip N results (default 0) |

### Recordings

```
GET /clients_api/doorbots/{device_id}/recordings
```

**Query params:**

| Parameter | Description |
|-----------|-------------|
| `date_from` | Filter from date (ISO format) |
| `date_to` | Filter to date (ISO format) |

### Shared Users

```
GET /clients_api/locations/{location_id}/users
GET /clients_api/locations/{location_id}/users/{user_id}
```

### Chime Settings

```
GET /clients_api/chimes/{device_id}/settings
PUT /clients_api/chimes/{device_id}/link
PUT /clients_api/chimes/{device_id}/unlink
```

**PUT body (link/unlink):**

```json
{
  "doorbell_id": 987001
}
```

### Motion Zones

```
GET /clients_api/doorbots/{device_id}/motion_zones
```

### Notification Preferences

```
GET /clients_api/notifications
GET /clients_api/notifications/{device_id}
PUT /clients_api/notifications/{device_id}
```

**PUT body (update notifications):**

```json
{
  "motion_alerts": true,
  "ding_alerts": true,
  "person_alerts": false,
  "package_alerts": true
}
```

### Siren

```
POST /clients_api/doorbots/{device_id}/siren_on
POST /clients_api/doorbots/{device_id}/siren_off
```

**POST body (siren_on):**

```json
{
  "duration_seconds": 30
}
```

### Floodlight

```
PUT /clients_api/doorbots/{device_id}/floodlight_light_on
```

**PUT body:**

```json
{
  "on": true
}
```

## Typical Workflow

1. `GET /health` to confirm the API is reachable.
2. `GET /clients_api/ring_devices` to list all devices and understand the setup.
3. `GET /clients_api/locations/loc_martinez_001` to load location context and current mode.
4. `GET /clients_api/doorbots/987001/history` to review recent events on the front door doorbell.
5. `GET /clients_api/dings/active` to check for currently active alerts (live motion or doorbell).
6. `GET /clients_api/dings/7001/recording` to get a recording URL for a specific event.
7. `GET /clients_api/doorbots/987005/health` to check device health (battery, WiFi signal).
8. `PUT /clients_api/locations/loc_martinez_001/mode` to change the security mode (home/away/disarmed).
9. `PUT /clients_api/doorbots/987003/settings` to adjust motion sensitivity on the driveway cam.
10. `GET /clients_api/notifications/987004` to verify notification preferences for a device.

## Bundled Resources

### Scripts

- **`scripts/fetch_ring_data.py`** — Helper script to list devices, check event history, view recordings, and inspect device health. Run `python3 scripts/fetch_ring_data.py --help` for usage.

### References

- **`references/ring-api-guide.md`** — Detailed endpoint reference with curl examples and common patterns.
