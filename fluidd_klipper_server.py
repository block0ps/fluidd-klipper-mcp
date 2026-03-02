#!/usr/bin/env python3
"""
Fluidd/Klipper 3D Printer Management MCP Server
"""
import os
import sys
import json
import logging
import asyncio
import base64
from datetime import datetime, timezone
import httpx
from mcp.server.fastmcp import FastMCP

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("fluidd-klipper-server")

# Initialize MCP server
mcp = FastMCP("fluidd-klipper")

# ── Configuration ────────────────────────────────────────────────────────────
PRINTER_HOST   = os.environ.get("PRINTER_HOST", "http://192.168.1.100")
PRINTER_TOKEN  = os.environ.get("PRINTER_API_TOKEN", "")
FILAMENT_COST  = float(os.environ.get("FILAMENT_COST_PER_KG", "25.0"))   # USD/kg
POWER_COST     = float(os.environ.get("POWER_COST_PER_KWH", "0.12"))     # USD/kWh
PRINTER_WATTS  = float(os.environ.get("PRINTER_WATTS", "150.0"))          # avg watts
MARKUP_PCT     = float(os.environ.get("MARKUP_PERCENTAGE", "30.0"))       # profit margin %

# ── Utility Functions ─────────────────────────────────────────────────────────

def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if PRINTER_TOKEN:
        h["X-Api-Key"] = PRINTER_TOKEN
    return h

async def _get(path: str, params: dict = None) -> dict:
    url = f"{PRINTER_HOST}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=_headers(), params=params or {})
        r.raise_for_status()
        return r.json()

async def _post(path: str, data: dict = None) -> dict:
    url = f"{PRINTER_HOST}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=_headers(), json=data or {})
        r.raise_for_status()
        return r.json()

async def _delete(path: str) -> dict:
    url = f"{PRINTER_HOST}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(url, headers=_headers())
        r.raise_for_status()
        return r.json()

def _fmt_time(seconds: float) -> str:
    if seconds < 0:
        return "N/A"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}h {m}m {s}s"

def _detect_anomalies(temps: dict, print_stats: dict) -> list:
    """Heuristic anomaly checks — returns list of warning strings."""
    warnings = []
    # Thermal runaway / anomaly
    bed_actual  = temps.get("heater_bed", {}).get("actual_temperature", 0)
    bed_target  = temps.get("heater_bed", {}).get ("target_temperature", 0)
    tool_actual = temps.get("extruder",   {}).get("actual_temperature", 0)
    tool_target = temps.get("extruder",   {}).get("target_temperature", 0)

    if bed_target > 0 and abs(bed_actual - bed_target) > 15:
        warnings.append(f"⚠️  THERMAL ANOMALY: Bed target {bed_target}°C but actual {bed_actual:.1f}°C")
    if tool_target > 0 and abs(tool_actual - tool_target) > 20:
        warnings.append(f"⚠️  THERMAL ANOMALY: Hotend target {tool_target}°C but actual {tool_actual:.1f}°C")

    # Stall / layer shift heuristic (print_duration stalled)
    state = print_stats.get("state", "")
    if state == "printing":
        progress = print_stats.get("progress", 0)
        if 0 < progress < 0.02:
            warnings.append("⚠️  POSSIBLE LAYER SHIFT / ADHESION FAILURE: Very low progress detected at print start")

    return warnings


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_printer_status(printer_host: str = "") -> str:
    """Get full real-time status of the 3D printer including temperatures, state, and progress."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_printer_status called")
    try:
        data = await _get("/printer/objects/query", {
            "params": (
                "print_stats extruder heater_bed toolhead "
                "fan virtual_sdcard display_status webhooks"
            )
        })
        s = data.get("result", {}).get("status", {})
        ps  = s.get("print_stats", {})
        ext = s.get("extruder", {})
        bed = s.get("heater_bed", {})
        th  = s.get("toolhead", {})
        vsd = s.get("virtual_sdcard", {})
        wh  = s.get("webhooks", {})

        state    = ps.get("state", "unknown").upper()
        filename = ps.get("filename", "N/A")
        progress = vsd.get("progress", 0) * 100
        elapsed  = ps.get("print_duration", 0)
        total    = ps.get("total_duration", 0)
        eta_sec  = (total - elapsed) if total > elapsed else -1

        anomalies = _detect_anomalies(s, ps)
        alert_block = ("\n\n🚨 ALERTS:\n" + "\n".join(anomalies)) if anomalies else ""

        return f"""📊 Printer Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
