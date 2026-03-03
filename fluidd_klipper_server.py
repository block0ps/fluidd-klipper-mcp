#!/usr/bin/env python3
"""
Fluidd/Klipper 3D Printer Management MCP Server — Multi-Printer
Supports multiple printers via PRINTER_HOSTS env var (JSON array).
Falls back to single PRINTER_HOST for backward compatibility.
"""
import os, sys, json, logging
from datetime import datetime
import httpx
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', stream=sys.stderr)
logger = logging.getLogger("fluidd-klipper-server")

mcp = FastMCP("fluidd-klipper")

# ── Configuration ─────────────────────────────────────────────────────────────

FILAMENT_COST = float(os.environ.get("FILAMENT_COST_PER_KG", "25.0"))
POWER_COST    = float(os.environ.get("POWER_COST_PER_KWH",  "0.12"))
PRINTER_WATTS = float(os.environ.get("PRINTER_WATTS",       "150.0"))
MARKUP_PCT    = float(os.environ.get("MARKUP_PERCENTAGE",   "30.0"))

PRINTERS = {}        # lowercase-name → {name, host, token}
DEFAULT_PRINTER = ""

def _load_printers():
    global PRINTERS, DEFAULT_PRINTER
    hosts_json = os.environ.get("PRINTER_HOSTS", "").strip()
    if hosts_json:
        try:
            for p in json.loads(hosts_json):
                name = (p.get("name") or p.get("host", "printer")).strip()
                key  = name.lower()
                PRINTERS[key] = {"name": name,
                                 "host": p.get("host","").rstrip("/"),
                                 "token": p.get("token","") or p.get("api_token","")}
            if PRINTERS:
                DEFAULT_PRINTER = list(PRINTERS.keys())[0]
                logger.info(f"Loaded {len(PRINTERS)} printer(s) from PRINTER_HOSTS")
                return
        except Exception as e:
            logger.warning(f"Failed to parse PRINTER_HOSTS: {e}")
    host  = os.environ.get("PRINTER_HOST","http://192.168.1.100").rstrip("/")
    token = os.environ.get("PRINTER_API_TOKEN","")
    PRINTERS["default"] = {"name":"Default","host":host,"token":token}
    DEFAULT_PRINTER = "default"
    logger.info(f"Using single printer: {host}")

_load_printers()

def _resolve(printer: str) -> tuple:
    p = (printer or "").strip()
    if not p:
        info = PRINTERS.get(DEFAULT_PRINTER, {})
        return info.get("host",""), info.get("token","")
    if p.startswith("http"):
        return p.rstrip("/"), ""
    for key, info in PRINTERS.items():
        if key == p.lower() or info["name"].lower() == p.lower():
            return info["host"], info["token"]
    return p.rstrip("/"), ""

def _label(host: str) -> str:
    if len(PRINTERS) <= 1: return ""
    for info in PRINTERS.values():
        if info["host"] == host: return f"[{info['name']}] "
    return ""

def _hdrs(token: str) -> dict:
    h = {"Content-Type":"application/json"}
    if token: h["X-Api-Key"] = token
    return h

async def _get(host, token, path, params=None):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{host}{path}", headers=_hdrs(token), params=params or {})
        r.raise_for_status(); return r.json()

async def _post(host, token, path, data=None):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{host}{path}", headers=_hdrs(token), json=data or {})
        r.raise_for_status(); return r.json()

async def _delete(host, token, path):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(f"{host}{path}", headers=_hdrs(token))
        r.raise_for_status(); return r.json()

def _fmt(s):
    if s < 0: return "N/A"
    return f"{int(s//3600)}h {int((s%3600)//60)}m {int(s%60)}s"

def _anomalies(s, ps):
    w = []
    ba,bt = s.get("heater_bed",{}).get("temperature",0), s.get("heater_bed",{}).get("target",0)
    ha,ht = s.get("extruder",{}).get("temperature",0),   s.get("extruder",{}).get("target",0)
    if bt > 0 and abs(ba-bt) > 15: w.append(f"⚠️  THERMAL ANOMALY: Bed target {bt}°C but actual {ba:.1f}°C")
    if ht > 0 and abs(ha-ht) > 20: w.append(f"⚠️  THERMAL ANOMALY: Hotend target {ht}°C but actual {ha:.1f}°C")
    if ps.get("state") == "printing" and 0 < ps.get("progress",0) < 0.02:
        w.append("⚠️  POSSIBLE LAYER SHIFT / ADHESION FAILURE: Very low progress at print start")
    return w

# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_printers(dummy: str = "") -> str:
    """List all configured printers with names, hosts, and live connection status."""
    lines = ["🖨️  Configured Printers", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for i, (key, info) in enumerate(PRINTERS.items(), 1):
        dflag = " ← default" if key == DEFAULT_PRINTER else ""
        lines.append(f"\n  {i}. {info['name']}{dflag}")
        lines.append(f"     Host  : {info['host']}")
        lines.append(f"     Auth  : {'token set' if info['token'] else 'no auth'}")
        try:
            d = await _get(info["host"], info["token"], "/server/info")
            lines.append(f"     Status: ✅ reachable — Klippy: {d.get('result',{}).get('klippy_state','?')}")
        except Exception as e:
            lines.append(f"     Status: ❌ unreachable — {e}")
    lines.append("\n💡 Use printer name or URL as the 'printer' param. Blank = default.")
    return "\n".join(lines)

@mcp.tool()
async def get_printer_status(printer: str = "") -> str:
    """Get full real-time status (temps, state, progress). 'printer' = name or URL, blank = default."""
    host, token = _resolve(printer)
    lbl = _label(host)
    try:
        data = await _get(host, token,
            "/printer/objects/query?print_stats&extruder&heater_bed&toolhead&fan&virtual_sdcard&display_status&webhooks")
        s  = data.get("result",{}).get("status",{})
        ps = s.get("print_stats",{}); ext = s.get("extruder",{}); bed = s.get("heater_bed",{})
        th = s.get("toolhead",{});   vsd = s.get("virtual_sdcard",{}); wh = s.get("webhooks",{})
        state = ps.get("state","unknown").upper(); fn = ps.get("filename","N/A")
        prog  = vsd.get("progress",0)*100; el = ps.get("print_duration",0)
        tot   = ps.get("total_duration",0); eta = (tot-el) if tot>el else -1
        pos   = th.get("position",[0,0,0,0])
        aw    = _anomalies(s, ps)
        ablock = ("\n\n🚨 ALERTS:\n"+"\n".join(aw)) if aw else ""
        return f"""📊 {lbl}Printer Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
State      : {state}
Klippy     : {wh.get('state','unknown')}
File       : {fn}
Progress   : {prog:.1f}%
Elapsed    : {_fmt(el)}
ETA        : {_fmt(eta)}

🌡️  Temperatures
  Hotend   : {ext.get('temperature',0):.1f}°C / {ext.get('target',0):.1f}°C
  Bed      : {bed.get('temperature',0):.1f}°C / {bed.get('target',0):.1f}°C

🔧 Toolhead
  Position : X={pos[0]:.1f} Y={pos[1]:.1f} Z={pos[2]:.1f}
  Speed    : {th.get('max_velocity',0)} mm/s{ablock}
"""
    except Exception as e:
        return f"❌ Error fetching status from {host}: {e}"

@mcp.tool()
async def get_temperatures(printer: str = "") -> str:
    """Get temperatures for all heaters and sensors. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token, "/printer/objects/query?extruder&heater_bed&temperature_sensor")
        s   = data.get("result",{}).get("status",{})
        ext = s.get("extruder",{}); bed = s.get("heater_bed",{})
        extra = "".join(f"\n  {n}: {v.get('temperature',0):.1f}°C" for n,v in s.items() if "temperature_sensor" in n)
        return f"""🌡️  {lbl}Temperature Report
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hotend   : {ext.get('temperature',0):.1f}°C → target {ext.get('target',0):.1f}°C  (power {ext.get('power',0)*100:.0f}%)
Bed      : {bed.get('temperature',0):.1f}°C → target {bed.get('target',0):.1f}°C  (power {bed.get('power',0)*100:.0f}%)
Extra Sensors:{extra if extra else ' None'}
"""
    except Exception as e:
        return f"❌ Error fetching temperatures from {host}: {e}"

@mcp.tool()
async def get_print_job_status(printer: str = "") -> str:
    """Get current job file, progress, and timing. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token, "/printer/objects/query?print_stats&virtual_sdcard&display_status")
        s   = data.get("result",{}).get("status",{})
        ps  = s.get("print_stats",{}); vsd = s.get("virtual_sdcard",{}); ds = s.get("display_status",{})
        prog = vsd.get("progress",0)*100; el = ps.get("print_duration",0)
        tot  = ps.get("total_duration",0)
        return f"""🖨️  {lbl}Print Job Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
File       : {ps.get('filename','N/A')}
State      : {ps.get('state','N/A').upper()}
Progress   : {prog:.1f}%
Layer Info : {ds.get('message','N/A') or 'N/A'}
Elapsed    : {_fmt(el)}
ETA        : {_fmt(max(0,tot-el))}
Filament   : {ps.get('filament_used',0):.1f} mm used
"""
    except Exception as e:
        return f"❌ Error fetching job status from {host}: {e}"

