#!/usr/bin/env python3
"""
Fluidd/Klipper Multi-Printer Management MCP Server

Supports multiple printers via PRINTER_HOSTS env var (JSON array) or the
legacy single-printer PRINTER_HOST env var for backward compatibility.

PRINTER_HOSTS format:
  [
    {"name": "Ender3",  "host": "http://192.168.1.100", "token": "",
     "filament_cost": 25.0, "power_cost": 0.12, "watts": 150.0, "markup": 30.0},
    {"name": "Bambu",   "host": "http://192.168.1.101", "token": "abc123"}
  ]

All tools accept an optional 'printer' parameter — supply a printer name or URL
to target a specific machine. Omit it to use the default (first) printer.
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("fluidd-klipper-server")

mcp = FastMCP("fluidd-klipper")

# ── Multi-printer registry ────────────────────────────────────────────────────

_PRINTERS: dict = {}      # lowercase name -> config dict
_PRINTER_ORDER: list = [] # ordered list of names for default resolution
_DEFAULT_COSTS = {
    "filament_cost": float(os.environ.get("FILAMENT_COST_PER_KG", "25.0")),
    "power_cost":    float(os.environ.get("POWER_COST_PER_KWH",   "0.12")),
    "watts":         float(os.environ.get("PRINTER_WATTS",        "150.0")),
    "markup":        float(os.environ.get("MARKUP_PERCENTAGE",    "30.0")),
}

def _load_printers():
    global _PRINTERS, _PRINTER_ORDER

    hosts_json = os.environ.get("PRINTER_HOSTS", "").strip()
    if hosts_json:
        try:
            printers = json.loads(hosts_json)
            for p in printers:
                cfg = {
                    "name":         p.get("name", p.get("host", "printer")),
                    "host":         p.get("host", "http://192.168.1.100").rstrip("/"),
                    "token":        p.get("token", p.get("api_token", "")),
                    "filament_cost": float(p.get("filament_cost", _DEFAULT_COSTS["filament_cost"])),
                    "power_cost":    float(p.get("power_cost",    _DEFAULT_COSTS["power_cost"])),
                    "watts":         float(p.get("watts",         _DEFAULT_COSTS["watts"])),
                    "markup":        float(p.get("markup",        _DEFAULT_COSTS["markup"])),
                }
                key = cfg["name"].lower()
                _PRINTERS[key] = cfg
                _PRINTER_ORDER.append(key)
            if _PRINTERS:
                logger.info(f"Loaded {len(_PRINTERS)} printers from PRINTER_HOSTS")
                return
        except Exception as e:
            logger.warning(f"Failed to parse PRINTER_HOSTS: {e} — falling back to PRINTER_HOST")

    # Backward-compat single printer
    host  = os.environ.get("PRINTER_HOST", "http://192.168.1.100").rstrip("/")
    token = os.environ.get("PRINTER_API_TOKEN", "")
    cfg   = {"name": "default", "host": host, "token": token, **_DEFAULT_COSTS}
    _PRINTERS["default"] = cfg
    _PRINTER_ORDER.append("default")
    logger.info(f"Single-printer mode: {host}")

_load_printers()

def _resolve_printer(printer: str = "") -> dict:
    """Resolve a printer name, URL, or empty string to a printer config dict.

    Priority:
      1. Exact name match (case-insensitive)
      2. Partial name match
      3. Direct URL (used as-is with default costs)
      4. Empty string → default printer (first in list)
    """
    if not _PRINTERS:
        return {"name": "default", "host": "http://192.168.1.100", "token": "", **_DEFAULT_COSTS}

    p = printer.strip()

    if not p:
        # Return first (default) printer
        return _PRINTERS[_PRINTER_ORDER[0]]

    # Exact name match
    if p.lower() in _PRINTERS:
        return _PRINTERS[p.lower()]

    # Partial name match
    for key, cfg in _PRINTERS.items():
        if p.lower() in key or key in p.lower():
            return cfg

    # URL match
    for cfg in _PRINTERS.values():
        if cfg["host"].lower() == p.lower():
            return cfg

    # Treat as direct URL
    return {"name": p, "host": p.rstrip("/"), "token": "", **_DEFAULT_COSTS}

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    h = {"Content-Type": "application/json"}
    if token:
        h["X-Api-Key"] = token
    return h

async def _get(host: str, path: str, params: dict = None, token: str = "") -> dict:
    url = f"{host}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers=_headers(token), params=params or {})
        r.raise_for_status()
        return r.json()

async def _post(host: str, path: str, data: dict = None, token: str = "") -> dict:
    url = f"{host}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=_headers(token), json=data or {})
        r.raise_for_status()
        return r.json()

async def _delete(host: str, path: str, token: str = "") -> dict:
    url = f"{host}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.delete(url, headers=_headers(token))
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
    warnings = []
    bed_actual  = temps.get("heater_bed", {}).get("actual_temperature", 0)
    bed_target  = temps.get("heater_bed", {}).get("target_temperature", 0)
    tool_actual = temps.get("extruder",   {}).get("actual_temperature", 0)
    tool_target = temps.get("extruder",   {}).get("target_temperature", 0)

    if bed_target > 0 and abs(bed_actual - bed_target) > 15:
        warnings.append(f"THERMAL ANOMALY: Bed target {bed_target}C but actual {bed_actual:.1f}C")
    if tool_target > 0 and abs(tool_actual - tool_target) > 20:
        warnings.append(f"THERMAL ANOMALY: Hotend target {tool_target}C but actual {tool_actual:.1f}C")

    state    = print_stats.get("state", "")
    progress = print_stats.get("progress", 0)
    if state == "printing" and 0 < progress < 0.02:
        warnings.append("POSSIBLE LAYER SHIFT / ADHESION FAILURE: Very low progress at print start")
    return warnings

# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_printers(dummy: str = "") -> str:
    """List all configured printers and their connection details."""
    if not _PRINTERS:
        return "No printers configured. Set PRINTER_HOSTS or PRINTER_HOST env var."
    lines = ["Configured Printers", "=" * 40]
    for i, key in enumerate(_PRINTER_ORDER):
        cfg = _PRINTERS[key]
        default_tag = " (default)" if i == 0 else ""
        lines.append(f"  [{i+1}] {cfg['name']}{default_tag}")
        lines.append(f"       Host   : {cfg['host']}")
        lines.append(f"       Auth   : {'set' if cfg.get('token') else 'none'}")
        lines.append(f"       Costs  : filament ${cfg['filament_cost']}/kg  "
                     f"power ${cfg['power_cost']}/kWh  {cfg['watts']}W  {cfg['markup']}% markup")
    lines.append("")
    lines.append("Pass printer name or URL as 'printer' param to any tool to target a specific machine.")
    lines.append("Example: get_printer_status(printer='Ender3')")
    return "\n".join(lines)


@mcp.tool()
async def get_printer_status(printer: str = "") -> str:
    """Get full real-time status of a printer. Use 'printer' param to specify which one (name or URL)."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    logger.info(f"get_printer_status [{cfg['name']}]")
    try:
        data = await _get(host, "/printer/objects/query", {
            "print_stats": None, "extruder": None, "heater_bed": None,
            "toolhead": None, "fan": None, "virtual_sdcard": None,
            "display_status": None, "webhooks": None
        }, token)
        s   = data.get("result", {}).get("status", {})
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

        anomalies  = _detect_anomalies(s, ps)
        alert_block = ("\n\nALERTS:\n" + "\n".join(anomalies)) if anomalies else ""

        return f"""Printer Status -- {cfg['name']}
{'=' * 40}
Host       : {host}
State      : {state}
Klippy     : {wh.get('state', 'unknown')}
File       : {filename}
Progress   : {progress:.1f}%
Elapsed    : {_fmt_time(elapsed)}
ETA        : {_fmt_time(eta_sec)}

Temperatures
  Hotend   : {ext.get('temperature', 0):.1f}C / {ext.get('target', 0):.1f}C
  Bed      : {bed.get('temperature', 0):.1f}C / {bed.get('target', 0):.1f}C

Toolhead
  Position : X={th.get('position', [0,0,0,0])[0]:.1f} Y={th.get('position', [0,0,0,0])[1]:.1f} Z={th.get('position', [0,0,0,0])[2]:.1f}
  Speed    : {th.get('max_velocity', 0)} mm/s{alert_block}
"""
    except Exception as e:
        logger.error(f"get_printer_status [{cfg['name']}] error: {e}")
        return f"Error fetching printer status from {cfg['name']} ({host}): {str(e)}"


