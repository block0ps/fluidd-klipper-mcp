#!/usr/bin/env python3
"""
Multi-Printer Monitor — Local Proxy + Alert Server
Polls all configured Moonraker instances, dispatches alerts via:
  • Push notification  — ntfy.sh (free, iOS/Android)
  • SMS                — Twilio
  • Email              — SMTP (Gmail, etc.)
  • iMessage           — macOS AppleScript

Serves the monitor UI at http://localhost:8484

Usage:
    python3 monitor_server.py                  # start server
    python3 monitor_server.py 9090             # custom port
    python3 monitor_server.py add-printer      # interactively add a printer

Config: monitor_config.json (auto-created on first run, auto-migrates old format)
"""

import sys, os, json, time, smtplib, threading, subprocess
import urllib.request, urllib.error, urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

ADD_PRINTER_MODE = (len(sys.argv) > 1 and sys.argv[1] == "add-printer")
PORT = 8484
for _arg in sys.argv[1:]:
    if _arg.isdigit(): PORT = int(_arg)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")

DEFAULT_CONFIG = {
    "printers": [
        {"id": "printer1", "name": "Printer 1", "host": "http://10.0.107.158",
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

config        = {}
alert_log     = []
global_lock   = threading.Lock()
printer_states = {}
poll_threads   = {}

def _make_printer_state():
    return {"last_status": {}, "active_alerts": [], "fired_alerts": set(),
            "last_poll": None, "errors": 0, "lock": threading.Lock()}

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        if "printer_host" in saved and "printers" not in saved:
            saved["printers"] = [{"id": "printer1", "name": "Printer 1",
                                   "host": saved.pop("printer_host"),
                                   "enabled": True, "api_token": ""}]
            print("  Migrated single-printer config to multi-printer format.")
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
        print(f"  Created {CONFIG_FILE} — edit to enable alert channels.\n")

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def get_printer_by_id(pid):
    for p in config.get("printers", []):
        if p["id"] == pid:
            return p
    return None

# ── Anomaly detection ──────────────────────────────────────────────────────────

def detect_anomalies(s):
    alerts = []
    ps  = s.get("print_stats",    {})
    ext = s.get("extruder",       {})
    bed = s.get("heater_bed",     {})
    vsd = s.get("virtual_sdcard", {})
    wh  = s.get("webhooks",       {})
    th  = s.get("toolhead",       {})
    pos = th.get("position", [0,0,0,0])
    state   = ps.get("state", "")
    elapsed = ps.get("print_duration", 0)
    fil     = ps.get("filament_used", 0)
    prog    = vsd.get("progress", 0)

    if wh.get("state","ready") != "ready":
        alerts.append(("critical", f"Klippy not ready: {wh.get('state')} -- {wh.get('state_message','')}"))
    if bed.get("target",0) > 0 and abs(bed.get("temperature",0) - bed.get("target",0)) > 15:
        alerts.append(("critical", f"Thermal anomaly -- Bed: target {bed['target']}C actual {bed.get('temperature',0):.1f}C"))
    if ext.get("target",0) > 0 and abs(ext.get("temperature",0) - ext.get("target",0)) > 20:
        alerts.append(("critical", f"Thermal anomaly -- Hotend: target {ext['target']}C actual {ext.get('temperature',0):.1f}C"))
    if state == "printing" and elapsed > 300 and fil < 5:
        alerts.append(("warning", "Possible clog/under-extrusion: very low filament after 5+ min"))
    if state == "printing" and elapsed > 600 and prog < 0.001:
        alerts.append(("warning", "Possible stall: no progress detected after 10 min"))
    if state == "printing" and elapsed > 120 and pos[2] < 0.1:
        alerts.append(("warning", f"Z position anomaly: Z={pos[2]:.3f}mm while printing"))
    if state == "error":
        alerts.append(("critical", f"Print error: {ps.get('message','unknown')}"))
    if state == "cancelled":
        alerts.append(("warning", "Print was cancelled"))
    if state == "complete":
        alerts.append(("success", f"Print complete! {ps.get('filename','')}"))
    return alerts

# ── Alert channels ─────────────────────────────────────────────────────────────

def send_ntfy(title, body, level):
    cfg = config.get("ntfy", {})
    if not cfg.get("enabled"): return False, "disabled"
    priority = {"critical":"urgent","warning":"high","success":"default"}.get(level,"default")
    tags     = {"critical":"rotating_light,printer","warning":"warning,printer",
                "success":"white_check_mark,printer"}.get(level,"printer")
    url = f"{cfg.get('server','https://ntfy.sh').rstrip('/')}/{cfg.get('topic','printer-alerts')}"
    try:
        # urllib encodes HTTP headers as latin-1 — strip all non-ASCII chars (emoji)
        # from the Title header. Emoji/Unicode go in the body, which we send as raw
        # UTF-8 bytes so they arrive intact on the ntfy app.
        ascii_title = title.encode("ascii", errors="ignore").decode("ascii").strip(" -\u2014")
        req = urllib.request.Request(
            url, data=body.encode("utf-8"),
            headers={
                "Title":        ascii_title or f"Printer Alert - {level.upper()}",
                "Priority":     priority,
                "Tags":         tags,
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST")
        urllib.request.urlopen(req, timeout=10)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_twilio_sms(body):
    cfg = config.get("twilio", {})
    if not cfg.get("enabled"): return False, "disabled"
    sid, token, frm, to = (cfg.get(k,"").strip() for k in
                           ("account_sid","auth_token","from_number","to_number"))
    if not all([sid, token, frm, to]): return False, "missing credentials"
    import base64
    url  = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"From":frm,"To":to,"Body":body}).encode()
    b64  = base64.b64encode(f"{sid}:{token}".encode()).decode()
    try:
        req = urllib.request.Request(url, data=data, method="POST",
            headers={"Authorization":f"Basic {b64}",
                     "Content-Type":"application/x-www-form-urlencoded"})
        urllib.request.urlopen(req, timeout=10)
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_email(subject, body):
    cfg  = config.get("email", {})
    if not cfg.get("enabled"): return False, "disabled"
    host = cfg.get("smtp_host","smtp.gmail.com")
    port = int(cfg.get("smtp_port", 587))
    user = cfg.get("username","").strip()
    pwd  = cfg.get("password","").strip()
    frm  = cfg.get("from_address", user).strip()
    to   = cfg.get("to_address","").strip()
    if not all([user, pwd, to]): return False, "missing credentials"
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"]=subject; msg["From"]=frm; msg["To"]=to
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls(); s.login(user, pwd); s.sendmail(frm, to, msg.as_string())
        return True, "ok"
    except Exception as e:
        return False, str(e)

def send_imessage(body):
    cfg = config.get("imessage", {})
    if not cfg.get("enabled"): return False, "disabled"
    to = cfg.get("to_number","").strip()
    if not to: return False, "no recipient configured"
    script = (f'tell application "Messages"\n'
              f'  set s to 1st service whose service type = iMessage\n'
              f'  set b to buddy "{to}" of s\n'
              f'  send "{body}" to b\nend tell')
    try:
        r = subprocess.run(["osascript","-e",script], capture_output=True, text=True, timeout=15)
        return (True,"ok") if r.returncode==0 else (False, r.stderr.strip())
    except FileNotFoundError:
        return False, "osascript not found (macOS only)"
    except Exception as e:
        return False, str(e)

def dispatch_alert(level, msg, printer_name="Printer"):
    ts    = datetime.now().strftime("%H:%M:%S")
    title = f"[{printer_name}] {level.upper()}"
    full  = f"[{ts}] {msg}\n\nPrinter: {printer_name}"
    results = {
        "ntfy":     send_ntfy(title, full, level),
        "sms":      send_twilio_sms(f"[{printer_name}] {msg}"),
        "email":    send_email(title, full),
        "imessage": send_imessage(f"[{printer_name}] {msg}"),
    }
    sent   = [ch for ch,(ok,_) in results.items() if ok]
    failed = [(ch,err) for ch,(ok,err) in results.items() if not ok and err!="disabled"]
    entry  = {"time": datetime.now().isoformat(), "level": level, "msg": msg,
              "printer": printer_name, "sent": sent, "failed": failed}
    with global_lock:
        alert_log.insert(0, entry)
        if len(alert_log) > 200: alert_log.pop()
    print(f"  [{ts}] [{printer_name}] ALERT -- {msg}")
    print(f"         sent: {', '.join(sent) if sent else 'none'}" +
          (f" | failed: {', '.join(f'{c}({e})' for c,e in failed)}" if failed else ""))
    return results

# ── Polling ─────────────────────────────────────────────────────────────────────

def fetch_printer_status(printer):
    host  = printer["host"].rstrip("/")
    token = printer.get("api_token","")
    hdrs  = {"Accept": "application/json"}
    if token: hdrs["X-Api-Key"] = token
    url = (host + "/printer/objects/query"
           "?print_stats&extruder&heater_bed&toolhead&virtual_sdcard&webhooks&display_status")
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("result",{}).get("status",{})

def process_printer_status(printer, s):
    pid   = printer["id"]
    pname = printer.get("name", pid)
    state = printer_states[pid]
    with state["lock"]:
        state["last_status"] = s
        state["last_poll"]   = datetime.now().isoformat()
    anomalies = detect_anomalies(s)
    print_state = s.get("print_stats",{}).get("state","")
    if print_state not in ("printing","paused"):
        with state["lock"]: state["fired_alerts"].clear()
    for level, msg in anomalies:
        key = f"{level}:{msg}"
        should_fire = (level in ("critical","success") or
                       (level=="warning" and config.get("alert_on_warnings",True)))
        with state["lock"]: already = key in state["fired_alerts"]
        if should_fire and not already:
            with state["lock"]: state["fired_alerts"].add(key)
            dispatch_alert(level, msg, pname)
    with state["lock"]:
        state["active_alerts"] = [{"level":l,"msg":m} for l,m in anomalies]

def start_printer_thread(printer):
    pid = printer["id"]
    if pid not in printer_states:
        printer_states[pid] = _make_printer_state()
    if pid not in poll_threads or not poll_threads[pid].is_alive():
        t = threading.Thread(target=_poll_loop, args=(printer,), daemon=True)
        poll_threads[pid] = t
        t.start()

def _poll_loop(printer):
    pid = printer["id"]
    while True:
        interval = config.get("poll_interval_seconds", 1800)
        current  = get_printer_by_id(pid) or printer
        if not current.get("enabled", True):
            time.sleep(60); continue
        try:
            s = fetch_printer_status(current)
            process_printer_status(current, s)
            printer_states[pid]["errors"] = 0
            ps  = s.get("print_stats",{})
            vsd = s.get("virtual_sdcard",{})
            pct = vsd.get("progress",0)*100
            stt = ps.get("state","?")
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{current['name']}] Poll OK -- {stt.upper()} {pct:.1f}%")
        except Exception as e:
            printer_states[pid]["errors"] += 1
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{printer['name']}] Poll error ({printer_states[pid]['errors']}): {e}")
            interval = min(interval, 300)
        time.sleep(interval)

def poll_once(printer):
    pid = printer["id"]
    if pid not in printer_states: printer_states[pid] = _make_printer_state()
    try:
        s = fetch_printer_status(printer)
        process_printer_status(printer, s)
    except Exception as e:
        print(f"  poll_once error [{printer['name']}]: {e}")

# ── Camera ──────────────────────────────────────────────────────────────────────

def fetch_camera_snapshot(printer):
    """Returns (image_bytes, content_type) or raises on failure."""
    host  = printer["host"].rstrip("/")
    token = printer.get("api_token","")
    hdrs  = {"Accept":"application/json"}
    if token: hdrs["X-Api-Key"] = token
    snapshot_url = None
    try:
        req = urllib.request.Request(f"{host}/server/webcams/list", headers=hdrs)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        webcams = data.get("result",{}).get("webcams",[])
        if webcams:
            snapshot_url = webcams[0].get("snapshot_url","")
    except Exception:
        pass
    if not snapshot_url:
        snapshot_url = "/webcam/?action=snapshot"
    if not snapshot_url.startswith("http"):
        snapshot_url = f"{host}{snapshot_url}"
    img_hdrs = {}
    if token: img_hdrs["X-Api-Key"] = token
    req = urllib.request.Request(snapshot_url, headers=img_hdrs)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.read(), resp.headers.get("Content-Type","image/jpeg")


# ── HTML UI ────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>Printer Fleet Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0f;color:#e2e8f0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;min-height:100vh;padding:20px}
.container{max-width:600px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.header{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #1e293b;padding-bottom:12px}
.header-title{font-size:15px;font-weight:700;color:#f8fafc}
.header-sub{font-size:11px;color:#475569;margin-top:2px}
.header-meta{text-align:right;font-size:11px;color:#475569;line-height:1.8}
.header-meta span{color:#94a3b8}
.printer-tabs{display:flex;gap:6px;flex-wrap:wrap}
.printer-tab{background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:12px;padding:6px 14px;border-radius:20px;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .15s}
.printer-tab:hover{border-color:#475569;color:#cbd5e1}
.printer-tab.active{background:#0f2a3f;border-color:#3b82f6;color:#60a5fa}
.tab-dot{width:6px;height:6px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot-printing{background:#22c55e;box-shadow:0 0 6px #22c55e80;animation:glow 2s infinite}
.dot-paused{background:#f59e0b}
.dot-error{background:#ef4444;box-shadow:0 0 6px #ef444480}
.dot-complete,.dot-cancelled,.dot-unknown,.dot-standby{background:#334155}
@keyframes glow{0%,100%{opacity:1}50%{opacity:.5}}
.alert{border-radius:6px;padding:10px 14px;font-size:12px;font-weight:600;border-left:3px solid;margin-bottom:6px}
.alert-critical{background:#1a0a0a;border-color:#ef4444;color:#fca5a5}
.alert-warning{background:#1a150a;border-color:#f59e0b;color:#fcd34d}
.alert-success{background:#0a1a0f;border-color:#22c55e;color:#86efac}
.alert-ok{background:#0a1a0f;border-color:#22c55e;color:#86efac;font-weight:500}
.card{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:14px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;color:#fff}
.badge-printing{background:#16a34a}.badge-paused{background:#d97706}
.badge-error{background:#dc2626}.badge-complete{background:#2563eb}
.badge-cancelled,.badge-standby,.badge-unknown{background:#334155}
.spinner{font-size:11px;color:#475569;animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.filename{font-size:11px;color:#64748b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.progress-row{display:flex;justify-content:space-between;font-size:12px;margin-top:4px}
.progress-pct{color:#4ade80;font-weight:700}.progress-layer{color:#64748b}
.progress-bar-bg{background:#1e293b;border-radius:99px;height:8px;margin-top:6px;overflow:hidden}
.progress-bar-fill{height:100%;border-radius:99px;background:#22c55e;transition:width .6s ease}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.stat-label{color:#475569;font-size:11px}.stat-value{color:#f1f5f9;font-size:13px;margin-top:1px}
.temps{border-top:1px solid #1e293b;padding-top:12px;display:flex;flex-direction:column;gap:8px}
.temp-row{display:flex;align-items:center;gap:10px}
.temp-label{color:#475569;width:52px;font-size:11px}
.temp-val{font-weight:700;font-size:13px}.temp-ok{color:#4ade80}.temp-bad{color:#f87171}
.temp-target{color:#475569;font-size:11px}
.channels{display:flex;gap:8px;flex-wrap:wrap}
.ch{padding:2px 8px;border-radius:12px;font-size:10px;font-weight:600;border:1px solid}
.ch-on{background:#0f2a1a;border-color:#22c55e;color:#4ade80}
.ch-off{background:#1a1a1a;border-color:#334155;color:#475569}
.btn-row{display:flex;gap:8px;flex-wrap:wrap}
.btn{flex:1;min-width:70px;background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:12px;padding:9px;border-radius:6px;cursor:pointer;transition:background .15s}
.btn:hover:not(:disabled){background:#263347}.btn:disabled{opacity:.4;cursor:default}
.camera-card{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px}
.camera-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.camera-title{font-size:11px;color:#94a3b8}
.camera-hint{font-size:10px;color:#334155}
#cameraImg{width:100%;border-radius:4px;background:#080c14;display:block}
#cameraErr{color:#ef4444;font-size:11px;padding:20px;text-align:center;display:none}
.log{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px}
.log h3{font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}
.log-list{max-height:240px;overflow-y:auto;display:flex;flex-direction:column;gap:5px}
.log-entry{font-size:11px;padding:6px 8px;border-radius:4px;border-left:2px solid}
.log-entry-critical{background:#140808;border-color:#ef4444;color:#fca5a5}
.log-entry-warning{background:#14100a;border-color:#f59e0b;color:#fcd34d}
.log-entry-success{background:#081408;border-color:#22c55e;color:#86efac}
.log-printer{color:#3b82f6;font-size:10px;font-weight:700;margin-right:2px}
.log-sent{color:#475569;font-size:10px;margin-top:3px}
.section-label{font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:.08em}
.footer{text-align:center;font-size:10px;color:#1e293b;padding-top:4px}
</style>
</head>
<body><div class="container">

<div class="header">
  <div>
    <div class="header-title">&#128424; Printer Fleet Monitor</div>
    <div class="header-sub" id="activePrinterHost">connecting...</div>
  </div>
  <div class="header-meta">
    UI polls: <span id="checkCount">0</span><br>
    Last: <span id="lastCheck">&#8212;</span><br>
    Next: <span id="countdown">&#8212;</span>
  </div>
</div>

<div id="printerTabsWrap" style="display:none">
  <div class="section-label" style="margin-bottom:6px">Printers</div>
  <div class="printer-tabs" id="printerTabs"></div>
</div>

<div>
  <div class="section-label" style="margin-bottom:6px">Alert channels (server-side)</div>
  <div class="channels">
    <span class="ch ch-off" id="ch-ntfy">&#128241; Push</span>
    <span class="ch ch-off" id="ch-sms">&#128172; SMS</span>
    <span class="ch ch-off" id="ch-email">&#128231; Email</span>
    <span class="ch ch-off" id="ch-imessage">&#128172; iMessage</span>
  </div>
</div>

<div id="alertsContainer"></div>

<div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between">
    <div style="display:flex;align-items:center;gap:8px">
      <span class="badge badge-unknown" id="stateBadge">CONNECTING</span>
      <span id="printerName" style="font-size:11px;color:#475569"></span>
    </div>
    <span class="spinner" id="spinner">syncing...</span>
  </div>
  <div>
    <div class="filename" id="filename">&#8212;</div>
    <div class="progress-row">
      <span class="progress-pct" id="progressPct">&#8212;</span>
      <span class="progress-layer" id="layerInfo">&#8212;</span>
    </div>
    <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill" style="width:0%"></div></div>
  </div>
  <div class="stats-grid">
    <div><div class="stat-label">Elapsed</div><div class="stat-value" id="elapsed">&#8212;</div></div>
    <div><div class="stat-label">ETA</div><div class="stat-value" id="eta">&#8212;</div></div>
    <div><div class="stat-label">Filament</div><div class="stat-value" id="filament">&#8212;</div></div>
    <div><div class="stat-label">Z Position</div><div class="stat-value" id="zpos">&#8212;</div></div>
  </div>
  <div class="temps">
    <div class="temp-row">
      <span class="temp-label">Hotend</span>
      <span class="temp-val temp-ok" id="hotendTemp">&#8212;</span>
      <span class="temp-target" id="hotendTarget">/ &#8212;</span>
      <span id="hotendIcon"></span>
    </div>
    <div class="temp-row">
      <span class="temp-label">Bed</span>
      <span class="temp-val temp-ok" id="bedTemp">&#8212;</span>
      <span class="temp-target" id="bedTarget">/ &#8212;</span>
      <span id="bedIcon"></span>
    </div>
  </div>
</div>

<div id="cameraSection" style="display:none">
  <div class="camera-card">
    <div class="camera-header">
      <span class="camera-title">&#128247; Live Camera</span>
      <span class="camera-hint">auto-refreshes every 5s</span>
    </div>
    <img id="cameraImg" alt="Camera snapshot"
         onerror="this.style.display='none';document.getElementById('cameraErr').style.display='block'" />
    <div id="cameraErr">No camera available or snapshot URL unreachable</div>
  </div>
</div>

<div class="btn-row">
  <button class="btn" onclick="triggerPoll()">&#128260; Poll Now</button>
  <button class="btn" id="cameraBtn" onclick="toggleCamera()">&#128247; Camera</button>
  <button class="btn" onclick="sendTest()">&#128276; Test</button>
  <button class="btn" onclick="openConfig()">&#9881; Config</button>
</div>

<div class="log">
  <h3>Alert Dispatch Log</h3>
  <div class="log-list" id="alertLogList">
    <div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>
  </div>
</div>

<div class="footer">
  Server polls every <span id="intervalLabel">30 min</span> &mdash; alerts fire even when this tab is closed<br>
  Edit <strong>monitor_config.json</strong> to enable channels &middot; proxy: localhost:__PORT__
</div>
</div>

<script>
var uiPollCount=0,activePrinterId=null,allPrinters=[],cameraVisible=false,cameraInterval=null,countdownID=null;

function fmtTime(s){
  if(s<0||s==null)return"\u2014";
  return Math.floor(s/3600)+"h "+Math.floor((s%3600)/60)+"m "+Math.floor(s%60)+"s";
}

function selectPrinter(pid){
  activePrinterId=pid;
  document.querySelectorAll('.printer-tab').forEach(function(el){
    el.classList.toggle('active',el.dataset.id===pid);
  });
  if(cameraVisible)refreshCamera();
}

function renderPrinterTabs(printers){
  var wrap=document.getElementById('printerTabsWrap');
  var tabs=document.getElementById('printerTabs');
  if(printers.length<=1){wrap.style.display='none';return;}
  wrap.style.display='block';
  tabs.innerHTML=printers.map(function(p){
    var s=(p.status&&p.status.print_stats&&p.status.print_stats.state)||'unknown';
    var err=p.errors>0?'<span style="color:#ef4444;font-size:10px">(err)</span>':'';
    return '<button class="printer-tab '+(p.id===activePrinterId?'active':'')+'" data-id="'+p.id+'" onclick="selectPrinter(\''+p.id+'\');refreshUI()">'+
      '<span class="tab-dot dot-'+s+'"></span>'+p.name+err+'</button>';
  }).join('');
}

function renderChannels(cfg){
  var map={ntfy:cfg.ntfy&&cfg.ntfy.enabled,sms:cfg.twilio&&cfg.twilio.enabled,
           email:cfg.email&&cfg.email.enabled,imessage:cfg.imessage&&cfg.imessage.enabled};
  var labels={ntfy:"\u{1F4F1} Push",sms:"\u{1F4AC} SMS",email:"\u{1F4E7} Email",imessage:"\u{1F4AC} iMessage"};
  Object.entries(map).forEach(function(pair){
    var el=document.getElementById('ch-'+pair[0]);
    if(el){el.className='ch '+(pair[1]?'ch-on':'ch-off');el.textContent=labels[pair[0]];}
  });
  var s=cfg.poll_interval_seconds||1800,m=Math.round(s/60);
  document.getElementById('intervalLabel').textContent=m>=60?(m/60)+'h':m+' min';
}

function updateCountdown(intervalSecs,lastPoll){
  if(countdownID)clearInterval(countdownID);
  if(!lastPoll){document.getElementById('countdown').textContent='\u2014';return;}
  var nextAt=new Date(lastPoll).getTime()+intervalSecs*1000;
  countdownID=setInterval(function(){
    var d=nextAt-Date.now();
    if(d<=0){document.getElementById('countdown').textContent='polling\u2026';return;}
    document.getElementById('countdown').textContent=Math.floor(d/60000)+'m '+Math.floor((d%60000)/1000)+'s';
  },1000);
}

function renderStatus(s,name){
  var ps=s.print_stats||{},ext=s.extruder||{},bed=s.heater_bed||{},
      vsd=s.virtual_sdcard||{},th=s.toolhead||{},pos=th.position||[0,0,0,0];
  var state=ps.state||'unknown',prog=(vsd.progress||0)*100,
      el=ps.print_duration||0,tot=ps.total_duration||0,eta=tot>el?tot-el:-1,
      layer=(ps.info&&ps.info.current_layer)||'?',totL=(ps.info&&ps.info.total_layer)||'?',
      fil=((ps.filament_used||0)/1000).toFixed(2);
  var b=document.getElementById('stateBadge');
  b.className='badge badge-'+state;b.textContent=state.toUpperCase();
  document.getElementById('printerName').textContent=name||'';
  var ap=allPrinters.find(function(p){return p.id===activePrinterId;});
  document.getElementById('activePrinterHost').textContent=ap?ap.host:'';
  document.getElementById('filename').textContent=ps.filename||'\u2014';
  document.getElementById('progressPct').textContent=prog.toFixed(1)+'%';
  document.getElementById('layerInfo').textContent='Layer '+layer+'/'+totL;
  document.getElementById('progressFill').style.width=Math.min(prog,100)+'%';
  document.getElementById('elapsed').textContent=fmtTime(el);
  document.getElementById('eta').textContent=fmtTime(eta);
  document.getElementById('filament').textContent=fil+'m';
  document.getElementById('zpos').textContent=((pos[2]||0).toFixed(2))+'mm';
  var hOk=ext.target===0||Math.abs((ext.temperature||0)-(ext.target||0))<=20;
  var bOk=bed.target===0||Math.abs((bed.temperature||0)-(bed.target||0))<=15;
  document.getElementById('hotendTemp').textContent=(ext.temperature||0).toFixed(1)+'\u00B0C';
  document.getElementById('hotendTemp').className='temp-val '+(hOk?'temp-ok':'temp-bad');
  document.getElementById('hotendTarget').textContent='/ '+(ext.target||0)+'\u00B0C';
  document.getElementById('hotendIcon').textContent=hOk?'\u2705':'\u26A0\uFE0F';
  document.getElementById('bedTemp').textContent=(bed.temperature||0).toFixed(1)+'\u00B0C';
  document.getElementById('bedTemp').className='temp-val '+(bOk?'temp-ok':'temp-bad');
  document.getElementById('bedTarget').textContent='/ '+(bed.target||0)+'\u00B0C';
  document.getElementById('bedIcon').textContent=bOk?'\u2705':'\u26A0\uFE0F';
}

function renderAlerts(alerts){
  var c=document.getElementById('alertsContainer');c.innerHTML='';
  if(!alerts||!alerts.length){
    c.innerHTML='<div class="alert alert-ok">\u2705 All systems nominal</div>';return;
  }
  alerts.forEach(function(a){
    var d=document.createElement('div');
    d.className='alert alert-'+a.level;d.textContent=a.msg;c.appendChild(d);
  });
  if(alerts.some(function(a){return a.level==='critical';})){
    document.title='\uD83D\uDEA8 ALERT \u2014 Printer Monitor';
    setTimeout(function(){document.title='\uD83D\uDDA8\uFE0F Printer Fleet Monitor';},6000);
    if(Notification.permission==='granted')
      new Notification('\uD83D\uDDA8\uFE0F Printer Alert',{body:alerts[0].msg});
  }
}

function renderAlertLog(log){
  var list=document.getElementById('alertLogList');
  if(!log||!log.length){
    list.innerHTML='<div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>';return;
  }
  list.innerHTML=log.slice(0,30).map(function(e){
    var t=new Date(e.time).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    var sent=e.sent&&e.sent.length?'sent: '+e.sent.join(', '):'no channels enabled';
    var failed=e.failed&&e.failed.length?' \u00B7 failed: '+e.failed.map(function(f){return f[0];}).join(','):'';
    var pname=e.printer?'<span class="log-printer">['+e.printer+']</span>':'';
    return '<div class="log-entry log-entry-'+e.level+'"><span style="opacity:.5">['+t+']</span> '+pname+e.msg+
      '<div class="log-sent">'+sent+failed+'</div></div>';
  }).join('');
}

function toggleCamera(){
  cameraVisible=!cameraVisible;
  document.getElementById('cameraSection').style.display=cameraVisible?'block':'none';
  document.getElementById('cameraBtn').textContent=cameraVisible?'\uD83D\uDCF7 Hide Cam':'\uD83D\uDCF7 Camera';
  if(cameraVisible){refreshCamera();cameraInterval=setInterval(refreshCamera,5000);}
  else{clearInterval(cameraInterval);}
}

function refreshCamera(){
  if(!activePrinterId)return;
  var img=document.getElementById('cameraImg');
  var err=document.getElementById('cameraErr');
  img.style.display='block';err.style.display='none';
  img.src='/api/printers/'+activePrinterId+'/camera?t='+Date.now();
}

async function triggerPoll(){
  await fetch('/api/poll',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({printer_id:activePrinterId})});
  setTimeout(refreshUI,2500);
}
async function sendTest(){
  await fetch('/api/test_alert',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({printer_id:activePrinterId})});
  setTimeout(refreshUI,2500);
}
function openConfig(){window.open('/monitor_config.json','_blank');}

async function refreshUI(){
  document.getElementById('spinner').style.display='inline';
  try{
    var results=await Promise.all([fetch('/api/printers'),fetch('/api/alerts'),fetch('/api/config')]);
    var pdata=await results[0].json();
    var adata=await results[1].json();
    var cfg=await results[2].json();
    allPrinters=pdata.printers||[];
    if(!activePrinterId&&allPrinters.length>0)activePrinterId=allPrinters[0].id;
    renderPrinterTabs(allPrinters);
    var ap=allPrinters.find(function(p){return p.id===activePrinterId;})||allPrinters[0];
    if(ap){
      renderStatus(ap.status||{},ap.name);
      renderAlerts(ap.active_alerts||[]);
      updateCountdown(cfg.poll_interval_seconds||1800,ap.last_poll);
    }
    renderAlertLog(adata.alert_log);
    renderChannels(cfg);
    uiPollCount++;
    document.getElementById('checkCount').textContent=uiPollCount;
    document.getElementById('lastCheck').textContent=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  }catch(e){
    document.getElementById('alertsContainer').innerHTML='<div class="alert alert-critical">Monitor server unreachable: '+e.message+'</div>';
  }finally{
    document.getElementById('spinner').style.display='none';
  }
}

if('Notification'in window&&Notification.permission==='default')Notification.requestPermission();
refreshUI();
setInterval(refreshUI,30000);
</script></body></html>"""


# ── HTTP handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            self.send_html(HTML.replace("__PORT__", str(PORT)))

        elif path == "/api/printers":
            result = []
            for p in config.get("printers", []):
                pid = p["id"]
                st  = printer_states.get(pid, {})
                lk  = st.get("lock", threading.Lock())
                with lk:
                    result.append({
                        "id": pid, "name": p.get("name",pid),
                        "host": p.get("host",""), "enabled": p.get("enabled",True),
                        "status": dict(st.get("last_status",{})),
                        "active_alerts": list(st.get("active_alerts",[])),
                        "last_poll": st.get("last_poll"),
                        "errors": st.get("errors",0)
                    })
            self.send_json(200, {"printers": result})

        elif "/api/printers/" in path and path.endswith("/camera"):
            parts = path.split("/")
            pid   = parts[3] if len(parts) > 3 else None
            printer = get_printer_by_id(pid) if pid else None
            if not printer:
                self.send_json(404, {"error": "printer not found"}); return
            try:
                img_data, ct = fetch_camera_snapshot(printer)
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", len(img_data))
                self.send_header("Cache-Control","no-cache, no-store")
                self.end_headers()
                self.wfile.write(img_data)
            except Exception as e:
                self.send_json(502, {"error": f"Camera unavailable: {e}"})

        elif path == "/api/alerts":
            with global_lock: lg = list(alert_log)
            self.send_json(200, {"alert_log": lg})

        elif path == "/api/config":
            safe = json.loads(json.dumps(config))
            for ch in ("twilio","email"):
                for k in ("auth_token","password"):
                    if safe.get(ch,{}).get(k): safe[ch][k] = "••••••••"
            self.send_json(200, safe)

        elif path == "/api/status":  # backward compat — returns first printer status
            printers = config.get("printers",[])
            first_id = printers[0]["id"] if printers else None
            st = printer_states.get(first_id, {})
            with st.get("lock", threading.Lock()):
                s = dict(st.get("last_status",{}))
            self.send_json(200, {"status": s, "last_poll": st.get("last_poll")})

        elif path == "/monitor_config.json":
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE,"rb") as f: body = f.read()
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",len(body))
                self.end_headers(); self.wfile.write(body)
            else:
                self.send_json(404, {"error":"config not found"})

        elif path.startswith("/proxy/"):
            printers = config.get("printers",[])
            host = printers[0]["host"].rstrip("/") if printers else ""
            self._proxy_to(host, path[7:])

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/poll":
            body    = self._read_body()
            pid     = body.get("printer_id")
            targets = ([get_printer_by_id(pid)] if pid else config.get("printers",[]))
            targets = [p for p in targets if p]
            for p in targets:
                threading.Thread(target=poll_once, args=(p,), daemon=True).start()
            self.send_json(200, {"ok": True, "polled": [p["id"] for p in targets]})

        elif path == "/api/test_alert":
            body  = self._read_body()
            pid   = body.get("printer_id")
            pname = "Printer"
            if pid:
                p = get_printer_by_id(pid)
                if p: pname = p.get("name","Printer")
            threading.Thread(target=dispatch_alert,
                args=("warning","Test alert — all enabled channels should receive this", pname),
                daemon=True).start()
            self.send_json(200, {"ok": True})

        elif path == "/api/printers":  # add printer at runtime
            body = self._read_body()
            name = body.get("name","").strip()
            host = body.get("host","").strip().rstrip("/")
            if not name or not host:
                self.send_json(400, {"error":"name and host are required"}); return
            pid = name.lower()
            for ch in " -./()[]{}": pid = pid.replace(ch,"_")
            while "__" in pid: pid = pid.replace("__","_")
            pid = pid.strip("_") or "printer"
            existing = [p["id"] for p in config.get("printers",[])]
            if pid in existing: pid = f"{pid}_{len(existing)+1}"
            printer = {"id":pid,"name":name,"host":host,"enabled":True,
                       "api_token":body.get("api_token","")}
            config.setdefault("printers",[]).append(printer)
            save_config()
            printer_states[pid] = _make_printer_state()
            start_printer_thread(printer)
            self.send_json(201, {"ok":True,"printer":printer})

        else:
            self.send_response(404); self.end_headers()

    def _proxy_to(self, host, path):
        target = host + ("/" + path.lstrip("/"))
        try:
            req = urllib.request.Request(target, headers={"Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",len(data))
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers(); self.wfile.write(data)
        except urllib.error.URLError as e:
            self.send_json(502, {"error":f"Cannot reach printer: {e.reason}"})
        except Exception as e:
            self.send_json(500, {"error":str(e)})

# ── CLI: add-printer ───────────────────────────────────────────────────────────

def cli_add_printer():
    print()
    print("  +-------------------------------------------------+")
    print("  |   Add Printer to Monitor Fleet                  |")
    print("  +-------------------------------------------------+")
    print()
    load_config()
    existing = config.get("printers",[])
    if existing:
        print(f"  Current printers ({len(existing)}):")
        for p in existing:
            status = "ON " if p.get("enabled",True) else "OFF"
            print(f"    [{status}]  {p['name']}  ({p['id']})  {p['host']}")
        print()

    name = input("  Printer name (e.g. 'Voron 2.4', 'Ender 5'): ").strip()
    if not name: print("  Name required. Exiting."); return

    host = input("  Printer URL (e.g. http://192.168.1.101): ").strip().rstrip("/")
    if not host: print("  URL required. Exiting."); return

    token = input("  API token (leave blank if not needed): ").strip()

    pid = name.lower()
    for ch in " -./()[]{}": pid = pid.replace(ch,"_")
    while "__" in pid: pid = pid.replace("__","_")
    pid = pid.strip("_") or "printer"
    existing_ids = [p["id"] for p in existing]
    if pid in existing_ids: pid = f"{pid}_{len(existing_ids)+1}"

    printer = {"id":pid,"name":name,"host":host,"enabled":True,"api_token":token}

    print(f"\n  About to add:")
    print(f"    Name  : {name}")
    print(f"    Host  : {host}")
    print(f"    ID    : {pid}")
    ans = input("\n  Save? [y/N]: ").strip().lower()
    if ans not in ("y","yes"): print("  Cancelled."); return

    config.setdefault("printers",[]).append(printer)
    save_config()
    print(f"\n  Printer '{name}' added to {CONFIG_FILE}")
    print("  Restart monitor_server.py to begin monitoring this printer.\n")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if ADD_PRINTER_MODE:
        cli_add_printer()
        sys.exit(0)

    load_config()

    for printer in config.get("printers", []):
        if printer.get("enabled", True):
            start_printer_thread(printer)

    server       = HTTPServer(("127.0.0.1", PORT), Handler)
    printers     = config.get("printers", [])
    interval_min = config.get("poll_interval_seconds",1800)//60
    icons = {k: "YES" if config.get(m,{}).get("enabled") else "NO"
             for k,m in [("ntfy","ntfy"),("sms","twilio"),("email","email"),("imsg","imessage")]}

    sep = "=" * 58
    print(f"\n  {sep}")
    print(f"  Printer Fleet Monitor -- Alert Server")
    print(f"  {sep}")
    print(f"  Monitor  : http://localhost:{PORT}")
    print(f"  Printers : {len(printers)} configured")
    for p in printers:
        status = "ON " if p.get("enabled",True) else "OFF"
        print(f"    [{status}]  {p['name']}: {p['host']}")
    print(f"  Polling  : every {interval_min} min (per-printer threads)")
    print(f"  {sep}")
    print(f"  Push(ntfy): {icons['ntfy']}  SMS(Twilio): {icons['sms']}  Email: {icons['email']}  iMessage: {icons['imsg']}")
    print(f"  {sep}")
    print(f"  Config   : monitor_config.json")
    print(f"  Add more : python3 monitor_server.py add-printer")
    print(f"  {sep}")
    print(f"\n  Open  http://localhost:{PORT}  in your browser.")
    print(f"  Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
