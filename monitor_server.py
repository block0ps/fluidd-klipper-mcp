#!/usr/bin/env python3
"""
Printer Monitor — Local Proxy + Alert Server
Polls Moonraker on a background thread and dispatches alerts via:
  • Push notification  — ntfy.sh (free, iOS/Android, no account needed)
  • SMS                — Twilio
  • Email              — SMTP (Gmail, etc.)
  • iMessage           — macOS AppleScript (no account needed)

Serves the monitor UI at http://localhost:8484

Usage:
    python3 monitor_server.py
    python3 monitor_server.py http://10.0.107.158
    python3 monitor_server.py http://10.0.107.158 8484

Config is stored in monitor_config.json alongside this script.
Edit it directly or use the Settings panel in the UI.
"""

import sys, os, json, time, smtplib, threading, subprocess
import urllib.request, urllib.error, urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

PRINTER_HOST = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://10.0.107.158"
PORT         = int(sys.argv[2])        if len(sys.argv) > 2 else 8484
CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")

DEFAULT_CONFIG = {
    "printer_host": PRINTER_HOST,
    "poll_interval_seconds": 1800,
    "alert_on_warnings": True,
    "ntfy":     {"enabled": False, "topic": "my-printer-alerts", "server": "https://ntfy.sh"},
    "twilio":   {"enabled": False, "account_sid": "", "auth_token": "", "from_number": "", "to_number": ""},
    "email":    {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                 "username": "", "password": "", "from_address": "", "to_address": ""},
    "imessage": {"enabled": False, "to_number": ""}
}

config        = {}
alert_log     = []
last_status   = {}
active_alerts = []
fired_alerts  = set()
lock          = threading.Lock()

# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            saved = json.load(f)
        merged = DEFAULT_CONFIG.copy()
        for k, v in saved.items():
            if isinstance(v, dict) and k in merged:
                merged[k].update(v)
            else:
                merged[k] = v
        config = merged
    else:
        config = DEFAULT_CONFIG.copy()
        save_config()
        print(f"  Created {CONFIG_FILE} — edit it to enable alert channels.\n")

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

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

    if wh.get("state","ready") != "ready":
        alerts.append(("critical", f"🔴 Klippy not ready: {wh.get('state')} — {wh.get('state_message','')}"))
    if bed.get("target",0) > 0 and abs(bed.get("temperature",0) - bed.get("target",0)) > 15:
        alerts.append(("critical", f"🌡️ Thermal anomaly — Bed: target {bed['target']}°C, actual {bed['temperature']:.1f}°C"))
    if ext.get("target",0) > 0 and abs(ext.get("temperature",0) - ext.get("target",0)) > 20:
        alerts.append(("critical", f"🌡️ Thermal anomaly — Hotend: target {ext['target']}°C, actual {ext['temperature']:.1f}°C"))

    state   = ps.get("state","")
    elapsed = ps.get("print_duration", 0)
    fil     = ps.get("filament_used",  0)
    prog    = vsd.get("progress", 0)

    if state == "printing" and elapsed > 300 and fil < 5:
        alerts.append(("warning", "🧵 Possible clog / under-extrusion: very low filament after 5+ min"))
    if state == "printing" and elapsed > 600 and prog < 0.001:
        alerts.append(("warning", "⏸️ Possible stall: no progress detected after 10 min"))
    if state == "printing" and elapsed > 120 and pos[2] < 0.1:
        alerts.append(("warning", f"📐 Z position anomaly: Z={pos[2]:.3f}mm while printing"))
    if state == "error":
        alerts.append(("critical", f"🛑 Print error: {ps.get('message','unknown')}"))
    if state == "cancelled":
        alerts.append(("warning", "🛑 Print was cancelled"))
    if state == "complete":
        alerts.append(("success", f"🎉 Print complete! {ps.get('filename','')}"))
    return alerts

# ── Alert channels ─────────────────────────────────────────────────────────────