State      : {state}
Klippy     : {wh.get('state', 'unknown')}
File       : {filename}
Progress   : {progress:.1f}%
Elapsed    : {_fmt_time(elapsed)}
ETA        : {_fmt_time(eta_sec)}

🌡️  Temperatures
  Hotend   : {ext.get('temperature', 0):.1f}°C / {ext.get('target', 0):.1f}°C
  Bed      : {bed.get('temperature', 0):.1f}°C / {bed.get('target', 0):.1f}°C

🔧 Toolhead
  Position : X={th.get('position', [0,0,0,0])[0]:.1f} Y={th.get('position', [0,0,0,0])[1]:.1f} Z={th.get('position', [0,0,0,0])[2]:.1f}
  Speed    : {th.get('max_velocity', 0)} mm/s{alert_block}
"""
    except Exception as e:
        logger.error(f"get_printer_status error: {e}")
        return f"❌ Error fetching printer status: {str(e)}"


@mcp.tool()
async def get_temperatures(printer_host: str = "") -> str:
    """Get current hotend, bed, and chamber temperatures with history."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_temperatures called")
    try:
        data = await _get("/printer/objects/query", {
            "params": "extruder heater_bed temperature_sensor"
        })
        s   = data.get("result", {}).get("status", {})
        ext = s.get("extruder", {})
        bed = s.get("heater_bed", {})
        sensors = {k: v for k, v in s.items() if "temperature_sensor" in k}

        extra = ""
        for name, vals in sensors.items():
            extra += f"\n  {name}: {vals.get('temperature', 0):.1f}°C"

        return f"""🌡️  Temperature Report
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hotend   : {ext.get('temperature', 0):.1f}°C → target {ext.get('target', 0):.1f}°C  (power {ext.get('power', 0)*100:.0f}%)
Bed      : {bed.get('temperature', 0):.1f}°C → target {bed.get('target', 0):.1f}°C  (power {bed.get('power', 0)*100:.0f}%)
Extra Sensors:{extra if extra else ' None'}
"""
    except Exception as e:
        logger.error(f"get_temperatures error: {e}")
        return f"❌ Error fetching temperatures: {str(e)}"


@mcp.tool()
async def get_print_job_status(printer_host: str = "") -> str:
    """Get current print job details including file, progress, and timing."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_print_job_status called")
    try:
        data = await _get("/printer/objects/query", {"params": "print_stats virtual_sdcard display_status"})
        s  = data.get("result", {}).get("status", {})
        ps = s.get("print_stats", {})
        vsd= s.get("virtual_sdcard", {})
        ds = s.get("display_status", {})

        progress = vsd.get("progress", 0) * 100
        elapsed  = ps.get("print_duration", 0)
        total    = ps.get("total_duration", 0)
        eta_sec  = max(0, total - elapsed)
        layer    = ds.get("message", "")

        return f"""🖨️  Print Job Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
File       : {ps.get('filename', 'N/A')}
State      : {ps.get('state', 'N/A').upper()}
Progress   : {progress:.1f}%
Layer Info : {layer or 'N/A'}
Elapsed    : {_fmt_time(elapsed)}
ETA        : {_fmt_time(eta_sec)}
Filament   : {ps.get('filament_used', 0):.1f} mm used
"""
    except Exception as e:
        logger.error(f"get_print_job_status error: {e}")
        return f"❌ Error fetching job status: {str(e)}"


@mcp.tool()
async def start_print(filename: str = "", printer_host: str = "") -> str:
    """Start printing a file from the printer's SD card / storage."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    if not filename.strip():
        return "❌ Error: filename is required"
    logger.info(f"start_print: {filename}")
    try:
        data = await _post("/printer/print/start", {"filename": filename.strip()})
        return f"✅ Print started: {filename}\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"start_print error: {e}")
        return f"❌ Error starting print: {str(e)}"


