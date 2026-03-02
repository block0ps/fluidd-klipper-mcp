# Fluidd / Klipper 3D Printer MCP Server

An MCP (Model Context Protocol) server that gives AI assistants (Claude, etc.) full
real-time control and visibility over 3D printers running **Klipper firmware** via the
**Fluidd / Moonraker** API stack.

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

---

## Quick Start

### 1. Clone & Build

```bash
git clone https://github.com/YOUR_USERNAME/fluidd-klipper-mcp.git
cd fluidd-klipper-mcp
docker build -t fluidd-klipper-mcp-server .
```

### 2. Set Secrets

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

### 3. Register in Docker MCP Catalog

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

### 4. Configure Claude Desktop

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

### 5. Restart Claude Desktop

---

## Usage Examples

Once configured, ask Claude:

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
live Moonraker data:

| Detection | Method |
|-----------|--------|
| **Thermal anomaly** | Heater actual vs target divergence > 15°C (bed) or 20°C (hotend) |
| **Under-extrusion / clog** | Very low filament usage after 5+ min of printing |
| **Layer shift / stall** | No Z progress after 10+ min of printing |
| **Z position anomaly** | Z < 0.1 mm after 2+ min |
| **Spaghetti (visual)** | Pull camera snapshot URL → analyze with vision AI |

For full AI spaghetti detection, get the snapshot URL and pass the image to a vision
model (Claude, GPT-4V, etc.) with a prompt like *"Is this 3D print failing?"*

---

## Architecture

```
Claude Desktop
     │
     ▼
MCP Gateway (Docker)
     │
     ▼
fluidd-klipper-mcp-server (Docker container)
     │
     ▼ HTTP/REST
Moonraker API (on printer host)
     │
     ▼
Klipper Firmware + Fluidd UI
```

---

## Development

### Local Testing

```bash
export PRINTER_HOST="http://192.168.1.100"
export PRINTER_API_TOKEN="your-key"
python fluidd_klipper_server.py

# Test MCP protocol
echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | python fluidd_klipper_server.py
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
- Emergency stop is intentionally available — treat chat access to this server with care

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `Connection refused` | Verify `PRINTER_HOST` is reachable from Docker network |
| `401 Unauthorized` | Set `PRINTER_API_TOKEN` via Docker secrets |
| Tools not appearing | Rebuild Docker image, restart Claude Desktop |
| Logs show no output | Check `docker logs <container>` |

---

## License

MIT