def send_ntfy(title, body, level):
    cfg = config.get("ntfy", {})
    if not cfg.get("enabled"): return False, "disabled"
    priority = {"critical":"urgent","warning":"high","success":"default"}.get(level,"default")
    url = f"{cfg.get('server','https://ntfy.sh').rstrip('/')}/{cfg.get('topic','printer-alerts')}"
    try:
        req = urllib.request.Request(url, data=body.encode(),
            headers={"Title": title, "Priority": priority,
                     "Tags": "printer,rotating_light" if level=="critical" else "printer"},
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
    cfg = config.get("email", {})
    if not cfg.get("enabled"): return False, "disabled"
    host  = cfg.get("smtp_host","smtp.gmail.com")
    port  = int(cfg.get("smtp_port", 587))
    user  = cfg.get("username","").strip()
    pwd   = cfg.get("password","").strip()
    frm   = cfg.get("from_address", user).strip()
    to    = cfg.get("to_address","").strip()
    if not all([user, pwd, to]): return False, "missing credentials"
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
    if not cfg.get("enabled"): return False, "disabled"
    to = cfg.get("to_number","").strip()
    if not to: return False, "no recipient configured"
    script = f'''tell application "Messages"
        set s to 1st service whose service type = iMessage
        set b to buddy "{to}" of s
        send "{body}" to b
    end tell'''
    try:
        r = subprocess.run(["osascript","-e",script], capture_output=True, text=True, timeout=15)
        return (True,"ok") if r.returncode==0 else (False, r.stderr.strip())
    except FileNotFoundError:
        return False, "osascript not found (macOS only)"
    except Exception as e:
        return False, str(e)

def dispatch_alert(level, msg):
    printer = config.get("printer_host", PRINTER_HOST)
    ts      = datetime.now().strftime("%H:%M:%S")
    title   = f"🖨️ Printer Alert — {level.upper()}"
    full    = f"[{ts}] {msg}\n\nPrinter: {printer}"
    results = {
        "ntfy":     send_ntfy(title, full, level),
        "sms":      send_twilio_sms(f"Printer Alert: {msg}"),
        "email":    send_email(title, full),
        "imessage": send_imessage(f"Printer Alert: {msg}"),
    }
    sent    = [ch for ch,(ok,_) in results.items() if ok]
    failed  = [(ch,err) for ch,(ok,err) in results.items() if not ok and err!="disabled"]
    entry   = {"time": datetime.now().isoformat(), "level": level, "msg": msg,
               "sent": sent, "failed": failed}
    with lock:
        alert_log.insert(0, entry)
        if len(alert_log) > 100: alert_log.pop()
    print(f"  [{ts}] ALERT — {msg}")
    print(f"         sent: {', '.join(sent) if sent else 'none'}" +
          (f" | failed: {', '.join(f'{c}({e})' for c,e in failed)}" if failed else ""))
    return results

# ── Background polling ─────────────────────────────────────────────────────────

def fetch_status():
    url = (config.get("printer_host", PRINTER_HOST) +
           "/printer/objects/query"
           "?print_stats&extruder&heater_bed&toolhead&virtual_sdcard&webhooks&display_status")
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("result",{}).get("status",{})

def process_status(s):
    global last_status, active_alerts, fired_alerts
    with lock:
        last_status = s
    anomalies = detect_anomalies(s)
    alert_on_warnings = config.get("alert_on_warnings", True)
    state = s.get("print_stats",{}).get("state","")
    if state not in ("printing","paused"):
        with lock:
            fired_alerts.clear()
    for level, msg in anomalies:
        key = f"{level}:{msg}"
        should_fire = level in ("critical","success") or (level=="warning" and alert_on_warnings)
        with lock:
            already_fired = key in fired_alerts
        if should_fire and not already_fired:
            with lock:
                fired_alerts.add(key)
            dispatch_alert(level, msg)
    with lock:
        active_alerts[:] = [{"level":l,"msg":m} for l,m in anomalies]

def poll_printer():
    errors = 0
    while True:
        interval = config.get("poll_interval_seconds", 1800)
        try:
            s = fetch_status()
            process_status(s)
            ps  = s.get("print_stats",{})
            vsd = s.get("virtual_sdcard",{})
            ts  = datetime.now().strftime("%H:%M:%S")
            pct = vsd.get("progress",0)*100
            layer   = ps.get("info",{}).get("current_layer","?")
            totlayer= ps.get("info",{}).get("total_layer","?")
            state   = ps.get("state","?")
            print(f"  [{ts}] Poll OK — {state.upper()} {pct:.1f}% L{layer}/{totlayer}")
            errors = 0
        except Exception as e:
            errors += 1
            print(f"  [{datetime.now().strftime('%H:%M:%S')}] Poll error ({errors}): {e}")
            interval = min(interval, 300)
        time.sleep(interval)

# ── HTML UI ────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>🖨️ Printer Monitor</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0f;color:#e2e8f0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;min-height:100vh;padding:20px}
    .container{max-width:580px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
    .header{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #1e293b;padding-bottom:12px}
    .header-title{font-size:15px;font-weight:700;color:#f8fafc}
    .header-sub{font-size:11px;color:#475569;margin-top:2px}
    .header-meta{text-align:right;font-size:11px;color:#475569;line-height:1.8}
    .header-meta span{color:#94a3b8}
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
    .btn-row{display:flex;gap:8px}
    .btn{flex:1;background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:12px;padding:9px;border-radius:6px;cursor:pointer;transition:background .15s}
    .btn:hover:not(:disabled){background:#263347}.btn:disabled{opacity:.4;cursor:default}
    .log{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px}
    .log h3{font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}
    .log-list{max-height:220px;overflow-y:auto;display:flex;flex-direction:column;gap:5px}
    .log-entry{font-size:11px;padding:6px 8px;border-radius:4px;border-left:2px solid}
    .log-entry-critical{background:#140808;border-color:#ef4444;color:#fca5a5}
    .log-entry-warning{background:#14100a;border-color:#f59e0b;color:#fcd34d}
    .log-entry-success{background:#081408;border-color:#22c55e;color:#86efac}
    .log-sent{color:#475569;font-size:10px;margin-top:3px}
    .section-label{font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:.08em}
    .footer{text-align:center;font-size:10px;color:#1e293b;padding-top:4px}
  </style>
</head>
<body><div class="container">

  <div class="header">
    <div>
      <div class="header-title">🖨️ Printer Monitor</div>
      <div class="header-sub" id="printerUrl">__PRINTER_HOST__</div>
    </div>
    <div class="header-meta">
      UI polls: <span id="checkCount">0</span><br>
      Last: <span id="lastCheck">—</span><br>
      Next alert: <span id="countdown">—</span>
    </div>
  </div>

  <div>
    <div class="section-label" style="margin-bottom:6px">Alert channels (server-side — active when tab is closed)</div>
    <div class="channels">
      <span class="ch ch-off" id="ch-ntfy">📱 Push</span>
      <span class="ch ch-off" id="ch-sms">💬 SMS</span>
      <span class="ch ch-off" id="ch-email">📧 Email</span>
      <span class="ch ch-off" id="ch-imessage">💬 iMessage</span>
    </div>
  </div>

  <div id="alertsContainer"></div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <span class="badge badge-unknown" id="stateBadge">CONNECTING…</span>
      <span class="spinner" id="spinner">syncing…</span>
    </div>
    <div>
      <div class="filename" id="filename">—</div>
      <div class="progress-row">
        <span class="progress-pct" id="progressPct">—</span>
        <span class="progress-layer" id="layerInfo">—</span>
      </div>
      <div class="progress-bar-bg"><div class="progress-bar-fill" id="progressFill" style="width:0%"></div></div>
    </div>
    <div class="stats-grid">
      <div><div class="stat-label">Elapsed</div><div class="stat-value" id="elapsed">—</div></div>
      <div><div class="stat-label">ETA</div><div class="stat-value" id="eta">—</div></div>
      <div><div class="stat-label">Filament</div><div class="stat-value" id="filament">—</div></div>
      <div><div class="stat-label">Z Position</div><div class="stat-value" id="zpos">—</div></div>
    </div>
    <div class="temps">
      <div class="temp-row">
        <span class="temp-label">Hotend</span>
        <span class="temp-val temp-ok" id="hotendTemp">—</span>
        <span class="temp-target" id="hotendTarget">/ —</span>
        <span id="hotendIcon"></span>
      </div>
      <div class="temp-row">
        <span class="temp-label">Bed</span>
        <span class="temp-val temp-ok" id="bedTemp">—</span>
        <span class="temp-target" id="bedTarget">/ —</span>
        <span id="bedIcon"></span>
      </div>
    </div>
  </div>

  <div class="btn-row">
    <button class="btn" onclick="triggerPoll()">🔄 Poll Now</button>
    <button class="btn" onclick="sendTest()">🔔 Test Alerts</button>
    <button class="btn" onclick="openConfig()">⚙️ Open Config</button>
  </div>

  <div class="log">
    <h3>Alert Dispatch Log</h3>
    <div class="log-list" id="alertLogList">
      <div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>
    </div>
  </div>

  <div class="footer">
    Server polls every <span id="intervalLabel">30 min</span> — alerts fire even when this tab is closed<br>
    Edit <strong>monitor_config.json</strong> to enable channels · proxy: localhost:__PORT__
  </div>
</div>

<script>
  let uiPollCount=0, countdownID=null, nextCheckAt=null;

  function fmtTime(s){
    if(s<0)return"—";
    return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ${Math.floor(s%60)}s`;
  }

  async function refreshUI(){
    document.getElementById("spinner").style.display="inline";
    try{
      const [sr,ar,cr] = await Promise.all([
        fetch("/api/status"), fetch("/api/alerts"), fetch("/api/config")
      ]);
      const {status:s, last_poll} = await sr.json();
      const {active_alerts, alert_log} = await ar.json();
      const cfg = await cr.json();
      if(s) renderStatus(s);
      renderAlerts(active_alerts||[]);
      renderAlertLog(alert_log||[]);
      renderChannels(cfg);
      updateCountdown(cfg.poll_interval_seconds||1800, last_poll);
      uiPollCount++;
      document.getElementById("checkCount").textContent=uiPollCount;
      document.getElementById("lastCheck").textContent=
        new Date().toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
    }catch(e){
      renderAlerts([{level:"critical",msg:`❌ Monitor server unreachable: ${e.message}`}]);
    }finally{
      document.getElementById("spinner").style.display="none";
    }
  }

  function renderChannels(cfg){
    const map={ntfy:cfg.ntfy?.enabled, sms:cfg.twilio?.enabled,
               email:cfg.email?.enabled, imessage:cfg.imessage?.enabled};
    const labels={ntfy:"📱 Push",sms:"💬 SMS",email:"📧 Email",imessage:"💬 iMessage"};
    for(const[ch,on] of Object.entries(map)){
      const el=document.getElementById(`ch-${ch}`);
      if(el){el.className=`ch ${on?"ch-on":"ch-off"}`;el.textContent=labels[ch];}
    }
    const s=cfg.poll_interval_seconds||1800,m=Math.round(s/60);
    document.getElementById("intervalLabel").textContent=m>=60?`${m/60}h`:`${m} min`;
  }

  function updateCountdown(intervalSecs, lastPoll){
    if(countdownID)clearInterval(countdownID);
    if(!lastPoll){document.getElementById("countdown").textContent="—";return;}
    nextCheckAt=new Date(lastPoll).getTime()+intervalSecs*1000;
    countdownID=setInterval(()=>{
      const d=nextCheckAt-Date.now();
      if(d<=0){document.getElementById("countdown").textContent="polling…";return;}
      document.getElementById("countdown").textContent=
        `${Math.floor(d/60000)}m ${Math.floor((d%60000)/1000)}s`;
    },1000);
  }

  function renderStatus(s){
    const ps=s.print_stats||{},ext=s.extruder||{},bed=s.heater_bed||{},
          vsd=s.virtual_sdcard||{},th=s.toolhead||{},pos=th.position||[0,0,0,0];
    const state=ps.state||"unknown",prog=(vsd.progress||0)*100,
          el=ps.print_duration||0,tot=ps.total_duration||0,eta=tot>el?tot-el:-1,
          layer=ps.info?.current_layer??"?",totL=ps.info?.total_layer??"?",
          fil=((ps.filament_used||0)/1000).toFixed(2);
    const b=document.getElementById("stateBadge");
    b.className=`badge badge-${state}`;b.textContent=state.toUpperCase();
    document.getElementById("filename").textContent=ps.filename||"—";
    document.getElementById("progressPct").textContent=`${prog.toFixed(1)}%`;
    document.getElementById("layerInfo").textContent=`Layer ${layer}/${totL}`;
    document.getElementById("progressFill").style.width=`${Math.min(prog,100)}%`;
    document.getElementById("elapsed").textContent=fmtTime(el);
    document.getElementById("eta").textContent=fmtTime(eta);
    document.getElementById("filament").textContent=`${fil}m`;
    document.getElementById("zpos").textContent=`${pos[2]?.toFixed(2)}mm`;
    const hOk=ext.target===0||Math.abs(ext.temperature-ext.target)<=20;
    const bOk=bed.target===0||Math.abs(bed.temperature-bed.target)<=15;
    document.getElementById("hotendTemp").textContent=`${ext.temperature?.toFixed(1)}°C`;
    document.getElementById("hotendTemp").className=`temp-val ${hOk?"temp-ok":"temp-bad"}`;
    document.getElementById("hotendTarget").textContent=`/ ${ext.target}°C`;
    document.getElementById("hotendIcon").textContent=hOk?"✅":"⚠️";
    document.getElementById("bedTemp").textContent=`${bed.temperature?.toFixed(1)}°C`;
    document.getElementById("bedTemp").className=`temp-val ${bOk?"temp-ok":"temp-bad"}`;
    document.getElementById("bedTarget").textContent=`/ ${bed.target}°C`;
    document.getElementById("bedIcon").textContent=bOk?"✅":"⚠️";
  }

  function renderAlerts(alerts){
    const c=document.getElementById("alertsContainer");c.innerHTML="";
    if(!alerts.length){
      c.innerHTML='<div class="alert alert-ok">✅ All systems nominal — no anomalies detected</div>';
      return;
    }
    alerts.forEach(a=>{
      const d=document.createElement("div");
      d.className=`alert alert-${a.level}`;d.textContent=a.msg;c.appendChild(d);
    });
    if(alerts.some(a=>a.level==="critical")){
      document.title="🚨 ALERT — Printer Monitor";
      setTimeout(()=>{document.title="🖨️ Printer Monitor";},6000);
      if(Notification.permission==="granted")
        new Notification("🖨️ Printer Alert",{body:alerts[0].msg});
    }
  }

  function renderAlertLog(log){
    const list=document.getElementById("alertLogList");
    if(!log.length){
      list.innerHTML='<div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>';
      return;
    }
    list.innerHTML=log.slice(0,20).map(e=>{
      const t=new Date(e.time).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
      const sent=e.sent.length?`sent: ${e.sent.join(", ")}`:"no channels enabled";
      const failed=e.failed.length?` · failed: ${e.failed.map(f=>f[0]).join(",")}` :"";
      return `<div class="log-entry log-entry-${e.level}">
        <span style="opacity:.5">[${t}]</span> ${e.msg}
        <div class="log-sent">${sent}${failed}</div>
      </div>`;
    }).join("");
  }

  async function triggerPoll(){
    await fetch("/api/poll",{method:"POST"});
    setTimeout(refreshUI, 2500);
  }
  async function sendTest(){
    await fetch("/api/test_alert",{method:"POST"});
    setTimeout(refreshUI, 2500);
  }
  function openConfig(){
    window.open("/monitor_config.json","_blank");
  }

  if("Notification"in window&&Notification.permission==="default")
    Notification.requestPermission();

  refreshUI();
  setInterval(refreshUI, 30000);
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
        body=html.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/","/index.html"):
            page = HTML.replace("__PRINTER_HOST__", config.get("printer_host",PRINTER_HOST)) \
                       .replace("__PORT__", str(PORT))
            self.send_html(page)
        elif self.path == "/api/status":
            with lock: s = dict(last_status)
            self.send_json(200, {"status": s, "last_poll": datetime.now().isoformat()})
        elif self.path == "/api/alerts":
            with lock: a,lg = list(active_alerts), list(alert_log)
            self.send_json(200, {"active_alerts": a, "alert_log": lg})
        elif self.path == "/api/config":
            safe = json.loads(json.dumps(config))
            for ch in ("twilio","email"):
                for k in ("auth_token","password"):
                    if safe.get(ch,{}).get(k): safe[ch][k] = "••••••••"
            self.send_json(200, safe)
        elif self.path == "/monitor_config.json":
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE,"rb") as f: body=f.read()
                self.send_response(200)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length",len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(404,{"error":"config not found"})
        elif self.path.startswith("/proxy/"):
            self._proxy(self.path[len("/proxy"):])
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/poll":
            threading.Thread(target=poll_once, daemon=True).start()
            self.send_json(200,{"ok":True})
        elif self.path == "/api/test_alert":
            threading.Thread(target=dispatch_alert,
                args=("warning","🔔 Test alert — all enabled channels should receive this"),
                daemon=True).start()
            self.send_json(200,{"ok":True})
        else:
            self.send_response(404); self.end_headers()

    def _proxy(self, path):
        target = config.get("printer_host", PRINTER_HOST) + path
        try:
            req = urllib.request.Request(target, headers={"Accept":"application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",len(data))
            self.send_header("Access-Control-Allow-Origin","*")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as e:
            self.send_json(502,{"error":f"Cannot reach printer: {e.reason}"})
        except Exception as e:
            self.send_json(500,{"error":str(e)})

def poll_once():
    try:
        s = fetch_status()
        process_status(s)
    except Exception as e:
        print(f"  poll_once error: {e}")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_config()
    threading.Thread(target=poll_printer, daemon=True).start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    interval_min = config.get("poll_interval_seconds",1800)//60
    icons = {k: "✅" if config.get(m,{}).get("enabled") else "❌"
             for k,m in [("ntfy","ntfy"),("sms","twilio"),("email","email"),("imsg","imessage")]}

    print(f"""
╔══════════════════════════════════════════════════════╗
║       🖨️  Printer Monitor — Alert Server             ║
╠══════════════════════════════════════════════════════╣
║  Monitor : http://localhost:{PORT:<26}║
║  Printer : {config.get('printer_host',PRINTER_HOST):<44}║
║  Polling : every {interval_min} min (background thread)          ║
╠══════════════════════════════════════════════════════╣
║  Alert Channels                                      ║
║    {icons['ntfy']} Push (ntfy.sh)   {icons['sms']} SMS (Twilio)          ║
║    {icons['email']} Email (SMTP)    {icons['imsg']} iMessage (macOS)      ║
╠══════════════════════════════════════════════════════╣
║  Config  : monitor_config.json                       ║
║  Set enabled:true per channel, then restart.         ║
╚══════════════════════════════════════════════════════╝

  Open  http://localhost:{PORT}  in your browser.
  Alerts fire on background thread — tab can be closed.
  Press Ctrl+C to stop.
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