@mcp.tool()
async def pause_print(printer_host: str = "") -> str:
    """Pause the currently running print job."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("pause_print called")
    try:
        data = await _post("/printer/print/pause")
        return f"⏸️  Print paused successfully.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"pause_print error: {e}")
        return f"❌ Error pausing print: {str(e)}"


@mcp.tool()
async def resume_print(printer_host: str = "") -> str:
    """Resume a paused print job."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("resume_print called")
    try:
        data = await _post("/printer/print/resume")
        return f"▶️  Print resumed successfully.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"resume_print error: {e}")
        return f"❌ Error resuming print: {str(e)}"


@mcp.tool()
async def cancel_print(printer_host: str = "") -> str:
    """Cancel the current print job."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("cancel_print called")
    try:
        data = await _post("/printer/print/cancel")
        return f"🛑 Print cancelled.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"cancel_print error: {e}")
        return f"❌ Error cancelling print: {str(e)}"


@mcp.tool()
async def emergency_stop(printer_host: str = "") -> str:
    """Trigger an emergency stop on the printer (M112 — halts all motion immediately)."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.warning("EMERGENCY STOP TRIGGERED")
    try:
        data = await _post("/printer/emergency_stop")
        return f"🚨 EMERGENCY STOP EXECUTED.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"emergency_stop error: {e}")
        return f"❌ Error executing emergency stop: {str(e)}"