@mcp.tool()
async def start_print(filename: str = "", printer: str = "") -> str:
    """Start printing a file from printer storage. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    if not filename.strip(): return "❌ filename is required"
    try:
        d = await _post(host, token, "/printer/print/start", {"filename": filename.strip()})
        return f"✅ {lbl}Print started: {filename}\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error starting print on {host}: {e}"

@mcp.tool()
async def pause_print(printer: str = "") -> str:
    """Pause the currently running print. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        d = await _post(host, token, "/printer/print/pause")
        return f"⏸️  {lbl}Print paused.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error pausing print on {host}: {e}"

@mcp.tool()
async def resume_print(printer: str = "") -> str:
    """Resume a paused print. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        d = await _post(host, token, "/printer/print/resume")
        return f"▶️  {lbl}Print resumed.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error resuming print on {host}: {e}"

@mcp.tool()
async def cancel_print(printer: str = "") -> str:
    """Cancel the current print. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        d = await _post(host, token, "/printer/print/cancel")
        return f"🛑 {lbl}Print cancelled.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error cancelling print on {host}: {e}"

@mcp.tool()
async def emergency_stop(printer: str = "") -> str:
    """Trigger an emergency stop (M112). 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    logger.warning(f"EMERGENCY STOP triggered for {host}")
    try:
        d = await _post(host, token, "/printer/emergency_stop")
        return f"🚨 {lbl}EMERGENCY STOP EXECUTED.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error executing emergency stop on {host}: {e}"

@mcp.tool()
async def list_print_files(path: str = "", printer: str = "") -> str:
    """List files on the printer's storage. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        folder = path.strip() or "gcodes"
        data = await _get(host, token, "/server/files/list", {"path": folder})
        files = data.get("result", [])
        if not files: return f"📁 {lbl}No files found in '{folder}'."
        lines = [f"📁 {lbl}Files in '{folder}':", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for f in files[:50]:
            sz = f.get("size",0)/1024
            mod = datetime.fromtimestamp(f.get("modified",0)).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  • {f.get('filename','N/A'):40s}  {sz:8.1f} KB  {mod}")
        if len(files) > 50: lines.append(f"  … and {len(files)-50} more files")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error listing files on {host}: {e}"

@mcp.tool()
async def get_print_history(limit: str = "10", printer: str = "") -> str:
    """Get recent print history with results and durations. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        n    = int(limit.strip()) if limit.strip().isdigit() else 10
        data = await _get(host, token, "/server/history/list", {"limit": n, "order": "desc"})
        jobs = data.get("result",{}).get("jobs",[])
        if not jobs: return f"📋 {lbl}No print history found."
        lines = [f"📋 {lbl}Print History", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for j in jobs:
            st = j.get("status","N/A"); fn = j.get("filename","N/A")
            el = _fmt(j.get("print_duration",0))
            started = datetime.fromtimestamp(j.get("start_time",0)).strftime("%Y-%m-%d %H:%M")
            fil = j.get("filament_used",0)
            icon = "✅" if st == "completed" else ("❌" if st == "error" else "⚠️")
            lines.append(f"  {icon} [{started}] {fn}")
            lines.append(f"      Duration: {el}  |  Filament: {fil:.1f} mm  |  Status: {st}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error fetching history from {host}: {e}"

@mcp.tool()
async def get_print_queue(printer: str = "") -> str:
    """Get the current Moonraker print queue. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token, "/server/job_queue/status")
        r = data.get("result",{}); queue = r.get("queued_jobs",[]); state = r.get("queue_state","N/A")
        if not queue: return f"📋 {lbl}Print Queue\nState: {state}\nQueue is empty."
        lines = [f"📋 {lbl}Print Queue (state: {state})", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for i, j in enumerate(queue, 1):
            lines.append(f"  {i}. {j.get('filename','N/A')}  [ID: {j.get('job_id','N/A')}]")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error fetching queue from {host}: {e}"

@mcp.tool()
async def add_to_queue(filename: str = "", printer: str = "") -> str:
    """Add a file to the Moonraker print queue. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    if not filename.strip(): return "❌ filename is required"
    try:
        d = await _post(host, token, "/server/job_queue/job", {"filenames": [filename.strip()]})
        return f"✅ {lbl}Added to queue: {filename}\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error adding to queue on {host}: {e}"

@mcp.tool()
async def remove_from_queue(job_id: str = "", printer: str = "") -> str:
    """Remove a job from the print queue by ID. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    if not job_id.strip(): return "❌ job_id is required"
    try:
        d = await _delete(host, token, f"/server/job_queue/job?job_ids={job_id.strip()}")
        return f"✅ {lbl}Removed job {job_id} from queue.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error removing from queue on {host}: {e}"

@mcp.tool()
async def set_temperature(heater: str = "extruder", target: str = "0", printer: str = "") -> str:
    """Set heater temperature ('extruder' or 'heater_bed'). 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    if not heater.strip(): return "❌ heater name is required"
    try: temp = float(target.strip()) if target.strip() else 0.0
    except ValueError: return f"❌ Invalid temperature: {target}"
    try:
        d = await _post(host, token, "/printer/gcode/script",
                        {"script": f"SET_HEATER_TEMPERATURE HEATER={heater.strip()} TARGET={temp}"})
        return f"🌡️  {lbl}Set {heater} to {temp}°C\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error setting temperature on {host}: {e}"

@mcp.tool()
async def send_gcode(command: str = "", printer: str = "") -> str:
    """Send a raw G-code command. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    if not command.strip(): return "❌ command is required"
    try:
        d = await _post(host, token, "/printer/gcode/script", {"script": command.strip()})
        return f"⚡ {lbl}G-code sent: {command}\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error sending G-code to {host}: {e}"

@mcp.tool()
async def get_klippy_status(printer: str = "") -> str:
    """Get Klippy firmware status and error messages. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token, "/printer/objects/query?webhooks")
        wh   = data.get("result",{}).get("status",{}).get("webhooks",{})
        state = wh.get("state","unknown")
        icon  = "✅" if state == "ready" else "❌"
        return f"""{icon} {lbl}Klippy Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
State   : {state.upper()}
Message : {wh.get('state_message','No message') or 'No message'}
"""
    except Exception as e:
        return f"❌ Error fetching Klippy status from {host}: {e}"

@mcp.tool()
async def restart_klippy(printer: str = "") -> str:
    """Restart the Klippy firmware service. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        d = await _post(host, token, "/printer/restart")
        return f"🔄 {lbl}Klippy restart initiated.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error restarting Klippy on {host}: {e}"

@mcp.tool()
async def restart_firmware(printer: str = "") -> str:
    """Perform a firmware restart. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        d = await _post(host, token, "/printer/firmware_restart")
        return f"🔄 {lbl}Firmware restart initiated.\n{json.dumps(d.get('result',d), indent=2)}"
    except Exception as e:
        return f"❌ Error restarting firmware on {host}: {e}"

@mcp.tool()
async def get_printer_logs(lines: str = "50", printer: str = "") -> str:
    """Retrieve recent Klippy log lines. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        n    = int(lines.strip()) if lines.strip().isdigit() else 50
        data = await _get(host, token, "/server/files/klippy.log")
        content   = data if isinstance(data, str) else json.dumps(data)
        log_lines = content.splitlines()
        recent    = log_lines[-n:]
        errors    = [l for l in recent if any(w in l.lower() for w in ("error","exception","traceback"))]
        eb        = ("\n\n🚨 Errors:\n" + "\n".join(errors[-10:])) if errors else ""
        return f"📋 {lbl}Last {n} log lines:\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(recent) + eb
    except Exception as e:
        return f"❌ Error fetching logs from {host}: {e}"

@mcp.tool()
async def check_failure_detection(printer: str = "") -> str:
    """Run failure detection: thermal anomalies, clogs, stalls, Z issues. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token,
            "/printer/objects/query?print_stats&extruder&heater_bed&virtual_sdcard&toolhead")
        s  = data.get("result",{}).get("status",{})
        ps = s.get("print_stats",{}); vsd = s.get("virtual_sdcard",{})
        th = s.get("toolhead",{})
        state = ps.get("state",""); prog = vsd.get("progress",0)
        el    = ps.get("print_duration",0); fil = ps.get("filament_used",0)
        pos   = th.get("position",[0,0,0,0])
        aw    = _anomalies(s, ps)
        if state == "printing" and el > 300 and fil < 10:
            aw.append("⚠️  POSSIBLE UNDER-EXTRUSION / CLOG: Low filament after 5+ min")
        if state == "printing" and el > 600 and prog < 0.001:
            aw.append("⚠️  POSSIBLE STALL: No progress after 10+ min")
        if state == "printing" and pos[2] < 0.1 and el > 120:
            aw.append(f"⚠️  Z ISSUE: Z={pos[2]:.3f} while printing 2+ min")
        if aw:
            return f"🚨 {lbl}FAILURE DETECTION\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(aw)
        return (f"✅ {lbl}No anomalies detected.\nState: {state.upper()}\n"
                f"Progress: {prog*100:.1f}%  Filament: {fil:.1f} mm")
    except Exception as e:
        return f"❌ Error running failure detection on {host}: {e}"

@mcp.tool()
async def calculate_print_cost(print_duration_hours: str = "", filament_used_grams: str = "", printer: str = "") -> str:
    """Calculate print cost and recommended sale price. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        if not print_duration_hours.strip() or not filament_used_grams.strip():
            data   = await _get(host, token, "/printer/objects/query?print_stats")
            ps     = data.get("result",{}).get("status",{}).get("print_stats",{})
            dur_h  = ps.get("print_duration",0) / 3600.0
            fil_g  = (ps.get("filament_used",0) / 1000.0) * 2.4
        else:
            dur_h = float(print_duration_hours.strip()); fil_g = float(filament_used_grams.strip())
        fil_kg = fil_g / 1000.0
        fc = fil_kg * FILAMENT_COST; pc = dur_h * (PRINTER_WATTS/1000.0) * POWER_COST
        le = dur_h * 0.5; tc = fc + pc + le; sp = tc * (1 + MARKUP_PCT/100.0)
        return f"""💰 {lbl}Print Cost Analysis
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Duration       : {dur_h:.2f} hours
Filament Used  : {fil_g:.1f} g

📦 Cost Breakdown
  Filament     : ${fc:.2f}  ({fil_kg:.4f} kg × ${FILAMENT_COST}/kg)
  Power         : ${pc:.2f}  ({dur_h:.2f}h × {PRINTER_WATTS}W × ${POWER_COST}/kWh)
  Machine Time : ${le:.2f}  (overhead)
  ───────────────────────────
  Total Cost   : ${tc:.2f}

📈 Pricing
  Markup       : {MARKUP_PCT:.0f}%
  Recommended  : ${sp:.2f}
  Profit       : ${sp-tc:.2f}
"""
    except Exception as e:
        return f"❌ Error calculating cost: {e}"

@mcp.tool()
async def get_camera_snapshot_url(camera_index: str = "0", printer: str = "") -> str:
    """Get webcam snapshot and stream URLs. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        idx  = int(camera_index.strip()) if camera_index.strip().isdigit() else 0
        data = await _get(host, token, "/server/webcams/list")
        cams = data.get("result",{}).get("webcams",[])
        if not cams: return f"📷 {lbl}No cameras configured.\nFallback: {host}/webcam/?action=snapshot"
        if idx >= len(cams): idx = 0
        cam = cams[idx]; name = cam.get("name","Camera")
        snap = cam.get("snapshot_url",""); stream = cam.get("stream_url","")
        if not snap.startswith("http"):   snap   = f"{host}{snap}"
        if not stream.startswith("http"): stream = f"{host}{stream}"
        others = ""
        if len(cams) > 1:
            others = "\n\nOther cameras:\n" + "\n".join(f"  [{i}] {c.get('name','N/A')}" for i,c in enumerate(cams) if i!=idx)
        return f"""📷 {lbl}Camera: {name} [index {idx}]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Snapshot URL : {snap}
Stream URL   : {stream}
{others}
💡 Open snapshot URL in browser for current frame.
"""
    except Exception as e:
        return f"❌ Error fetching camera info from {host}: {e}\nFallback: {host}/webcam/?action=snapshot"

@mcp.tool()
async def get_moonraker_status(printer: str = "") -> str:
    """Get Moonraker API server health. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token, "/server/info")
        info = data.get("result",{})
        return f"""🌐 {lbl}Moonraker Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Klippy Connected : {info.get('klippy_connected',False)}
Klippy State     : {info.get('klippy_state','N/A')}
API Version      : {info.get('api_version_string','N/A')}
Hostname         : {info.get('hostname','N/A')}
"""
    except Exception as e:
        return f"❌ Error fetching Moonraker status from {host}: {e}"