@mcp.tool()
async def get_temperatures(printer: str = "") -> str:
    """Get current hotend, bed, and chamber temperatures. Use 'printer' to specify which printer."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    logger.info(f"get_temperatures [{cfg['name']}]")
    try:
        data = await _get(host, "/printer/objects/query",
                          {"extruder": None, "heater_bed": None, "temperature_sensor": None}, token)
        s   = data.get("result", {}).get("status", {})
        ext = s.get("extruder", {})
        bed = s.get("heater_bed", {})
        sensors = {k: v for k, v in s.items() if "temperature_sensor" in k}
        extra = "".join(f"\n  {n}: {v.get('temperature', 0):.1f}C" for n, v in sensors.items())
        return f"""Temperature Report -- {cfg['name']}
{'=' * 40}
Hotend   : {ext.get('temperature', 0):.1f}C -> target {ext.get('target', 0):.1f}C  (power {ext.get('power', 0)*100:.0f}%)
Bed      : {bed.get('temperature', 0):.1f}C -> target {bed.get('target', 0):.1f}C  (power {bed.get('power', 0)*100:.0f}%)
Extra Sensors:{extra if extra else ' None'}
"""
    except Exception as e:
        return f"Error fetching temperatures from {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_print_job_status(printer: str = "") -> str:
    """Get current print job details including file, progress, and timing."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    logger.info(f"get_print_job_status [{cfg['name']}]")
    try:
        data = await _get(host, "/printer/objects/query",
                          {"print_stats": None, "virtual_sdcard": None, "display_status": None}, token)
        s   = data.get("result", {}).get("status", {})
        ps  = s.get("print_stats", {})
        vsd = s.get("virtual_sdcard", {})
        ds  = s.get("display_status", {})
        progress = vsd.get("progress", 0) * 100
        elapsed  = ps.get("print_duration", 0)
        total    = ps.get("total_duration", 0)
        eta_sec  = max(0, total - elapsed)
        return f"""Print Job Status -- {cfg['name']}
{'=' * 40}
File       : {ps.get('filename', 'N/A')}
State      : {ps.get('state', 'N/A').upper()}
Progress   : {progress:.1f}%
Layer Info : {ds.get('message', '') or 'N/A'}
Elapsed    : {_fmt_time(elapsed)}
ETA        : {_fmt_time(eta_sec)}
Filament   : {ps.get('filament_used', 0):.1f} mm used
"""
    except Exception as e:
        return f"Error fetching job status from {cfg['name']}: {str(e)}"


