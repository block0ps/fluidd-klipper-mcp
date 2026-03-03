# Fluidd / Klipper 3D Printer MCP Server

An MCP (Model Context Protocol) server that gives AI assistants (Claude, etc.) full
real-time control and visibility over 3D printers running **Klipper firmware** via the
**Fluidd / Moonraker** API stack.

Also includes a standalone **local monitor server** (`monitor_server.py`) with a live
web UI and multi-channel alerting — push, SMS, email, and iMessage — that fires even
when the browser tab is closed.

---

## Features

### 📊 Real-Time Monitoring
| Tool | Description |
|------|-------------|
| `get_printer_status` | Full real-time status — state, temps, progress, ETA, position |
| `get_temperatures` | Hotend, bed, and all extra sensor temperatures |
| `get_print_job_status` | Current job file, layer, progress, elapsed & ETA |
| `get_klippy_status` | Klippy firmware health and error messages |
| `get_moonraker_status` | Moonraker API server health |
| `get_active_alerts` | All active alerts in one call |
| `get_printer_logs` | Recent Klippy log lines with error highlighting |

### 🚨 Failure Detection & Alerts
| Tool | Description |
|------|-------------|
| `check_failure_detection` | Heuristic checks for spaghetti, layer shifts, thermal anomalies, under-extrusion, and stalls |
| `get_active_alerts` | Continuous alert summary — thermal runaway, Klippy errors, clog detection |

> **Camera integration**: Use `get_camera_snapshot_url` to retrieve live snapshot/stream
> URLs from Fluidd-configured webcams. For AI-powered visual spaghetti detection,
> pipe the snapshot URL to a vision model (e.g., Claude's image analysis).

### 📷 Camera
| Tool | Description |
|------|-------------|
| `get_camera_snapshot_url` | Snapshot + stream URL(s) for all configured webcams |

### 🖨️ Print Job Management
| Tool | Description |
|------|-------------|
| `start_print` | Start printing a file from storage |
| `pause_print` | Pause the current print |
| `resume_print` | Resume a paused print |
| `cancel_print` | Cancel the current print |
| `emergency_stop` | Immediate M112 hardware stop |

### 📋 Queue & File Management
| Tool | Description |
|------|-------------|
| `get_print_queue` | View the Moonraker job queue |
| `add_to_queue` | Add a file to the print queue |
| `remove_from_queue` | Remove a job by ID |
| `list_print_files` | Browse files on printer storage |
| `get_print_history` | Recent job history with durations and results |

### 💰 Cost & Profitability
| Tool | Description |
|------|-------------|
| `calculate_print_cost` | Calculate material + power cost and recommended sale price with configurable markup |

### ⚙️ Printer Control
| Tool | Description |
|------|-------------|
| `set_temperature` | Set hotend or bed temperature |
| `send_gcode` | Send any raw G-code command |
| `restart_klippy` | Restart Klippy service |
| `restart_firmware` | Firmware restart |

---

## Prerequisites

- Docker Desktop with MCP Toolkit enabled
- A 3D printer running **Klipper** + **Moonraker** (exposed on your network)
- Fluidd or Mainsail as the front-end (optional — Moonraker API is the interface)
- Moonraker API key (if authentication is enabled)
- Python 3.7+ (for the monitor server — no extra packages needed)

---

## Quick Start — MCP Server (Claude Desktop)

### ⚡ Automated Setup (Recommended)

Run the interactive setup script — it handles all steps below, asks for confirmation
before making changes, and works on macOS, Linux, and Windows (Git Bash).

```bash
git clone https://github.com/block0ps/fluidd-klipper-mcp.git
cd fluidd-klipper-mcp
chmod +x setup.sh
./setup.sh
```

Or via one-liner (no clone needed):

```bash
curl -fsSL https://raw.githubusercontent.com/block0ps/fluidd-klipper-mcp/main/setup.sh | bash
```

---

### Manual Setup

#### 1. Clone & Build

```bash
git clone https://github.com/block0ps/fluidd-klipper-mcp.git
cd fluidd-klipper-mcp
docker build -t fluidd-klipper-mcp-server .
```

#### 2. Set Secrets

```bash
docker mcp secret set PRINTER_HOST="http://192.168.1.100"
docker mcp secret set PRINTER_API_TOKEN="your-moonraker-api-key"

# Cost calculation config (optional — defaults shown)
docker mcp secret set FILAMENT_COST_PER_KG="25.0"
docker mcp secret set POWER_COST_PER_KWH="0.12"
docker mcp secret set PRINTER_WATTS="150.0"
docker mcp secret set MARKUP_PERCENTAGE="30.0"

docker mcp secret list
```

#### 3. Register in Docker MCP Catalog

```bash
mkdir -p ~/.docker/mcp/catalogs
cp custom.yaml ~/.docker/mcp/catalogs/custom.yaml
```

Add to `~/.docker/mcp/registry.yaml` under the `registry:` key:

```yaml
registry:
  fluidd-klipper:
    ref: ""
```

#### 4. Configure Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcp-toolkit-gateway": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "/Users/YOUR_USERNAME/.docker/mcp:/mcp",
        "docker/mcp-gateway",
        "--catalog=/mcp/catalogs/docker-mcp.yaml",
        "--catalog=/mcp/catalogs/custom.yaml",
        "--config=/mcp/config.yaml",
        "--registry=/mcp/registry.yaml",
        "--tools-config=/mcp/tools.yaml",
        "--transport=stdio"
      ]
    }
  }
}
```

#### 5. Restart Claude Desktop

---

## 🖥️ Live Monitor & Alerting (`monitor_server.py`)

A standalone Python server that runs locally, serves a live web UI, and dispatches
alerts across four channels when anomalies are detected. **Polling runs on a background
thread — alerts fire even when the browser tab is closed or your screen is locked.**

### Why a separate server?

Browsers block direct `fetch()` calls from `file://` or `https://` pages to local
`http://` printer IPs (CORS + mixed content policy). `monitor_server.py` acts as a
local proxy: the browser talks to `localhost`, and Python forwards requests to
Moonraker server-side where CORS rules don't apply.