@mcp.tool()
async def get_active_alerts(printer: str = "") -> str:
    """Check all active alerts: thermal, print state, Klippy errors. 'printer' = name or URL."""
    host, token = _resolve(printer); lbl = _label(host)
    try:
        data = await _get(host, token,
            "/printer/objects/query?print_stats&extruder&heater_bed&webhooks&virtual_sdcard&toolhead")
        s  = data.get("result",{}).get("status",{})
        ps = s.get("print_stats",{}); wh = s.get("webhooks",{})
        alerts = []
        ks = wh.get("state","ready")
        if ks != "ready": alerts.append(f"🔴 KLIPPY NOT READY: {ks} — {wh.get('state_message','')}")
        alerts += _anomalies(s, ps)
        if ps.get("state") == "printing" and ps.get("print_duration",0) > 300 and ps.get("filament_used",0) < 5:
            alerts.append("⚠️  CLOG / UNDER-EXTRUSION: Very low filament while printing")
        if not alerts: return f"✅ {lbl}No active alerts. All systems nominal."
        return f"🚨 {lbl}ACTIVE ALERTS\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(alerts)
    except Exception as e:
        return f"❌ Error checking alerts on {host}: {e}"

@mcp.tool()
async def list_available_tools(dummy: str = "") -> str:
    """List all available MCP tools with descriptions."""
    names = ", ".join(info['name'] for info in PRINTERS.values())
    return f"""🛠️  Fluidd/Klipper MCP Tools
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🖨️  FLEET
  list_printers           — All configured printers with live status

📊 STATUS
  get_printer_status      — Full real-time status
  get_temperatures        — All heater/sensor temperatures
  get_print_job_status    — Current job details
  get_klippy_status       — Klippy firmware health
  get_moonraker_status    — Moonraker API health
  get_active_alerts       — All active alerts
  check_failure_detection — Thermal/clog/stall/Z checks
  get_printer_logs        — Recent Klippy log lines

📷 CAMERAS
  get_camera_snapshot_url — Snapshot and stream URLs

🖨️  JOB CONTROL
  start_print / pause_print / resume_print / cancel_print / emergency_stop

📋 QUEUE & FILES
  get_print_queue / add_to_queue / remove_from_queue
  list_print_files / get_print_history

💰 COST
  calculate_print_cost    — Cost & recommended sale price

⚙️  CONTROL
  set_temperature / send_gcode / restart_klippy / restart_firmware

📡 Printers: {names}
   All tools accept 'printer' param (name or URL). Blank = default printer.
"""

# ── Startup ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    summary = ", ".join(f"{i['name']} ({i['host']})" for i in PRINTERS.values())
    logger.info(f"Starting Fluidd/Klipper MCP | Printers: {summary}")
    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