@mcp.tool()
async def list_print_files(path: str = "", printer_host: str = "") -> str:
    """List files available to print on the printer storage."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info(f"list_print_files path={path}")
    try:
        params = {"path": path.strip() if path.strip() else "gcodes"}
        data = await _get("/server/files/list", params)
        files = data.get("result", [])
        if not files:
            return "📁 No files found in storage."
        lines = [f"📁 Files in '{params['path']}':", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for f in files[:50]:
            size_kb = f.get("size", 0) / 1024
            modified = datetime.fromtimestamp(f.get("modified", 0)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  • {f.get('filename', 'N/A'):40s}  {size_kb:8.1f} KB  {modified}")
        if len(files) > 50:
            lines.append(f"  ... and {len(files)-50} more files")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"list_print_files error: {e}")
        return f"❌ Error listing files: {str(e)}"


@mcp.tool()
async def get_print_history(limit: str = "10", printer_host: str = "") -> str:
    """Get the recent print job history with results and durations."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info(f"get_print_history limit={limit}")
    try:
        n = int(limit.strip()) if limit.strip().isdigit() else 10
        data = await _get("/server/history/list", {"limit": n, "order": "desc"})
        jobs = data.get("result", {}).get("jobs", [])
        if not jobs:
            return "📋 No print history found."
        lines = ["📋 Print History", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for j in jobs:
            status  = j.get("status", "N/A")
            fname   = j.get("filename", "N/A")
            elapsed = _fmt_time(j.get("print_duration", 0))
            started = datetime.fromtimestamp(j.get("start_time", 0)).strftime("%Y-%m-%d %H:%M")
            fil_used= j.get("filament_used", 0)
            icon    = "✅" if status == "completed" else ("❌" if status == "error" else "⚠️")
            lines.append(f"  {icon} [{started}] {fname}")
            lines.append(f"      Duration: {elapsed}  |  Filament: {fil_used:.1f} mm  |  Status: {status}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_print_history error: {e}")
        return f"❌ Error fetching history: {str(e)}"


@mcp.tool()
async def get_print_queue(printer_host: str = "") -> str:
    """Get the current Moonraker print queue."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_print_queue called")
    try:
        data = await _get("/server/job_queue/status")
        result = data.get("result", {})
        queue  = result.get("queued_jobs", [])
        state  = result.get("queue_state", "N/A")
        if not queue:
            return f"📋 Print Queue\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nState: {state}\nQueue is empty."
        lines = [f"📋 Print Queue (state: {state})", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for i, job in enumerate(queue, 1):
            lines.append(f"  {i}. {job.get('filename', 'N/A')}  [ID: {job.get('job_id', 'N/A')}]")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"get_print_queue error: {e}")
        return f"❌ Error fetching queue: {str(e)}"


@mcp.tool()
async def add_to_queue(filename: str = "", printer_host: str = "") -> str:
    """Add a file to the Moonraker print queue."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    if not filename.strip():
        return "❌ Error: filename is required"
    logger.info(f"add_to_queue: {filename}")
    try:
        data = await _post("/server/job_queue/job", {"filenames": [filename.strip()]})
        return f"✅ Added to queue: {filename}\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"add_to_queue error: {e}")
        return f"❌ Error adding to queue: {str(e)}"


@mcp.tool()
async def remove_from_queue(job_id: str = "", printer_host: str = "") -> str:
    """Remove a job from the print queue by job ID."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    if not job_id.strip():
        return "❌ Error: job_id is required (get it from get_print_queue)"
    logger.info(f"remove_from_queue: {job_id}")
    try:
        data = await _delete(f"/server/job_queue/job?job_ids={job_id.strip()}")
        return f"✅ Removed job {job_id} from queue.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"remove_from_queue error: {e}")
        return f"❌ Error removing from queue: {str(e)}"


@mcp.tool()
async def set_temperature(heater: str = "extruder", target: str = "0", printer_host: str = "") -> str:
    """Set temperature for a heater — heater can be 'extruder' or 'heater_bed'."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    if not heater.strip():
        return "❌ Error: heater name is required (extruder or heater_bed)"
    try:
        temp = float(target.strip()) if target.strip() else 0.0
    except ValueError:
        return f"❌ Error: invalid target temperature: {target}"
    logger.info(f"set_temperature {heater}={temp}")
    try:
        gcode = f"SET_HEATER_TEMPERATURE HEATER={heater.strip()} TARGET={temp}"
        data  = await _post("/printer/gcode/script", {"script": gcode})
        return f"🌡️  Set {heater} to {temp}°C\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"set_temperature error: {e}")
        return f"❌ Error setting temperature: {str(e)}"


@mcp.tool()
async def send_gcode(command: str = "", printer_host: str = "") -> str:
    """Send a raw G-code command to the printer."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    if not command.strip():
        return "❌ Error: command is required"
    logger.info(f"send_gcode: {command}")
    try:
        data = await _post("/printer/gcode/script", {"script": command.strip()})
        return f"⚡ G-code sent: {command}\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"send_gcode error: {e}")
        return f"❌ Error sending G-code: {str(e)}"


@mcp.tool()
async def get_klippy_status(printer_host: str = "") -> str:
    """Get Klippy firmware status and any error messages."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_klippy_status called")
    try:
        data = await _get("/printer/objects/query", {"params": "webhooks"})
        wh   = data.get("result", {}).get("status", {}).get("webhooks", {})
        state      = wh.get("state", "unknown")
        state_msg  = wh.get("state_message", "")
        icon = "✅" if state == "ready" else "❌"
        return f"""{icon} Klippy Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
State   : {state.upper()}
Message : {state_msg or 'No message'}
"""
    except Exception as e:
        logger.error(f"get_klippy_status error: {e}")
        return f"❌ Error fetching Klippy status: {str(e)}"


