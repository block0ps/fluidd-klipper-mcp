#!/usr/bin/env python3
"""
Multi-Printer Monitor — Local Proxy + Alert Server
Polls multiple Moonraker instances on background threads and dispatches alerts via:
  • Push notification  — ntfy.sh (free, iOS/Android, no account needed)
  • SMS                — Twilio
  • Email              — SMTP (Gmail, etc.)
  • iMessage           — macOS AppleScript

Serves the monitor UI at http://localhost:8484

Usage:
    python3 monitor_server.py                 # Start monitor
    python3 monitor_server.py add-printer     # Interactive: add a printer
    python3 monitor_server.py 8484            # Custom port
    python3 monitor_server.py add-printer 9000

Config stored in monitor_config.json alongside this script.
Old single-printer configs (printer_host key) are auto-migrated on first run.
"""

import sys, os, json, time, smtplib, threading, subprocess, base64, re
import urllib.request, urllib.error, urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── CLI arg parsing ────────────────────────────────────────────────────────────
SUBCOMMAND = None
PORT = 8484

for arg in sys.argv[1:]:
    if arg == "add-printer":
        SUBCOMMAND = "add-printer"
    elif arg.isdigit():
        PORT = int(arg)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")

DEFAULT_CONFIG = {
    "printers": [
        {"id": "printer1", "name": "Printer 1", "host": "http://192.168.1.100", "enabled": True}
    ],
    "poll_interval_seconds": 1800,
    "alert_on_warnings": True,
    "ntfy":     {"enabled": False, "topic": "my-printer-alerts", "server": "https://ntfy.sh"},
    "twilio":   {"enabled": False, "account_sid": "", "auth_token": "", "from_number": "", "to_number": ""},
    "email":    {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                 "username": "", "password": "", "from_address": "", "to_address": ""},
    "imessage": {"enabled": False, "to_number": ""}
}

config         = {}
printer_states = {}   # printer_id -> {last_status, active_alerts, fired_alerts, last_poll, error_count, camera_url, online}
alert_log      = []   # combined across all printers
lock           = threading.Lock()

# ── Config ─────────────────────────────────────────────────────────────────────

def _migrate_config(saved):
    """Auto-migrate old single-printer config to multi-printer format."""
    if "printers" not in saved and "printer_host" in saved:
        host = saved.pop("printer_host", "http://192.168.1.100")
        saved["printers"] = [{"id": "printer1", "name": "Printer 1", "host": host, "enabled": True}]
        print("  Info: Migrated single-printer config to multi-printer format.")
    return saved

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        saved = _migrate_config(saved)
        merged = DEFAULT_CONFIG.copy()
        for k, v in saved.items():
            if isinstance(v, dict) and k in merged and k != "printers":
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
        config = merged
    else:
        config = DEFAULT_CONFIG.copy()
        save_config()
        print(f"  Created {CONFIG_FILE} — edit it to enable alert channels.\n")
    _init_printer_states()

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def _init_printer_states():
    for p in config.get("printers", []):
        pid = p["id"]
        if pid not in printer_states:
            printer_states[pid] = {
                "last_status":   {},
                "active_alerts": [],
                "fired_alerts":  set(),
                "last_poll":     None,
                "error_count":   0,
                "camera_url":    None,
                "online":        None,
            }

# ── Anomaly detection ──────────────────────────────────────────────────────────

def detect_anomalies(s):
    alerts = []
    ps  = s.get("print_stats",    {})
    ext = s.get("extruder",       {})
    bed = s.get("heater_bed",     {})
    vsd = s.get("virtual_sdcard", {})
    wh  = s.get("webhooks",       {})
    th  = s.get("toolhead",       {})
    pos = th.get("position", [0, 0, 0, 0])

    if wh.get("state", "ready") != "ready":
        alerts.append(("critical", f"Klippy not ready: {wh.get('state')} -- {wh.get('state_message', '')}"))
    if bed.get("target", 0) > 0 and abs(bed.get("temperature", 0) - bed.get("target", 0)) > 15:
        alerts.append(("critical", f"Thermal anomaly -- Bed: target {bed['target']}C, actual {bed.get('temperature',0):.1f}C"))
    if ext.get("target", 0) > 0 and abs(ext.get("temperature", 0) - ext.get("target", 0)) > 20:
        alerts.append(("critical", f"Thermal anomaly -- Hotend: target {ext['target']}C, actual {ext.get('temperature',0):.1f}C"))

    state   = ps.get("state", "")
    elapsed = ps.get("print_duration", 0)
    fil     = ps.get("filament_used",  0)
    prog    = vsd.get("progress", 0)

    if state == "printing" and elapsed > 300 and fil < 5:
        alerts.append(("warning", "Possible clog / under-extrusion: very low filament after 5+ min"))
    if state == "printing" and elapsed > 600 and prog < 0.001:
        alerts.append(("warning", "Possible stall: no progress detected after 10 min"))
    if state == "printing" and elapsed > 120 and len(pos) > 2 and pos[2] < 0.1:
        alerts.append(("warning", f"Z position anomaly: Z={pos[2]:.3f}mm while printing"))
    if state == "error":
        alerts.append(("critical", f"Print error: {ps.get('message', 'unknown')}"))
    if state == "cancelled":
        alerts.append(("warning", "Print was cancelled"))
    if state == "complete":
        alerts.append(("success", f"Print complete: {ps.get('filename', '')}"))
    return alerts