@mcp.tool()
async def start_print(filename: str = "", printer: str = "") -> str:
    """Start printing a file. Use 'printer' to specify which printer."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    if not filename.strip():
        return "Error: filename is required"
    logger.info(f"start_print [{cfg['name']}]: {filename}")
    try:
        data = await _post(host, "/printer/print/start", {"filename": filename.strip()}, token)
        return f"Print started on {cfg['name']}: {filename}\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error starting print on {cfg['name']}: {str(e)}"


@mcp.tool()
async def pause_print(printer: str = "") -> str:
    """Pause the currently running print job."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _post(host, "/printer/print/pause", token=token)
        return f"Print paused on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error pausing print on {cfg['name']}: {str(e)}"


@mcp.tool()
async def resume_print(printer: str = "") -> str:
    """Resume a paused print job."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _post(host, "/printer/print/resume", token=token)
        return f"Print resumed on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error resuming print on {cfg['name']}: {str(e)}"


@mcp.tool()
async def cancel_print(printer: str = "") -> str:
    """Cancel the current print job."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _post(host, "/printer/print/cancel", token=token)
        return f"Print cancelled on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error cancelling print on {cfg['name']}: {str(e)}"


@mcp.tool()
async def emergency_stop(printer: str = "") -> str:
    """Trigger an emergency stop (M112 — halts all motion immediately)."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    logger.warning(f"EMERGENCY STOP on {cfg['name']}")
    try:
        data = await _post(host, "/printer/emergency_stop", token=token)
        return f"EMERGENCY STOP executed on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error executing emergency stop on {cfg['name']}: {str(e)}"


@mcp.tool()
async def list_print_files(path: str = "", printer: str = "") -> str:
    """List files available on printer storage."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        params = {"path": path.strip() if path.strip() else "gcodes"}
        data = await _get(host, "/server/files/list", params, token)
        files = data.get("result", [])
        if not files:
            return f"No files found on {cfg['name']} storage."
        lines = [f"Files on {cfg['name']} -- '{params['path']}':", "=" * 40]
        for f in files[:50]:
            size_kb  = f.get("size", 0) / 1024
            modified = datetime.fromtimestamp(f.get("modified", 0)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  {f.get('filename', 'N/A'):40s}  {size_kb:8.1f} KB  {modified}")
        if len(files) > 50:
            lines.append(f"  ... and {len(files)-50} more files")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing files on {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_print_history(limit: str = "10", printer: str = "") -> str:
    """Get recent print job history."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        n = int(limit.strip()) if limit.strip().isdigit() else 10
        data = await _get(host, "/server/history/list", {"limit": n, "order": "desc"}, token)
        jobs = data.get("result", {}).get("jobs", [])
        if not jobs:
            return f"No print history on {cfg['name']}."
        lines = [f"Print History -- {cfg['name']}", "=" * 40]
        for j in jobs:
            status  = j.get("status", "N/A")
            fname   = j.get("filename", "N/A")
            elapsed = _fmt_time(j.get("print_duration", 0))
            started = datetime.fromtimestamp(j.get("start_time", 0)).strftime("%Y-%m-%d %H:%M")
            fil_used= j.get("filament_used", 0)
            icon    = "OK" if status == "completed" else ("ERR" if status == "error" else "---")
            lines.append(f"  [{icon}] [{started}] {fname}")
            lines.append(f"         Duration: {elapsed}  |  Filament: {fil_used:.1f} mm  |  Status: {status}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching history from {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_print_queue(printer: str = "") -> str:
    """Get the current Moonraker print queue."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data   = await _get(host, "/server/job_queue/status", token=token)
        result = data.get("result", {})
        queue  = result.get("queued_jobs", [])
        state  = result.get("queue_state", "N/A")
        if not queue:
            return f"Print Queue -- {cfg['name']}\nState: {state}\nQueue is empty."
        lines = [f"Print Queue -- {cfg['name']} (state: {state})", "=" * 40]
        for i, job in enumerate(queue, 1):
            lines.append(f"  {i}. {job.get('filename', 'N/A')}  [ID: {job.get('job_id', 'N/A')}]")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching queue from {cfg['name']}: {str(e)}"


@mcp.tool()
async def add_to_queue(filename: str = "", printer: str = "") -> str:
    """Add a file to the Moonraker print queue."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    if not filename.strip():
        return "Error: filename is required"
    try:
        data = await _post(host, "/server/job_queue/job", {"filenames": [filename.strip()]}, token)
        return f"Added to queue on {cfg['name']}: {filename}\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error adding to queue on {cfg['name']}: {str(e)}"


@mcp.tool()
async def remove_from_queue(job_id: str = "", printer: str = "") -> str:
    """Remove a job from the print queue by job ID."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    if not job_id.strip():
        return "Error: job_id is required (get it from get_print_queue)"
    try:
        data = await _delete(host, f"/server/job_queue/job?job_ids={job_id.strip()}", token)
        return f"Removed job {job_id} from queue on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error removing from queue on {cfg['name']}: {str(e)}"


@mcp.tool()
async def set_temperature(heater: str = "extruder", target: str = "0", printer: str = "") -> str:
    """Set temperature for a heater (extruder or heater_bed)."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    if not heater.strip():
        return "Error: heater name is required (extruder or heater_bed)"
    try:
        temp = float(target.strip()) if target.strip() else 0.0
    except ValueError:
        return f"Error: invalid target temperature: {target}"
    try:
        gcode = f"SET_HEATER_TEMPERATURE HEATER={heater.strip()} TARGET={temp}"
        data  = await _post(host, "/printer/gcode/script", {"script": gcode}, token)
        return f"Set {heater} to {temp}C on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error setting temperature on {cfg['name']}: {str(e)}"


@mcp.tool()
async def send_gcode(command: str = "", printer: str = "") -> str:
    """Send a raw G-code command to the printer."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    if not command.strip():
        return "Error: command is required"
    try:
        data = await _post(host, "/printer/gcode/script", {"script": command.strip()}, token)
        return f"G-code sent to {cfg['name']}: {command}\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error sending G-code to {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_klippy_status(printer: str = "") -> str:
    """Get Klippy firmware status and any error messages."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _get(host, "/printer/objects/query", {"webhooks": None}, token)
        wh   = data.get("result", {}).get("status", {}).get("webhooks", {})
        state     = wh.get("state", "unknown")
        state_msg = wh.get("state_message", "")
        ok = "READY" if state == "ready" else "ERROR"
        return f"""Klippy Status -- {cfg['name']}
{'=' * 40}
State   : {state.upper()}  [{ok}]
Message : {state_msg or 'No message'}
"""
    except Exception as e:
        return f"Error fetching Klippy status from {cfg['name']}: {str(e)}"


@mcp.tool()
async def restart_klippy(printer: str = "") -> str:
    """Restart the Klippy firmware service."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _post(host, "/printer/restart", token=token)
        return f"Klippy restart initiated on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error restarting Klippy on {cfg['name']}: {str(e)}"


@mcp.tool()
async def restart_firmware(printer: str = "") -> str:
    """Perform a firmware restart (FIRMWARE_RESTART)."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _post(host, "/printer/firmware_restart", token=token)
        return f"Firmware restart initiated on {cfg['name']}.\nResponse: {json.dumps(data.get('result', data), indent=2)}"
    except Exception as e:
        return f"Error restarting firmware on {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_printer_logs(lines: str = "50", printer: str = "") -> str:
    """Retrieve recent Klippy log lines for debugging."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        n    = int(lines.strip()) if lines.strip().isdigit() else 50
        data = await _get(host, "/server/files/klippy.log", token=token)
        content   = data if isinstance(data, str) else json.dumps(data)
        log_lines = content.splitlines()
        recent = log_lines[-n:] if len(log_lines) > n else log_lines
        errors = [l for l in recent if any(kw in l.lower() for kw in ("error", "exception", "traceback"))]
        err_block = ""
        if errors:
            err_block = "\n\nErrors/Exceptions found:\n" + "\n".join(errors[-10:])
        return f"Last {n} log lines from {cfg['name']}:\n{'=' * 40}\n" + "\n".join(recent) + err_block
    except Exception as e:
        return f"Error fetching logs from {cfg['name']}: {str(e)}"


@mcp.tool()
async def check_failure_detection(printer: str = "") -> str:
    """Run failure detection checks for thermal anomalies, spaghetti, and layer shifts."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _get(host, "/printer/objects/query", {
            "print_stats": None, "extruder": None, "heater_bed": None,
            "virtual_sdcard": None, "toolhead": None
        }, token)
        s   = data.get("result", {}).get("status", {})
        ps  = s.get("print_stats", {})
        vsd = s.get("virtual_sdcard", {})
        th  = s.get("toolhead", {})

        state    = ps.get("state", "")
        progress = vsd.get("progress", 0)
        elapsed  = ps.get("print_duration", 0)
        fil_used = ps.get("filament_used", 0)

        anomalies = _detect_anomalies(s, ps)

        if state == "printing" and elapsed > 300 and fil_used < 10:
            anomalies.append("POSSIBLE UNDER-EXTRUSION / CLOG: Low filament usage after 5+ min")
        if state == "printing" and elapsed > 600 and progress < 0.001:
            anomalies.append("POSSIBLE PRINT FAILURE / STALL: No progress after 10 min")

        pos = th.get("position", [0, 0, 0, 0])
        if state == "printing" and pos[2] < 0.1 and elapsed > 120:
            anomalies.append(f"LAYER SHIFT / Z ISSUE: Z={pos[2]:.3f} while printing for 2+ min")

        if anomalies:
            return f"FAILURE DETECTION -- {cfg['name']}\n{'=' * 40}\n" + "\n".join(anomalies)
        return (f"No Anomalies -- {cfg['name']}\n{'=' * 40}\n"
                f"State: {state.upper()}\nProgress: {progress*100:.1f}%\nFilament: {fil_used:.1f} mm")
    except Exception as e:
        return f"Error running failure detection on {cfg['name']}: {str(e)}"


@mcp.tool()
async def calculate_print_cost(
    print_duration_hours: str = "",
    filament_used_grams: str = "",
    printer: str = ""
) -> str:
    """Calculate print cost and recommended sale price. Uses live data if params omitted."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    FILAMENT_COST = cfg["filament_cost"]
    POWER_COST    = cfg["power_cost"]
    PRINTER_WATTS = cfg["watts"]
    MARKUP_PCT    = cfg["markup"]

    try:
        if not print_duration_hours.strip() or not filament_used_grams.strip():
            data = await _get(host, "/printer/objects/query", {"print_stats": None}, token)
            ps   = data.get("result", {}).get("status", {}).get("print_stats", {})
            dur_h  = ps.get("print_duration", 0) / 3600.0
            fil_mm = ps.get("filament_used", 0)
            fil_g  = (fil_mm / 1000.0) * 2.4
        else:
            dur_h = float(print_duration_hours.strip())
            fil_g = float(filament_used_grams.strip())

        fil_kg        = fil_g / 1000.0
        filament_cost = fil_kg * FILAMENT_COST
        power_cost    = dur_h * (PRINTER_WATTS / 1000.0) * POWER_COST
        labor_est     = dur_h * 0.5
        total_cost    = filament_cost + power_cost + labor_est
        sale_price    = total_cost * (1 + MARKUP_PCT / 100.0)
        profit        = sale_price - total_cost

        return f"""Print Cost Analysis -- {cfg['name']}
{'=' * 40}
Duration         : {dur_h:.2f} hours
Filament Used    : {fil_g:.1f} g

Cost Breakdown
  Filament       : ${filament_cost:.2f}  ({fil_kg:.4f} kg x ${FILAMENT_COST}/kg)
  Power          : ${power_cost:.2f}  ({dur_h:.2f}h x {PRINTER_WATTS}W x ${POWER_COST}/kWh)
  Machine Time   : ${labor_est:.2f}  (overhead estimate)
  ----------------------------------
  Total Cost     : ${total_cost:.2f}

Pricing
  Markup         : {MARKUP_PCT:.0f}%
  Recommended    : ${sale_price:.2f}
  Profit Margin  : ${profit:.2f}
"""
    except Exception as e:
        return f"Error calculating cost for {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_camera_snapshot_url(camera_index: str = "0", printer: str = "") -> str:
    """Get the camera snapshot and stream URLs for live monitoring."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        idx  = int(camera_index.strip()) if camera_index.strip().isdigit() else 0
        data = await _get(host, "/server/webcams/list", token=token)
        cams = data.get("result", {}).get("webcams", [])
        if not cams:
            return f"No cameras configured on {cfg['name']}.\nCheck: {host}/webcam/?action=snapshot"
        if idx >= len(cams):
            idx = 0
        cam      = cams[idx]
        name     = cam.get("name", "Camera")
        snapshot = cam.get("snapshot_url", "")
        stream   = cam.get("stream_url", "")
        if not snapshot.startswith("http"):
            snapshot = f"{host}{snapshot}"
        if not stream.startswith("http"):
            stream = f"{host}{stream}"
        others = ""
        if len(cams) > 1:
            others = "\n\nOther cameras:\n" + "\n".join(
                f"  [{i}] {c.get('name','N/A')}" for i, c in enumerate(cams) if i != idx
            )
        return f"""Camera: {name} [index {idx}] -- {cfg['name']}
{'=' * 40}
Snapshot URL : {snapshot}
Stream URL   : {stream}
{others}
Open Snapshot URL in browser for current frame.
"""
    except Exception as e:
        return f"Error fetching camera info from {cfg['name']}: {str(e)}\nTry: {host}/webcam/?action=snapshot"


@mcp.tool()
async def get_moonraker_status(printer: str = "") -> str:
    """Get Moonraker API server health and connection status."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _get(host, "/server/info", token=token)
        info = data.get("result", {})
        return f"""Moonraker Status -- {cfg['name']}
{'=' * 40}
Klippy Connected : {info.get('klippy_connected', False)}
Klippy State     : {info.get('klippy_state', 'N/A')}
API Version      : {info.get('api_version_string', 'N/A')}
Hostname         : {info.get('hostname', 'N/A')}
"""
    except Exception as e:
        return f"Error fetching Moonraker status from {cfg['name']}: {str(e)}"


@mcp.tool()
async def get_active_alerts(printer: str = "") -> str:
    """Check for all active alerts including thermal, print state, and Klippy errors."""
    cfg = _resolve_printer(printer)
    host, token = cfg["host"], cfg.get("token", "")
    try:
        data = await _get(host, "/printer/objects/query", {
            "print_stats": None, "extruder": None, "heater_bed": None,
            "webhooks": None, "virtual_sdcard": None, "toolhead": None
        }, token)
        s  = data.get("result", {}).get("status", {})
        ps = s.get("print_stats", {})
        wh = s.get("webhooks", {})

        alerts = []
        klippy_state = wh.get("state", "ready")
        if klippy_state != "ready":
            alerts.append(f"KLIPPY NOT READY: {klippy_state} -- {wh.get('state_message', '')}")

        alerts += [msg for _, msg in _detect_anomalies(s, ps)]

        state   = ps.get("state", "")
        elapsed = ps.get("print_duration", 0)
        fil     = ps.get("filament_used", 0)
        if state == "printing" and elapsed > 300 and fil < 5:
            alerts.append("CLOG / UNDER-EXTRUSION: Very low filament usage while printing")

        if not alerts:
            return f"No active alerts on {cfg['name']}. All systems nominal."
        return f"ACTIVE ALERTS -- {cfg['name']}\n{'=' * 40}\n" + "\n".join(alerts)
    except Exception as e:
        return f"Error checking alerts on {cfg['name']}: {str(e)}"


@mcp.tool()
async def list_available_tools(dummy: str = "") -> str:
    """List all available MCP tools with descriptions for this Fluidd/Klipper server."""
    printer_count = len(_PRINTERS)
    printer_list  = ", ".join(cfg["name"] for cfg in _PRINTERS.values())
    return f"""Available Fluidd/Klipper MCP Tools
{'=' * 40}
Printers configured: {printer_count} ({printer_list})
All tools accept optional 'printer' param: name, partial name, or URL.
Omit 'printer' to use the default (first configured) printer.

MULTI-PRINTER
  list_printers           -- List all configured printers

STATUS & MONITORING
  get_printer_status      -- Full real-time printer status
  get_temperatures        -- Temperature readings for all heaters/sensors
  get_print_job_status    -- Current job details and progress
  get_klippy_status       -- Klippy firmware health
  get_moonraker_status    -- Moonraker API server health
  get_active_alerts       -- All active alerts and anomalies
  check_failure_detection -- Spaghetti / layer shift / thermal checks
  get_printer_logs        -- Recent Klippy log lines

CAMERAS
  get_camera_snapshot_url -- Snapshot and stream URLs for webcam(s)

JOB MANAGEMENT
  start_print             -- Start printing a file
  pause_print             -- Pause current print
  resume_print            -- Resume paused print
  cancel_print            -- Cancel current print
  emergency_stop          -- Immediate hardware stop (M112)

QUEUE & FILES
  get_print_queue         -- View the Moonraker job queue
  add_to_queue            -- Add a file to the queue
  remove_from_queue       -- Remove a job from the queue
  list_print_files        -- Browse files on printer storage
  get_print_history       -- Recent print job history

COST / PROFITABILITY
  calculate_print_cost    -- Calculate cost and recommended sale price

CONTROL
  set_temperature         -- Set heater targets
  send_gcode              -- Send raw G-code commands
  restart_klippy          -- Restart Klippy service
  restart_firmware        -- Firmware restart
"""


# ── Server Startup ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    printer_list = ", ".join(cfg["name"] for cfg in _PRINTERS.values())
    logger.info(f"Starting Fluidd/Klipper MCP server | Printers: {printer_list}")
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