@mcp.tool()
async def restart_klippy(printer_host: str = "") -> str:
    """Restart the Klippy firmware service on the printer host."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("restart_klippy called")
    try:
        data = await _post("/printer/restart")
        return f"🔄 Klippy restart initiated.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"restart_klippy error: {e}")
        return f"❌ Error restarting Klippy: {str(e)}"


@mcp.tool()
async def restart_firmware(printer_host: str = "") -> str:
    """Perform a firmware restart (FIRMWARE_RESTART) on the printer."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("restart_firmware called")
    try:
        data = await _post("/printer/firmware_restart")
        return f"🔄 Firmware restart initiated.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        logger.error(f"restart_firmware error: {e}")
        return f"❌ Error restarting firmware: {str(e)}"


@mcp.tool()
async def get_printer_logs(lines: str = "50", printer_host: str = "") -> str:
    """Retrieve recent Klippy log lines for debugging and error analysis."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info(f"get_printer_logs lines={lines}")
    try:
        n = int(lines.strip()) if lines.strip().isdigit() else 50
        data = await _get("/server/files/klippy.log")
        content = data if isinstance(data, str) else json.dumps(data)
        log_lines = content.splitlines()
        recent = log_lines[-n:] if len(log_lines) > n else log_lines
        errors = [l for l in recent if "error" in l.lower() or "exception" in l.lower() or "traceback" in l.lower()]
        err_block = ""
        if errors:
            err_block = "\n\n🚨 Errors/Exceptions found:\n" + "\n".join(errors[-10:])
        return f"📋 Last {n} log lines:\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(recent) + err_block
    except Exception as e:
        logger.error(f"get_printer_logs error: {e}")
        return f"❌ Error fetching logs: {str(e)}"


@mcp.tool()
async def check_failure_detection(printer_host: str = "") -> str:
    """Run failure detection checks for thermal anomalies, spaghetti, and layer shifts."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("check_failure_detection called")
    try:
        data = await _get("/printer/objects/query", {
            "params": "print_stats extruder heater_bed virtual_sdcard toolhead"
        })
        s   = data.get("result", {}).get("status", {})
        ps  = s.get("print_stats", {})
        ext = s.get("extruder", {})
        bed = s.get("heater_bed", {})
        vsd = s.get("virtual_sdcard", {})
        th  = s.get("toolhead", {})

        state    = ps.get("state", "")
        progress = vsd.get("progress", 0)
        elapsed  = ps.get("print_duration", 0)
        fil_used = ps.get("filament_used", 0)

        anomalies = _detect_anomalies(s, ps)

        # Filament under-extrusion heuristic
        if state == "printing" and elapsed > 300 and fil_used < 10:
            anomalies.append("⚠️  POSSIBLE UNDER-EXTRUSION / CLOG: Low filament usage after 5+ min printing")

        # Progress stall heuristic
        if state == "printing" and elapsed > 600 and progress < 0.001:
            anomalies.append("⚠️  POSSIBLE PRINT FAILURE / STALL: No progress detected after 10 min")

        # Position anomaly — Z very low while printing
        pos = th.get("position", [0, 0, 0, 0])
        if state == "printing" and pos[2] < 0.1 and elapsed > 120:
            anomalies.append(f"⚠️  LAYER SHIFT / Z ISSUE: Z position={pos[2]:.3f} while printing for 2+ min")

        if anomalies:
            return "🚨 FAILURE DETECTION REPORT\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(anomalies)
        else:
            return f"✅ Failure Detection Report\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nNo anomalies detected.\nState: {state.upper()}\nProgress: {progress*100:.1f}%\nFilament used: {fil_used:.1f} mm"
    except Exception as e:
        logger.error(f"check_failure_detection error: {e}")
        return f"❌ Error running failure detection: {str(e)}"


