#!/usr/bin/env python3
"""
Printer Monitor — Local Proxy Server
Serves the monitor UI on http://localhost:8484 and proxies
API calls to your Moonraker instance, bypassing CORS entirely.

Usage:
    python3 monitor_server.py
    python3 monitor_server.py http://10.0.107.158
    python3 monitor_server.py http://10.0.107.158 8484
"""

import sys
import json
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Config ────────────────────────────────────────────────────────────────────
PRINTER_HOST = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://10.0.107.158"
PORT         = int(sys.argv[2])        if len(sys.argv) > 2 else 8484

# ── Embedded monitor HTML (single-file, talks to /proxy/* on this server) ────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>🖨️ Printer Monitor</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0a0a0f;color:#e2e8f0;font-family:'SF Mono','Fira Code',monospace;font-size:13px;min-height:100vh;padding:20px}
    .container{max-width:560px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
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
    .card-top{display:flex;align-items:center;justify-content:space-between}
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
    .temp-val{font-weight:700;font-size:13px}
    .temp-ok{color:#4ade80}.temp-bad{color:#f87171}
    .temp-target{color:#475569;font-size:11px}
    .interval-row{display:flex;align-items:center;gap:10px;font-size:11px;color:#475569}
    .interval-row select{background:#0a0a0f;border:1px solid #334155;color:#e2e8f0;font-family:inherit;font-size:11px;padding:3px 6px;border-radius:4px}
    .btn{width:100%;background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:12px;padding:9px;border-radius:6px;cursor:pointer;transition:background .15s}
    .btn:hover:not(:disabled){background:#263347}.btn:disabled{opacity:.4;cursor:default}
    .log{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:14px}
    .log h3{font-size:11px;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}
    .log-list{max-height:180px;overflow-y:auto;display:flex;flex-direction:column;gap:4px}
    .log-entry{display:flex;align-items:center;gap:8px;font-size:11px}
    .log-time{color:#334155;width:44px;flex-shrink:0}.log-pct{color:#64748b;width:38px}
    .log-layer{color:#334155}.log-alert{color:#f87171;font-weight:600;margin-left:auto}
    .log-ok{color:#22c55e;margin-left:auto}
    .footer{text-align:center;font-size:10px;color:#1e293b;padding-top:4px}
  </style>
</head>
<body>
<div class="container">

  <div class="header">
    <div>
      <div class="header-title">🖨️ Printer Monitor</div>
      <div class="header-sub" id="printerUrl">__PRINTER_HOST__</div>
    </div>
    <div class="header-meta">
      Checks: <span id="checkCount">0</span><br>
      Last: <span id="lastCheck">—</span><br>
      Next: <span id="countdown">—</span>
    </div>
  </div>

  <div class="interval-row">
    ⏱ Check every
    <select id="cfgInterval" onchange="changeInterval()">
      <option value="300000">5 min</option>
      <option value="600000">10 min</option>
      <option value="900000">15 min</option>
      <option value="1800000" selected>30 min</option>
      <option value="3600000">60 min</option>
    </select>
  </div>

  <div id="alertsContainer"></div>

  <div class="card" id="statusCard">
    <div class="card-top">
      <span class="badge badge-unknown" id="stateBadge">CONNECTING…</span>
      <span class="spinner" id="spinner">fetching…</span>
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

  <button class="btn" id="refreshBtn" onclick="checkNow()">🔄 Check Now</button>

  <div class="log" id="logPanel">
    <h3>Check History</h3>
    <div class="log-list" id="logList"></div>
  </div>

  <div class="footer">Proxy: localhost:__PORT__ → __PRINTER_HOST__ · Monitors: thermal · Klippy · clog · stall · Z shift · state</div>
</div>

<script>
  let INTERVAL_MS = 1800000;
  let timerID = null, countdownID = null, nextCheckAt = null, checkCount = 0;

  function fmtTime(s){
    if(s<0)return"—";
    return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m ${Math.floor(s%60)}s`;
  }
  function fmtNow(){
    return new Date().toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
  }

  function detectAnomalies(s){
    const a=[], ps=s.print_stats||{}, ext=s.extruder||{}, bed=s.heater_bed||{},
          vsd=s.virtual_sdcard||{}, wh=s.webhooks||{}, th=s.toolhead||{};
    if(wh.state&&wh.state!=="ready")
      a.push({level:"critical",msg:`🔴 Klippy not ready: ${wh.state} — ${wh.state_message||""}`});
    if(bed.target>0&&Math.abs(bed.temperature-bed.target)>15)
      a.push({level:"critical",msg:`🌡️ Thermal anomaly — Bed: target ${bed.target}°C, actual ${bed.temperature?.toFixed(1)}°C`});
    if(ext.target>0&&Math.abs(ext.temperature-ext.target)>20)
      a.push({level:"critical",msg:`🌡️ Thermal anomaly — Hotend: target ${ext.target}°C, actual ${ext.temperature?.toFixed(1)}°C`});
    const st=ps.state||"", el=ps.print_duration||0, fil=ps.filament_used||0,
          pr=vsd.progress||0, pos=th.position||[0,0,0,0];
    if(st==="printing"&&el>300&&fil<5)
      a.push({level:"warning",msg:"🧵 Possible clog / under-extrusion: very low filament after 5+ min"});
    if(st==="printing"&&el>600&&pr<0.001)
      a.push({level:"warning",msg:"⏸️ Possible stall: no progress after 10 min"});
    if(st==="printing"&&el>120&&pos[2]<0.1)
      a.push({level:"warning",msg:`📐 Z anomaly: Z=${pos[2]?.toFixed(3)}mm while printing`});
    if(st==="error")
      a.push({level:"critical",msg:`🛑 Print error: ${ps.message||"unknown"}`});
    if(st==="cancelled")
      a.push({level:"warning",msg:"🛑 Print was cancelled"});
    if(st==="complete")
      a.push({level:"success",msg:`🎉 Print complete! ${ps.filename}`});
    return a;
  }

  async function fetchStatus(){
    document.getElementById("spinner").style.display="inline";
    document.getElementById("refreshBtn").disabled=true;
    try{
      const resp = await fetch("/proxy/printer/objects/query?print_stats&extruder&heater_bed&toolhead&virtual_sdcard&webhooks&display_status");
      if(!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const s = data.result?.status||{};
      renderStatus(s);
      const found = detectAnomalies(s);
      renderAlerts(found);
      addLog(s, found);
      if(found.some(a=>a.level==="critical"||a.level==="warning")){
        if(Notification.permission==="granted")
          new Notification("🖨️ Printer Alert",{body:found[0].msg});
        document.title="🚨 ALERT — Printer Monitor";
        setTimeout(()=>{document.title="🖨️ Printer Monitor";},5000);
      }
      checkCount++;
      document.getElementById("checkCount").textContent=checkCount;
      document.getElementById("lastCheck").textContent=fmtNow();
    }catch(e){
      renderAlerts([{level:"critical",msg:`❌ Proxy error: ${e.message} — is monitor_server.py running?`}]);
    }finally{
      document.getElementById("spinner").style.display="none";
      document.getElementById("refreshBtn").disabled=false;
    }
  }

  function renderStatus(s){
    const ps=s.print_stats||{}, ext=s.extruder||{}, bed=s.heater_bed||{},
          vsd=s.virtual_sdcard||{}, th=s.toolhead||{}, pos=th.position||[0,0,0,0];
    const state=ps.state||"unknown", prog=(vsd.progress||0)*100,
          el=ps.print_duration||0, tot=ps.total_duration||0, eta=tot>el?tot-el:-1,
          layer=ps.info?.current_layer??"?", totL=ps.info?.total_layer??"?",
          fil=((ps.filament_used||0)/1000).toFixed(2);
    const b=document.getElementById("stateBadge");
    b.className=`badge badge-${state}`; b.textContent=state.toUpperCase();
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
    const c=document.getElementById("alertsContainer"); c.innerHTML="";
    if(!alerts.length){
      c.innerHTML='<div class="alert alert-ok">✅ All systems nominal — no anomalies detected</div>';
      return;
    }
    alerts.forEach(a=>{
      const d=document.createElement("div");
      d.className=`alert alert-${a.level}`; d.textContent=a.msg; c.appendChild(d);
    });
  }

  function addLog(s,alerts){
    const ps=s.print_stats||{}, vsd=s.virtual_sdcard||{};
    const state=ps.state||"unknown", prog=((vsd.progress||0)*100).toFixed(1),
          layer=ps.info?.current_layer??"?", totL=ps.info?.total_layer??"?";
    const list=document.getElementById("logList");
    const div=document.createElement("div"); div.className="log-entry";
    div.innerHTML=`
      <span class="log-time">${fmtNow()}</span>
      <span class="badge badge-${state}" style="font-size:10px;padding:1px 5px">${state.toUpperCase()}</span>
      <span class="log-pct">${prog}%</span>
      <span class="log-layer">L${layer}/${totL}</span>
      ${alerts.length?`<span class="log-alert">⚠️ ${alerts.length}</span>`:'<span class="log-ok">✅</span>'}
    `;
    list.prepend(div);
    while(list.children.length>48) list.removeChild(list.lastChild);
  }

  function checkNow(){
    if(timerID) clearTimeout(timerID);
    if(countdownID) clearInterval(countdownID);
    fetchStatus().then(scheduleNext);
  }

  function scheduleNext(){
    if(timerID) clearTimeout(timerID);
    nextCheckAt=Date.now()+INTERVAL_MS;
    timerID=setTimeout(checkNow, INTERVAL_MS);
    if(countdownID) clearInterval(countdownID);
    countdownID=setInterval(()=>{
      const d=nextCheckAt-Date.now();
      if(d<=0){document.getElementById("countdown").textContent="checking…";return;}
      document.getElementById("countdown").textContent=`${Math.floor(d/60000)}m ${Math.floor((d%60000)/1000)}s`;
    },1000);
  }

  function changeInterval(){
    INTERVAL_MS=parseInt(document.getElementById("cfgInterval").value);
    checkNow();
  }

  if("Notification"in window&&Notification.permission==="default")
    Notification.requestPermission();

  checkNow();
</script>
</body>
</html>
"""

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        # Suppress default access log noise; only print errors
        if str(args[1]) not in ("200", "304"):
            print(f"  {self.address_string()} {fmt % args}")

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Serve monitor UI
        if self.path == "/" or self.path == "/index.html":
            page = HTML.replace("__PRINTER_HOST__", PRINTER_HOST).replace("__PORT__", str(PORT))
            self.send_html(page)
            return

        # Proxy all /proxy/* requests to Moonraker
        if self.path.startswith("/proxy/"):
            moonraker_path = self.path[len("/proxy"):]   # strip /proxy prefix
            target_url = f"{PRINTER_HOST}{moonraker_path}"
            try:
                req = urllib.request.Request(target_url, headers={"Accept": "application/json"})
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
            return

        self.send_response(404)
        self.end_headers()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"""
╔══════════════════════════════════════════════════╗
║         🖨️  Printer Monitor — Local Proxy        ║
╠══════════════════════════════════════════════════╣
║  Monitor URL  : http://localhost:{PORT:<18}║
║  Printer      : {PRINTER_HOST:<34}║
╚══════════════════════════════════════════════════╝

  Open http://localhost:{PORT} in your browser.
  Press Ctrl+C to stop.
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