# ── Header encoding helper — fixes ntfy emoji/UTF-8 encoding error ─────────────
#
# Root cause: Python's urllib encodes HTTP header values as latin-1 by default.
# Emoji characters (e.g. the printer icon in the Title header) exceed the latin-1
# range and raise: 'latin-1' codec can't encode characters in position 0-1'.
# Fix: detect non-latin-1 content and encode as MIME base64 (RFC 2047),
# which ntfy.sh supports for all header fields.

def _encode_header(s):
    """Encode a string for use in an HTTP header value.
    Uses MIME base64 (RFC 2047) if the string contains non-latin-1 characters."""
    try:
        s.encode("latin-1")
        return s
    except (UnicodeEncodeError, UnicodeDecodeError):
        b64 = base64.b64encode(s.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="

# ── Alert channels ─────────────────────────────────────────────────────────────

def send_ntfy(title, body, level):
    cfg = config.get("ntfy", {})
    if not cfg.get("enabled"):
        return False, "disabled"
    priority = {"critical": "urgent", "warning": "high", "success": "default"}.get(level, "default")
    url = f"{cfg.get('server', 'https://ntfy.sh').rstrip('/')}/{cfg.get('topic', 'printer-alerts')}"
    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={
                "Title":        _encode_header(title),
                "Priority":     priority,
                "Tags":         "rotating_light" if level == "critical" else "printer",
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_twilio_sms(body):
    cfg = config.get("twilio", {})
    if not cfg.get("enabled"):
        return False, "disabled"
    sid, token, frm, to = (cfg.get(k, "").strip() for k in
                           ("account_sid", "auth_token", "from_number", "to_number"))
    if not all([sid, token, frm, to]):
        return False, "missing credentials"
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": body}).encode()
    b64  = base64.b64encode(f"{sid}:{token}".encode()).decode()
    try:
        req = urllib.request.Request(url, data=data, method="POST",
            headers={"Authorization": f"Basic {b64}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_email(subject, body):
    cfg = config.get("email", {})
    if not cfg.get("enabled"):
        return False, "disabled"
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))
    user = cfg.get("username", "").strip()
    pwd  = cfg.get("password", "").strip()
    frm  = cfg.get("from_address", user).strip()
    to   = cfg.get("to_address", "").strip()
    if not all([user, pwd, to]):
        return False, "missing credentials"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = frm
        msg["To"]      = to
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(frm, to, msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_imessage(body):
    cfg = config.get("imessage", {})
    if not cfg.get("enabled"):
        return False, "disabled"
    to = cfg.get("to_number", "").strip()
    if not to:
        return False, "no recipient configured"
    script = f'''tell application "Messages"
        set s to 1st service whose service type = iMessage
        set b to buddy "{to}" of s
        send "{body}" to b
    end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        return (True, "ok") if r.returncode == 0 else (False, r.stderr.strip())
    except FileNotFoundError:
        return False, "osascript not found (macOS only)"
    except Exception as e:
        return False, str(e)

def dispatch_alert(level, msg, printer_name=None):
    ts    = datetime.now().strftime("%H:%M:%S")
    label = f" [{printer_name}]" if printer_name else ""
    title = f"Printer Alert{label} -- {level.upper()}"
    full  = f"[{ts}]{label} {msg}"
    if printer_name:
        full += f"\n\nPrinter: {printer_name}"
    results = {
        "ntfy":     send_ntfy(title, full, level),
        "sms":      send_twilio_sms(f"Printer Alert{label}: {msg}"),
        "email":    send_email(title, full),
        "imessage": send_imessage(f"Printer Alert{label}: {msg}"),
    }
    sent   = [ch for ch, (ok, _) in results.items() if ok]
    failed = [(ch, err) for ch, (ok, err) in results.items() if not ok and err != "disabled"]
    entry  = {
        "time": datetime.now().isoformat(), "level": level, "msg": msg,
        "printer_name": printer_name or "", "sent": sent, "failed": failed,
    }
    with lock:
        alert_log.insert(0, entry)
        if len(alert_log) > 200:
            alert_log.pop()
    print(f"  [{ts}] ALERT{label} -- {msg}")
    print(f"         sent: {', '.join(sent) if sent else 'none'}" +
          (f" | failed: {', '.join(f'{c}({e})' for c, e in failed)}" if failed else ""))
    return results

# ── Background polling ─────────────────────────────────────────────────────────

def fetch_status(printer_cfg):
    host = printer_cfg["host"]
    url  = (host + "/printer/objects/query"
            "?print_stats&extruder&heater_bed&toolhead&virtual_sdcard&webhooks&display_status")
    req  = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("result", {}).get("status", {})

def fetch_camera_url(printer_cfg):
    """Fetch the webcam snapshot URL from Moonraker, with fallback to common paths."""
    host = printer_cfg["host"]
    # Try Moonraker webcam API
    try:
        req = urllib.request.Request(host + "/server/webcams/list",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        cams = data.get("result", {}).get("webcams", [])
        if cams:
            snapshot = cams[0].get("snapshot_url", "")
            if snapshot and not snapshot.startswith("http"):
                snapshot = host + snapshot
            if snapshot:
                return snapshot
    except Exception:
        pass
    # Fallback: standard mjpeg-streamer / crowsnest path
    for path in ("/webcam/?action=snapshot", "/webcam/snapshot", "/cam/snapshot"):
        try:
            req = urllib.request.Request(host + path, method="HEAD")
            urllib.request.urlopen(req, timeout=4)
            return host + path
        except Exception:
            pass
    return None

def process_status(printer_cfg, s):
    pid          = printer_cfg["id"]
    printer_name = printer_cfg["name"]
    with lock:
        printer_states[pid]["last_status"] = s
        printer_states[pid]["online"]      = True
        printer_states[pid]["last_poll"]   = datetime.now().isoformat()

    anomalies = detect_anomalies(s)
    alert_on_warnings = config.get("alert_on_warnings", True)
    state = s.get("print_stats", {}).get("state", "")

    if state not in ("printing", "paused"):
        with lock:
            printer_states[pid]["fired_alerts"].clear()

    for level, msg in anomalies:
        key = f"{level}:{msg}"
        should_fire = level in ("critical", "success") or (level == "warning" and alert_on_warnings)
        with lock:
            already_fired = key in printer_states[pid]["fired_alerts"]
        if should_fire and not already_fired:
            with lock:
                printer_states[pid]["fired_alerts"].add(key)
            dispatch_alert(level, msg, printer_name)

    with lock:
        printer_states[pid]["active_alerts"] = [{"level": l, "msg": m} for l, m in anomalies]

def poll_printer(printer_cfg):
    """Background thread: continuously poll one printer."""
    pid  = printer_cfg["id"]
    name = printer_cfg["name"]
    errors           = 0
    cam_refresh_tick = 0

    # Fetch camera URL at startup
    cam_url = fetch_camera_url(printer_cfg)
    with lock:
        printer_states[pid]["camera_url"] = cam_url

    while True:
        interval = config.get("poll_interval_seconds", 1800)
        try:
            s = fetch_status(printer_cfg)
            process_status(printer_cfg, s)

            # Refresh camera URL every 10 polls
            cam_refresh_tick += 1
            if cam_refresh_tick >= 10:
                cam_url = fetch_camera_url(printer_cfg)
                with lock:
                    printer_states[pid]["camera_url"] = cam_url
                cam_refresh_tick = 0

            ps  = s.get("print_stats", {})
            vsd = s.get("virtual_sdcard", {})
            ts  = datetime.now().strftime("%H:%M:%S")
            pct = vsd.get("progress", 0) * 100
            info = ps.get("info") if isinstance(ps.get("info"), dict) else {}
            layer    = info.get("current_layer", "?")
            totlayer = info.get("total_layer",   "?")
            state    = ps.get("state", "?")
            print(f"  [{ts}] [{name}] Poll OK -- {state.upper()} {pct:.1f}% L{layer}/{totlayer}")
            errors = 0
            with lock:
                printer_states[pid]["error_count"] = 0
        except Exception as e:
            errors += 1
            with lock:
                printer_states[pid]["online"]      = False
                printer_states[pid]["error_count"] = errors
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{name}] Poll error ({errors}): {e}")
            interval = min(interval, 300)
        time.sleep(interval)

def poll_once(printer_cfg):
    try:
        s = fetch_status(printer_cfg)
        process_status(printer_cfg, s)
    except Exception as e:
        print(f"  poll_once [{printer_cfg['name']}] error: {e}")


# ── HTML UI ────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Printer Monitor</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0f;color:#e2e8f0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;min-height:100vh;padding:16px}
    .container{max-width:700px;margin:0 auto;display:flex;flex-direction:column;gap:12px}
    .header{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #1e293b;padding-bottom:10px}
    .header-title{font-size:16px;font-weight:700;color:#f8fafc}
    .header-sub{font-size:11px;color:#475569;margin-top:2px}
    .header-meta{text-align:right;font-size:11px;color:#475569;line-height:1.8}
    .header-meta span{color:#94a3b8}
    .printer-tabs{display:flex;gap:6px;flex-wrap:wrap}
    .tab-btn{display:flex;align-items:center;gap:6px;background:#0f172a;border:1px solid #1e293b;
             color:#64748b;font-family:inherit;font-size:12px;padding:6px 12px;border-radius:20px;cursor:pointer;transition:all .15s}
    .tab-btn:hover{border-color:#334155;color:#94a3b8}
    .tab-btn.active{background:#1e293b;border-color:#3b82f6;color:#f8fafc}
    .tab-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
    .dot-online{background:#22c55e}.dot-offline{background:#ef4444}.dot-unknown{background:#475569}
    .alert{border-radius:6px;padding:10px 14px;font-size:12px;font-weight:600;border-left:3px solid;margin-bottom:6px}
    .alert-critical{background:#1a0a0a;border-color:#ef4444;color:#fca5a5}
    .alert-warning{background:#1a150a;border-color:#f59e0b;color:#fcd34d}
    .alert-success{background:#0a1a0f;border-color:#22c55e;color:#86efac}
    .alert-ok{background:#0a120a;border-color:#22c55e;color:#4ade80;font-weight:500}
    .card{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:12px}
    .card-top{display:flex;align-items:center;justify-content:space-between}
    .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;color:#fff}
    .badge-printing{background:#16a34a}.badge-paused{background:#d97706}
    .badge-error{background:#dc2626}.badge-complete{background:#2563eb}
    .badge-cancelled,.badge-standby,.badge-unknown{background:#334155}
    .badge-offline{background:#7f1d1d}
    .spinner{font-size:11px;color:#475569;animation:pulse 1.5s infinite}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
    .filename{font-size:11px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .progress-row{display:flex;justify-content:space-between;font-size:12px;margin-top:4px}
    .progress-pct{color:#4ade80;font-weight:700}.progress-layer{color:#64748b}
    .progress-bar-bg{background:#1e293b;border-radius:99px;height:8px;margin-top:6px;overflow:hidden}
    .progress-bar-fill{height:100%;border-radius:99px;background:#22c55e;transition:width .6s ease}
    .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .stat-label{color:#475569;font-size:11px}.stat-value{color:#f1f5f9;font-size:13px;margin-top:1px}
    .temps{border-top:1px solid #1e293b;padding-top:10px;display:flex;flex-direction:column;gap:7px}
    .temp-row{display:flex;align-items:center;gap:10px}
    .temp-label{color:#475569;width:52px;font-size:11px}
    .temp-val{font-weight:700;font-size:13px}.temp-ok{color:#4ade80}.temp-bad{color:#f87171}
    .temp-target{color:#475569;font-size:11px}
    .camera-section{border-top:1px solid #1e293b;padding-top:10px;display:flex;flex-direction:column;gap:8px}
    .camera-hdr{display:flex;align-items:center;justify-content:space-between}
    .camera-img{width:100%;border-radius:6px;border:1px solid #1e293b;display:block;background:#050a14}
    .camera-placeholder{width:100%;height:90px;border-radius:6px;border:1px dashed #1e293b;
                        display:flex;align-items:center;justify-content:center;color:#334155;font-size:12px}
    .btn-cam{background:#1e293b;border:1px solid #334155;color:#64748b;font-family:inherit;
             font-size:11px;padding:4px 10px;border-radius:12px;cursor:pointer;transition:background .15s}
    .btn-cam:hover{background:#263347;color:#94a3b8}
    .btn-cam.cam-active{border-color:#3b82f6;color:#93c5fd}
    .channels{display:flex;gap:7px;flex-wrap:wrap}
    .ch{padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;border:1px solid}
    .ch-on{background:#0f2a1a;border-color:#22c55e;color:#4ade80}
    .ch-off{background:#1a1a1a;border-color:#334155;color:#475569}
    .btn-row{display:flex;gap:7px;flex-wrap:wrap}
    .btn{flex:1;min-width:80px;background:#1e293b;border:1px solid #334155;color:#94a3b8;
         font-family:inherit;font-size:12px;padding:9px 6px;border-radius:6px;cursor:pointer;transition:background .15s}
    .btn:hover:not(:disabled){background:#263347}.btn:disabled{opacity:.4;cursor:default}
    .log{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px}
    .log h3{font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}
    .log-list{max-height:240px;overflow-y:auto;display:flex;flex-direction:column;gap:5px}
    .log-entry{font-size:11px;padding:6px 8px;border-radius:4px;border-left:2px solid}
    .log-entry-critical{background:#140808;border-color:#ef4444;color:#fca5a5}
    .log-entry-warning{background:#14100a;border-color:#f59e0b;color:#fcd34d}
    .log-entry-success{background:#081408;border-color:#22c55e;color:#86efac}
    .log-sent{color:#475569;font-size:10px;margin-top:2px}
    .log-printer-tag{display:inline-block;background:#1e293b;color:#64748b;font-size:9px;
                     padding:1px 5px;border-radius:3px;margin-right:4px}
    .section-label{font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
    .footer{text-align:center;font-size:10px;color:#1e293b;padding-top:4px}
  </style>
</head>
<body><div class="container">

  <div class="header">
    <div>
      <div class="header-title">Printer Monitor</div>
      <div class="header-sub" id="subTitle">Loading...</div>
    </div>
    <div class="header-meta">
      UI polls: <span id="checkCount">0</span><br>
      Last: <span id="lastCheck">--</span><br>
      Next: <span id="countdown">--</span>
    </div>
  </div>

  <div>
    <div class="section-label">Printers</div>
    <div class="printer-tabs" id="printerTabs">
      <span style="color:#334155;font-size:11px">Loading...</span>
    </div>
  </div>

  <div id="alertsContainer"></div>

  <div class="card">
    <div class="card-top">
      <span class="badge badge-unknown" id="stateBadge">CONNECTING...</span>
      <span class="spinner" id="spinner">syncing...</span>
    </div>
    <div>
      <div class="filename" id="filename">--</div>
      <div class="progress-row">
        <span class="progress-pct" id="progressPct">--</span>
        <span class="progress-layer" id="layerInfo">--</span>
      </div>
      <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill" style="width:0%"></div></div>
    </div>
    <div class="stats-grid">
      <div><div class="stat-label">Elapsed</div><div class="stat-value" id="elapsed">--</div></div>
      <div><div class="stat-label">ETA</div><div class="stat-value" id="eta">--</div></div>
      <div><div class="stat-label">Filament</div><div class="stat-value" id="filament">--</div></div>
      <div><div class="stat-label">Z Position</div><div class="stat-value" id="zpos">--</div></div>
    </div>
    <div class="temps">
      <div class="temp-row">
        <span class="temp-label">Hotend</span>
        <span class="temp-val temp-ok" id="hotendTemp">--</span>
        <span class="temp-target" id="hotendTarget">/ --</span>
        <span id="hotendIcon"></span>
      </div>
      <div class="temp-row">
        <span class="temp-label">Bed</span>
        <span class="temp-val temp-ok" id="bedTemp">--</span>
        <span class="temp-target" id="bedTarget">/ --</span>
        <span id="bedIcon"></span>
      </div>
    </div>
    <div class="camera-section" id="cameraSection" style="display:none">
      <div class="camera-hdr">
        <div class="section-label" style="margin-bottom:0">Camera</div>
        <button class="btn-cam" id="camBtn" onclick="toggleCamera()">Show Camera</button>
      </div>
      <div id="cameraWrap">
        <div class="camera-placeholder" id="camPlaceholder">Camera available -- click Show Camera</div>
        <img id="cameraImg" class="camera-img" src="" alt="Camera" style="display:none"
          onerror="this.style.display='none';document.getElementById('camPlaceholder').style.display='flex'">
      </div>
    </div>
  </div>

  <div>
    <div class="section-label">Alert channels (server-side -- active when this tab is closed)</div>
    <div class="channels">
      <span class="ch ch-off" id="ch-ntfy">Push</span>
      <span class="ch ch-off" id="ch-sms">SMS</span>
      <span class="ch ch-off" id="ch-email">Email</span>
      <span class="ch ch-off" id="ch-imessage">iMessage</span>
    </div>
  </div>

  <div class="btn-row">
    <button class="btn" onclick="triggerPoll(false)">Poll Selected</button>
    <button class="btn" onclick="triggerPoll(true)">Poll All</button>
    <button class="btn" onclick="sendTest()">Test Alerts</button>
    <button class="btn" onclick="openConfig()">Config</button>
  </div>

  <div class="log">
    <h3>Alert Log -- All Printers</h3>
    <div class="log-list" id="alertLogList">
      <div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>
    </div>
  </div>

  <div class="footer">
    Server polls every <span id="intervalLabel">30 min</span> per printer -- alerts fire even when this tab is closed<br>
    Edit monitor_config.json to configure channels and add printers -- localhost:__PORT__
  </div>
</div>

<script>
  let uiPollCount=0,countdownID=null,nextCheckAt=null;
  let selectedPrinter=null,allPrinters=[];
  let cameraEnabled=false,cameraIntervalId=null;

  function fmtTime(s){
    if(s==null||s<0)return"--";
    return Math.floor(s/3600)+"h "+Math.floor((s%3600)/60)+"m "+Math.floor(s%60)+"s";
  }

  function renderPrinterTabs(printers){
    const tabs=document.getElementById("printerTabs");
    if(!printers||!printers.length){
      tabs.innerHTML='<span style="color:#334155;font-size:11px">No printers configured</span>';
      return;
    }
    const online=printers.filter(p=>p.online===true).length;
    document.getElementById("subTitle").textContent=
      printers.length+" printer"+(printers.length!==1?"s":"")+" -- "+online+" online";
    tabs.innerHTML=printers.map(p=>{
      const dc=p.online===true?"dot-online":p.online===false?"dot-offline":"dot-unknown";
      return '<button class="tab-btn'+(p.id===selectedPrinter?" active":"")+'" onclick="selectPrinter(\''+p.id+'\')">'+
             '<span class="tab-dot '+dc+'"></span>'+p.name+'</button>';
    }).join("");
  }

  function selectPrinter(id){
    selectedPrinter=id;
    renderPrinterTabs(allPrinters);
    const p=allPrinters.find(x=>x.id===id);
    if(p){renderPrinterCard(p);renderActiveAlerts(p.active_alerts||[]);}
    cameraEnabled=false; clearInterval(cameraIntervalId);
    document.getElementById("cameraImg").style.display="none";
    document.getElementById("cameraImg").src="";
    document.getElementById("camPlaceholder").style.display="flex";
    document.getElementById("camBtn").textContent="Show Camera";
    document.getElementById("camBtn").classList.remove("cam-active");
  }

  function renderPrinterCard(p){
    const s=p.status||{},ps=s.print_stats||{},ext=s.extruder||{},bed=s.heater_bed||{},
          vsd=s.virtual_sdcard||{},th=s.toolhead||{},pos=th.position||[0,0,0,0];
    const state=ps.state||"unknown",prog=(vsd.progress||0)*100,
          el=ps.print_duration||0,tot=ps.total_duration||0,eta=tot>el?tot-el:-1,
          info=(ps.info&&typeof ps.info==="object")?ps.info:{},
          layer=info.current_layer!==undefined?info.current_layer:"?",
          totL=info.total_layer!==undefined?info.total_layer:"?",
          fil=((ps.filament_used||0)/1000).toFixed(2);
    const b=document.getElementById("stateBadge");
    b.className="badge badge-"+(p.online===false?"offline":state);
    b.textContent=p.online===false?"OFFLINE":state.toUpperCase();
    document.getElementById("filename").textContent=ps.filename||"--";
    document.getElementById("progressPct").textContent=prog.toFixed(1)+"%";
    document.getElementById("layerInfo").textContent="Layer "+layer+"/"+totL;
    document.getElementById("progressFill").style.width=Math.min(prog,100)+"%";
    document.getElementById("elapsed").textContent=fmtTime(el);
    document.getElementById("eta").textContent=fmtTime(eta);
    document.getElementById("filament").textContent=fil+"m";
    document.getElementById("zpos").textContent=(pos[2]||0).toFixed(2)+"mm";
    const hOk=!ext.target||Math.abs((ext.temperature||0)-(ext.target||0))<=20;
    const bOk=!bed.target||Math.abs((bed.temperature||0)-(bed.target||0))<=15;
    document.getElementById("hotendTemp").textContent=(ext.temperature||0).toFixed(1)+"C";
    document.getElementById("hotendTemp").className="temp-val "+(hOk?"temp-ok":"temp-bad");
    document.getElementById("hotendTarget").textContent="/ "+(ext.target||0)+"C";
    document.getElementById("hotendIcon").textContent=hOk?"OK":"!!";
    document.getElementById("bedTemp").textContent=(bed.temperature||0).toFixed(1)+"C";
    document.getElementById("bedTemp").className="temp-val "+(bOk?"temp-ok":"temp-bad");
    document.getElementById("bedTarget").textContent="/ "+(bed.target||0)+"C";
    document.getElementById("bedIcon").textContent=bOk?"OK":"!!";
    document.getElementById("cameraSection").style.display=p.camera_url?"block":"none";
  }

  function refreshCamera(){
    if(!selectedPrinter||!cameraEnabled)return;
    const img=document.getElementById("cameraImg");
    img.src="/camera/"+selectedPrinter+"?t="+Date.now();
    img.style.display="block";
    document.getElementById("camPlaceholder").style.display="none";
  }

  function toggleCamera(){
    cameraEnabled=!cameraEnabled;
    const btn=document.getElementById("camBtn");
    if(cameraEnabled){
      btn.textContent="Hide Camera"; btn.classList.add("cam-active");
      refreshCamera(); cameraIntervalId=setInterval(refreshCamera,5000);
    } else {
      btn.textContent="Show Camera"; btn.classList.remove("cam-active");
      clearInterval(cameraIntervalId);
      document.getElementById("cameraImg").style.display="none";
      document.getElementById("cameraImg").src="";
      document.getElementById("camPlaceholder").style.display="flex";
    }
  }

  function renderActiveAlerts(alerts){
    const c=document.getElementById("alertsContainer"); c.innerHTML="";
    if(!alerts.length){
      c.innerHTML='<div class="alert alert-ok">All systems nominal -- no anomalies detected</div>';
      return;
    }
    alerts.forEach(a=>{
      const d=document.createElement("div");
      d.className="alert alert-"+a.level; d.textContent=a.msg; c.appendChild(d);
    });
    if(alerts.some(a=>a.level==="critical")){
      document.title="!! ALERT -- Printer Monitor";
      setTimeout(()=>{document.title="Printer Monitor";},6000);
      if(Notification.permission==="granted")
        new Notification("Printer Alert",{body:alerts[0].msg});
    }
  }

  function renderAlertLog(log){
    const list=document.getElementById("alertLogList");
    if(!log.length){
      list.innerHTML='<div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>';
      return;
    }
    list.innerHTML=log.slice(0,30).map(e=>{
      const t=new Date(e.time).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
      const sent=e.sent&&e.sent.length?"sent: "+e.sent.join(", "):"no channels enabled";
      const failed=e.failed&&e.failed.length?" -- failed: "+e.failed.map(f=>f[0]).join(","):"";
      const tag=e.printer_name?'<span class="log-printer-tag">'+e.printer_name+'</span>':"";
      return '<div class="log-entry log-entry-'+e.level+'">'+
             tag+'<span style="opacity:.5">['+t+']</span> '+e.msg+
             '<div class="log-sent">'+sent+failed+'</div></div>';
    }).join("");
  }

  function renderChannels(cfg){
    const map={ntfy:cfg.ntfy&&cfg.ntfy.enabled,sms:cfg.twilio&&cfg.twilio.enabled,
               email:cfg.email&&cfg.email.enabled,imessage:cfg.imessage&&cfg.imessage.enabled};
    const labels={ntfy:"Push (ntfy)",sms:"SMS",email:"Email",imessage:"iMessage"};
    for(const[ch,on] of Object.entries(map)){
      const el=document.getElementById("ch-"+ch);
      if(el){el.className="ch "+(on?"ch-on":"ch-off");el.textContent=labels[ch];}
    }
    const s=cfg.poll_interval_seconds||1800,m=Math.round(s/60);
    document.getElementById("intervalLabel").textContent=m>=60?(m/60)+"h":m+" min";
  }

  function updateCountdown(intervalSecs,lastPoll){
    if(countdownID)clearInterval(countdownID);
    if(!lastPoll){document.getElementById("countdown").textContent="--";return;}
    nextCheckAt=new Date(lastPoll).getTime()+intervalSecs*1000;
    countdownID=setInterval(()=>{
      const d=nextCheckAt-Date.now();
      if(d<=0){document.getElementById("countdown").textContent="polling...";return;}
      document.getElementById("countdown").textContent=
        Math.floor(d/60000)+"m "+Math.floor((d%60000)/1000)+"s";
    },1000);
  }

  async function refreshUI(){
    document.getElementById("spinner").style.display="inline";
    try{
      const [pr,ar,cr]=await Promise.all([
        fetch("/api/printers"),fetch("/api/alerts"),fetch("/api/config")
      ]);
      const {printers}=await pr.json();
      const {active_alerts,alert_log}=await ar.json();
      const cfg=await cr.json();
      allPrinters=printers||[];
      if(!selectedPrinter&&allPrinters.length) selectedPrinter=allPrinters[0].id;
      renderPrinterTabs(allPrinters);
      renderChannels(cfg);
      renderAlertLog(alert_log||[]);
      const sel=allPrinters.find(p=>p.id===selectedPrinter);
      if(sel){
        renderPrinterCard(sel);
        renderActiveAlerts(sel.active_alerts||[]);
        updateCountdown(cfg.poll_interval_seconds||1800,sel.last_poll);
      }
      uiPollCount++;
      document.getElementById("checkCount").textContent=uiPollCount;
      document.getElementById("lastCheck").textContent=
        new Date().toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
    }catch(e){
      document.getElementById("alertsContainer").innerHTML=
        '<div class="alert alert-critical">Monitor server unreachable: '+e.message+'</div>';
    }finally{
      document.getElementById("spinner").style.display="none";
    }
  }

  async function triggerPoll(allP){
    const body=allP?{}:{printer_id:selectedPrinter};
    await fetch("/api/poll",{method:"POST",headers:{"Content-Type":"application/json"},
                             body:JSON.stringify(body)});
    setTimeout(refreshUI,2500);
  }
  async function sendTest(){
    await fetch("/api/test_alert",{method:"POST"});
    setTimeout(refreshUI,2500);
  }
  function openConfig(){window.open("/monitor_config.json","_blank");}

  if("Notification"in window&&Notification.permission==="default")
    Notification.requestPermission();
  refreshUI();
  setInterval(refreshUI,30000);
</script></body></html>"""


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self.send_html(HTML.replace("__PORT__", str(PORT)))

        elif path == "/api/printers":
            with lock:
                result = []
                for p in config.get("printers", []):
                    pid   = p["id"]
                    state = printer_states.get(pid, {})
                    result.append({
                        "id":            pid,
                        "name":          p["name"],
                        "host":          p["host"],
                        "enabled":       p.get("enabled", True),
                        "online":        state.get("online"),
                        "last_poll":     state.get("last_poll"),
                        "status":        state.get("last_status", {}),
                        "active_alerts": state.get("active_alerts", []),
                        "camera_url":    state.get("camera_url"),
                        "error_count":   state.get("error_count", 0),
                    })
            self.send_json(200, {"printers": result})

        elif path == "/api/alerts":
            with lock:
                all_active = []
                for p in config.get("printers", []):
                    pid   = p["id"]
                    state = printer_states.get(pid, {})
                    for alert in state.get("active_alerts", []):
                        all_active.append({**alert, "printer_id": pid, "printer_name": p["name"]})
                log = list(alert_log)
            self.send_json(200, {"active_alerts": all_active, "alert_log": log})

        elif path == "/api/config":
            safe = json.loads(json.dumps(config))
            for ch in ("twilio", "email"):
                for k in ("auth_token", "password"):
                    if safe.get(ch, {}).get(k):
                        safe[ch][k] = "????????"
            self.send_json(200, safe)

        elif path == "/monitor_config.json":
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(404, {"error": "config not found"})

        elif path.startswith("/camera/"):
            printer_id   = path[8:]
            state        = printer_states.get(printer_id, {})
            snapshot_url = state.get("camera_url")
            if not snapshot_url:
                self.send_response(404)
                self.end_headers()
                return
            try:
                req = urllib.request.Request(snapshot_url, headers={"Accept": "image/*"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data         = resp.read()
                    content_type = resp.headers.get("Content-Type", "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type",   content_type)
                self.send_header("Content-Length",  len(data))
                self.send_header("Cache-Control",   "no-cache, no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self.send_json(502, {"error": f"Cannot fetch camera snapshot: {e}"})

        elif path.startswith("/proxy/"):
            printers = config.get("printers", [])
            host = printers[0]["host"] if printers else "http://localhost"
            self._proxy_request(host, path[len("/proxy"):])

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        if self.path == "/api/poll":
            printer_id = body.get("printer_id")
            if printer_id:
                p = next((x for x in config.get("printers", []) if x["id"] == printer_id), None)
                if p:
                    threading.Thread(target=poll_once, args=(p,), daemon=True).start()
            else:
                for p in config.get("printers", []):
                    if p.get("enabled", True):
                        threading.Thread(target=poll_once, args=(p,), daemon=True).start()
            self.send_json(200, {"ok": True})

        elif self.path == "/api/test_alert":
            threading.Thread(
                target=dispatch_alert,
                args=("warning", "Test alert -- all enabled channels should receive this"),
                daemon=True
            ).start()
            self.send_json(200, {"ok": True})

        elif self.path == "/api/printers":
            if not body.get("host") or not body.get("name"):
                self.send_json(400, {"error": "name and host are required"})
                return
            pid = body.get("id") or re.sub(r"\W+", "_", body["name"].lower()).strip("_")
            new_printer = {
                "id":      pid,
                "name":    body["name"],
                "host":    body["host"].rstrip("/"),
                "enabled": body.get("enabled", True),
            }
            with lock:
                existing = [p["id"] for p in config.get("printers", [])]
                if pid in existing:
                    self.send_json(409, {"error": f"Printer id '{pid}' already exists"})
                    return
                config.setdefault("printers", []).append(new_printer)
                save_config()
            _init_printer_states()
            threading.Thread(target=poll_printer, args=(new_printer,), daemon=True).start()
            self.send_json(200, {"ok": True, "printer": new_printer})

        else:
            self.send_response(404)
            self.end_headers()

    def _proxy_request(self, host, path):
        target = host + path
        try:
            req = urllib.request.Request(target, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(data))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            self.send_json(502, {"error": f"Cannot reach printer: {e.reason}"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

# ── Interactive: add-printer subcommand ────────────────────────────────────────

def add_printer_interactive():
    """Interactive CLI to add a printer to monitor_config.json."""
    print()
    print("=== Add Printer -- Printer Monitor Setup ===")
    print()

    load_config()
    printers = config.get("printers", [])

    if printers:
        print("  Existing printers:")
        for p in printers:
            status = "enabled" if p.get("enabled", True) else "disabled"
            print(f"    [{p['id']}] {p['name']} -- {p['host']}  ({status})")
    print()

    name = input("  Printer name (e.g. Ender 3, Bambu P1S): ").strip()
    if not name:
        print("  Cancelled."); return

    default_id = re.sub(r"\W+", "_", name.lower()).strip("_")
    pid_input  = input(f"  Printer ID [{default_id}]: ").strip()
    pid        = pid_input if pid_input else default_id

    existing_ids = [p["id"] for p in printers]
    if pid in existing_ids:
        suffix = 2
        while f"{pid}_{suffix}" in existing_ids:
            suffix += 1
        pid = f"{pid}_{suffix}"
        print(f"  ID conflict -- using '{pid}' instead.")

    host = input("  Printer IP or URL (e.g. http://192.168.1.101): ").strip().rstrip("/")
    if not host:
        print("  Cancelled."); return
    if not host.startswith("http"):
        host = "http://" + host

    print(f"\n  Testing connectivity to {host} ...")
    try:
        req = urllib.request.Request(host + "/server/info",
                                     headers={"Accept": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        print("  OK: Printer is reachable!")
    except Exception as e:
        print(f"  Warning: Could not reach printer: {e}")
        cont = input("  Continue anyway? [y/N]: ").strip().lower()
        if cont != "y":
            print("  Cancelled."); return

    enabled_input = input("  Enable monitoring now? [Y/n]: ").strip().lower()
    enabled = enabled_input != "n"

    config["printers"].append({"id": pid, "name": name, "host": host, "enabled": enabled})
    save_config()

    print()
    print(f"  Added '{name}' (id: {pid}) to {CONFIG_FILE}")
    print(f"  Restart monitor_server.py to begin polling this printer.")
    print()

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if SUBCOMMAND == "add-printer":
        add_printer_interactive()
        sys.exit(0)

    load_config()

    printers = config.get("printers", [])
    enabled  = [p for p in printers if p.get("enabled", True)]

    for p in enabled:
        threading.Thread(target=poll_printer, args=(p,), daemon=True).start()

    server       = HTTPServer(("127.0.0.1", PORT), Handler)
    interval_min = config.get("poll_interval_seconds", 1800) // 60
    icons = {k: "YES" if config.get(m, {}).get("enabled") else "no"
             for k, m in [("push", "ntfy"), ("sms", "twilio"),
                          ("email", "email"), ("imsg", "imessage")]}

    print()
    print("==============================================================")
    print("    Multi-Printer Monitor -- Alert Server")
    print("==============================================================")
    print(f"  Monitor  : http://localhost:{PORT}")
    print(f"  Printers : {len(enabled)} of {len(printers)} enabled")
    for p in printers:
        s = "polling" if p.get("enabled", True) else "paused "
        print(f"    [{s}]  {p['name']:<18}  {p['host']}")
    print(f"  Polling  : every {interval_min} min per printer")
    print("--------------------------------------------------------------")
    print(f"  Push: {icons['push']}  SMS: {icons['sms']}  "
          f"Email: {icons['email']}  iMessage: {icons['imsg']}")
    print("--------------------------------------------------------------")
    print("  Add more printers: python3 monitor_server.py add-printer")
    print("  Config file      : monitor_config.json")
    print("==============================================================")
    print()
    print(f"  Open http://localhost:{PORT} in your browser.")
    print("  Press Ctrl+C to stop.")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