@mcp.tool()
async def calculate_print_cost(
    print_duration_hours: str = "",
    filament_used_grams: str = "",
    printer_host: str = ""
) -> str:
    """Calculate print cost and recommended sale price from duration and filament usage."""
    logger.info(f"calculate_print_cost dur={print_duration_hours} fil={filament_used_grams}")
    try:
        # Fetch live data if params not provided
        if not print_duration_hours.strip() or not filament_used_grams.strip():
            global PRINTER_HOST
            if printer_host.strip():
                PRINTER_HOST = printer_host.strip()
            data = await _get("/printer/objects/query", {"params": "print_stats"})
            ps   = data.get("result", {}).get("status", {}).get("print_stats", {})
            dur_h = ps.get("print_duration", 0) / 3600.0
            # Filament used in mm → convert to grams (assume 1.75mm PLA ~2.4 g/m)
            fil_mm  = ps.get("filament_used", 0)
            fil_g   = (fil_mm / 1000.0) * 2.4
        else:
            dur_h  = float(print_duration_hours.strip())
            fil_g  = float(filament_used_grams.strip())

        fil_kg      = fil_g / 1000.0
        filament_cost = fil_kg * FILAMENT_COST
        power_cost    = dur_h * (PRINTER_WATTS / 1000.0) * POWER_COST
        labor_est     = dur_h * 0.5           # $0.50/hr machine time
        total_cost    = filament_cost + power_cost + labor_est
        sale_price    = total_cost * (1 + MARKUP_PCT / 100.0)
        profit        = sale_price - total_cost

        return f"""💰 Print Cost Analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Duration         : {dur_h:.2f} hours
Filament Used    : {fil_g:.1f} g ({fil_kg*1000:.1f} g)

📦 Cost Breakdown
  Filament       : ${filament_cost:.2f}  ({fil_kg:.4f} kg × ${FILAMENT_COST}/kg)
  Power           : ${power_cost:.2f}  ({dur_h:.2f}h × {PRINTER_WATTS}W × ${POWER_COST}/kWh)
  Machine Time   : ${labor_est:.2f}  (overhead estimate)
  ─────────────────────────────
  Total Cost     : ${total_cost:.2f}

📈 Pricing
  Markup         : {MARKUP_PCT:.0f}%
  Recommended    : ${sale_price:.2f}
  Profit Margin  : ${profit:.2f}

⚙️  Config: Filament ${FILAMENT_COST}/kg | Power ${POWER_COST}/kWh | {PRINTER_WATTS}W printer
   Adjust via FILAMENT_COST_PER_KG, POWER_COST_PER_KWH, PRINTER_WATTS env vars
"""
    except Exception as e:
        logger.error(f"calculate_print_cost error: {e}")
        return f"❌ Error calculating cost: {str(e)}"


@mcp.tool()
async def get_camera_snapshot_url(camera_index: str = "0", printer_host: str = "") -> str:
    """Get the camera snapshot and stream URLs for live monitoring."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info(f"get_camera_snapshot_url camera={camera_index}")
    try:
        idx  = int(camera_index.strip()) if camera_index.strip().isdigit() else 0
        data = await _get("/server/webcams/list")
        cams = data.get("result", {}).get("webcams", [])
        if not cams:
            return f"📷 No cameras configured.\nManually check: {PRINTER_HOST}/webcam/?action=snapshot"
        if idx >= len(cams):
            idx = 0
        cam = cams[idx]
        name     = cam.get("name", "Camera")
        snapshot = cam.get("snapshot_url", "")
        stream   = cam.get("stream_url", "")
        if not snapshot.startswith("http"):
            snapshot = f"{PRINTER_HOST}{snapshot}"
        if not stream.startswith("http"):
            stream = f"{PRINTER_HOST}{stream}"
        others = ""
        if len(cams) > 1:
            others = "\n\nOther cameras:\n" + "\n".join(
                f"  [{i}] {c.get('name','N/A')}" for i, c in enumerate(cams) if i != idx
            )
        return f"""📷 Camera: {name} [index {idx}]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Snapshot URL : {snapshot}
