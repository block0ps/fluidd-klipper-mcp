# CLAUDE.md — Fluidd/Klipper MCP Server

## Project Overview

This is an MCP (Model Context Protocol) server built with **FastMCP** that wraps the
**Moonraker REST API** to give Claude (or any MCP-compatible AI) full remote control
and monitoring capability over 3D printers running Klipper firmware.

## Architecture

```
FastMCP (stdio transport)
  └── fluidd_klipper_server.py
        └── httpx async client → Moonraker REST API → Klipper firmware
```

All tools communicate with Moonraker over HTTP. Moonraker is the API layer that sits
between Fluidd/Mainsail and the Klipper firmware process.

## Key Files

| File | Purpose |
|------|---------|
| `fluidd_klipper_server.py` | Main MCP server — all tools live here |
| `Dockerfile` | Container definition (python:3.11-slim, non-root user) |
| `requirements.txt` | `mcp[cli]`, `httpx` |
| `custom.yaml` | Docker MCP catalog registration |
| `README.md` | End-user setup guide |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PRINTER_HOST` | `http://192.168.1.100` | Moonraker base URL |
| `PRINTER_API_TOKEN` | *(empty)* | Moonraker API key |
| `FILAMENT_COST_PER_KG` | `25.0` | USD/kg for cost calc |
| `POWER_COST_PER_KWH` | `0.12` | USD/kWh for cost calc |
| `PRINTER_WATTS` | `150.0` | Printer average draw |
| `MARKUP_PERCENTAGE` | `30.0` | Profit margin % |

## Tool Inventory (25 tools)

### Monitoring
- `get_printer_status` — Consolidated status (state, temps, progress, position)
- `get_temperatures` — All heater/sensor temps
- `get_print_job_status` — Job details + progress
- `get_klippy_status` — Klippy firmware health
- `get_moonraker_status` — Moonraker API health
- `get_active_alerts` — All active alerts + anomaly detection
- `get_printer_logs` — Klippy log tail with error highlighting
- `check_failure_detection` — Heuristic spaghetti/layer-shift/thermal checks

### Camera
- `get_camera_snapshot_url` — Returns snapshot + stream URLs from Moonraker webcam config

### Job Control
- `start_print` — `/printer/print/start`
- `pause_print` — `/printer/print/pause`
- `resume_print` — `/printer/print/resume`
- `cancel_print` — `/printer/print/cancel`
- `emergency_stop` — `/printer/emergency_stop`

### Queue & Files
- `get_print_queue` — `/server/job_queue/status`
- `add_to_queue` — `/server/job_queue/job` (POST)
- `remove_from_queue` — `/server/job_queue/job` (DELETE)
- `list_print_files` — `/server/files/list`
- `get_print_history` — `/server/history/list`

### Cost
- `calculate_print_cost` — Material + power cost, recommended price

### Control
- `set_temperature` — `SET_HEATER_TEMPERATURE` G-code
- `send_gcode` — `/printer/gcode/script`
- `restart_klippy` — `/printer/restart`
- `restart_firmware` — `/printer/firmware_restart`
- `list_available_tools` — Self-documentation

## MCP Rules Followed

- ✅ No `@mcp.prompt()` decorators
- ✅ No `prompt=` parameter in `FastMCP()`
- ✅ No `typing` module imports
- ✅ All params default to `""` (not `None`)
- ✅ All docstrings are single-line
- ✅ All tools return `str`
- ✅ Empty-string checks use `.strip()`
- ✅ Full error handling in every tool
- ✅ Logging to `stderr` only
- ✅ Docker container, non-root user

## Failure Detection Heuristics

The server implements lightweight, real-time heuristics (no camera vision required):

| Condition | Threshold |
|-----------|-----------|
| Thermal anomaly (bed) | `|actual - target| > 15°C` when target > 0 |
| Thermal anomaly (hotend) | `|actual - target| > 20°C` when target > 0 |
| Under-extrusion / clog | `filament_used < 5mm` after `print_duration > 300s` |
| Print stall | `progress < 0.001` after `print_duration > 600s` |
| Z anomaly / layer shift | `Z < 0.1mm` after `print_duration > 120s` |

For visual spaghetti detection, use `get_camera_snapshot_url` and pass the image
to a multimodal model.

## Extending This Server

1. Add a new async function decorated with `@mcp.tool()`
2. Single-line docstring only
3. All params typed as `str` with `= ""` default
4. Return a formatted `str`
5. Add the tool name to `custom.yaml` → `tools:` list
6. Run `docker build -t fluidd-klipper-mcp-server .`

## Testing Locally

```bash
export PRINTER_HOST="http://192.168.1.100"
python fluidd_klipper_server.py
```

MCP protocol test:
```bash
echo '{"jsonrpc":"2.0","method":"tools/list","id":1}' | python fluidd_klipper_server.py
```