### Starting the monitor

```bash
# Uses default printer at http://10.0.107.158, port 8484
python3 monitor_server.py

# Custom printer URL
python3 monitor_server.py http://192.168.1.100

# Custom printer URL and port
python3 monitor_server.py http://192.168.1.100 9000
```

Then open **http://localhost:8484** in your browser.

No dependencies beyond Python's standard library — nothing to `pip install`.

### What it monitors

| Check | Threshold |
|-------|-----------|
| 🌡️ Thermal anomaly — hotend | Actual vs target divergence > 20°C |
| 🌡️ Thermal anomaly — bed | Actual vs target divergence > 15°C |
| 🔴 Klippy not ready | Any non-`ready` firmware state |
| 🧵 Clog / under-extrusion | < 5mm filament used after 5+ min printing |
| ⏸️ Print stall | No progress after 10+ min printing |
| 📐 Z position anomaly | Z < 0.1mm after 2+ min printing |
| 🛑 Print error / cancelled | State change to `error` or `cancelled` |
| 🎉 Print complete | State change to `complete` |

### Configuring alert channels

On first run, `monitor_server.py` creates **`monitor_config.json`** in the same
directory. Open it, set `"enabled": true` for each channel you want, fill in the
credentials, then restart the server.

```json
{
  "printer_host": "http://10.0.107.158",
  "poll_interval_seconds": 1800,
  "alert_on_warnings": true,

  "ntfy": {
    "enabled": false,
    "topic": "my-printer-alerts",
    "server": "https://ntfy.sh"
  },
  "twilio": {
    "enabled": false,
    "account_sid": "",
    "auth_token": "",
    "from_number": "+1XXXXXXXXXX",
    "to_number": "+1XXXXXXXXXX"
  },
  "email": {
    "enabled": false,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "username": "you@gmail.com",
    "password": "your-app-password",
    "from_address": "you@gmail.com",
    "to_address": "you@gmail.com"
  },
  "imessage": {
    "enabled": false,
    "to_number": "+1XXXXXXXXXX"
  }
}
```