Stream URL   : {stream}
{others}
💡 Open snapshot URL in browser for current frame.
"""
    except Exception as e:
        logger.error(f"get_camera_snapshot_url error: {e}")
        return f"❌ Error fetching camera info: {str(e)}\nTry: {PRINTER_HOST}/webcam/?action=snapshot"


@mcp.tool()
async def get_moonraker_status(printer_host: str = "") -> str:
    """Get Moonraker API server health and connection status."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_moonraker_status called")
    try:
        data = await _get("/server/info")
        info = data.get("result", {})
        return f"""🌐 Moonraker Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Klippy Connected : {info.get('klippy_connected', False)}
Klippy State     : {info.get('klippy_state', 'N/A')}
API Version      : {info.get('api_version_string', 'N/A')}
Hostname         : {info.get('hostname', 'N/A')}
"""
    except Exception as e:
        logger.error(f"get_moonraker_status error: {e}")
        return f"❌ Error fetching Moonraker status: {str(e)}"


@mcp.tool()
async def get_active_alerts(printer_host: str = "") -> str:
    """Check for all active alerts including thermal, print state, and Klippy errors."""
    global PRINTER_HOST
    if printer_host.strip():
        PRINTER_HOST = printer_host.strip()
    logger.info("get_active_alerts called")
    try:
        data = await _get("/printer/objects/query", {
            "params": "print_stats extruder heater_bed webhooks virtual_sdcard toolhead"
        })
        s  = data.get("result", {}).get("status", {})
        ps = s.get("print_stats", {})
        wh = s.get("webhooks", {})

        alerts = []

        # Klippy errors
        klippy_state = wh.get("state", "ready")
        if klippy_state != "ready":
            alerts.append(f"🔴 KLIPPY NOT READY: {klippy_state} — {wh.get('state_message', '')}")

        # Thermal anomalies
        alerts += _detect_anomalies(s, ps)

        # Print failure heuristics
        state   = ps.get("state", "")
        elapsed = ps.get("print_duration", 0)
        fil     = ps.get("filament_used", 0)
        if state == "printing" and elapsed > 300 and fil < 5:
            alerts.append("⚠️  CLOG / UNDER-EXTRUSION: Very low filament usage while printing")

        if not alerts:
            return "✅ No active alerts. All systems nominal."

        return "🚨 ACTIVE ALERTS\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(alerts)
    except Exception as e:
        logger.error(f"get_active_alerts error: {e}")
        return f"❌ Error checking alerts: {str(e)}"


@mcp.tool()
async def list_available_tools(dummy: str = "") -> str:
    """List all available MCP tools with descriptions for this Fluidd/Klipper server."""
    return """🛠️  Available Fluidd/Klipper MCP Tools
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 STATUS & MONITORING
  get_printer_status      — Full real-time printer status
  get_temperatures        — Temperature readings for all heaters/sensors
  get_print_job_status    — Current job details and progress
  get_klippy_status       — Klippy firmware health
  get_moonraker_status    — Moonraker API server health
  get_active_alerts       — All active alerts and anomalies
  check_failure_detection — Spaghetti / layer shift / thermal checks
  get_printer_logs        — Recent Klippy log lines

📷 CAMERAS
  get_camera_snapshot_url — Snapshot and stream URLs for webcam(s)

🖨️  JOB MANAGEMENT
  start_print             — Start printing a file
  pause_print             — Pause current print
  resume_print            — Resume paused print
  cancel_print            — Cancel current print
  emergency_stop          — Immediate hardware stop (M112)

📋 QUEUE & FILES
  get_print_queue         — View the Moonraker job queue
  add_to_queue            — Add a file to the queue
  remove_from_queue       — Remove a job from the queue
  list_print_files        — Browse files on printer storage
  get_print_history       — Recent print job history

💰 COST / PROFITABILITY
  calculate_print_cost    — Calculate cost & recommended sale price

⚙️  CONTROL
  set_temperature         — Set heater targets
  send_gcode              — Send raw G-code commands
  restart_klippy          — Restart Klippy service
  restart_firmware        — Firmware restart

All tools accept optional 'printer_host' to override the default printer URL.
"""


# ── Server Startup ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info(f"Starting Fluidd/Klipper MCP server | Host: {PRINTER_HOST}")
    if not PRINTER_TOKEN:
        logger.warning("PRINTER_API_TOKEN not set — auth may be required")
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
