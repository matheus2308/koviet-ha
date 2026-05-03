# KOVIET AC — Home Assistant Integration

A custom Home Assistant integration for **KOVIET portable air conditioners** (and likely other brands built on the same **I4SEASON** platform, such as those using the `iotsapp.ikoviet.com` cloud backend).

Controls the AC as a standard `climate` entity: power, mode, target temperature, and fan speed. State updates arrive in real time via MQTT push — no polling.

> **Confirmed working:** KOVIET 12000 BTU WiFi Portable AC (model A1, firmware 1.0.13)
> Other I4SEASON-based models are likely compatible — see [Compatibility](#compatibility).

---

## Features

- Power on / off
- Modes: Cool, Dry, Fan Only
- Target temperature (°F, 60–86)
- Fan speed: Low / Medium / High
- Live room temperature sensor (pushed by device, no polling)
- Auto-reconnects if cloud connection drops

---

## Installation

### Option A — HACS (recommended)

1. In HACS → Integrations → ⋮ → Custom repositories
2. Add `https://github.com/matheus2308/koviet-ha` with category **Integration**
3. Click **Download** on the KOVIET AC card
4. Restart Home Assistant

### Option B — Manual

Copy the `custom_components/koviet_ac/` folder into your HA config directory:

```
/config/custom_components/koviet_ac/
```

Then restart Home Assistant.

---

## Configuration

You need three values before configuring: your **MQTT username**, **MQTT password**, and **device serial number**. See [Getting your credentials](#getting-your-credentials) below.

Add to `configuration.yaml`:

```yaml
climate:
  - platform: koviet_ac
    name: "Living Room AC"
    device_sn: "YOUR_DEVICE_SN"
    mqtt_username: "YOUR_MQTT_USERNAME"
    mqtt_password: "YOUR_MQTT_PASSWORD"
```

Restart HA. You'll have a `climate.living_room_ac` entity.

---

## Getting your credentials

The integration connects to KOVIET's cloud MQTT broker using per-account credentials that you extract once from the official app. This is a one-time process.

### What you need

- An Android phone (or emulator) with the **KOVIET** app installed and your AC already paired
- A computer on the same WiFi network
- Python 3 with `mitmproxy` installed (`pipx install mitmproxy`)

### Step 1 — Find your device serial number

Open the KOVIET app → tap your AC → tap the ⚙ icon → look for **SN** or **Serial Number**. It looks like `2K06A100M0XXXXXXXXXXXXXXXXXX` (alphanumeric, ~32 characters).

Alternatively, get it from adb after intercepting traffic (it appears in every MQTT topic).

### Step 2 — Intercept the app's API traffic with mitmproxy

**On your computer**, find your IP address and start the proxy:

```bash
ip addr show   # note your IP, e.g. 192.168.1.100
mitmproxy --listen-port 8080 --mode regular
```

**On your Android phone:**
1. Go to WiFi settings → long-press your network → Modify network
2. Set **Proxy** to Manual, host = your computer's IP, port = `8080`
3. Open a browser and go to `http://mitm.it` — install the mitmproxy certificate
4. Grant it trust under Settings → Security → Trusted credentials → User

**Back on your computer**, open the KOVIET app and watch mitmproxy. You will see requests to `iotsapp.ikoviet.com`. Look for:

```
GET /iot1/mqtt/userinfo
```

The response contains your MQTT credentials:

```json
{
  "username": "a1b2c3d4e5f6...",
  "password": "f6e5d4c3b2a1..."
}
```

You can also trigger this by opening the app while the proxy is running — it fetches credentials on startup.

### Step 3 — Verify the credentials work

```bash
pip install paho-mqtt
python3 - <<'EOF'
import paho.mqtt.client as mqtt, sys

USERNAME = "PASTE_USERNAME_HERE"
PASSWORD = "PASTE_PASSWORD_HERE"
SN       = "PASTE_SN_HERE"

def on_connect(c, u, f, rc, p=None):
    print(f"Connected: {rc}")
    c.subscribe(f"dev/I4SEASON/{SN}/command/reply")
    print("Listening — open the app and control the AC now")

def on_message(c, u, msg):
    print(f"{msg.topic}: {msg.payload.decode()}")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
c.username_pw_set(USERNAME, PASSWORD)
c.tls_set()
c.ws_set_options(path="/ws/iot1")
c.on_connect = on_connect
c.on_message = on_message
c.connect("iotsapp.ikoviet.com", 443)
c.loop_forever()
EOF
```

If you see device state messages, you're good to go.

### Credential lifetime

The credentials appear to be long-lived (static SHA1 hashes tied to your account/device, not session tokens). If they ever stop working, repeat Step 2 after logging out and back in to the app.

---

## Protocol reference

This section documents the MQTT protocol for developers and for porting to other devices.

### Broker

| Property | Value |
|---|---|
| Host | `iotsapp.ikoviet.com` |
| Port | `443` |
| Transport | MQTT over WebSocket (`wss://`) |
| WebSocket path | `/ws/iot1` |
| TLS | Yes (standard CA-signed cert) |
| Auth | Username + password (per-account, fetched from REST API) |

### Topics

```
dev/I4SEASON/{SN}/command/request   ← publish commands here (app→device)
dev/I4SEASON/{SN}/command/reply     ← subscribe for state (device→app)
```

### Commands

All messages are JSON.

#### Query current state (`cmd: 3`)

Request:
```json
{"cmd": 3, "user": "ha"}
```

Reply (full device state):
```json
{
  "cmd": 3,
  "sn": "YOUR_DEVICE_SN",
  "result": {
    "poweron": false,
    "mode": 3,
    "windlevel": 2,
    "templevel": 73,
    "temperature": 74,
    "sleep": false,
    "eco": false,
    "lighton": true,
    "muteon": false,
    "childlockon": false,
    "rh": 32,
    "tempunit": 1,
    "wrong": 0
  }
}
```

#### Set state (`cmd: 6`)

Send any subset of state fields:

```json
{"cmd": 6, "user": "ha", "data": {"state": {"poweron": true}}}
{"cmd": 6, "user": "ha", "data": {"state": {"mode": 1}}}
{"cmd": 6, "user": "ha", "data": {"state": {"templevel": 72}}}
{"cmd": 6, "user": "ha", "data": {"state": {"windlevel": 3}}}
```

#### State push from device (`cmd: 4`)

Sent by the device after each change (either from a command or physical button). Contains only the changed fields:

```json
{"cmd": 4, "result": {"poweron": true, "origin": 1}}
{"cmd": 4, "result": {"temperature": 73, "origin": 0}}
```

`origin: 1` = change triggered by app command; `origin: 0` = device-originated (e.g. temperature update).

### Field reference

| Field | Type | Values |
|---|---|---|
| `poweron` | bool | `true` / `false` |
| `mode` | int | `1`=Cool, `2`=Dry, `3`=Fan |
| `windlevel` | int | `1`=Low, `2`=Medium, `3`=High |
| `templevel` | int | Target temp in °F (60–86) |
| `temperature` | int | Current room temp in °F |
| `tempunit` | int | `1`=°F, `0`=°C (unverified) |
| `sleep` | bool | Sleep mode |
| `eco` | bool | Eco mode |
| `lighton` | bool | Display light |
| `muteon` | bool | Mute beep |
| `childlockon` | bool | Child lock |
| `rh` | int | Current relative humidity % |
| `wrong` | int | Error code (`0` = no error) |

### REST API (for reference)

The app also calls a REST API on the same host for login and device management. All endpoints are under `https://iotsapp.ikoviet.com/iot1/`.

| Method | Path | Description |
|---|---|---|
| POST | `/user/login` | Login with email + password → Bearer token |
| GET | `/device/list` | List paired devices |
| GET | `/mqtt/userinfo` | Get MQTT credentials for current account |
| GET | `/job/list` | Scheduled jobs / timers |

---

## Compatibility

This integration targets the **I4SEASON** IoT platform (`iotsapp.ikoviet.com`). Other brands that use this platform should work with the same integration and credentials extraction process.

If you have a different brand that uses the same platform, please open a [compatibility report](https://github.com/matheus2308/koviet-ha/issues/new?template=new_model.md) — even just to confirm it works.

Known to work: KOVIET A1 series.

Possibly compatible (same I4SEASON backend): other portable ACs sold under store brands that use an app with package name `com.i4season.*`.

---

## How this was reverse-engineered

The protocol was discovered through:

1. **Traffic interception** — mitmproxy captured the REST API calls made at app startup, revealing the login flow and the `/mqtt/userinfo` endpoint.

2. **Network capture** — `tcpdump` on the home router showed the device connecting to `47.253.105.139:443` (Alibaba Cloud). DNS capture revealed the hostname `iotsapp.ikoviet.com`.

3. **APK decompilation** — The app (EEUI/Weex framework, essentially JavaScript) was extracted via `adb pull` and the JS bundle searched for the WebSocket URL. Found: `wss://iotsapp.ikoviet.com/ws/iot1`.

4. **MQTT sniffing** — A Python MQTT client subscribed to the device topic while the app was used to control the AC, capturing all command types and payloads.

The device uses no local API — all control goes through the cloud broker. There is no known way to control it locally without intercepting and replaying MQTT messages through the cloud.

---

## Contributing

PRs welcome, especially for:

- Config flow (UI-based setup instead of YAML)
- Additional features: eco mode, sleep mode, child lock, display light
- Temperature unit auto-detection (°C support)
- Other I4SEASON-based AC models

---

## Disclaimer

This project is not affiliated with KOVIET, I4SEASON, or any related company. Use at your own risk. The integration communicates with KOVIET's cloud servers — it will stop working if they change their API or shut down the service.