---

### 📱 Push Notifications — ntfy.sh (free, no account)

[ntfy.sh](https://ntfy.sh) is a free, open-source push notification service.

1. Install the **ntfy** app on your phone ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy))
2. In the app, tap **Subscribe to topic** and enter a unique topic name (e.g. `mahdi-printer-9482`) — treat it like a password, anyone who knows it can send to it
3. In `monitor_config.json`:

```json
"ntfy": {
  "enabled": true,
  "topic": "mahdi-printer-9482",
  "server": "https://ntfy.sh"
}
```

Alerts arrive instantly on your phone with priority levels — critical alerts are marked urgent and bypass Do Not Disturb.

**Self-hosting**: If you prefer to run your own ntfy server, change `"server"` to your instance URL.

---

### 💬 SMS — Twilio

1. Sign up at [twilio.com](https://twilio.com) (free trial includes test credits)
2. From the Twilio Console, copy your **Account SID** and **Auth Token**
3. Get a Twilio phone number (free with trial)
4. In `monitor_config.json`:

```json
"twilio": {
  "enabled": true,
  "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "auth_token": "your_auth_token",
  "from_number": "+15551234567",
  "to_number": "+15559876543"
}
```

> **Trial accounts**: Twilio trial numbers can only send to verified phone numbers.
> Verify your number at Console → Phone Numbers → Verified Caller IDs.

---

### 📧 Email — SMTP (Gmail)

1. In your Google Account, go to **Security → 2-Step Verification** and enable it
2. Then go to **Security → App Passwords**, create a new app password for "Mail"
3. Copy the 16-character password (spaces don't matter)
4. In `monitor_config.json`:

```json
"email": {
  "enabled": true,
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "username": "you@gmail.com",
  "password": "abcd efgh ijkl mnop",
  "from_address": "you@gmail.com",
  "to_address": "you@gmail.com"
}
```

**Other providers**: Change `smtp_host` and `smtp_port` for Outlook (`smtp.office365.com`, 587), Yahoo (`smtp.mail.yahoo.com`, 587), or any other SMTP provider.

---

### 💬 iMessage — macOS only, no setup required

Uses macOS AppleScript to send iMessages via the Messages app already on your Mac.
The Mac running `monitor_server.py` must be signed into iMessage.

```json
"imessage": {
  "enabled": true,
  "to_number": "+15551234567"
}
```

`to_number` accepts a phone number (`+15551234567`) or an Apple ID email address (`you@icloud.com`).

> **Note**: macOS may prompt you to grant Terminal (or your Python app) access to
> Messages the first time. Accept the permission request in System Settings → Privacy & Security → Automation.

---

### Testing alerts

Once configured, use the **🔔 Test Alerts** button in the web UI to fire a test
message across all enabled channels before walking away from the printer.

You can also trigger an immediate poll (without waiting for the interval) using the
**🔄 Poll Now** button.

---

### Alert deduplication

Each unique alert fires **once per print job** — you won't get spammed with repeated
SMS messages if a thermal anomaly persists across multiple poll cycles. The dedup set
resets automatically when a new print job starts.

---

### Adjusting the poll interval

Change `poll_interval_seconds` in `monitor_config.json` and restart the server.
Recommended values:

| Print type | Suggested interval |
|------------|--------------------|
| Short print (< 2h) | 300 (5 min) |
| Overnight print | 900 (15 min) |
| Multi-day print | 1800 (30 min) |

---

## Usage Examples (Claude Desktop)

Once the MCP server is configured, ask Claude:

- *"What's my printer doing right now?"*
- *"Are there any alerts or errors on the printer?"*
- *"Show me my print queue"*
- *"Start printing benchy.gcode"*
- *"Pause the current print"*
- *"Check for spaghetti or layer shifts"*
- *"What's the camera snapshot URL?"*
- *"How much did this print cost? What should I charge?"*
- *"Set the bed to 60°C"*
- *"Show me the last 20 log lines"*
- *"Restart Klippy"*

---

## Cost Calculation

The `calculate_print_cost` tool uses these configurable parameters:

| Env Var | Default | Description |
|---------|---------|-------------|
| `FILAMENT_COST_PER_KG` | `25.0` | USD per kilogram of filament |
| `POWER_COST_PER_KWH` | `0.12` | USD per kWh electricity |
| `PRINTER_WATTS` | `150.0` | Average power draw of your printer |
| `MARKUP_PERCENTAGE` | `30.0` | Profit margin % on top of costs |

---

## Failure Detection

The `check_failure_detection` and `get_active_alerts` tools use heuristics against
live Moonraker data (same logic as the monitor server):

| Detection | Method |
|-----------|--------|
| **Thermal anomaly** | Heater actual vs target divergence > 15°C (bed) or 20°C (hotend) |
| **Under-extrusion / clog** | Very low filament usage after 5+ min of printing |
| **Layer shift / stall** | No Z progress after 10+ min of printing |
| **Z position anomaly** | Z < 0.1 mm after 2+ min |
| **Spaghetti (visual)** | Pull camera snapshot URL → analyze with vision AI |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Your Network                       │
│                                                      │
│  Claude Desktop                                      │
│       │                                              │
│       ▼                                              │
│  MCP Gateway (Docker)                                │
│       │                                              │
│       ▼                                              │
│  fluidd-klipper-mcp-server (Docker)                  │
│       │                          ┌─────────────────┐ │
│       │    monitor_server.py ────┤  Background     │ │
│       │    (localhost:8484)      │  Poll Thread    │ │
│       │         │                └────────┬────────┘ │
│       │         │  Alerts                 │          │
│       │         ├──── 📱 ntfy push        │          │
│       │         ├──── 💬 SMS (Twilio)     │          │
│       │         ├──── 📧 Email (SMTP)     │          │
│       │         └──── 💬 iMessage         │          │
│       │                                  │          │
│       └──────────────────────────────────┘          │
│                        │                             │
│                        ▼ HTTP/REST                   │
│              Moonraker API (printer)                 │
│                        │                             │
│                        ▼                             │
│              Klipper Firmware + Fluidd               │
└─────────────────────────────────────────────────────┘
```

---

## Development

### Local Testing

```bash
export PRINTER_HOST="http://192.168.1.100"
export PRINTER_API_TOKEN="your-key"
python3 fluidd_klipper_server.py

# Test MCP protocol
echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | python3 fluidd_klipper_server.py
```

### Adding New Tools

1. Add a new `@mcp.tool()` function to `fluidd_klipper_server.py`
2. Keep the docstring to a single line
3. Default all parameters to `""` (empty string)
4. Always return a string
5. Add the tool name to `custom.yaml`
6. Rebuild: `docker build -t fluidd-klipper-mcp-server .`

---

## Security

- All secrets stored in Docker Desktop secrets — never hardcoded
- Container runs as non-root user (`mcpuser`)
- Sensitive values (API tokens) are never logged
- `monitor_config.json` stores credentials locally — keep it out of version control (it's in `.gitignore`)
- Emergency stop is intentionally available — treat chat access to this server with care

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Connection refused` on MCP server | Verify `PRINTER_HOST` is reachable from Docker network |
| `401 Unauthorized` on MCP server | Set `PRINTER_API_TOKEN` via Docker secrets |
| Tools not appearing in Claude | Rebuild Docker image, restart Claude Desktop |
| Monitor shows "proxy error" | Ensure `monitor_server.py` is running (`python3 monitor_server.py`) |
| Push alerts not arriving | Check ntfy topic name matches app subscription; try **🔔 Test Alerts** |
| SMS not sending | Verify Twilio trial number is verified; check account SID / auth token |
| Email not sending | Use an App Password (not your Gmail login password); ensure 2FA is on |
| iMessage permission denied | Grant Terminal access in System Settings → Privacy & Security → Automation |
| Alerts firing repeatedly | Dedup resets per print job — check if a new job started |

---

## License

MIT
