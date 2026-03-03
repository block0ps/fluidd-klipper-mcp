#!/usr/bin/env python3
"""
Printer Monitor — Multi-Printer Proxy + Alert Server
Monitors multiple Klipper/Moonraker printers on a background thread.
Dispatches alerts via push (ntfy.sh), SMS (Twilio), email (SMTP), iMessage (macOS).

Usage:
    python3 monitor_server.py                        # start server
    python3 monitor_server.py http://10.0.107.158    # override printer host (single-printer legacy)
    python3 monitor_server.py add-printer            # interactive CLI to add a printer
"""

import sys, os, json, time, smtplib, threading, subprocess
import urllib.request, urllib.error, urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ── CLI mode detection ─────────────────────────────────────────────────────────

ADD_PRINTER_MODE = len(sys.argv) > 1 and sys.argv[1] == "add-printer"

if ADD_PRINTER_MODE:
    PORT = 0
elif len(sys.argv) > 2 and sys.argv[2].isdigit():
    PORT = int(sys.argv[2])
elif len(sys.argv) > 1 and sys.argv[1].startswith("http"):
    PORT = 8484
else:
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8484

LEGACY_HOST  = sys.argv[1].rstrip("/") if (len(sys.argv) > 1 and sys.argv[1].startswith("http")) else None
CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")

DEFAULT_CONFIG = {
    "printers": [
        {"id": "printer1", "name": "Printer 1", "host": LEGACY_HOST or "http://10.0.107.158",
         "enabled": True, "api_token": ""}
    ],
    "poll_interval_seconds": 1800,
    "alert_on_warnings": True,
    "ntfy":     {"enabled": False, "topic": "my-printer-alerts", "server": "https://ntfy.sh"},
    "twilio":   {"enabled": False, "account_sid": "", "auth_token": "",
                 "from_number": "", "to_number": ""},
    "email":    {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                 "username": "", "password": "", "from_address": "", "to_address": ""},
    "imessage": {"enabled": False, "to_number": ""}
}

config       = {}
alert_log    = []           # combined across all printers, newest first
lock         = threading.Lock()

# Per-printer state: id → {status, cameras, active_alerts, fired_alerts, last_poll, errors}
printer_states = {}

# ── Config ─────────────────────────────────────────────────────────────────────

def _migrate_old_config(saved):
    """Auto-migrate single-printer config format to multi-printer printers array."""
    if "printers" not in saved and "printer_host" in saved:
        host = saved.pop("printer_host", "http://10.0.107.158")
        saved["printers"] = [
            {"id": "printer1", "name": "Printer 1", "host": host,
             "enabled": True, "api_token": ""}
        ]
        print("  ℹ️  Migrated single-printer config to multi-printer format.")
    return saved

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        saved = _migrate_old_config(saved)
        # Deep-merge with defaults (preserves sub-keys not in saved)
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for k, v in saved.items():
            if isinstance(v, dict) and k in merged and isinstance(merged[k], dict):
                merged[k].update(v)
            else:
                merged[k] = v
        config = merged
    else:
        config = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config()
        print(f"  Created {CONFIG_FILE} — edit it to enable alert channels.\n")
    # Init printer_states for each printer
    for p in config.get("printers", []):
        pid = p.get("id", p.get("name","printer").lower())
        if pid not in printer_states:
            printer_states[pid] = {
                "status": {}, "cameras": [], "active_alerts": [],
                "fired_alerts": set(), "last_poll": None, "errors": 0
            }

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def get_printer_by_id(pid):
    for p in config.get("printers", []):
        if p.get("id") == pid or p.get("name","").lower() == pid:
            return p
    return None

# ── Anomaly detection ──────────────────────────────────────────────────────────

def detect_anomalies(s):
    alerts = []
    ps  = s.get("print_stats", {})
    ext = s.get("extruder", {})
    bed = s.get("heater_bed", {})
    vsd = s.get("virtual_sdcard", {})
    wh  = s.get("webhooks", {})
    th  = s.get("toolhead", {})
    pos = th.get("position", [0, 0, 0, 0])

    if wh.get("state", "ready") != "ready":
        alerts.append(("critical", f"Klippy not ready: {wh.get('state')} — {wh.get('state_message','')}"))
    if bed.get("target", 0) > 0 and abs(bed.get("temperature", 0) - bed.get("target", 0)) > 15:
        alerts.append(("critical", f"Thermal anomaly — Bed: target {bed['target']}°C, actual {bed.get('temperature',0):.1f}°C"))
    if ext.get("target", 0) > 0 and abs(ext.get("temperature", 0) - ext.get("target", 0)) > 20:
        alerts.append(("critical", f"Thermal anomaly — Hotend: target {ext['target']}°C, actual {ext.get('temperature',0):.1f}°C"))

    state   = ps.get("state", "")
    elapsed = ps.get("print_duration", 0)
    fil     = ps.get("filament_used", 0)
    prog    = vsd.get("progress", 0)

    if state == "printing" and elapsed > 300 and fil < 5:
        alerts.append(("warning", "Possible clog / under-extrusion: very low filament after 5+ min"))
    if state == "printing" and elapsed > 600 and prog < 0.001:
        alerts.append(("warning", "Possible stall: no progress detected after 10 min"))
    if state == "printing" and elapsed > 120 and pos[2] < 0.1:
        alerts.append(("warning", f"Z position anomaly: Z={pos[2]:.3f}mm while printing"))
    if state == "error":
        alerts.append(("critical", f"Print error: {ps.get('message', 'unknown')}"))
    if state == "cancelled":
        alerts.append(("warning", "Print was cancelled"))
    if state == "complete":
        alerts.append(("success", f"Print complete! {ps.get('filename', '')}"))
    return alerts

# ── Alert channels ─────────────────────────────────────────────────────────────

def send_ntfy(title, body, level):
    cfg = config.get("ntfy", {})
    if not cfg.get("enabled"): return False, "disabled"
    priority = {"critical": "urgent", "warning": "high", "success": "default"}.get(level, "default")
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    topic  = cfg.get("topic", "printer-alerts")
    url    = f"{server}/{topic}"
    # HTTP headers must be ASCII — emoji go in the body only, not the Title header
    safe_title = title.encode("ascii", "ignore").decode("ascii").strip()
    if not safe_title:
        safe_title = f"Printer Alert [{level.upper()}]"
    try:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers={
                "Title":        safe_title,
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
    if not cfg.get("enabled"): return False, "disabled"
    sid, tok, frm, to = (cfg.get(k, "").strip() for k in
                         ("account_sid", "auth_token", "from_number", "to_number"))
    if not all([sid, tok, frm, to]): return False, "missing credentials"
    import base64
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From": frm, "To": to, "Body": body}).encode()
    b64  = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    try:
        req = urllib.request.Request(url, data=data, method="POST",
            headers={"Authorization": f"Basic {b64}",
                     "Content-Type":  "application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_email(subject, body):
    cfg = config.get("email", {})
    if not cfg.get("enabled"): return False, "disabled"
    host = cfg.get("smtp_host", "smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))
    user = cfg.get("username", "").strip()
    pwd  = cfg.get("password",  "").strip()
    frm  = cfg.get("from_address", user).strip()
    to   = cfg.get("to_address",   "").strip()
    if not all([user, pwd, to]): return False, "missing credentials"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject; msg["From"] = frm; msg["To"] = to
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(); s.login(user, pwd); s.sendmail(frm, to, msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_imessage(body):
    cfg = config.get("imessage", {})
    if not cfg.get("enabled"): return False, "disabled"
    to = cfg.get("to_number", "").strip()
    if not to: return False, "no recipient configured"
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

def dispatch_alert(level, msg, printer_name="", printer_id=""):
    ts      = datetime.now().strftime("%H:%M:%S")
    prefix  = f"[{printer_name}] " if printer_name else ""
    title   = f"{prefix}Printer Alert [{level.upper()}]"
    host_str = ""
    if printer_id:
        p = get_printer_by_id(printer_id)
        if p: host_str = f"\nPrinter: {p.get('name','')} ({p.get('host','')})"
    full    = f"[{ts}] {prefix}{msg}{host_str}"
    results = {
        "ntfy":     send_ntfy(title, full, level),
        "sms":      send_twilio_sms(f"Printer Alert {prefix}: {msg}"),
        "email":    send_email(title, full),
        "imessage": send_imessage(f"Printer Alert {prefix}: {msg}"),
    }
    sent   = [ch for ch, (ok, _)  in results.items() if ok]
    failed = [(ch, err) for ch, (ok, err) in results.items() if not ok and err != "disabled"]
    entry  = {
        "time": datetime.now().isoformat(), "level": level,
        "msg":  f"{prefix}{msg}", "sent": sent, "failed": failed,
        "printer_id": printer_id, "printer_name": printer_name
    }
    with lock:
        alert_log.insert(0, entry)
        if len(alert_log) > 200: alert_log.pop()
    print(f"  [{ts}] ALERT {prefix}— {msg}")
    print(f"         sent: {', '.join(sent) if sent else 'none'}" +
          (f" | failed: {', '.join(f'{c}({e})' for c,e in failed)}" if failed else ""))

# ── Polling ────────────────────────────────────────────────────────────────────

def _fetch_printer(printer):
    host  = printer.get("host", "").rstrip("/")
    token = printer.get("api_token", "")
    url   = (host + "/printer/objects/query"
             "?print_stats&extruder&heater_bed&toolhead&virtual_sdcard&webhooks&display_status")
    headers = {"Accept": "application/json"}
    if token: headers["X-Api-Key"] = token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("result", {}).get("status", {})

def _fetch_cameras(printer):
    host  = printer.get("host", "").rstrip("/")
    token = printer.get("api_token", "")
    url   = host + "/server/webcams/list"
    headers = {"Accept": "application/json"}
    if token: headers["X-Api-Key"] = token
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            cams = json.loads(resp.read()).get("result", {}).get("webcams", [])
        result = []
        for cam in cams:
            snap   = cam.get("snapshot_url", "")
            stream = cam.get("stream_url", "")
            if not snap.startswith("http"):   snap   = f"{host}{snap}"
            if not stream.startswith("http"): stream = f"{host}{stream}"
            result.append({"name": cam.get("name","Camera"), "snapshot_url": snap, "stream_url": stream})
        return result
    except:
        return []

def _process_printer(printer, status):
    pid    = printer.get("id", printer.get("name","").lower())
    pname  = printer.get("name", "")
    state  = printer_states.get(pid, {})
    ps     = status.get("print_stats", {})
    job_id = ps.get("filename", "") + str(round(ps.get("print_duration", 0) / 60))

    # Clear dedup set when printer goes idle
    if ps.get("state", "") not in ("printing", "paused"):
        with lock: state.get("fired_alerts", set()).clear()

    anomalies = detect_anomalies(status)
    alert_on_warnings = config.get("alert_on_warnings", True)
    for level, msg in anomalies:
        key = f"{level}:{msg}"
        should_fire = level in ("critical", "success") or (level == "warning" and alert_on_warnings)
        with lock: already = key in state.get("fired_alerts", set())
        if should_fire and not already:
            with lock: state.setdefault("fired_alerts", set()).add(key)
            dispatch_alert(level, msg, printer_name=pname, printer_id=pid)

    with lock:
        printer_states[pid]["status"]        = status
        printer_states[pid]["active_alerts"] = [{"level": l, "msg": m} for l, m in anomalies]
        printer_states[pid]["last_poll"]     = datetime.now().isoformat()
        printer_states[pid]["errors"]        = 0

def poll_all_printers():
    while True:
        interval = config.get("poll_interval_seconds", 1800)
        for printer in config.get("printers", []):
            if not printer.get("enabled", True): continue
            pid   = printer.get("id", printer.get("name","").lower())
            pname = printer.get("name", pid)
            try:
                status  = _fetch_printer(printer)
                cameras = _fetch_cameras(printer)
                _process_printer(printer, status)
                with lock:
                    printer_states[pid]["cameras"] = cameras
                ps   = status.get("print_stats", {})
                vsd  = status.get("virtual_sdcard", {})
                ts   = datetime.now().strftime("%H:%M:%S")
                pct  = vsd.get("progress", 0) * 100
                st   = ps.get("state", "?")
                print(f"  [{ts}] [{pname}] Poll OK — {st.upper()} {pct:.1f}%")
            except Exception as e:
                with lock:
                    printer_states.setdefault(pid, {})["errors"] = \
                        printer_states.get(pid, {}).get("errors", 0) + 1
                print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{pname}] Poll error: {e}")
        time.sleep(interval)

def poll_once_all():
    for printer in config.get("printers", []):
        if not printer.get("enabled", True): continue
        pid = printer.get("id", printer.get("name","").lower())
        try:
            status  = _fetch_printer(printer)
            cameras = _fetch_cameras(printer)
            _process_printer(printer, status)
            with lock: printer_states[pid]["cameras"] = cameras
        except Exception as e:
            print(f"  poll_once [{pid}] error: {e}")

def poll_once_printer(pid):
    p = get_printer_by_id(pid)
    if not p: return
    try:
        status  = _fetch_printer(p)
        cameras = _fetch_cameras(p)
        _process_printer(p, status)
        with lock: printer_states[pid]["cameras"] = cameras
    except Exception as e:
        print(f"  poll_once [{pid}] error: {e}")

# ── HTML ────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>🖨️ Printer Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0f;color:#e2e8f0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;min-height:100vh;padding:20px}
.container{max-width:900px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.header{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #1e293b;padding-bottom:12px}
.header-title{font-size:15px;font-weight:700;color:#f8fafc}
.header-sub{font-size:11px;color:#475569;margin-top:2px}
.header-meta{text-align:right;font-size:11px;color:#475569;line-height:1.8}
.header-meta span{color:#94a3b8}
.channels{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px}
.ch{padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;border:1px solid}
.ch-on{background:#0f2a1a;border-color:#22c55e;color:#4ade80}
.ch-off{background:#1a1a1a;border-color:#334155;color:#475569}
.tabs{display:flex;gap:6px;flex-wrap:wrap}
.tab{padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid #334155;background:#0f172a;color:#64748b;transition:all .15s}
.tab:hover{background:#1e293b;color:#94a3b8}
.tab.active{background:#1e3a5f;border-color:#3b82f6;color:#93c5fd}
.printer-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}
.card{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:12px}
.card-header{display:flex;justify-content:space-between;align-items:center}
.card-name{font-size:12px;font-weight:700;color:#94a3b8}
.card-host{font-size:10px;color:#334155;margin-top:1px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;color:#fff}
.badge-printing{background:#16a34a}.badge-paused{background:#d97706}
.badge-error{background:#dc2626}.badge-complete{background:#2563eb}
.badge-cancelled,.badge-standby,.badge-unknown,.badge-connecting{background:#334155}
.badge-offline{background:#7f1d1d}
.alert-strip{border-radius:5px;padding:7px 10px;font-size:11px;font-weight:600;border-left:3px solid;margin-bottom:4px}
.alert-critical{background:#1a0a0a;border-color:#ef4444;color:#fca5a5}
.alert-warning{background:#1a150a;border-color:#f59e0b;color:#fcd34d}
.alert-success{background:#0a1a0f;border-color:#22c55e;color:#86efac}
.alert-ok{background:#0a120f;border-color:#166534;color:#4ade80;font-weight:500}
.filename{font-size:11px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.progress-row{display:flex;justify-content:space-between;font-size:12px;margin-top:4px}
.progress-pct{color:#4ade80;font-weight:700}.progress-info{color:#64748b}
.progress-bar-bg{background:#1e293b;border-radius:99px;height:6px;margin-top:6px;overflow:hidden}
.progress-bar-fill{height:100%;border-radius:99px;background:#22c55e;transition:width .6s ease}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat-label{color:#475569;font-size:10px}.stat-value{color:#f1f5f9;font-size:12px;margin-top:1px}
.temps{border-top:1px solid #1e293b;padding-top:10px;display:flex;flex-direction:column;gap:6px}
.temp-row{display:flex;align-items:center;gap:8px}
.temp-label{color:#475569;width:52px;font-size:10px}
.temp-val{font-weight:700;font-size:13px}.temp-ok{color:#4ade80}.temp-bad{color:#f87171}
.temp-target{color:#475569;font-size:10px}
.camera-section{border-top:1px solid #1e293b;padding-top:10px}
.camera-toggle{background:none;border:1px solid #334155;color:#64748b;font-family:inherit;font-size:10px;padding:3px 10px;border-radius:4px;cursor:pointer;margin-bottom:8px}
.camera-toggle:hover{background:#1e293b;color:#94a3b8}
.camera-img{width:100%;border-radius:4px;border:1px solid #1e293b;display:none;background:#050a14}
.camera-img.visible{display:block}
.camera-no-cam{font-size:10px;color:#334155}
.btn-row{display:flex;gap:6px}
.btn{flex:1;background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:11px;padding:8px;border-radius:6px;cursor:pointer;transition:background .15s}
.btn:hover:not(:disabled){background:#263347}.btn:disabled{opacity:.4;cursor:default}
.log{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px}
.log h3{font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.log-list{max-height:260px;overflow-y:auto;display:flex;flex-direction:column;gap:4px}
.log-entry{font-size:11px;padding:6px 8px;border-radius:4px;border-left:2px solid}
.log-entry-critical{background:#140808;border-color:#ef4444;color:#fca5a5}
.log-entry-warning{background:#14100a;border-color:#f59e0b;color:#fcd34d}
.log-entry-success{background:#081408;border-color:#22c55e;color:#86efac}
.log-sent{color:#475569;font-size:10px;margin-top:2px}
.printer-badge{display:inline-block;background:#1e293b;color:#64748b;border-radius:3px;padding:1px 5px;font-size:9px;margin-right:4px}
.spinner{font-size:10px;color:#475569;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.section-label{font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:.08em}
.footer{text-align:center;font-size:10px;color:#1e293b;padding-top:4px}
.error-badge{font-size:10px;color:#f87171;margin-left:6px}
</style></head>
<body><div class="container">

<div class="header">
  <div>
    <div class="header-title">🖨️ Multi-Printer Monitor</div>
    <div class="header-sub" id="printerCount">Loading printers…</div>
  </div>
  <div class="header-meta">
    UI polls: <span id="checkCount">0</span><br>
    Last: <span id="lastCheck">—</span>
  </div>
</div>

<div>
  <div class="section-label" style="margin-bottom:6px">Alert channels (server-side)</div>
  <div class="channels" id="channelBar">
    <span class="ch ch-off" id="ch-ntfy">📱 Push</span>
    <span class="ch ch-off" id="ch-sms">💬 SMS</span>
    <span class="ch ch-off" id="ch-email">📧 Email</span>
    <span class="ch ch-off" id="ch-imessage">💬 iMessage</span>
  </div>
</div>

<div id="tabBar" class="tabs" style="display:none"></div>

<div class="printer-grid" id="printerGrid"></div>

<div class="btn-row">
  <button class="btn" onclick="pollAll()">🔄 Poll All</button>
  <button class="btn" onclick="sendTest()">🔔 Test Alerts</button>
  <button class="btn" onclick="openConfig()">⚙️ Config</button>
</div>

<div class="log">
  <h3>Alert Dispatch Log</h3>
  <div class="log-list" id="alertLogList">
    <div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>
  </div>
</div>

<div class="footer">
  Server polls every <span id="intervalLabel">—</span> — alerts fire even when tab is closed<br>
  Edit <strong>monitor_config.json</strong> to add printers or enable channels
</div>
</div>

<script>
let uiPollCount = 0;
let cameraIntervals = {};
let allPrinters = [];
let selectedPrinter = null;

function fmtTime(s) {
  if (s < 0) return "—";
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ${Math.floor(s%60)}s`;
}

function fmtTemp(t) { return t != null ? `${t.toFixed(1)}°C` : "—"; }

async function refreshUI() {
  try {
    const [pr, ar, cr] = await Promise.all([
      fetch("/api/printers").then(r=>r.json()),
      fetch("/api/alerts").then(r=>r.json()),
      fetch("/api/config").then(r=>r.json())
    ]);
    allPrinters = pr.printers || [];
    renderChannels(cr);
    renderPrinterTabs(allPrinters);
    renderPrinterGrid(allPrinters);
    renderAlertLog(ar.alert_log || []);
    const m = Math.round((cr.poll_interval_seconds||1800)/60);
    document.getElementById("intervalLabel").textContent = m >= 60 ? `${m/60}h` : `${m} min`;
    const active = allPrinters.filter(p=>p.enabled).length;
    document.getElementById("printerCount").textContent =
      `${active} printer${active!==1?"s":""} monitored`;
    uiPollCount++;
    document.getElementById("checkCount").textContent = uiPollCount;
    document.getElementById("lastCheck").textContent =
      new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
  } catch(e) {
    document.getElementById("printerCount").textContent = `❌ Monitor server unreachable: ${e.message}`;
  }
}

function renderChannels(cfg) {
  const map = {ntfy: cfg.ntfy?.enabled, sms: cfg.twilio?.enabled,
               email: cfg.email?.enabled, imessage: cfg.imessage?.enabled};
  const labels = {ntfy:"📱 Push", sms:"💬 SMS", email:"📧 Email", imessage:"💬 iMessage"};
  for (const [ch, on] of Object.entries(map)) {
    const el = document.getElementById(`ch-${ch}`);
    if (el) { el.className = `ch ${on?"ch-on":"ch-off"}`; el.textContent = labels[ch]; }
  }
}

function renderPrinterTabs(printers) {
  const bar = document.getElementById("tabBar");
  if (printers.length <= 1) { bar.style.display = "none"; return; }
  bar.style.display = "flex";
  const currentIds = [...bar.querySelectorAll(".tab")].map(t=>t.dataset.id);
  const newIds = printers.map(p=>p.id);
  if (JSON.stringify(currentIds) === JSON.stringify(newIds)) {
    // just update active state
    for (const t of bar.querySelectorAll(".tab")) {
      t.classList.toggle("active", t.dataset.id === selectedPrinter);
    }
    return;
  }
  bar.innerHTML = "";
  printers.forEach(p => {
    const t = document.createElement("div");
    t.className = "tab" + (p.id === selectedPrinter ? " active" : "");
    t.dataset.id = p.id;
    const stateClass = (p.state||"unknown").toLowerCase();
    const icon = p.errors > 0 ? "❌" : (p.state === "printing" ? "🟢" : p.state === "error" ? "🔴" : "⚪");
    t.textContent = `${icon} ${p.name}`;
    t.onclick = () => selectPrinter(p.id);
    bar.appendChild(t);
  });
}

function selectPrinter(id) {
  selectedPrinter = id;
  for (const t of document.querySelectorAll(".tab")) {
    t.classList.toggle("active", t.dataset.id === id);
  }
}

function renderPrinterGrid(printers) {
  const grid = document.getElementById("printerGrid");
  for (const p of printers) {
    let card = document.getElementById(`card-${p.id}`);
    if (!card) {
      card = document.createElement("div");
      card.className = "card"; card.id = `card-${p.id}`;
      grid.appendChild(card);
    }
    const s = p.status || {};
    const ps = s.print_stats || {}, ext = s.extruder || {}, bed = s.heater_bed || {},
          vsd = s.virtual_sdcard || {}, th = s.toolhead || {}, pos = th.position || [0,0,0,0];
    const state = p.errors > 0 ? "offline" : (ps.state || "unknown");
    const prog  = (vsd.progress || 0) * 100;
    const el    = ps.print_duration || 0, tot = ps.total_duration || 0;
    const eta   = tot > el ? tot - el : -1;
    const fil   = ((ps.filament_used || 0) / 1000).toFixed(2);
    const hOk   = ext.target == null || ext.target === 0 || Math.abs(ext.temperature - ext.target) <= 20;
    const bOk   = bed.target == null || bed.target === 0 || Math.abs(bed.temperature - bed.target) <= 15;
    const errBadge = p.errors > 0 ? `<span class="error-badge">⚠ ${p.errors} error${p.errors>1?"s":""}</span>` : "";

    // alerts for this printer
    let alertHTML = "";
    if (p.active_alerts && p.active_alerts.length) {
      alertHTML = p.active_alerts.map(a =>
        `<div class="alert-strip alert-${a.level}">${a.msg}</div>`
      ).join("");
    } else if (state !== "offline") {
      alertHTML = `<div class="alert-strip alert-ok">✅ All systems nominal</div>`;
    }

    // camera section
    const cams = p.cameras || [];
    let camHTML = "";
    if (cams.length > 0) {
      const snapUrl = cams[0].snapshot_url;
      camHTML = `
        <div class="camera-section">
          <button class="camera-toggle" onclick="toggleCamera('${p.id}', '${snapUrl.replace(/'/g,"\\'")}')">
            📷 ${cams[0].name || "Camera"} — show/hide
          </button>
          <img id="cam-${p.id}" class="camera-img" alt="Camera feed"
               src="" loading="lazy"/>
        </div>`;
    }

    card.innerHTML = `
      <div class="card-header">
        <div>
          <div class="card-name">${p.name}${errBadge}</div>
          <div class="card-host">${p.host}</div>
        </div>
        <span class="badge badge-${state}">${state.toUpperCase()}</span>
      </div>
      <div>${alertHTML}</div>
      <div>
        <div class="filename">${ps.filename || "—"}</div>
        <div class="progress-row">
          <span class="progress-pct">${prog.toFixed(1)}%</span>
          <span class="progress-info">
            L${ps.info?.current_layer ?? "?"}/${ps.info?.total_layer ?? "?"}
            · ETA ${fmtTime(eta)}
          </span>
        </div>
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" style="width:${Math.min(prog,100)}%"></div>
        </div>
      </div>
      <div class="stats-grid">
        <div><div class="stat-label">Elapsed</div><div class="stat-value">${fmtTime(el)}</div></div>
        <div><div class="stat-label">Filament</div><div class="stat-value">${fil}m</div></div>
        <div><div class="stat-label">Z Position</div><div class="stat-value">${pos[2]?.toFixed(2)}mm</div></div>
        <div><div class="stat-label">Last Poll</div><div class="stat-value">${
          p.last_poll ? new Date(p.last_poll).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"}) : "—"}</div></div>
      </div>
      <div class="temps">
        <div class="temp-row">
          <span class="temp-label">Hotend</span>
          <span class="temp-val ${hOk?"temp-ok":"temp-bad"}">${fmtTemp(ext.temperature)}</span>
          <span class="temp-target">/ ${fmtTemp(ext.target)}</span>
          <span>${hOk?"✅":"⚠️"}</span>
        </div>
        <div class="temp-row">
          <span class="temp-label">Bed</span>
          <span class="temp-val ${bOk?"temp-ok":"temp-bad"}">${fmtTemp(bed.temperature)}</span>
          <span class="temp-target">/ ${fmtTemp(bed.target)}</span>
          <span>${bOk?"✅":"⚠️"}</span>
        </div>
      </div>
      ${camHTML}
    `;

    // Flash title on critical
    if (p.active_alerts && p.active_alerts.some(a => a.level === "critical")) {
      document.title = "🚨 ALERT — Printer Monitor";
      setTimeout(() => { document.title = "🖨️ Printer Monitor"; }, 6000);
      if (Notification.permission === "granted")
        new Notification(`🖨️ ${p.name} Alert`, {body: p.active_alerts[0].msg});
    }
  }
}

function toggleCamera(pid, snapUrl) {
  const img = document.getElementById(`cam-${pid}`);
  if (!img) return;
  const visible = img.classList.toggle("visible");
  if (visible) {
    img.src = snapUrl + (snapUrl.includes("?") ? "&" : "?") + "_t=" + Date.now();
    if (cameraIntervals[pid]) clearInterval(cameraIntervals[pid]);
    cameraIntervals[pid] = setInterval(() => {
      if (img.classList.contains("visible")) {
        img.src = snapUrl + (snapUrl.includes("?") ? "&" : "?") + "_t=" + Date.now();
      }
    }, 5000);
  } else {
    if (cameraIntervals[pid]) { clearInterval(cameraIntervals[pid]); delete cameraIntervals[pid]; }
    img.src = "";
  }
}

function renderAlertLog(log) {
  const list = document.getElementById("alertLogList");
  if (!log.length) {
    list.innerHTML = '<div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>';
    return;
  }
  list.innerHTML = log.slice(0, 30).map(e => {
    const t = new Date(e.time).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
    const sent   = e.sent.length ? `sent: ${e.sent.join(", ")}` : "no channels enabled";
    const failed = e.failed.length ? ` · failed: ${e.failed.map(f=>f[0]).join(",")}` : "";
    const pBadge = e.printer_name ? `<span class="printer-badge">${e.printer_name}</span>` : "";
    return `<div class="log-entry log-entry-${e.level}">
      <span style="opacity:.5">[${t}]</span> ${pBadge}${e.msg}
      <div class="log-sent">${sent}${failed}</div>
    </div>`;
  }).join("");
}

async function pollAll() {
  await fetch("/api/poll", {method:"POST"});
  setTimeout(refreshUI, 2500);
}
async function sendTest() {
  await fetch("/api/test_alert", {method:"POST"});
  setTimeout(refreshUI, 2500);
}
function openConfig() { window.open("/monitor_config.json","_blank"); }

if ("Notification" in window && Notification.permission === "default")
  Notification.requestPermission();

refreshUI();
setInterval(refreshUI, 15000);
</script></body></html>"""

# ── HTTP Handler ───────────────────────────────────────────────────────────────

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
            self.send_html(HTML)

        elif path == "/api/printers":
            printers_out = []
            for p in config.get("printers", []):
                pid   = p.get("id", p.get("name","").lower())
                state = printer_states.get(pid, {})
                ps    = state.get("status", {}).get("print_stats", {})
                printers_out.append({
                    "id":             pid,
                    "name":           p.get("name", pid),
                    "host":           p.get("host", ""),
                    "enabled":        p.get("enabled", True),
                    "status":         state.get("status", {}),
                    "cameras":        state.get("cameras", []),
                    "active_alerts":  state.get("active_alerts", []),
                    "last_poll":      state.get("last_poll"),
                    "errors":         state.get("errors", 0),
                    "state":          ps.get("state", "unknown"),
                })
            self.send_json(200, {"printers": printers_out})

        elif path == "/api/alerts":
            with lock: log = list(alert_log)
            all_alerts = []
            for p in config.get("printers", []):
                pid = p.get("id", p.get("name","").lower())
                all_alerts.extend(printer_states.get(pid, {}).get("active_alerts", []))
            self.send_json(200, {"active_alerts": all_alerts, "alert_log": log})

        elif path == "/api/config":
            safe = json.loads(json.dumps(config))
            for ch in ("twilio", "email"):
                for k in ("auth_token", "password"):
                    if safe.get(ch, {}).get(k): safe[ch][k] = "••••••••"
            self.send_json(200, safe)

        elif path == "/monitor_config.json":
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "rb") as f: body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(404, {"error": "config not found"})

        elif path.startswith("/proxy/"):
            # /proxy/{printer_id}/rest/of/path
            parts = path[len("/proxy/"):].split("/", 1)
            pid   = parts[0]
            tail  = "/" + parts[1] if len(parts) > 1 else "/"
            # Re-attach query string
            qs = self.path[self.path.find("?")+1:] if "?" in self.path else ""
            if qs: tail += f"?{qs}"
            self._proxy(pid, tail)

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/poll":
            qs  = self.path[self.path.find("?")+1:] if "?" in self.path else ""
            pid = dict(urllib.parse.parse_qsl(qs)).get("id")
            if pid:
                threading.Thread(target=poll_once_printer, args=(pid,), daemon=True).start()
            else:
                threading.Thread(target=poll_once_all, daemon=True).start()
            self.send_json(200, {"ok": True})

        elif path == "/api/test_alert":
            threading.Thread(target=dispatch_alert,
                args=("warning", "Test alert — all enabled channels should receive this"),
                daemon=True).start()
            self.send_json(200, {"ok": True})

        elif path == "/api/printers":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                new_printer = json.loads(body)
                pid = new_printer.get("id") or new_printer.get("name","").lower().replace(" ","_")
                new_printer["id"] = pid
                # Check for duplicate
                existing_ids = [p.get("id") for p in config.get("printers", [])]
                if pid in existing_ids:
                    self.send_json(409, {"error": f"Printer ID '{pid}' already exists"})
                    return
                config.setdefault("printers", []).append(new_printer)
                printer_states[pid] = {
                    "status": {}, "cameras": [], "active_alerts": [],
                    "fired_alerts": set(), "last_poll": None, "errors": 0
                }
                save_config()
                threading.Thread(target=poll_once_printer, args=(pid,), daemon=True).start()
                self.send_json(201, {"ok": True, "id": pid})
            except Exception as e:
                self.send_json(400, {"error": str(e)})

        else:
            self.send_response(404); self.end_headers()

    def _proxy(self, printer_id, path):
        p = get_printer_by_id(printer_id)
        if not p:
            self.send_json(404, {"error": f"Printer '{printer_id}' not found"})
            return
        target = p.get("host", "").rstrip("/") + path
        token  = p.get("api_token", "")
        headers = {"Accept": "application/json"}
        if token: headers["X-Api-Key"] = token
        try:
            req = urllib.request.Request(target, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data         = resp.read()
                content_type = resp.headers.get("Content-Type", "application/json")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", len(data))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            self.send_json(502, {"error": f"Cannot reach printer: {e.reason}"})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

# ── CLI: add-printer ───────────────────────────────────────────────────────────

def run_add_printer_cli():
    """Interactive CLI to add a new printer to monitor_config.json."""
    print("""
╔══════════════════════════════════════════════════════╗
║         🖨️  Add Printer — Monitor Config             ║
╚══════════════════════════════════════════════════════╝
""")
    load_config()
    existing = config.get("printers", [])
    if existing:
        print("  Current printers:")
        for p in existing:
            print(f"    • {p.get('name')} ({p.get('host')})")
        print()

    name = input("  Printer name (e.g. 'Ender 3 Pro'): ").strip()
    if not name:
        print("  ❌ Name is required.")
        sys.exit(1)
    host = input("  Printer host URL (e.g. http://192.168.1.101): ").strip().rstrip("/")
    if not host:
        print("  ❌ Host is required.")
        sys.exit(1)
    token = input("  API token (leave blank if none): ").strip()

    pid = name.lower().replace(" ", "_").replace("/","_")
    # Deduplicate id
    existing_ids = [p.get("id","") for p in existing]
    base = pid; n = 2
    while pid in existing_ids:
        pid = f"{base}_{n}"; n += 1

    new_p = {"id": pid, "name": name, "host": host, "enabled": True, "api_token": token}

    # Test connectivity
    print(f"\n  Testing connectivity to {host} …")
    try:
        url = host + "/server/info"
        headers = {"Accept": "application/json"}
        if token: headers["X-Api-Key"] = token
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            info  = json.loads(resp.read()).get("result", {})
            state = info.get("klippy_state", "unknown")
        print(f"  ✅ Reachable — Klippy state: {state}")
    except Exception as e:
        print(f"  ⚠️  Could not reach printer: {e}")
        cont = input("  Add anyway? [y/N]: ").strip().lower()
        if cont != "y":
            print("  Aborted.")
            sys.exit(0)

    config.setdefault("printers", []).append(new_p)
    save_config()
    print(f"\n  ✅ Added '{name}' (id: {pid}) to {CONFIG_FILE}")
    print(f"  Restart monitor_server.py to start monitoring this printer.\n")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if ADD_PRINTER_MODE:
        run_add_printer_cli()
        sys.exit(0)

    load_config()

    # Start background polling thread
    threading.Thread(target=poll_all_printers, daemon=True).start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)

    printers    = config.get("printers", [])
    n_printers  = len(printers)
    interval_min = config.get("poll_interval_seconds", 1800) // 60

    icons = {k: "✅" if config.get(m, {}).get("enabled") else "❌"
             for k, m in [("ntfy","ntfy"),("sms","twilio"),("email","email"),("imsg","imessage")]}

    print(f"""
╔══════════════════════════════════════════════════════╗
║       🖨️  Multi-Printer Monitor — Alert Server       ║
╠══════════════════════════════════════════════════════╣
║  Monitor  : http://localhost:{PORT:<26}║
║  Printers : {n_printers} configured{" " * max(0, 37-len(str(n_printers))-11)}║""")
    for p in printers:
        name = p.get("name","?")[:30]; host = p.get("host","?")[:36]
        en   = "✅" if p.get("enabled", True) else "❌"
        print(f"║    {en} {name:<30} {host:<36}║")
    print(f"""╠══════════════════════════════════════════════════════╣
║  Polling  : every {interval_min} min (background thread)          ║
╠══════════════════════════════════════════════════════╣
║  Alert Channels                                      ║
║    {icons['ntfy']} Push (ntfy.sh)   {icons['sms']} SMS (Twilio)          ║
║    {icons['email']} Email (SMTP)    {icons['imsg']} iMessage (macOS)      ║
╠══════════════════════════════════════════════════════╣
║  Add a printer: python3 monitor_server.py add-printer║
║  Config file : monitor_config.json                   ║
╚══════════════════════════════════════════════════════╝

  Open  http://localhost:{PORT}  in your browser.
  Alerts fire on background thread — tab can be closed.
  Press Ctrl+C to stop.
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
