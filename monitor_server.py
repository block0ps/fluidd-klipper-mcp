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

import sys, os, json, time, smtplib, threading, subprocess, uuid
import urllib.request, urllib.error, urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer as _HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class HTTPServer(ThreadingMixIn, _HTTPServer):
    """Thread-per-request so LLM calls never block the UI poll loop."""
    daemon_threads = True
from datetime import datetime, timedelta

ADD_PRINTER_MODE    = (len(sys.argv) > 1 and sys.argv[1] == "add-printer")
CONFIGURE_LLM_MODE  = (len(sys.argv) > 1 and sys.argv[1] == "configure-llm")
ENABLE_TIER3_MODE   = (len(sys.argv) > 1 and sys.argv[1] == "enable-tier3")
PORT = 8484
for _arg in sys.argv[1:]:
    if _arg.isdigit(): PORT = int(_arg)

CONFIG_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_config.json")
CHAT_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_history.json")

DEFAULT_CONFIG = {
    "printers": [
        {"id": "printer1", "name": "Printer 1", "host": "http://10.0.107.158",
         "enabled": True, "api_token": ""}
    ],
    "poll_interval_seconds": 1800,
    "pause_escalate_minutes": 30,
    "alert_on_warnings": True,
    "ntfy":     {"enabled": False, "topic": "my-printer-alerts", "server": "https://ntfy.sh"},
    "twilio":   {"enabled": False, "account_sid": "", "auth_token": "",
                 "from_number": "", "to_number": ""},
    "email":    {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                 "username": "", "password": "", "from_address": "", "to_address": ""},
    "imessage": {"enabled": False, "to_number": ""},
    "agent": {
        "tier": 2,
        "trust_mode": {"enabled": False, "expires_at": None, "duration_hours": 24},
        "tier3": {"enabled": False, "expires_at": None}
    },
    "llm": {
        "enabled": False,
        "provider": "anthropic",
        "history_enabled": True,
        "history_max_messages": 100,
        "anthropic": {"api_key": "", "model": "claude-haiku-4-5-20251001"},
        "openai":    {"api_key": "", "model": "gpt-4o-mini",
                      "base_url": "https://api.openai.com/v1"},
        "ollama":    {"base_url": "http://localhost:11434", "model": "llama3.2"},
        "gemini":    {"api_key": "", "model": "gemini-2.0-flash"}
    }
}

config        = {}
alert_log     = []
global_lock   = threading.Lock()
printer_states = {}
poll_threads   = {}
chat_history  = []           # in-memory; optionally persisted to chat_history.json
chat_lock            = threading.Lock()
pending_actions      = {}           # action_id -> pending-action dict
pending_lock         = threading.Lock()

# ── LLM adapter layer ─────────────────────────────────────────────────────────
# To swap providers, change config["llm"]["provider"].
# Each adapter speaks only stdlib urllib — zero extra dependencies.

class LLMAdapter:
    """Abstract base. chat() returns (text_or_None, tool_calls, raw_assistant)."""
    def chat(self, messages: list, system: str, tools: list = None):
        raise NotImplementedError
    def chat_with_results(self, msgs, asst_raw, tc_results, system):
        raise NotImplementedError
    def _fmt_tool_oai(self, t):
        return {"name": t["name"], "description": t["description"],
                "parameters": t["parameters"]}

class AnthropicAdapter(LLMAdapter):
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001"):
        self.api_key = api_key; self.model = model
    def _fmt_tool(self, t):
        return {"name": t["name"], "description": t["description"],
                "input_schema": t["parameters"]}
    def chat(self, messages: list, system: str, tools: list = None):
        payload = {"model": self.model, "max_tokens": 2048,
                   "system": system, "messages": messages}
        if tools:
            payload["tools"] = [self._fmt_tool(t) for t in tools]
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        content = data.get("content", [])
        text = "\n".join(b["text"] for b in content if b.get("type") == "text") or None
        calls = [{"id": b["id"], "name": b["name"], "args": b.get("input", {}), "raw": b}
                 for b in content if b.get("type") == "tool_use"]
        return text, calls, {"content": content}
    def chat_with_results(self, msgs, asst_raw, tc_results, system):
        m = list(msgs) + [
            {"role": "assistant", "content": asst_raw["content"]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tc["id"], "content": r}
                for tc, r in tc_results]}]
        text, _, _ = self.chat(m, system, [])
        return text or ""

class OpenAIAdapter(LLMAdapter):
    """OpenAI, Groq, Mistral, LM Studio, Together, OpenRouter, etc."""
    def __init__(self, api_key: str, model: str = "gpt-4o-mini",
                 base_url: str = "https://api.openai.com/v1"):
        self.api_key = api_key; self.model = model
        self.base_url = base_url.rstrip("/")
    def chat(self, messages: list, system: str, tools: list = None):
        full = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": full, "max_tokens": 2048}
        if tools:
            payload["tools"] = [{"type": "function",
                                  "function": self._fmt_tool_oai(t)} for t in tools]
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; FleetMonitor/1.0)"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        msg = data["choices"][0]["message"]
        text = msg.get("content") or None
        raw_calls = msg.get("tool_calls") or []
        calls = [{"id": c["id"], "name": c["function"]["name"],
                  "args": json.loads(c["function"].get("arguments") or "{}"), "raw": c}
                 for c in raw_calls]
        return text, calls, msg
    def chat_with_results(self, msgs, asst_raw, tc_results, system):
        full = [{"role": "system", "content": system}] + list(msgs)
        full.append({"role": "assistant", "content": asst_raw.get("content"),
                     "tool_calls": asst_raw.get("tool_calls", [])})
        for tc, r in tc_results:
            full.append({"role": "tool", "tool_call_id": tc["id"], "content": r})
        payload = {"model": self.model, "messages": full, "max_tokens": 2048}
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (compatible; FleetMonitor/1.0)"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"].get("content") or ""

class OllamaAdapter(LLMAdapter):
    """Local Ollama — no API key. Tool calling requires llama3.1:8b+ or mistral-nemo."""
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3.2"):
        self.base_url = base_url.rstrip("/"); self.model = model
    def chat(self, messages: list, system: str, tools: list = None):
        full = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": full, "stream": False}
        if tools:
            payload["tools"] = [{"type": "function",
                                  "function": self._fmt_tool_oai(t)} for t in tools]
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        msg = data.get("message", {})
        text = msg.get("content") or None
        raw_calls = msg.get("tool_calls") or []
        calls = [{"id": f"call_{i}", "name": c["function"]["name"],
                  "args": c["function"].get("arguments", {}), "raw": c}
                 for i, c in enumerate(raw_calls)]
        return text, calls, msg
    def chat_with_results(self, msgs, asst_raw, tc_results, system):
        full = [{"role": "system", "content": system}] + list(msgs)
        full.append({"role": "assistant", "content": asst_raw.get("content", ""),
                     "tool_calls": asst_raw.get("tool_calls", [])})
        for _, r in tc_results:
            full.append({"role": "tool", "content": r})
        payload = {"model": self.model, "messages": full, "stream": False}
        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return data.get("message", {}).get("content") or ""

class GeminiAdapter(LLMAdapter):
    """Google Gemini — free tier: 1 500 req/day on gemini-2.0-flash.
    API key at https://aistudio.google.com (zero cost, no billing required)."""
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key = api_key; self.model = model

    def _url(self, method: str) -> str:
        return f"{self.BASE}/models/{self.model}:{method}?key={self.api_key}"

    def _convert_messages(self, messages: list) -> list:
        """Convert role/content messages to Gemini contents format."""
        contents = []
        for m in messages:
            role = "model" if m["role"] == "assistant" else "user"
            # Support pre-built Gemini content blocks (used in chat_with_results)
            if isinstance(m.get("content"), list):
                contents.append({"role": role, "parts": m["content"]})
            else:
                contents.append({"role": role,
                                  "parts": [{"text": m.get("content") or ""}]})
        return contents

    def _fmt_tools(self, tools: list) -> list:
        """Convert internal tool defs to Gemini function_declarations format."""
        if not tools: return []
        return [{"function_declarations": [
            {"name": t["name"], "description": t["description"],
             "parameters": t["parameters"]} for t in tools]}]

    def chat(self, messages: list, system: str, tools: list = None):
        contents = self._convert_messages(messages)
        payload  = {"contents": contents,
                    "systemInstruction": {"parts": [{"text": system}]},
                    "generationConfig": {"maxOutputTokens": 2048}}
        if tools:
            payload["tools"] = self._fmt_tools(tools)
        req = urllib.request.Request(
            self._url("generateContent"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        parts  = data["candidates"][0]["content"]["parts"]
        text   = "\n".join(p["text"] for p in parts if "text" in p) or None
        calls  = []
        for i, p in enumerate(parts):
            if "functionCall" in p:
                fc = p["functionCall"]
                calls.append({"id": f"gemini_call_{i}", "name": fc["name"],
                               "args": fc.get("args", {}), "raw": p})
        # Store the raw model content for chat_with_results
        raw = {"parts": parts, "role": "model"}
        return text, calls, raw

    def chat_with_results(self, msgs, asst_raw, tc_results, system):
        # Reconstruct the full contents including model tool call + function responses
        contents = self._convert_messages(msgs)
        contents.append({"role": "model", "parts": asst_raw["parts"]})
        # Add function responses as a single user turn
        resp_parts = [
            {"functionResponse": {"name": tc["name"],
                                   "response": {"result": result}}}
            for tc, result in tc_results]
        contents.append({"role": "user", "parts": resp_parts})
        payload = {"contents": contents,
                   "systemInstruction": {"parts": [{"text": system}]},
                   "generationConfig": {"maxOutputTokens": 2048}}
        req = urllib.request.Request(
            self._url("generateContent"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        parts = data["candidates"][0]["content"]["parts"]
        return "\n".join(p["text"] for p in parts if "text" in p) or ""

def get_llm_adapter() -> LLMAdapter:
    """Instantiate the active LLM adapter from config. Raises ValueError if misconfigured."""
    llm  = config.get("llm", {})
    prov = llm.get("provider", "anthropic")
    if prov == "anthropic":
        key   = llm.get("anthropic", {}).get("api_key", "").strip()
        model = llm.get("anthropic", {}).get("model", "claude-haiku-4-5-20251001")
        if not key: raise ValueError("Anthropic API key not configured.")
        return AnthropicAdapter(key, model)
    elif prov == "openai":
        key   = llm.get("openai", {}).get("api_key", "").strip()
        model = llm.get("openai", {}).get("model", "gpt-4o-mini")
        base  = llm.get("openai", {}).get("base_url", "https://api.openai.com/v1")
        if not key: raise ValueError("OpenAI API key not configured.")
        return OpenAIAdapter(key, model, base)
    elif prov == "ollama":
        base  = llm.get("ollama", {}).get("base_url", "http://localhost:11434")
        model = llm.get("ollama", {}).get("model", "llama3.2")
        return OllamaAdapter(base, model)
    elif prov == "gemini":
        key   = llm.get("gemini", {}).get("api_key", "").strip()
        model = llm.get("gemini", {}).get("model", "gemini-2.0-flash")
        if not key: raise ValueError("Gemini API key not configured.")
        return GeminiAdapter(key, model)
    else:
        raise ValueError(f"Unknown LLM provider: {prov!r}")

def build_system_prompt() -> str:
    """Inject live fleet state into the system prompt so the LLM always has context."""
    lines = [
        "You are Fleet AI, an intelligent assistant embedded in a 3D printer monitoring dashboard.",
        f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Live Fleet Status",
    ]
    for p in config.get("printers", []):
        if not p.get("enabled", True):
            continue
        pid = p["id"]
        st  = printer_states.get(pid, {})
        lk  = st.get("lock", threading.Lock())
        with lk:
            s       = dict(st.get("last_status", {}))
            aalerts = list(st.get("active_alerts", []))
            lp      = st.get("last_poll")
        ps  = s.get("print_stats",    {})
        ext = s.get("extruder",       {})
        bed = s.get("heater_bed",     {})
        vsd = s.get("virtual_sdcard", {})
        state = ps.get("state", "unknown")
        prog  = vsd.get("progress", 0) * 100
        el    = ps.get("print_duration", 0)
        tot   = ps.get("total_duration", 0)
        eta   = max(tot - el, 0)
        def _t(sec):
            h = int(sec)//3600; m = (int(sec)%3600)//60
            return f"{h}h {m}m"
        lines.append(f"\n**{p.get('name', pid)}** ({p.get('host','')})")
        lines.append(f"  State: {state.upper()}  |  Progress: {prog:.1f}%")
        if ps.get("filename"):
            lines.append(f"  File: {ps['filename']}")
        lines.append(f"  Hotend: {ext.get('temperature',0):.1f}C / {ext.get('target',0)}C  "
                     f"|  Bed: {bed.get('temperature',0):.1f}C / {bed.get('target',0)}C")
        if state == "printing":
            lines.append(f"  Elapsed: {_t(el)}  |  ETA: {_t(eta)}")
        lines.append("  Active alerts: " + (
            " | ".join(a["msg"] for a in aalerts) if aalerts else "None"))
        if lp:
            lines.append(f"  Last polled: {lp}")

    with global_lock:
        recent = list(alert_log[:10])
    if recent:
        lines.append("\n## Recent Alert Log (last 10)")
        for e in recent:
            t = e.get("time","")[:16]
            lines.append(f"  [{t}] [{e.get('printer','')}] {e.get('level','').upper()}: {e.get('msg','')}")

    lines.append(f"\n## Monitor Config")
    lines.append(f"  Poll interval: {config.get('poll_interval_seconds',1800)//60} min")
    enabled_ch = [k for k in ("ntfy","twilio","email","imessage")
                  if config.get(k,{}).get("enabled")]
    lines.append(f"  Alert channels: {', '.join(enabled_ch) or 'none enabled'}")
    lines.append("")
    # Agent capabilities section
    tier = get_agent_tier()
    trust = is_trust_active()
    tier_names = {1: "Read-only (Tier 1)", 2: "Reversible actions (Tier 2)",
                  3: "Full access including irreversible (Tier 3)"}
    lines.append("## Agent Capabilities")
    lines.append(f"  Active tier: {tier} — {tier_names.get(tier,str(tier))}")
    lines.append(f"  Trust mode: {'ACTIVE — actions execute without confirmation' if trust else 'inactive — user confirms each action'}")
    active_tools = get_active_tools()
    lines.append(f"  Available tools: {chr(10).join('    ' + t['name'] + ' — ' + t['description'][:60] for t in active_tools)}")
    lines.append("")
    lines.append("Printer IDs for tool calls:")
    for p_ in config.get("printers", []):
        if p_.get("enabled", True):
            lines.append(f"  {p_['id']} = {p_.get('name', p_['id'])}")
    lines.append("")
    lines.append("Be concise and technically precise. Reference specific values when relevant.")
    lines.append("When taking actions, use tool calls rather than describing what you would do.")
    lines.append("For multi-printer operations use printer_id='all' where supported.")
    return "\n".join(lines)

def load_chat_history():
    global chat_history
    if not os.path.exists(CHAT_HISTORY_FILE):
        return
    try:
        with open(CHAT_HISTORY_FILE) as f:
            data = json.load(f)
        with chat_lock:
            chat_history = data.get("messages", [])
        print(f"  Loaded {len(chat_history)} chat history messages.")
    except Exception as e:
        print(f"  Warning: could not load chat history: {e}")

def save_chat_history():
    llm = config.get("llm", {})
    if not llm.get("history_enabled", True):
        return
    max_msgs = llm.get("history_max_messages", 100)
    with chat_lock:
        msgs = list(chat_history[-max_msgs:])
    try:
        with open(CHAT_HISTORY_FILE, "w") as f:
            json.dump({"messages": msgs}, f, indent=2)
    except Exception as e:
        print(f"  Warning: could not save chat history: {e}")

# ── Agent tool registry ───────────────────────────────────────────────────────
TOOL_REGISTRY = [
    # ── Tier 1: read-only ─────────────────────────────────────────────────────
    {"name": "get_printer_status", "tier": 1,
     "description": "Get current status of one or all printers (state, temps, progress, alerts).",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID, or omit for all"}},
         "required": []}},
    {"name": "list_files", "tier": 1,
     "description": "List gcode files on a printer, newest first.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"},
         "limit": {"type": "integer", "description": "Max files to return (default 20)"}},
         "required": ["printer_id"]}},
    {"name": "get_alert_log", "tier": 1,
     "description": "Get the recent monitor alert log.",
     "parameters": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "Number of recent alerts (default 20)"}},
         "required": []}},
    # ── Tier 2: reversible actions ────────────────────────────────────────────
    {"name": "pause_print", "tier": 2,
     "description": "Pause an active print job.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string",
             "description": "Printer ID, or 'all' to pause all printing printers"}},
         "required": ["printer_id"]}},
    {"name": "resume_print", "tier": 2,
     "description": "Resume a paused print job.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string",
             "description": "Printer ID, or 'all' to resume all paused printers"}},
         "required": ["printer_id"]}},
    {"name": "set_temperature", "tier": 2,
     "description": "Set hotend or bed temperature target (0 = off).",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"},
         "heater": {"type": "string", "enum": ["hotend", "bed"]},
         "temperature": {"type": "number", "description": "Target °C (0 to turn off)"}},
         "required": ["printer_id", "heater", "temperature"]}},
    {"name": "set_speed_factor", "tier": 2,
     "description": "Set print speed factor as percentage (100 = normal).",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"},
         "factor": {"type": "integer", "description": "Speed % (10-300)"}},
         "required": ["printer_id", "factor"]}},
    {"name": "set_flow_rate", "tier": 2,
     "description": "Set extrusion flow rate as percentage (100 = normal).",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"},
         "factor": {"type": "integer", "description": "Flow % (10-200)"}},
         "required": ["printer_id", "factor"]}},
    {"name": "trigger_poll", "tier": 2,
     "description": "Force immediate status poll for one or all printers.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID or 'all'"}},
         "required": []}},
    # ── Tier 3: irreversible / high-risk ──────────────────────────────────────
    {"name": "cancel_print", "tier": 3,
     "description": "Cancel current print job. IRREVERSIBLE.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID or 'all'"}},
         "required": ["printer_id"]}},
    {"name": "delete_file", "tier": 3,
     "description": "Delete a gcode file from printer storage. IRREVERSIBLE.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"},
         "filename": {"type": "string", "description": "Filename to delete"}},
         "required": ["printer_id", "filename"]}},
    {"name": "emergency_stop", "tier": 3,
     "description": "Emergency stop. Requires firmware restart to recover. IRREVERSIBLE.",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"}},
         "required": ["printer_id"]}},
    {"name": "home_axes", "tier": 3,
     "description": "Home one or more axes on a printer (moves print head).",
     "parameters": {"type": "object", "properties": {
         "printer_id": {"type": "string", "description": "Printer ID"},
         "axes": {"type": "string", "description": "Axes to home e.g. 'XYZ', 'Z'"}},
         "required": ["printer_id"]}},
]

# ── Agent tier / trust management ─────────────────────────────────────────────
def get_agent_tier() -> int:
    agent = config.get("agent", {})
    t3    = agent.get("tier3", {})
    if t3.get("enabled") and t3.get("expires_at"):
        try:
            if datetime.now() < datetime.fromisoformat(t3["expires_at"]):
                return 3
            _revert_tier3()
        except Exception:
            _revert_tier3()
    return agent.get("tier", 2)

def _revert_tier3():
    a = config.setdefault("agent", {})
    a.setdefault("tier3", {}).update({"enabled": False, "expires_at": None})
    save_config()
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Tier 3 expired — reverted to Tier {a.get('tier',2)}")

def is_trust_active() -> bool:
    t = config.get("agent", {}).get("trust_mode", {})
    if not t.get("enabled"): return False
    exp = t.get("expires_at")
    if not exp: return False
    try:
        if datetime.now() < datetime.fromisoformat(exp): return True
        _revert_trust(); return False
    except Exception: return False

def _revert_trust():
    config.setdefault("agent", {}).setdefault("trust_mode", {}).update(
        {"enabled": False, "expires_at": None})
    save_config()

def activate_trust(hours: int):
    hours = max(1, min(168, int(hours)))
    exp   = (datetime.now() + timedelta(hours=hours)).isoformat()
    config.setdefault("agent", {}).setdefault("trust_mode", {}).update(
        {"enabled": True, "expires_at": exp, "duration_hours": hours})
    save_config()
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Trust mode ON for {hours}h → {exp[:16]}")

def enable_tier3(hours: int):
    hours = max(1, min(168, int(hours)))
    exp   = (datetime.now() + timedelta(hours=hours)).isoformat()
    config.setdefault("agent", {}).setdefault("tier3", {}).update(
        {"enabled": True, "expires_at": exp})
    save_config()
    print(f"  [{datetime.now().strftime('%H:%M:%S')}] Tier 3 ENABLED for {hours}h → {exp[:16]}")

def get_active_tools() -> list:
    tier = get_agent_tier()
    return [t for t in TOOL_REGISTRY if t["tier"] <= tier]

def agent_status_dict() -> dict:
    """Serialisable agent status for /api/agent/status."""
    agent = config.get("agent", {})
    tier  = get_agent_tier()
    trust = agent.get("trust_mode", {})
    t3    = agent.get("tier3", {})
    return {
        "tier": tier,
        "base_tier": agent.get("tier", 2),
        "trust_active": is_trust_active(),
        "trust_expires_at": trust.get("expires_at"),
        "tier3_active": tier == 3,
        "tier3_expires_at": t3.get("expires_at"),
    }

# ── Tool execution ─────────────────────────────────────────────────────────────
def _mkr_post(printer, endpoint, data=None):
    host = printer["host"].rstrip("/")
    tok  = printer.get("api_token", "")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    if tok: hdrs["X-Api-Key"] = tok
    req  = urllib.request.Request(host + endpoint,
           data=json.dumps(data or {}).encode(), headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())

def _mkr_get(printer, endpoint):
    host = printer["host"].rstrip("/")
    tok  = printer.get("api_token", "")
    hdrs = {"Accept": "application/json"}
    if tok: hdrs["X-Api-Key"] = tok
    req  = urllib.request.Request(host + endpoint, headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())

def _mkr_delete(printer, endpoint):
    host = printer["host"].rstrip("/")
    tok  = printer.get("api_token", "")
    hdrs = {"Accept": "application/json"}
    if tok: hdrs["X-Api-Key"] = tok
    req  = urllib.request.Request(host + endpoint, headers=hdrs, method="DELETE")
    with urllib.request.urlopen(req, timeout=15) as r: return json.loads(r.read())

def _tool_no_printer(tool_name, args) -> str:
    if tool_name == "get_alert_log":
        limit = int(args.get("limit", 20))
        with global_lock: entries = list(alert_log[:limit])
        if not entries: return "No recent alerts."
        return "\n".join(
            f"[{e.get('time','')[:16]}] [{e.get('printer','')}] "
            f"{e.get('level','').upper()}: {e.get('msg','')}"
            for e in entries)
    elif tool_name == "trigger_poll":
        for pr in config.get("printers", []):
            if pr.get("enabled", True):
                threading.Thread(target=poll_once, args=(pr,), daemon=True).start()
        return "✓ Poll triggered for all printers."
    return f"Error: {tool_name} requires a printer_id."

def execute_tool(tool_name: str, args: dict, printer_id: str) -> str:
    pr = get_printer_by_id(printer_id)
    if not pr: return f"Error: printer '{printer_id}' not found."
    nm = pr.get("name", printer_id)
    try:
        if tool_name == "get_printer_status":
            st = printer_states.get(printer_id, {})
            with st.get("lock", threading.Lock()): s = dict(st.get("last_status", {}))
            ps  = s.get("print_stats", {}); ext = s.get("extruder", {})
            bed = s.get("heater_bed", {}); vsd = s.get("virtual_sdcard", {})
            return (f"{nm}: {ps.get('state','unknown').upper()} "
                    f"{vsd.get('progress',0)*100:.1f}% | "
                    f"Hotend {ext.get('temperature',0):.1f}/{ext.get('target',0)}°C | "
                    f"Bed {bed.get('temperature',0):.1f}/{bed.get('target',0)}°C | "
                    f"File: {ps.get('filename','none')}")
        elif tool_name == "list_files":
            d = _mkr_get(pr, "/server/files/list?root=gcodes")
            files = sorted(d.get("result", []),
                           key=lambda f: f.get("modified", 0), reverse=True)
            names = [f.get("path", f.get("filename","?"))
                     for f in files[:int(args.get("limit", 20))]]
            return f"{nm} ({len(names)} files): " + ", ".join(names)
        elif tool_name == "pause_print":
            _mkr_post(pr, "/printer/print/pause"); return f"✓ Paused {nm}."
        elif tool_name == "resume_print":
            _mkr_post(pr, "/printer/print/resume"); return f"✓ Resumed {nm}."
        elif tool_name == "set_temperature":
            heater = args.get("heater", "hotend"); temp = float(args.get("temperature", 0))
            gcode  = f"M104 S{temp:.0f}" if heater == "hotend" else f"M140 S{temp:.0f}"
            _mkr_post(pr, "/printer/gcode/script", {"script": gcode})
            return f"✓ Set {nm} {heater} → {temp:.0f}°C."
        elif tool_name == "set_speed_factor":
            f_ = max(10, min(300, int(args.get("factor", 100))))
            _mkr_post(pr, "/printer/gcode/script", {"script": f"M220 S{f_}"})
            return f"✓ Set {nm} speed → {f_}%."
        elif tool_name == "set_flow_rate":
            f_ = max(10, min(200, int(args.get("factor", 100))))
            _mkr_post(pr, "/printer/gcode/script", {"script": f"M221 S{f_}"})
            return f"✓ Set {nm} flow → {f_}%."
        elif tool_name == "trigger_poll":
            threading.Thread(target=poll_once, args=(pr,), daemon=True).start()
            return f"✓ Poll triggered for {nm}."
        elif tool_name == "cancel_print":
            _mkr_post(pr, "/printer/print/cancel"); return f"✓ Cancelled print on {nm}."
        elif tool_name == "delete_file":
            fname = args.get("filename", "")
            if not fname: return "Error: filename required."
            _mkr_delete(pr, f"/server/files/gcodes/{urllib.parse.quote(fname, safe='')}")
            return f"✓ Deleted '{fname}' from {nm}."
        elif tool_name == "emergency_stop":
            _mkr_post(pr, "/printer/emergency_stop")
            return f"✓ Emergency stop on {nm}. Firmware restart required."
        elif tool_name == "home_axes":
            axes = args.get("axes", "XYZ").upper()
            _mkr_post(pr, "/printer/gcode/script", {"script": f"G28 {axes}"})
            return f"✓ Homed {axes} on {nm}."
        return f"Error: unknown tool '{tool_name}'."
    except Exception as e:
        return f"Error: {tool_name} on {nm} failed — {e}"

def _action_description(tool: str, args: dict, printer_name: str) -> str:
    m = {
        "get_printer_status": f"Get status of {printer_name}",
        "list_files":         f"List files on {printer_name}",
        "get_alert_log":      "Get recent alert log",
        "pause_print":        f"Pause print on {printer_name}",
        "resume_print":       f"Resume print on {printer_name}",
        "set_temperature":    f"Set {args.get('heater','?')} → {args.get('temperature','?')}°C on {printer_name}",
        "set_speed_factor":   f"Set speed → {args.get('factor','?')}% on {printer_name}",
        "set_flow_rate":      f"Set flow → {args.get('factor','?')}% on {printer_name}",
        "trigger_poll":       f"Poll {printer_name} now",
        "cancel_print":       f"⚠️ CANCEL print on {printer_name}",
        "delete_file":        f"⚠️ DELETE '{args.get('filename','?')}' from {printer_name}",
        "emergency_stop":     f"🚨 EMERGENCY STOP {printer_name}",
        "home_axes":          f"Home axes {args.get('axes','XYZ')} on {printer_name}",
    }
    return m.get(tool, f"{tool} on {printer_name}")

def expand_tool_calls(raw_calls: list) -> list:
    """Expand 'all' printer_id targets into per-printer actions."""
    printers = [p for p in config.get("printers", []) if p.get("enabled", True)]
    expanded = []
    for call in raw_calls:
        name = call["name"]; args = call.get("args", {})
        pid  = args.get("printer_id", "")
        # Tools that operate on the monitor (no printer)
        if name in ("get_alert_log",):
            expanded.append({"tool": name, "args": args, "printer_id": None,
                "printer_name": "Monitor", "call_id": call["id"],
                "description": _action_description(name, args, "Monitor")})
            continue
        if name == "trigger_poll" and not pid:
            expanded.append({"tool": name, "args": args, "printer_id": None,
                "printer_name": "All printers", "call_id": call["id"],
                "description": "Poll all printers now"})
            continue
        # Resolve target printers
        if pid == "all" or not pid:
            if name == "pause_print":
                targets = [p for p in printers if printer_states.get(
                    p["id"],{}).get("last_status",{}).get("print_stats",{}).get("state") == "printing"]
            elif name == "resume_print":
                targets = [p for p in printers if printer_states.get(
                    p["id"],{}).get("last_status",{}).get("print_stats",{}).get("state") == "paused"]
            else:
                targets = printers
        else:
            pr = get_printer_by_id(pid)
            targets = [pr] if pr else []
        for pr in targets:
            a = dict(args); a["printer_id"] = pr["id"]
            pname = pr.get("name", pr["id"])
            expanded.append({"tool": name, "args": a, "printer_id": pr["id"],
                "printer_name": pname, "call_id": call["id"],
                "description": _action_description(name, a, pname)})
    return expanded

def _record_chat(user_msg: str, reply: str):
    llm_cfg = config.get("llm", {})
    now = datetime.now().isoformat()
    with chat_lock:
        chat_history.append({"role": "user",      "content": user_msg,  "time": now})
        chat_history.append({"role": "assistant",  "content": reply,
                              "time": datetime.now().isoformat()})
        mx = llm_cfg.get("history_max_messages", 100)
        while len(chat_history) > mx: chat_history.pop(0)
    save_chat_history()

def _execute_pending(pending: dict):
    """Execute all actions in a pending dict; return (final_reply, exec_log)."""
    adapter  = get_llm_adapter()
    expanded = pending["expanded"]
    exec_log = []
    by_call  = {}
    for action in expanded:
        if action["printer_id"]:
            r = execute_tool(action["tool"], action["args"], action["printer_id"])
        else:
            r = _tool_no_printer(action["tool"], action["args"])
        exec_log.append({"description": action["description"], "result": r})
        print(f"  [Agent] {action['description']} → {r[:80]}")
        cid = action["call_id"]
        by_call.setdefault(cid, []).append(r)
    # build tc_results aligned with original tool_calls
    tc_results = [(tc, "\n".join(by_call.get(tc["id"], ["no result"])))
                  for tc in pending["tool_calls"]]
    final = adapter.chat_with_results(
        pending["msg_payload"], pending["asst_raw"], tc_results, pending["system"])
    return final, exec_log

def process_chat_agentic(user_message: str):
    """Agentic chat entry point. Returns a result dict the HTTP handler serialises."""
    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("enabled", False):
        raise ValueError("LLM chat is not enabled. Open \u2699 settings to configure.")
    adapter = get_llm_adapter()
    tools   = get_active_tools()
    system  = build_system_prompt()
    with chat_lock:
        msg_payload = [{"role": m["role"], "content": m["content"]} for m in chat_history]
    msg_payload.append({"role": "user", "content": user_message})
    # First LLM call
    text, tool_calls, asst_raw = adapter.chat(msg_payload, system, tools)
    if not tool_calls:
        # Pure text response
        _record_chat(user_message, text or "")
        with chat_lock: hist = list(chat_history)
        return {"type": "reply", "reply": text or "", "history": hist}
    # Has tool calls
    expanded = expand_tool_calls(tool_calls)
    is_multi = len(expanded) > 1
    if is_trust_active():
        final, exec_log = _execute_pending({
            "expanded": expanded, "tool_calls": tool_calls, "asst_raw": asst_raw,
            "msg_payload": msg_payload, "system": system})
        _record_chat(user_message, final)
        with chat_lock: hist = list(chat_history)
        return {"type": "reply", "reply": final, "history": hist, "exec_log": exec_log}
    # Queue for confirmation
    aid = uuid.uuid4().hex[:12]
    with pending_lock:
        pending_actions[aid] = {
            "user_message": user_message, "msg_payload": msg_payload,
            "asst_raw": asst_raw, "tool_calls": tool_calls, "expanded": expanded,
            "system": system, "is_multi": is_multi,
            "created_at": datetime.now().isoformat()}
    return {
        "type": "pending_action",
        "action_id": aid,
        "is_multi": is_multi,
        "actions": [{"tool": a["tool"], "printer_id": a["printer_id"],
                     "printer_name": a["printer_name"], "description": a["description"]}
                    for a in expanded]}

def confirm_pending(action_id: str, confirmed: bool, trust_hours: int = 0):
    """Confirm or deny a pending action. Returns result dict."""
    with pending_lock:
        pending = pending_actions.pop(action_id, None)
    if not pending:
        return {"type": "reply", "reply": "Action expired or not found.", "history": []}
    if not confirmed:
        msg = "Action cancelled."
        _record_chat(pending["user_message"], msg)
        with chat_lock: hist = list(chat_history)
        return {"type": "reply", "reply": msg, "history": hist}
    if trust_hours and trust_hours > 0:
        activate_trust(trust_hours)
    final, exec_log = _execute_pending(pending)
    _record_chat(pending["user_message"], final)
    with chat_lock: hist = list(chat_history)
    return {"type": "reply", "reply": final, "history": hist, "exec_log": exec_log}

def _make_printer_state():
    return {"last_status": {}, "active_alerts": [], "fired_alerts": set(),
            "last_poll": None, "errors": 0, "lock": threading.Lock(),
            "paused_since": None,   # datetime when pause was first detected
            "prev_print_state": None,  # last observed print_stats.state
           }

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

def _extract_pause_reason(s: dict) -> str:
    """Return the most likely human-readable cause of the current pause.
    Looks at print_stats.message, display_status.message, and the recent
    gcode_store for M118 notifications fired just before the pause."""

    # 1. print_stats.message — Klipper sets this on macro-initiated pauses
    ps_msg = s.get("print_stats", {}).get("message", "").strip()
    if ps_msg:
        return ps_msg

    # 2. display_status.message — set by M117; skip generic "Last File:" entries
    ds_msg = s.get("display_status", {}).get("message", "").strip()
    if ds_msg and not ds_msg.lower().startswith("last file"):
        return ds_msg

    # 3. Scan gcode_store for recent //notification messages (M118 output).
    #    Take the most recent non-noise notification from the last 90 seconds.
    store = s.get("_gcode_store", [])
    if not store:
        return ""

    now = time.time()
    candidates = []
    for entry in reversed(store):           # newest first
        if now - entry.get("time", 0) > 90: # only look 90 s back
            break
        msg = entry.get("message", "")
        if entry.get("type") != "response":
            continue
        # M118 output arrives with "// " prefix
        if not msg.startswith("//"):
            continue
        clean = msg.lstrip("/ ").strip()
        if not clean or _is_noise(clean):
            continue
        candidates.append(clean)

    return candidates[0] if candidates else ""

# Human-readable hint appended to the pause alert based on the trigger text
_PAUSE_HINTS = [
    (("filament motion sensor", "entangl", "tangle"),
     " — Filament motion/entanglement trigger. Resume printing if transient."),
    (("runout", "ran out", "no filament"),
     " — Filament runout detected. Load new filament before resuming."),
    (("m600", "filament change"),
     " — Filament change requested (M600). Swap filament and resume."),
    (("power loss", "interruption", "was_interrupted"),
     " — Possible power-loss recovery. Check print progress before resuming."),
    (("thermal", "temperature", "temp"),
     " — Thermal event. Check temperatures before resuming."),
    (("clog", "under-extrusion", "underextrusion"),
     " — Possible clog or under-extrusion. Check extruder before resuming."),
]

def _classify_pause_hint(reason: str) -> str:
    lo = reason.lower()
    for keywords, hint in _PAUSE_HINTS:
        if any(k in lo for k in keywords):
            return hint
    return ""

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
    if state == "paused":
        reason = _extract_pause_reason(s)
        fname  = ps.get("filename","").lstrip(".cache/")
        base   = f"Print paused at {prog*100:.1f}% — {fname}"
        if reason:
            base += f"\n  Trigger: {reason}"
            base += _classify_pause_hint(reason)
        alerts.append(("warning", base))
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

# Notification messages we don't want to surface as pause reasons
_NOISE_PATTERNS = (
    "filament width sensor", "filament dia (measured", "filament width measurements",
    "pressure_advance", "b:", "t0:", "t1:", "stats ", "mcu_awake",
)

def _is_noise(msg: str) -> bool:
    lo = msg.lower().lstrip("/ ")
    return any(lo.startswith(p) for p in _NOISE_PATTERNS)

def fetch_printer_status(printer):
    host  = printer["host"].rstrip("/")
    token = printer.get("api_token","")
    hdrs  = {"Accept": "application/json"}
    if token: hdrs["X-Api-Key"] = token

    url = (host + "/printer/objects/query"
           "?print_stats&extruder&heater_bed&toolhead&virtual_sdcard&webhooks&display_status")
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = json.loads(resp.read()).get("result",{}).get("status",{})

    # Also fetch recent gcode notifications (M118 messages, macro output, etc.)
    # These carry pause reasons, filament sensor triggers, etc.
    try:
        gs_url = host + "/server/gcode_store?count=40"
        gs_req = urllib.request.Request(gs_url, headers=hdrs)
        with urllib.request.urlopen(gs_req, timeout=10) as resp:
            gs = json.loads(resp.read()).get("result",{}).get("gcode_store",[])
        status["_gcode_store"] = gs
    except Exception:
        status["_gcode_store"] = []

    return status

# How many minutes of continuous pause before escalating with a fresh alert.
# Set to 0 to disable escalation (only fires on first pause detection).
def process_printer_status(printer, s):
    pid   = printer["id"]
    pname = printer.get("name", pid)
    pst   = printer_states[pid]
    now   = datetime.now()

    with pst["lock"]:
        pst["last_status"] = s
        pst["last_poll"]   = now.isoformat()

    anomalies   = detect_anomalies(s)
    print_state = s.get("print_stats", {}).get("state", "")
    prog        = s.get("virtual_sdcard", {}).get("progress", 0)
    filename    = s.get("print_stats", {}).get("filename", "")

    # ── State-transition bookkeeping ──────────────────────────────────────────
    with pst["lock"]:
        prev_state = pst.get("prev_print_state")
        pst["prev_print_state"] = print_state

    # Reset dedup when job ends (not printing or paused)
    if print_state not in ("printing", "paused"):
        with pst["lock"]:
            pst["fired_alerts"].clear()
            pst["paused_since"] = None

    # ── Pause tracking + escalation ───────────────────────────────────────────
    if print_state == "paused":
        with pst["lock"]:
            if pst["paused_since"] is None:
                pst["paused_since"] = now   # record when pause started
            paused_since = pst["paused_since"]

        paused_minutes = (now - paused_since).total_seconds() / 60

        _esc_mins = config.get("pause_escalate_minutes", 30)
        if _esc_mins > 0 and paused_minutes >= _esc_mins:
            # Fire one escalation alert per window; bucket = which window we are in
            bucket = int(paused_minutes // _esc_mins)
            esc_key = f"warning:pause_escalation:{bucket}"
            with pst["lock"]:
                already_esc = esc_key in pst["fired_alerts"]
            if not already_esc:
                with pst["lock"]:
                    pst["fired_alerts"].add(esc_key)
                hrs  = int(paused_minutes) // 60
                mins = int(paused_minutes) % 60
                duration_str = (f"{hrs}h {mins}m" if hrs else f"{mins}m")
                dispatch_alert(
                    "warning",
                    f"Still paused after {duration_str} at {prog*100:.1f}% — {filename}",
                    pname)
    else:
        with pst["lock"]:
            pst["paused_since"] = None   # clear if resumed

    # ── Standard anomaly dispatch ─────────────────────────────────────────────
    for level, msg in anomalies:
        key = f"{level}:{msg}"
        should_fire = (level in ("critical", "success") or
                       (level == "warning" and config.get("alert_on_warnings", True)))
        with pst["lock"]:
            already = key in pst["fired_alerts"]
        if should_fire and not already:
            with pst["lock"]:
                pst["fired_alerts"].add(key)
            dispatch_alert(level, msg, pname)

    with pst["lock"]:
        pst["active_alerts"] = [{"level": l, "msg": m} for l, m in anomalies]

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
.container{max-width:620px;margin:0 auto;display:flex;flex-direction:column;gap:14px;transition:max-width .3s}
body.fleet-mode .container{max-width:1120px}
.header{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #1e293b;padding-bottom:12px}
.header-title{font-size:15px;font-weight:700;color:#f8fafc}
.header-sub{font-size:11px;color:#475569;margin-top:2px}
.header-right{display:flex;flex-direction:column;align-items:flex-end;gap:8px}
.header-meta{text-align:right;font-size:11px;color:#475569;line-height:1.8}
.header-meta span{color:#94a3b8}
.view-toggle{display:flex;background:#0f172a;border:1px solid #1e293b;border-radius:20px;padding:3px}
.vtab{background:none;border:none;color:#475569;font-family:inherit;font-size:11px;padding:4px 14px;border-radius:16px;cursor:pointer;transition:all .15s;white-space:nowrap}
.vtab.active{background:#1e293b;color:#f8fafc}
.printer-tabs-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.printer-tabs{display:flex;gap:6px;flex-wrap:wrap;flex:1}
.printer-tab{background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:12px;padding:6px 14px;border-radius:20px;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .15s}
.printer-tab:hover{border-color:#475569;color:#cbd5e1}
.printer-tab.active{background:#0f2a3f;border-color:#3b82f6;color:#60a5fa}
.tab-dot,.fc-dot{border-radius:50%;display:inline-block;flex-shrink:0}
.tab-dot{width:6px;height:6px}.fc-dot{width:7px;height:7px}
.dot-printing{background:#22c55e;box-shadow:0 0 6px #22c55e80;animation:glow 2s infinite}
.dot-paused{background:#f59e0b}
.dot-error{background:#ef4444;box-shadow:0 0 6px #ef444480}
.dot-complete,.dot-cancelled,.dot-unknown,.dot-standby{background:#334155}
@keyframes glow{0%,100%{opacity:1}50%{opacity:.5}}
.btn-manage{background:none;border:1px solid #334155;color:#475569;font-family:inherit;font-size:11px;padding:5px 10px;border-radius:16px;cursor:pointer;transition:all .15s;white-space:nowrap}
.btn-manage:hover{border-color:#3b82f6;color:#60a5fa}
.alert{border-radius:6px;padding:10px 14px;font-size:12px;font-weight:600;border-left:3px solid;margin-bottom:6px}
.alert-critical{background:#1a0a0a;border-color:#ef4444;color:#fca5a5}
.alert-warning{background:#1a150a;border-color:#f59e0b;color:#fcd34d}
.alert-success{background:#0a1a0f;border-color:#22c55e;color:#86efac}
.alert-ok{background:#0a1a0f;border-color:#22c55e;color:#86efac}
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
.camera-title{font-size:11px;color:#94a3b8}.camera-hint{font-size:10px;color:#334155}
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
.panel{background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:16px;display:flex;flex-direction:column;gap:12px}
.panel-title{font-size:12px;color:#94a3b8;font-weight:700;border-bottom:1px solid #1e293b;padding-bottom:8px;margin-bottom:4px}
.printer-row{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid #0f1a2e}
.printer-row:last-child{border-bottom:none}
.printer-row-info{flex:1;min-width:0}
.printer-row-name{font-size:12px;color:#f1f5f9;font-weight:600}
.printer-row-host{font-size:10px;color:#475569;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.printer-row-actions{display:flex;gap:5px;flex-shrink:0}
.btn-sm{background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:10px;padding:4px 8px;border-radius:4px;cursor:pointer;transition:background .15s}
.btn-sm:hover{background:#263347;color:#e2e8f0}
.btn-sm-success{border-color:#166534;color:#4ade80}.btn-sm-success:hover{background:#0a1a0f}
.toggle{position:relative;width:34px;height:18px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;inset:0;background:#334155;border-radius:9px;cursor:pointer;transition:.2s}
.toggle-slider::before{content:'';position:absolute;width:12px;height:12px;left:3px;top:3px;background:#94a3b8;border-radius:50%;transition:.2s}
.toggle input:checked+.toggle-slider{background:#16a34a}
.toggle input:checked+.toggle-slider::before{background:#fff;transform:translateX(16px)}
.edit-row{display:flex;flex-direction:column;gap:10px;padding:10px;background:#080c14;border-radius:6px;border:1px solid #1e293b}
.edit-label{font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px}
.edit-input{width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;font-family:inherit;font-size:12px;padding:6px 8px;border-radius:4px;outline:none}
.edit-input:focus{border-color:#3b82f6}
.edit-actions{display:flex;gap:6px;margin-top:4px}
.add-form{display:flex;flex-direction:column;gap:8px}
.add-form-row{display:flex;gap:8px}
.add-input{flex:1;background:#0f172a;border:1px solid #334155;color:#e2e8f0;font-family:inherit;font-size:12px;padding:6px 8px;border-radius:4px;outline:none}
.add-input:focus{border-color:#3b82f6}
.add-input::placeholder{color:#334155}
/* ── Fleet grid ── */
.fleet-bar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.fleet-summary{font-size:11px;color:#475569}
.fleet-summary b{color:#f1f5f9}
.fleet-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.fc{background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:10px;cursor:pointer;transition:border-color .2s,box-shadow .2s;position:relative;overflow:hidden}
.fc:hover{border-color:#334155;box-shadow:0 0 0 1px #1e293b}
.fc:hover .fc-hint{opacity:1}
.fc-hint{position:absolute;bottom:8px;right:10px;font-size:9px;color:#475569;opacity:0;transition:opacity .2s}
.fc.fc-crit{border-color:#7f1d1d;background:#0d0505;animation:fc-pulse 2.5s infinite}
.fc.fc-warn{border-color:#92400e}
@keyframes fc-pulse{0%,100%{border-color:#7f1d1d}50%{border-color:#dc2626;box-shadow:0 0 14px #dc262640}}
.fc-top{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.fc-name{font-size:13px;font-weight:700;color:#f8fafc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-host{font-size:10px;color:#334155;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;color:#fff;flex-shrink:0}
.fc-badge-printing{background:#16a34a}.fc-badge-paused{background:#d97706}
.fc-badge-error{background:#dc2626}.fc-badge-complete{background:#2563eb}
.fc-badge-cancelled,.fc-badge-standby,.fc-badge-unknown{background:#334155}
.fc-prog{display:flex;flex-direction:column;gap:5px}
.fc-prog-row{display:flex;justify-content:space-between;font-size:11px}
.fc-pct{color:#4ade80;font-weight:700}.fc-eta{color:#475569}
.fc-bar-bg{background:#1e293b;border-radius:99px;height:5px;overflow:hidden}
.fc-bar{height:100%;border-radius:99px;background:#22c55e;transition:width .6s}
.fc-file{font-size:10px;color:#334155;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-temps{display:flex;gap:16px}
.fc-tmp{display:flex;flex-direction:column;gap:1px}
.fc-tmp-lbl{font-size:9px;color:#334155;text-transform:uppercase;letter-spacing:.06em}
.fc-tmp-val{font-size:12px;font-weight:700}
.tc-ok{color:#4ade80}.tc-bad{color:#f87171}.tc-off{color:#334155}
.fc-chips{display:flex;flex-direction:column;gap:3px}
.fc-chip{font-size:10px;padding:3px 7px;border-radius:4px;border-left:2px solid;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fc-chip-ok{background:#0a140a;border-color:#22c55e;color:#4ade80}
.fc-chip-crit{background:#1a0808;border-color:#ef4444;color:#fca5a5}
.fc-chip-warn{background:#1a120a;border-color:#f59e0b;color:#fcd34d}
.fc-cam{border-radius:6px;overflow:hidden;background:#080c14;border:1px solid #1e293b;position:relative;min-height:38px}
.fc-cam img{width:100%;display:block;max-height:170px;object-fit:cover}
.fc-cam-err{font-size:10px;color:#334155;padding:10px;text-align:center}
.fc-cam-badge{position:absolute;top:5px;left:6px;background:rgba(0,0,0,.75);color:#60a5fa;font-size:9px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.05em}
.section-label{font-size:10px;color:#334155;text-transform:uppercase;letter-spacing:.08em}
.footer{text-align:center;font-size:10px;color:#1e293b;padding-top:4px}
/* ── Chat bubble & panel ── */
.chat-bubble{position:fixed;bottom:22px;right:22px;z-index:9000}
.chat-fab{width:52px;height:52px;border-radius:50%;background:#1d4ed8;border:2px solid #2563eb;color:#fff;font-size:22px;cursor:pointer;box-shadow:0 4px 18px rgba(0,0,0,.6);transition:transform .15s,background .15s;display:flex;align-items:center;justify-content:center}
.chat-fab:hover{transform:scale(1.08);background:#2563eb}
.chat-fab-dot{position:absolute;top:2px;right:2px;width:11px;height:11px;border-radius:50%;background:#ef4444;border:2px solid #0a0a0f;display:none;animation:glow 2s infinite}
.chat-panel{position:fixed;bottom:86px;right:22px;width:370px;height:530px;background:#0f172a;border:1px solid #1e293b;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.8);display:flex;flex-direction:column;overflow:hidden;z-index:8999;animation:chat-in .18s ease}
@media(max-width:440px){.chat-panel{width:calc(100vw - 16px);right:8px}}
@keyframes chat-in{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
.chat-header{background:#080c14;border-bottom:1px solid #1e293b;padding:11px 14px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.chat-header-title{font-size:13px;font-weight:700;color:#f8fafc;display:flex;align-items:center;gap:7px}
.chat-header-actions{display:flex;gap:2px}
.chat-icon-btn{background:none;border:none;color:#475569;cursor:pointer;font-size:15px;padding:4px 6px;border-radius:4px;transition:color .15s,background .15s}
.chat-icon-btn:hover{color:#94a3b8;background:#1e293b}
.chat-messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:9px;scroll-behavior:smooth}
.chat-messages::-webkit-scrollbar{width:3px}
.chat-messages::-webkit-scrollbar-thumb{background:#1e293b;border-radius:2px}
/* Agent confirmation card */
.action-card{background:#0f2137;border:1px solid #1e4a7a;border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:10px;font-size:12px}
.action-card.tier3{border-color:#7f1d1d;background:#1a0a0a}
.action-card-title{font-size:12px;font-weight:700;color:#93c5fd}
.action-card.tier3 .action-card-title{color:#fca5a5}
.action-list{display:flex;flex-direction:column;gap:4px}
.action-item{font-size:11px;color:#cbd5e1;padding:3px 6px;background:#0a1929;border-radius:4px}
.action-card.tier3 .action-item{color:#fecaca}
.action-btns{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.btn-confirm{background:#1d4ed8;color:#fff;border:none;border-radius:6px;padding:5px 13px;font-size:11px;font-weight:700;cursor:pointer}
.btn-confirm:hover{background:#2563eb}
.btn-deny{background:transparent;color:#94a3b8;border:1px solid #334155;border-radius:6px;padding:5px 11px;font-size:11px;cursor:pointer}
.btn-deny:hover{border-color:#ef4444;color:#ef4444}
.btn-abort{background:#7f1d1d;color:#fecaca;border:none;border-radius:6px;padding:5px 13px;font-size:11px;font-weight:700;cursor:pointer;animation:pulse-red 1s infinite}
.btn-abort:hover{background:#991b1b}
@keyframes pulse-red{0%,100%{opacity:1}50%{opacity:.75}}
.countdown-bar{height:3px;background:#1e293b;border-radius:2px;overflow:hidden}
.countdown-bar-fill{height:100%;background:#3b82f6;border-radius:2px;transition:width .25s linear}
.countdown-txt{font-size:10px;color:#64748b;text-align:center}
.trust-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.trust-row label{font-size:10px;color:#64748b;cursor:pointer;user-select:none}
.trust-row select{font-size:10px;background:#0f172a;color:#94a3b8;border:1px solid #1e293b;border-radius:4px;padding:2px 4px}
/* Agent/trust badge in header */
.agent-badge{font-size:9px;padding:2px 6px;border-radius:8px;font-weight:700;margin-left:4px}
.agent-badge-trust{background:#14532d;color:#4ade80;border:1px solid #166534}
.agent-badge-t3{background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b}
.agent-badge-tier{background:#1e3a5f;color:#93c5fd;border:1px solid #1d4ed8}
/* Tier 3 warning card in settings */
.cs-t3-warn{background:#1a0a0a;border:1px solid #7f1d1d;border-radius:8px;padding:10px 12px;font-size:10px;color:#fca5a5;line-height:1.5}
.cs-t3-active{background:#14000a;border:1px solid #be123c;border-radius:8px;padding:10px 12px}
.cs-t3-active-title{font-size:11px;font-weight:700;color:#fca5a5;margin-bottom:4px}
.cs-t3-countdown{font-size:10px;color:#fda4af}
.btn-revoke{background:#7f1d1d;color:#fecaca;border:none;border-radius:5px;padding:4px 10px;font-size:10px;cursor:pointer;margin-top:6px}
.btn-revoke:hover{background:#991b1b}
.msg{max-width:90%;display:flex;flex-direction:column;gap:2px}
.msg-user{align-self:flex-end;align-items:flex-end}
.msg-ai{align-self:flex-start;align-items:flex-start}
.msg-bubble{padding:8px 12px;border-radius:10px;font-size:12px;line-height:1.55;word-break:break-word;white-space:pre-wrap}
.msg-user .msg-bubble{background:#1d4ed8;color:#dbeafe;border-bottom-right-radius:3px}
.msg-ai .msg-bubble{background:#1e293b;color:#e2e8f0;border-bottom-left-radius:3px}
.msg-time{font-size:9px;color:#334155}
.chat-typing{display:flex;align-items:center;gap:5px;padding:9px 12px;background:#1e293b;border-radius:10px;border-bottom-left-radius:3px;align-self:flex-start}
.chat-typing span{width:5px;height:5px;border-radius:50%;background:#475569;animation:typing-dot 1.2s infinite}
.chat-typing span:nth-child(2){animation-delay:.2s}
.chat-typing span:nth-child(3){animation-delay:.4s}
@keyframes typing-dot{0%,60%,100%{transform:translateY(0);opacity:.4}30%{transform:translateY(-5px);opacity:1}}
.chat-input-row{padding:10px 12px;border-top:1px solid #1e293b;display:flex;gap:8px;flex-shrink:0;background:#080c14;align-items:flex-end}
.chat-input{flex:1;background:#1e293b;border:1px solid #334155;color:#e2e8f0;font-family:inherit;font-size:12px;padding:8px 10px;border-radius:6px;outline:none;resize:none;max-height:80px;line-height:1.45;overflow-y:auto}
.chat-input:focus{border-color:#3b82f6}
.chat-input::placeholder{color:#334155}
.chat-send{background:#1d4ed8;border:none;color:#fff;font-family:inherit;font-size:12px;padding:8px 13px;border-radius:6px;cursor:pointer;transition:background .15s;flex-shrink:0;margin-bottom:1px}
.chat-send:hover:not(:disabled){background:#2563eb}
.chat-send:disabled{opacity:.35;cursor:default}
.chat-empty{color:#334155;font-size:11px;text-align:center;padding:28px 10px;line-height:2.2}
/* ── Chat settings drawer ── */
.chat-settings{position:absolute;inset:0;background:#0f172a;z-index:10;display:flex;flex-direction:column;animation:chat-in .15s ease}
.cs-header{background:#080c14;border-bottom:1px solid #1e293b;padding:11px 14px;display:flex;align-items:center;gap:8px;flex-shrink:0}
.cs-title{font-size:13px;font-weight:700;color:#f8fafc;flex:1}
.cs-body{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:14px}
.cs-group{display:flex;flex-direction:column;gap:7px}
.cs-lbl{font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.07em}
.cs-select,.cs-input{width:100%;background:#080c14;border:1px solid #334155;color:#e2e8f0;font-family:inherit;font-size:12px;padding:7px 9px;border-radius:5px;outline:none}
.cs-select:focus,.cs-input:focus{border-color:#3b82f6}
.cs-select option{background:#080c14}
.cs-row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.cs-row-lbl{font-size:12px;color:#94a3b8}
.cs-footer{padding:11px 14px;border-top:1px solid #1e293b;display:flex;gap:8px;flex-shrink:0;background:#080c14}
.cs-btn{flex:1;background:#1e293b;border:1px solid #334155;color:#94a3b8;font-family:inherit;font-size:12px;padding:8px;border-radius:5px;cursor:pointer;transition:all .15s}
.cs-btn:hover{background:#263347;color:#e2e8f0}
.cs-btn-primary{background:#1d4ed8;border-color:#2563eb;color:#fff}
.cs-btn-primary:hover{background:#2563eb}
.cs-btn-danger{border-color:#7f1d1d;color:#ef4444}
.cs-btn-danger:hover{background:#1a0808}
.cs-status{font-size:10px;min-height:13px;text-align:center;padding:2px 0}
.cs-provider-badge{display:inline-block;padding:1px 7px;border-radius:8px;font-size:9px;font-weight:700}
.cs-badge-on{background:#0f2a1a;border:1px solid #22c55e;color:#4ade80}
.cs-badge-off{background:#1a1a2e;border:1px solid #334155;color:#475569}
</style>
</head>
<body><div class="container">

<div class="header">
  <div>
    <div class="header-title">&#128424;&#65039; Printer Fleet Monitor</div>
    <div class="header-sub" id="headerSub">connecting...</div>
  </div>
  <div class="header-right">
    <div id="viewToggleWrap" style="display:none">
      <div class="view-toggle">
        <button class="vtab" id="vtabFleet" onclick="setView('fleet')">&#9732; Fleet</button>
        <button class="vtab active" id="vtabDetail" onclick="setView('detail')">&#128202; Detail</button>
      </div>
    </div>
    <div class="header-meta">
      <span id="checkCount">0</span> polls &middot; <span id="lastCheck">&#8212;</span><br>
      Next: <span id="countdown">&#8212;</span>
    </div>
    <span class="spinner" id="spinner" style="display:none">syncing...</span>
  </div>
</div>

<div id="detailNav">
  <div class="section-label" style="margin-bottom:6px">Printers</div>
  <div class="printer-tabs-row">
    <div class="printer-tabs" id="printerTabs"></div>
    <button class="btn-manage" onclick="toggleManagePanel()">&#9881; Manage</button>
  </div>
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

<div id="fleetView" style="display:none">
  <div class="fleet-bar">
    <div class="fleet-summary" id="fleetSummary">Loading&hellip;</div>
    <button class="btn-manage" id="fleetCamBtn" onclick="toggleFleetCameras()">&#128247; Show Cameras</button>
  </div>
  <div class="fleet-grid" id="fleetGrid"></div>
</div>

<div id="detailView">
  <div id="alertsContainer"></div>
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="badge badge-unknown" id="stateBadge">CONNECTING</span>
        <span id="printerName" style="font-size:11px;color:#475569"></span>
      </div>
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
      <img id="cameraImg" alt="snapshot"
           onerror="this.style.display='none';document.getElementById('cameraErr').style.display='block'"/>
      <div id="cameraErr">No camera available or snapshot URL unreachable</div>
    </div>
  </div>
  <div class="btn-row">
    <button class="btn" onclick="triggerPoll()">&#128260; Poll Now</button>
    <button class="btn" id="cameraBtn" onclick="toggleCamera()">&#128247; Camera</button>
    <button class="btn" onclick="sendTest()">&#128276; Test</button>
    <button class="btn" onclick="openConfig()">&#128196; Config</button>
  </div>
</div>

<div id="managePanel" style="display:none">
  <div class="panel">
    <div class="panel-title">&#9881; Manage Printers</div>
    <div id="printerRowList"></div>
    <div id="editFormWrap"></div>
    <div>
      <div class="section-label" style="margin-bottom:8px">+ Add Printer</div>
      <div class="add-form">
        <div class="add-form-row">
          <input class="add-input" id="addName" placeholder="Name (e.g. Voron 2.4)"/>
          <input class="add-input" id="addHost" placeholder="http://192.168.1.101"/>
        </div>
        <div class="add-form-row">
          <input class="add-input" id="addToken" placeholder="API token (optional)" style="flex:.5"/>
          <button class="btn" style="flex:.5;min-width:0" onclick="addPrinter()">&#43; Add</button>
        </div>
        <div id="addMsg" style="font-size:11px;color:#475569;min-height:14px"></div>
      </div>
    </div>
  </div>
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
<!-- ═══ CHAT BUBBLE (fixed, outside container) ═══ -->
<div class="chat-bubble">
  <button class="chat-fab" id="chatFab" onclick="toggleChat()" title="Fleet AI Assistant">
    <span id="chatFabIcon">&#128172;</span>
    <span class="chat-fab-dot" id="chatFabDot"></span>
  </button>
  <div class="chat-panel" id="chatPanel" style="display:none">
    <!-- Header -->
    <div class="chat-header">
      <div class="chat-header-title">
        &#129302; Fleet AI
        <span class="cs-provider-badge cs-badge-off" id="chatProvBadge">off</span>
        <span class="agent-badge agent-badge-tier" id="agentTierBadge" style="display:none"></span>
        <span class="agent-badge agent-badge-trust" id="agentTrustBadge" style="display:none">TRUST ON</span>
        <span class="agent-badge agent-badge-t3" id="agentT3Badge" style="display:none">T3 ACTIVE</span>
      </div>
      <div class="chat-header-actions">
        <button class="chat-icon-btn" onclick="openChatSettings()" title="LLM Settings">&#9881;</button>
        <button class="chat-icon-btn" onclick="toggleChat()" title="Close">&#10005;</button>
      </div>
    </div>
    <!-- Messages -->
    <div class="chat-messages" id="chatMessages">
      <div class="chat-empty" id="chatEmpty">
        Ask me about your fleet,<br>alerts, or 3D printing tips.<br>
        <span style="color:#1e293b">Open &#9881; to configure your LLM.</span>
      </div>
    </div>
    <!-- Input -->
    <div class="chat-input-row">
      <textarea class="chat-input" id="chatInput" rows="1"
        placeholder="Ask about your fleet&#8230;"
        onkeydown="chatKeydown(event)" oninput="chatAutoResize(this)"></textarea>
      <button class="chat-send" id="chatSend" onclick="sendChat()" disabled>Send</button>
    </div>
    <!-- Settings drawer (overlaid) -->
    <div class="chat-settings" id="chatSettings" style="display:none">
      <div class="cs-header">
        <button class="chat-icon-btn" onclick="closeChatSettings()">&#8592;</button>
        <span class="cs-title">&#9881; LLM Settings</span>
      </div>
      <div class="cs-body">
        <div class="cs-group">
          <div class="cs-lbl">Chat Assistant</div>
          <div class="cs-row">
            <span class="cs-row-lbl">Enable Fleet AI</span>
            <label class="toggle" style="margin:0">
              <input type="checkbox" id="csEnabled">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">Provider</div>
          <select class="cs-select" id="csProvider" onchange="csProviderChange()">
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="openai">OpenAI-compatible (Groq, Mistral, LM Studio&hellip;)</option>
            <option value="ollama">Ollama (local / offline)</option>
            <option value="gemini">Google Gemini (free tier)</option>
          </select>
        </div>
        <div class="cs-group" id="csApiKeyGroup">
          <div class="cs-lbl">API Key</div>
          <input class="cs-input" id="csApiKey" type="password" placeholder="sk-&#8230;"/>
        </div>
        <div class="cs-group" id="csBaseUrlGroup" style="display:none">
          <div class="cs-lbl">Base URL</div>
          <input class="cs-input" id="csBaseUrl" placeholder="http://localhost:11434"/>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">Model</div>
          <input class="cs-input" id="csModel" placeholder="claude-haiku-4-5-20251001"/>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">History</div>
          <div class="cs-row">
            <span class="cs-row-lbl">Persist across restarts</span>
            <label class="toggle" style="margin:0">
              <input type="checkbox" id="csHistoryEnabled">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">Monitor &mdash; Polling</div>
          <div class="cs-row" style="align-items:flex-start;flex-direction:column;gap:6px">
            <div style="display:flex;align-items:center;justify-content:space-between;width:100%">
              <span class="cs-row-lbl">Poll interval</span>
              <span id="csPollLabel" style="font-size:11px;color:#4ade80;font-weight:700">30 min</span>
            </div>
            <input type="range" id="csPollSlider" min="5" max="60" step="5" value="30"
              style="width:100%;accent-color:#3b82f6;cursor:pointer"
              oninput="csPollSliderChange(this.value)"/>
            <div style="display:flex;justify-content:space-between;width:100%;font-size:9px;color:#334155">
              <span>5 min</span><span>15</span><span>30</span><span>45</span><span>60 min</span>
            </div>
          </div>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">Pause escalation</div>
          <div class="cs-row">
            <span class="cs-row-lbl">Re-alert if still paused every</span>
          </div>
          <div style="display:flex;align-items:center;gap:7px;margin-top:2px">
            <input class="cs-input" id="csEscalateMinutes" type="number"
              min="0" max="240" step="5" value="30"
              style="width:70px;text-align:center"/>
            <span style="font-size:11px;color:#475569">min &nbsp;(0 = once only)</span>
          </div>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">Agent &mdash; Action Tier</div>
          <select class="cs-select" id="csTier" onchange="csTierChange()">
            <option value="1">Tier 1 &mdash; Read-only</option>
            <option value="2" selected>Tier 2 &mdash; Reversible actions</option>
          </select>
          <div style="font-size:10px;color:#475569;margin-top:2px">
            Tier 3 (irreversible) requires CLI: <code style="color:#64748b">python3 monitor_server.py enable-tier3</code>
          </div>
        </div>
        <div class="cs-group">
          <div class="cs-lbl">Trust Mode</div>
          <div class="cs-row">
            <span class="cs-row-lbl">Default trust duration</span>
          </div>
          <div style="display:flex;align-items:center;gap:7px;margin-top:2px">
            <input class="cs-input" id="csTrustHours" type="number"
              min="1" max="168" step="1" value="24"
              style="width:60px;text-align:center"/>
            <span style="font-size:11px;color:#475569">hours (1&ndash;168, used when trust checkbox shown)</span>
          </div>
        </div>
        <div id="csT3Section" style="display:none">
          <div class="cs-t3-active" id="csT3Active" style="display:none">
            <div class="cs-t3-active-title">&#128680; Tier 3 Active</div>
            <div class="cs-t3-countdown" id="csT3Countdown"></div>
            <button class="btn-revoke" onclick="revokeT3()">Revoke Now</button>
          </div>
          <div class="cs-t3-warn" id="csT3Warn" style="display:none">
            &#9888;&#65039; Tier 3 is currently <strong>inactive</strong>.
            Enable via CLI only.
          </div>
        </div>
        <div class="cs-status" id="csStatus"></div>
      </div>
      <div class="cs-footer">
        <button class="cs-btn cs-btn-danger" onclick="clearChatHistory()">Clear History</button>
        <button class="cs-btn cs-btn-primary" onclick="saveChatSettings()">Save</button>
      </div>
    </div>
  </div>
</div>

</div>

<script>
var uiPollCount=0,allPrinters=[],activePrinterId=null,countdownID=null;
var cameraVisible=false,cameraInterval=null;
var managePanelOpen=false,editingPrinterId=null;
var viewMode='detail';
var fleetCamsOn=false,fleetCamTimers={};

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmtTime(s){
  if(s==null||s<0)return'\u2014';
  return Math.floor(s/3600)+'h\u202f'+Math.floor((s%3600)/60)+'m\u202f'+Math.floor(s%60)+'s';
}

/* ── View switching ── */
function setView(v){
  viewMode=v;
  var isF=(v==='fleet');
  document.getElementById('fleetView').style.display=isF?'block':'none';
  document.getElementById('detailView').style.display=isF?'none':'block';
  document.getElementById('detailNav').style.display=isF?'none':'block';
  document.body.classList.toggle('fleet-mode',isF);
  document.getElementById('vtabFleet').className='vtab'+(isF?' active':'');
  document.getElementById('vtabDetail').className='vtab'+(!isF?' active':'');
  var ap=allPrinters.find(function(p){return p.id===activePrinterId;});
  document.getElementById('headerSub').textContent=isF
    ?allPrinters.length+' printers in fleet':(ap?ap.host:'');
  if(isF){if(cameraVisible)toggleCamera();renderFleetGrid();}
  else{if(ap){renderDetailStatus(ap.status||{},ap.name);renderDetailAlerts(ap.active_alerts||[]);}}
}

/* ── Printer tabs ── */
function selectPrinter(pid){
  activePrinterId=pid;
  document.querySelectorAll('.printer-tab').forEach(function(el){
    el.classList.toggle('active',el.dataset.id===pid);
  });
  var ap=allPrinters.find(function(p){return p.id===pid;});
  if(ap){document.getElementById('headerSub').textContent=ap.host;
    renderDetailStatus(ap.status||{},ap.name);renderDetailAlerts(ap.active_alerts||[]);}
  if(cameraVisible)refreshDetailCam();
}

function renderPrinterTabs(printers){
  var tabs=document.getElementById('printerTabs');
  tabs.innerHTML=printers.map(function(p){
    var s=(p.status&&p.status.print_stats&&p.status.print_stats.state)||'unknown';
    var off=!p.enabled?'<span style="font-size:9px;color:#475569;margin-left:2px">off</span>':'';
    var err=p.errors>0&&p.enabled?'<span style="color:#ef4444;font-size:9px;margin-left:2px">err</span>':'';
    return '<button class="printer-tab'+(p.id===activePrinterId?' active':'')+'" data-id="'+p.id+
      '" onclick="selectPrinter(\''+p.id+'\');refreshUI()">'+
      '<span class="tab-dot dot-'+s+'"></span>'+esc(p.name)+off+err+'</button>';
  }).join('');
  document.getElementById('viewToggleWrap').style.display=printers.length>1?'block':'none';
}

/* ── Fleet grid ── */
function renderFleetGrid(){
  var en=allPrinters.filter(function(p){return p.enabled!==false;});
  var printing=en.filter(function(p){var ps=p.status&&p.status.print_stats;return ps&&ps.state==='printing';}).length;
  var crits=en.filter(function(p){return(p.active_alerts||[]).some(function(a){return a.level==='critical';});}).length;
  var warns=en.filter(function(p){return(p.active_alerts||[]).some(function(a){return a.level==='warning';});}).length;
  var s='<b>'+en.length+'</b> printers &middot; <b>'+printing+'</b> printing';
  if(crits)s+=' &middot; <span style="color:#ef4444;font-weight:700">&#9888; '+crits+' CRITICAL</span>';
  else if(warns)s+=' &middot; <span style="color:#f59e0b">'+warns+' warning'+(warns>1?'s':'')+'</span>';
  else s+=' &middot; <span style="color:#4ade80">\u2705 all nominal</span>';
  document.getElementById('fleetSummary').innerHTML=s;
  document.getElementById('fleetGrid').innerHTML=en.map(fleetCard).join('');
  if(fleetCamsOn)en.forEach(function(p){startFleetCam(p.id);});
}

function fleetCard(p){
  var s=p.status||{},ps=s.print_stats||{},ext=s.extruder||{},bed=s.heater_bed||{},
      vsd=s.virtual_sdcard||{};
  var state=ps.state||'unknown',prog=(vsd.progress||0)*100,
      el=ps.print_duration||0,tot=ps.total_duration||0,eta=tot>el?tot-el:-1;
  var alerts=p.active_alerts||[];
  var hasCrit=alerts.some(function(a){return a.level==='critical';});
  var hasWarn=!hasCrit&&alerts.some(function(a){return a.level==='warning';});
  var hOn=ext.target>0,bOn=bed.target>0;
  var hOk=!hOn||Math.abs((ext.temperature||0)-ext.target)<=20;
  var bOk=!bOn||Math.abs((bed.temperature||0)-bed.target)<=15;

  var chips='';
  if(!alerts.length){chips='<div class="fc-chip fc-chip-ok">\u2705 Nominal</div>';}
  else{
    chips=alerts.slice(0,2).map(function(a){
      var cls=a.level==='critical'?'fc-chip-crit':'fc-chip-warn';
      var txt=a.msg.length>60?a.msg.substring(0,60)+'\u2026':a.msg;
      return '<div class="fc-chip '+cls+'">'+esc(txt)+'</div>';
    }).join('');
    if(alerts.length>2)chips+='<div style="font-size:9px;color:#475569;padding:2px 4px">+'+
      (alerts.length-2)+' more &mdash; click for detail</div>';
  }

  var progHtml='';
  if(state==='printing'||state==='paused'){
    progHtml='<div class="fc-prog">'
      +'<div class="fc-prog-row"><span class="fc-pct">'+prog.toFixed(1)+'%</span>'
      +(eta>0?'<span class="fc-eta">ETA '+fmtTime(eta)+'</span>':'')+'</div>'
      +'<div class="fc-bar-bg"><div class="fc-bar" style="width:'+Math.min(prog,100)+'%"></div></div>'
      +(ps.filename?'<div class="fc-file">'+esc(ps.filename)+'</div>':'')+'</div>';
  }

  var camHtml=fleetCamsOn
    ?'<div class="fc-cam" id="fccam-'+p.id+'"><div class="fc-cam-err">Loading\u2026</div></div>'
    :'';

  return '<div class="fc'+(hasCrit?' fc-crit':hasWarn?' fc-warn':'')+'" onclick="fleetDrill(\''+p.id+'\')">'
    +'<div class="fc-top"><div>'
    +'<div class="fc-name">'+esc(p.name)+'</div>'
    +'<div class="fc-host">'+esc(p.host)+'</div>'
    +'</div>'
    +'<span class="fc-badge fc-badge-'+state+'"><span class="fc-dot dot-'+state+'"></span>'+state.toUpperCase()+'</span>'
    +'</div>'
    +progHtml
    +'<div class="fc-temps">'
    +'<div class="fc-tmp"><div class="fc-tmp-lbl">Hotend</div>'
    +'<div class="fc-tmp-val '+(hOn?(hOk?'tc-ok':'tc-bad'):'tc-off')+'">'+(ext.temperature||0).toFixed(1)+'\u00b0C'+(hOn?' /\u202f'+(ext.target||0)+'\u00b0':'')+'</div></div>'
    +'<div class="fc-tmp"><div class="fc-tmp-lbl">Bed</div>'
    +'<div class="fc-tmp-val '+(bOn?(bOk?'tc-ok':'tc-bad'):'tc-off')+'">'+(bed.temperature||0).toFixed(1)+'\u00b0C'+(bOn?' /\u202f'+(bed.target||0)+'\u00b0':'')+'</div></div>'
    +(p.errors>0?'<div class="fc-tmp"><div class="fc-tmp-lbl">Conn</div><div class="fc-tmp-val tc-bad" style="font-size:11px">offline</div></div>':'')
    +'</div>'
    +'<div class="fc-chips">'+chips+'</div>'
    +camHtml
    +'<div class="fc-hint">&#128202; View detail \u2192</div>'
    +'</div>';
}

function fleetDrill(pid){
  activePrinterId=pid;
  setView('detail');
  renderPrinterTabs(allPrinters);
  var ap=allPrinters.find(function(p){return p.id===pid;});
  if(ap){renderDetailStatus(ap.status||{},ap.name);renderDetailAlerts(ap.active_alerts||[]);}
}

/* ── Fleet cameras ── */
function toggleFleetCameras(){
  fleetCamsOn=!fleetCamsOn;
  document.getElementById('fleetCamBtn').textContent=fleetCamsOn?'\uD83D\uDCF7 Hide Cameras':'\uD83D\uDCF7 Show Cameras';
  if(!fleetCamsOn){Object.values(fleetCamTimers).forEach(clearInterval);fleetCamTimers={};}
  renderFleetGrid();
  if(fleetCamsOn)allPrinters.filter(function(p){return p.enabled!==false;}).forEach(function(p){startFleetCam(p.id);});
}
function startFleetCam(pid){
  if(fleetCamTimers[pid])clearInterval(fleetCamTimers[pid]);
  loadFleetCamFrame(pid);
  fleetCamTimers[pid]=setInterval(function(){loadFleetCamFrame(pid);},5000);
}
function loadFleetCamFrame(pid){
  var wrap=document.getElementById('fccam-'+pid);if(!wrap)return;
  var url='/api/printers/'+pid+'/camera?t='+Date.now();
  var img=new Image();
  img.onload=function(){
    wrap.innerHTML='<div class="fc-cam-badge">LIVE</div>'
      +'<img src="'+url+'" style="width:100%;display:block;max-height:170px;object-fit:cover">';
  };
  img.onerror=function(){wrap.innerHTML='<div class="fc-cam-err">No camera available</div>';};
  img.src=url;
}

/* ── Alert channels ── */
function renderChannels(cfg){
  var map={ntfy:cfg.ntfy&&cfg.ntfy.enabled,sms:cfg.twilio&&cfg.twilio.enabled,
           email:cfg.email&&cfg.email.enabled,imessage:cfg.imessage&&cfg.imessage.enabled};
  var lbl={ntfy:'\u{1F4F1} Push',sms:'\u{1F4AC} SMS',email:'\u{1F4E7} Email',imessage:'\u{1F4AC} iMessage'};
  Object.keys(map).forEach(function(k){
    var el=document.getElementById('ch-'+k);
    if(el){el.className='ch '+(map[k]?'ch-on':'ch-off');el.textContent=lbl[k];}
  });
  var m=Math.round((cfg.poll_interval_seconds||1800)/60);
  document.getElementById('intervalLabel').textContent=m>=60?(m/60)+'h':m+' min';
}
function updateCountdown(secs,lastPoll){
  if(countdownID)clearInterval(countdownID);
  if(!lastPoll){document.getElementById('countdown').textContent='\u2014';return;}
  var nextAt=new Date(lastPoll).getTime()+secs*1000;
  countdownID=setInterval(function(){
    var d=nextAt-Date.now();
    if(d<=0){document.getElementById('countdown').textContent='polling\u2026';return;}
    document.getElementById('countdown').textContent=Math.floor(d/60000)+'m '+Math.floor((d%60000)/1000)+'s';
  },1000);
}

/* ── Detail status ── */
function renderDetailStatus(s,name){
  var ps=s.print_stats||{},ext=s.extruder||{},bed=s.heater_bed||{},
      vsd=s.virtual_sdcard||{},th=s.toolhead||{},pos=th.position||[0,0,0,0];
  var state=ps.state||'unknown',prog=(vsd.progress||0)*100,
      el=ps.print_duration||0,tot=ps.total_duration||0,eta=tot>el?tot-el:-1,
      layer=(ps.info&&ps.info.current_layer)||'?',totL=(ps.info&&ps.info.total_layer)||'?',
      fil=((ps.filament_used||0)/1000).toFixed(2);
  document.getElementById('stateBadge').className='badge badge-'+state;
  document.getElementById('stateBadge').textContent=state.toUpperCase();
  document.getElementById('printerName').textContent=name||'';
  document.getElementById('filename').textContent=ps.filename||'\u2014';
  document.getElementById('progressPct').textContent=prog.toFixed(1)+'%';
  document.getElementById('layerInfo').textContent='Layer '+layer+'/'+totL;
  document.getElementById('progressFill').style.width=Math.min(prog,100)+'%';
  document.getElementById('elapsed').textContent=fmtTime(el);
  document.getElementById('eta').textContent=fmtTime(eta);
  document.getElementById('filament').textContent=fil+'m';
  document.getElementById('zpos').textContent=((pos[2]||0).toFixed(2))+'mm';
  var hOk=!ext.target||Math.abs((ext.temperature||0)-ext.target)<=20;
  var bOk=!bed.target||Math.abs((bed.temperature||0)-bed.target)<=15;
  document.getElementById('hotendTemp').textContent=(ext.temperature||0).toFixed(1)+'\u00b0C';
  document.getElementById('hotendTemp').className='temp-val '+(hOk?'temp-ok':'temp-bad');
  document.getElementById('hotendTarget').textContent='/ '+(ext.target||0)+'\u00b0C';
  document.getElementById('hotendIcon').textContent=hOk?'\u2705':'\u26a0\ufe0f';
  document.getElementById('bedTemp').textContent=(bed.temperature||0).toFixed(1)+'\u00b0C';
  document.getElementById('bedTemp').className='temp-val '+(bOk?'temp-ok':'temp-bad');
  document.getElementById('bedTarget').textContent='/ '+(bed.target||0)+'\u00b0C';
  document.getElementById('bedIcon').textContent=bOk?'\u2705':'\u26a0\ufe0f';
}
function renderDetailAlerts(alerts){
  var c=document.getElementById('alertsContainer');c.innerHTML='';
  if(!alerts||!alerts.length){c.innerHTML='<div class="alert alert-ok">\u2705 All systems nominal</div>';return;}
  alerts.forEach(function(a){var d=document.createElement('div');d.className='alert alert-'+a.level;d.textContent=a.msg;c.appendChild(d);});
  if(alerts.some(function(a){return a.level==='critical';})){
    document.title='\uD83D\uDEA8 ALERT \u2014 Printer Monitor';
    setTimeout(function(){document.title='Printer Fleet Monitor';},6000);
    if(Notification.permission==='granted')new Notification('Printer Alert',{body:alerts[0].msg});
  }
}

/* ── Alert log ── */
function renderAlertLog(log){
  var list=document.getElementById('alertLogList');
  if(!log||!log.length){list.innerHTML='<div style="color:#334155;font-size:11px">No alerts dispatched yet.</div>';return;}
  list.innerHTML=log.slice(0,30).map(function(e){
    var t=new Date(e.time).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    var sent=e.sent&&e.sent.length?'sent: '+e.sent.join(', '):'no channels enabled';
    var fail=e.failed&&e.failed.length?' \u00b7 failed: '+e.failed.map(function(f){return f[0];}).join(','):'';
    var pn=e.printer?'<span class="log-printer">['+esc(e.printer)+']</span>':'';
    return '<div class="log-entry log-entry-'+e.level+'"><span style="opacity:.5">['+t+']</span> '+pn+esc(e.msg)+'<div class="log-sent">'+sent+fail+'</div></div>';
  }).join('');
}

/* ── Manage panel ── */
function toggleManagePanel(){
  managePanelOpen=!managePanelOpen;
  document.getElementById('managePanel').style.display=managePanelOpen?'block':'none';
  if(managePanelOpen)renderManagePanel();
}
function renderManagePanel(){
  var list=document.getElementById('printerRowList');
  if(!allPrinters.length){list.innerHTML='<div style="color:#334155;font-size:11px">No printers.</div>';return;}
  list.innerHTML=allPrinters.map(function(p){
    var on=p.enabled!==false;
    return '<div class="printer-row"><div class="printer-row-info">'
      +'<div class="printer-row-name">'+esc(p.name)+'</div>'
      +'<div class="printer-row-host">'+esc(p.host)+'</div></div>'
      +'<div class="printer-row-actions">'
      +'<button class="btn-sm" onclick="startEdit(\''+p.id+'\')">&#9998; Edit</button>'
      +'<label class="toggle"><input type="checkbox" '+(on?'checked':'')
      +' onchange="toggleEnabled(\''+p.id+'\',this.checked)"><span class="toggle-slider"></span></label>'
      +'</div></div>';
  }).join('');
  if(editingPrinterId&&!allPrinters.find(function(p){return p.id===editingPrinterId;})){
    editingPrinterId=null;document.getElementById('editFormWrap').innerHTML='';
  }
}
function startEdit(pid){
  editingPrinterId=pid;
  var p=allPrinters.find(function(x){return x.id===pid;});if(!p)return;
  document.getElementById('editFormWrap').innerHTML=
    '<div class="edit-row"><div class="section-label" style="margin-bottom:6px">Editing: '+esc(p.name)+'</div>'
    +'<div><div class="edit-label">Name</div><input class="edit-input" id="editName" value="'+esc(p.name)+'"/></div>'
    +'<div><div class="edit-label">Host URL</div><input class="edit-input" id="editHost" value="'+esc(p.host)+'"/></div>'
    +'<div><div class="edit-label">API Token</div><input class="edit-input" id="editToken" value="'+esc(p.api_token||'')+'" placeholder="leave blank if not needed"/></div>'
    +'<div class="edit-actions"><button class="btn-sm btn-sm-success" onclick="saveEdit(\''+pid+'\')">&#10003; Save</button>'
    +'<button class="btn-sm" onclick="cancelEdit()">Cancel</button></div>'
    +'<div id="editMsg" style="font-size:11px;color:#475569;min-height:14px"></div></div>';
  document.getElementById('editFormWrap').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function cancelEdit(){editingPrinterId=null;document.getElementById('editFormWrap').innerHTML='';}
async function saveEdit(pid){
  var name=document.getElementById('editName').value.trim();
  var host=document.getElementById('editHost').value.trim();
  var token=document.getElementById('editToken').value.trim();
  var msg=document.getElementById('editMsg');
  if(!name||!host){msg.textContent='Name and host required.';msg.style.color='#ef4444';return;}
  msg.textContent='Saving\u2026';msg.style.color='#475569';
  try{
    var r=await fetch('/api/printers/'+pid,{method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name,host:host,api_token:token})});
    var d=await r.json();
    if(r.ok){msg.textContent='Saved!';msg.style.color='#4ade80';await refreshUI();renderManagePanel();setTimeout(cancelEdit,1200);}
    else{msg.textContent=d.error||'Error.';msg.style.color='#ef4444';}
  }catch(e){msg.textContent='Failed: '+e.message;msg.style.color='#ef4444';}
}
async function toggleEnabled(pid,en){
  try{
    await fetch('/api/printers/'+pid,{method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:en})});
    await refreshUI();if(managePanelOpen)renderManagePanel();
  }catch(e){console.error(e);}
}
async function addPrinter(){
  var name=document.getElementById('addName').value.trim();
  var host=document.getElementById('addHost').value.trim();
  var token=document.getElementById('addToken').value.trim();
  var msg=document.getElementById('addMsg');
  if(!name||!host){msg.textContent='Name and host required.';msg.style.color='#ef4444';return;}
  msg.textContent='Adding\u2026';msg.style.color='#475569';
  try{
    var r=await fetch('/api/printers',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name,host:host,api_token:token})});
    var d=await r.json();
    if(r.ok||r.status===201){
      msg.textContent='Added!';msg.style.color='#4ade80';
      document.getElementById('addName').value='';document.getElementById('addHost').value='';document.getElementById('addToken').value='';
      await refreshUI();if(managePanelOpen)renderManagePanel();
    }else{msg.textContent=d.error||'Error.';msg.style.color='#ef4444';}
  }catch(e){msg.textContent='Failed: '+e.message;msg.style.color='#ef4444';}
}

/* ── Single-printer camera ── */
function toggleCamera(){
  cameraVisible=!cameraVisible;
  document.getElementById('cameraSection').style.display=cameraVisible?'block':'none';
  document.getElementById('cameraBtn').textContent=cameraVisible?'\uD83D\uDCF7 Hide Cam':'\uD83D\uDCF7 Camera';
  if(cameraVisible){refreshDetailCam();cameraInterval=setInterval(refreshDetailCam,5000);}
  else clearInterval(cameraInterval);
}
function refreshDetailCam(){
  if(!activePrinterId)return;
  var img=document.getElementById('cameraImg');
  var err=document.getElementById('cameraErr');
  img.style.display='block';err.style.display='none';
  img.src='/api/printers/'+activePrinterId+'/camera?t='+Date.now();
}

/* ── Actions ── */
async function triggerPoll(){
  await fetch('/api/poll',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({printer_id:activePrinterId})});
  setTimeout(refreshUI,2500);
}
async function sendTest(){
  await fetch('/api/test_alert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({printer_id:activePrinterId})});
  setTimeout(refreshUI,2500);
}
function openConfig(){window.open('/monitor_config.json','_blank');}

/* ── Main refresh ── */
async function refreshUI(){
  document.getElementById('spinner').style.display='inline';
  try{
    var res=await Promise.all([fetch('/api/printers'),fetch('/api/alerts'),fetch('/api/config')]);
    var pd=await res[0].json(),ad=await res[1].json(),cfg=await res[2].json();
    allPrinters=pd.printers||[];
    if(!activePrinterId&&allPrinters.length)activePrinterId=allPrinters[0].id;
    if(uiPollCount===0&&allPrinters.length>1)viewMode='fleet';
    renderPrinterTabs(allPrinters);
    renderChannels(cfg);
    renderAlertLog(ad.alert_log);
    if(managePanelOpen)renderManagePanel();
    updateChatFabDot();
    var ap=allPrinters.find(function(p){return p.id===activePrinterId;})||allPrinters[0];
    if(viewMode==='fleet'){
      document.getElementById('fleetView').style.display='block';
      document.getElementById('detailView').style.display='none';
      document.getElementById('detailNav').style.display='none';
      document.body.classList.add('fleet-mode');
      document.getElementById('headerSub').textContent=allPrinters.length+' printers in fleet';
      renderFleetGrid();
    }else{
      document.getElementById('fleetView').style.display='none';
      document.getElementById('detailView').style.display='block';
      document.getElementById('detailNav').style.display='block';
      document.body.classList.remove('fleet-mode');
      if(ap){
        document.getElementById('headerSub').textContent=ap.host;
        renderDetailStatus(ap.status||{},ap.name);
        renderDetailAlerts(ap.active_alerts||[]);
        updateCountdown(cfg.poll_interval_seconds||1800,ap.last_poll);
      }
    }
    uiPollCount++;
    document.getElementById('checkCount').textContent=uiPollCount;
    document.getElementById('lastCheck').textContent=new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  }catch(e){
    document.getElementById('alertsContainer').innerHTML='<div class="alert alert-critical">Monitor server unreachable: '+e.message+'</div>';
  }finally{document.getElementById('spinner').style.display='none';}
}

/* ═══════════════════════════════════════════════════════
   FLEET AI CHAT
═══════════════════════════════════════════════════════ */
var chatOpen=false, chatHistory=[], chatLoaded=false;

function toggleChat(){
  chatOpen=!chatOpen;
  document.getElementById('chatPanel').style.display=chatOpen?'flex':'none';
  document.getElementById('chatFabIcon').textContent=chatOpen?'\u2715':'\u{1F4AC}';
  if(chatOpen&&!chatLoaded){loadChatHistory();}
  if(chatOpen){updateChatProvBadge();refreshAgentStatus();}
}

function loadChatHistory(){
  fetch('/api/chat/history').then(function(r){return r.json();}).then(function(d){
    chatHistory=d.history||[];
    chatLoaded=true;
    renderChatMessages();
  }).catch(function(){});
}

function updateChatProvBadge(){
  fetch('/api/config').then(function(r){return r.json();}).then(function(cfg){
    var llm=cfg.llm||{};
    var on=!!llm.enabled;
    var prov=llm.provider||'anthropic';
    var labels={'anthropic':'Claude','openai':'OpenAI','ollama':'Ollama','gemini':'Gemini'};
    var badge=document.getElementById('chatProvBadge');
    if(badge){
      badge.textContent=on?(labels[prov]||prov):'off';
      badge.className='cs-provider-badge '+(on?'cs-badge-on':'cs-badge-off');
    }
    document.getElementById('chatSend').disabled=!on;
    var empty=document.getElementById('chatEmpty');
    if(empty&&!chatHistory.length){
      if(on)empty.innerHTML='Ask me about your fleet,<br>alerts, or 3D printing tips.';
      else empty.innerHTML='Open \u2699 to configure your LLM provider.';
    }
  }).catch(function(){});
}

function renderChatMessages(){
  var box=document.getElementById('chatMessages');
  var empty=document.getElementById('chatEmpty');
  // Remove previous message bubbles
  box.querySelectorAll('.msg').forEach(function(el){el.remove();});
  if(!chatHistory.length){if(empty)empty.style.display='block';return;}
  if(empty)empty.style.display='none';
  chatHistory.forEach(function(m){
    var div=document.createElement('div');
    div.className='msg '+(m.role==='user'?'msg-user':'msg-ai');
    var t=m.time?new Date(m.time).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}):'';
    div.innerHTML='<div class="msg-bubble">'+escChatMsg(m.content)+'</div>'
      +'<div class="msg-time">'+t+'</div>';
    box.appendChild(div);
  });
  box.scrollTop=box.scrollHeight;
}

/* Escape HTML but preserve newlines as <br> for readability */
function escChatMsg(s){
  return String(s||'')
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

function chatAutoResize(el){
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,80)+'px';
}

function chatKeydown(e){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat();}
}

/* ── Agent state ── */
var agentStatus={tier:2,trust_active:false,tier3_active:false};
var pendingCountdowns={};  // action_id -> intervalID

function refreshAgentStatus(){
  fetch('/api/agent/status').then(function(r){return r.json();}).then(function(d){
    agentStatus=d;
    var tb=document.getElementById('agentTierBadge');
    var trustB=document.getElementById('agentTrustBadge');
    var t3B=document.getElementById('agentT3Badge');
    if(tb){tb.textContent='T'+d.tier;tb.style.display='inline';}
    if(trustB)trustB.style.display=d.trust_active?'inline':'none';
    if(t3B)t3B.style.display=d.tier3_active?'inline':'none';
  }).catch(function(){});
}

async function sendChat(){
  var inp=document.getElementById('chatInput');
  var msg=inp.value.trim();
  if(!msg)return;
  // If there's an active multi-printer countdown, typing = abort
  var activeCD=Object.keys(pendingCountdowns);
  if(activeCD.length>0){
    activeCD.forEach(function(aid){abortMultiAction(aid,true);});
    return;
  }
  inp.value='';inp.style.height='auto';
  var now=new Date().toISOString();
  chatHistory.push({role:'user',content:msg,time:now});
  renderChatMessages();
  var box=document.getElementById('chatMessages');
  var typing=document.createElement('div');
  typing.className='chat-typing';typing.id='chatTyping';
  typing.innerHTML='<span></span><span></span><span></span>';
  box.appendChild(typing);box.scrollTop=box.scrollHeight;
  document.getElementById('chatSend').disabled=true;
  try{
    var r=await fetch('/api/chat',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg})});
    if(typing&&typing.parentNode)typing.remove();
    var d=await r.json();
    if(!r.ok){
      chatHistory.push({role:'assistant',
        content:'\u26a0\ufe0f Error: '+(d.error||'Unknown error'),
        time:new Date().toISOString()});
      renderChatMessages();return;
    }
    if(d.type==='reply'){
      chatHistory=d.history||chatHistory;
      renderChatMessages();
    }else if(d.type==='pending_action'){
      renderPendingAction(d);
    }
  }catch(err){
    if(document.getElementById('chatTyping'))document.getElementById('chatTyping').remove();
    chatHistory.push({role:'assistant',
      content:'\u26a0\ufe0f Could not reach server: '+err.message,
      time:new Date().toISOString()});
    renderChatMessages();
  }finally{
    updateChatProvBadge();
    refreshAgentStatus();
  }
}

function renderPendingAction(d){
  var box=document.getElementById('chatMessages');
  var isTier3=d.actions&&d.actions.some(function(a){
    return['cancel_print','delete_file','emergency_stop'].includes(a.tool);
  });
  var card=document.createElement('div');
  card.className='action-card'+(isTier3?' tier3':'');
  card.id='action-card-'+d.action_id;

  var title=document.createElement('div');
  title.className='action-card-title';
  title.textContent=d.is_multi
    ?'\ud83e\udd16 Fleet action — '+d.actions.length+' printers'
    :'\ud83e\udd16 Action requested';
  card.appendChild(title);

  var list=document.createElement('div');
  list.className='action-list';
  (d.actions||[]).forEach(function(a){
    var it=document.createElement('div');
    it.className='action-item';
    it.textContent=a.description;
    list.appendChild(it);
  });
  card.appendChild(list);

  if(d.is_multi){
    // 30-second countdown with abort
    var cdBar=document.createElement('div');cdBar.className='countdown-bar';
    var fill=document.createElement('div');fill.className='countdown-bar-fill';
    fill.id='cd-fill-'+d.action_id;fill.style.width='100%';
    cdBar.appendChild(fill);card.appendChild(cdBar);
    var cdTxt=document.createElement('div');cdTxt.className='countdown-txt';
    cdTxt.id='cd-txt-'+d.action_id;
    cdTxt.textContent='Executing in 30s — type anything or click Abort to cancel';
    card.appendChild(cdTxt);
    var btns=document.createElement('div');btns.className='action-btns';
    var abortBtn=document.createElement('button');
    abortBtn.className='btn-abort';abortBtn.textContent='Abort';
    abortBtn.onclick=function(){abortMultiAction(d.action_id,false);};
    btns.appendChild(abortBtn);
    card.appendChild(btns);
    box.appendChild(card);box.scrollTop=box.scrollHeight;
    // Start countdown
    var secs=30,total=30;
    var iv=setInterval(function(){
      secs--;
      var pct=(secs/total)*100;
      var f=document.getElementById('cd-fill-'+d.action_id);
      var t=document.getElementById('cd-txt-'+d.action_id);
      if(f)f.style.width=pct+'%';
      if(t)t.textContent='Executing in '+secs+'s — type anything or click Abort to cancel';
      if(f)f.style.background=secs<=10?'#ef4444':'#3b82f6';
      if(secs<=0){
        clearInterval(iv);
        delete pendingCountdowns[d.action_id];
        executeConfirmedAction(d.action_id,false,0,card);
      }
    },1000);
    pendingCountdowns[d.action_id]=iv;
  } else {
    // Normal: Confirm / Deny + optional trust checkbox
    var btns=document.createElement('div');btns.className='action-btns';
    var conf=document.createElement('button');
    conf.className='btn-confirm';conf.textContent='Confirm';
    conf.onclick=function(){
      var th=parseInt(document.getElementById('trust-hrs-'+d.action_id).value)||0;
      var useTrust=document.getElementById('trust-cb-'+d.action_id).checked;
      executeConfirmedAction(d.action_id,true,useTrust?th:0,card);
    };
    var deny=document.createElement('button');
    deny.className='btn-deny';deny.textContent='Deny';
    deny.onclick=function(){executeConfirmedAction(d.action_id,false,0,card);};
    btns.appendChild(conf);btns.appendChild(deny);

    // Trust row
    var trustRow=document.createElement('div');trustRow.className='trust-row';
    var tcb=document.createElement('input');
    tcb.type='checkbox';tcb.id='trust-cb-'+d.action_id;tcb.style.accentColor='#3b82f6';
    var tsel=document.createElement('select');
    tsel.id='trust-hrs-'+d.action_id;
    [1,4,8,24,48,72,168].forEach(function(h){
      var o=document.createElement('option');
      o.value=h;o.textContent=h<24?h+'h':Math.round(h/24)+'d';
      if(h===(agentStatus.trust_duration_hours||24))o.selected=true;
      tsel.appendChild(o);
    });
    var tlbl=document.createElement('label');
    tlbl.htmlFor='trust-cb-'+d.action_id;
    tlbl.textContent='Trust LLM actions for';
    trustRow.appendChild(tcb);trustRow.appendChild(tlbl);
    trustRow.appendChild(tsel);
    btns.appendChild(trustRow);
    card.appendChild(btns);
    box.appendChild(card);box.scrollTop=box.scrollHeight;
  }
  document.getElementById('chatSend').disabled=false;
}

function abortMultiAction(action_id, silent){
  if(pendingCountdowns[action_id]){
    clearInterval(pendingCountdowns[action_id]);
    delete pendingCountdowns[action_id];
  }
  if(!silent) executeConfirmedAction(action_id,false,0,
    document.getElementById('action-card-'+action_id));
}

async function executeConfirmedAction(action_id, confirmed, trust_hours, card){
  if(card)card.remove();
  var r=await fetch('/api/chat/action',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action_id:action_id,confirmed:confirmed,
      trust_hours:trust_hours})});
  var d=await r.json();
  if(d.history)chatHistory=d.history;
  // Show exec log if present
  if(d.exec_log&&d.exec_log.length){
    var log=d.exec_log.map(function(e){
      return(e.result.startsWith('\u2713')?'\u2705 ':'  ')+e.description+'\n  '+e.result;
    }).join('\n');
    chatHistory.push({role:'assistant',content:log,time:new Date().toISOString()});
  }
  renderChatMessages();
  refreshAgentStatus();
  document.getElementById('chatSend').disabled=false;
}

/* ── Settings drawer ── */
function openChatSettings(){
  document.getElementById('chatSettings').style.display='flex';
  fetch('/api/config').then(function(r){return r.json();}).then(function(cfg){
    var llm=cfg.llm||{};
    var prov=llm.provider||'anthropic';
    document.getElementById('csEnabled').checked=!!llm.enabled;
    document.getElementById('csProvider').value=prov;
    document.getElementById('csHistoryEnabled').checked=llm.history_enabled!==false;
    var pollMin=Math.round((cfg.poll_interval_seconds||1800)/60);
    // Clamp to slider range 5-60; values outside (e.g. 90 min) show as 60
    var sliderVal=Math.min(60,Math.max(5,pollMin));
    document.getElementById('csPollSlider').value=sliderVal;
    csPollSliderChange(sliderVal);
    document.getElementById('csEscalateMinutes').value=
      cfg.pause_escalate_minutes!=null?cfg.pause_escalate_minutes:30;
    if(prov==='anthropic'){
      document.getElementById('csApiKey').value=
        (llm.anthropic&&llm.anthropic.api_key)?'\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7':'';
      document.getElementById('csModel').value=
        (llm.anthropic&&llm.anthropic.model)||'claude-haiku-4-5-20251001';
    }else if(prov==='openai'){
      document.getElementById('csApiKey').value=
        (llm.openai&&llm.openai.api_key)?'\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7':'';
      document.getElementById('csBaseUrl').value=
        (llm.openai&&llm.openai.base_url)||'https://api.openai.com/v1';
      document.getElementById('csModel').value=
        (llm.openai&&llm.openai.model)||'gpt-4o-mini';
    }else if(prov==='ollama'){
      document.getElementById('csBaseUrl').value=
        (llm.ollama&&llm.ollama.base_url)||'http://localhost:11434';
      document.getElementById('csModel').value=
        (llm.ollama&&llm.ollama.model)||'llama3.2';
    }else if(prov==='gemini'){
      document.getElementById('csApiKey').value=
        (llm.gemini&&llm.gemini.api_key)?'\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7':'';
      document.getElementById('csModel').value=
        (llm.gemini&&llm.gemini.model)||'gemini-2.0-flash';
    }
    csProviderChange();
    // Agent settings
    var agent=cfg.agent||{};
    var tier=Math.min(2,agent.tier||2);
    var tierSel=document.getElementById('csTier');
    if(tierSel)tierSel.value=String(tier);
    document.getElementById('csTrustHours').value=
      (agent.trust_mode&&agent.trust_mode.duration_hours)||24;
    // Tier 3 section
    var t3=agent.tier3||{};
    var t3sec=document.getElementById('csT3Section');
    if(t3sec)t3sec.style.display='block';
    var t3act=document.getElementById('csT3Active');
    var t3warn=document.getElementById('csT3Warn');
    if(t3.enabled&&t3.expires_at){
      if(t3act){t3act.style.display='block';
        document.getElementById('csT3Countdown').textContent=
          'Active until: '+t3.expires_at.slice(0,16)+' ('+
          Math.max(0,Math.round((new Date(t3.expires_at)-new Date())/3600000))+'h remaining)';}
      if(t3warn)t3warn.style.display='none';
    }else{
      if(t3act)t3act.style.display='none';
      if(t3warn)t3warn.style.display='block';
    }
  });
}

function csTierChange(){
  // Tier 3 cannot be set via UI — selector only shows 1 and 2.
  // If tier3 is currently active (injected server-side), show the T3 section.
  var sel=document.getElementById('csTier');
  if(sel)sel.value=Math.min(2,parseInt(sel.value)||2);
}

function csPollSliderChange(v){
  var n=parseInt(v);
  document.getElementById('csPollLabel').textContent=n>=60?'60 min (max)':n+' min';
}

function closeChatSettings(){
  document.getElementById('chatSettings').style.display='none';
  document.getElementById('csStatus').textContent='';
}

function csProviderChange(){
  var prov=document.getElementById('csProvider').value;
  document.getElementById('csApiKeyGroup').style.display=
    prov==='ollama'?'none':'block';
  document.getElementById('csBaseUrlGroup').style.display=
    prov!=='anthropic'?'block':'none';
  var ph={'anthropic':'claude-haiku-4-5-20251001','openai':'gpt-4o-mini',
          'ollama':'llama3.2','gemini':'gemini-2.0-flash'};
  document.getElementById('csModel').placeholder=ph[prov]||'model-name';
  // Gemini uses API key but no base URL
  if(prov==='gemini'){
    document.getElementById('csApiKeyGroup').style.display='block';
    document.getElementById('csBaseUrlGroup').style.display='none';
    document.getElementById('csApiKey').placeholder='AIza...';
  }else{
    document.getElementById('csApiKey').placeholder='sk-\u2026';
  }
}

async function saveChatSettings(){
  var prov=document.getElementById('csProvider').value;
  var enabled=document.getElementById('csEnabled').checked;
  var model=document.getElementById('csModel').value.trim();
  var histOn=document.getElementById('csHistoryEnabled').checked;
  var apiKey=document.getElementById('csApiKey').value.trim();
  var baseUrl=document.getElementById('csBaseUrl').value.trim();
  var status=document.getElementById('csStatus');
  status.textContent='Saving\u2026';status.style.color='#475569';
  var pollSecs=parseInt(document.getElementById('csPollSlider').value)*60;
  var escMins=parseInt(document.getElementById('csEscalateMinutes').value)||0;
  var agentTier=parseInt((document.getElementById('csTier')||{value:'2'}).value)||2;
  var trustHours=parseInt(document.getElementById('csTrustHours').value)||24;
  var payload={
    poll_interval_seconds: pollSecs,
    pause_escalate_minutes: escMins,
    agent:{tier:agentTier,trust_mode:{duration_hours:trustHours}},
    llm:{enabled:enabled,provider:prov,history_enabled:histOn}
  };
  if(prov==='anthropic'){
    payload.llm.anthropic={model:model||'claude-haiku-4-5-20251001'};
    if(apiKey&&apiKey!=='\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7')
      payload.llm.anthropic.api_key=apiKey;
  }else if(prov==='openai'){
    payload.llm.openai={model:model||'gpt-4o-mini',
      base_url:baseUrl||'https://api.openai.com/v1'};
    if(apiKey&&apiKey!=='\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7')
      payload.llm.openai.api_key=apiKey;
  }else if(prov==='ollama'){
    payload.llm.ollama={model:model||'llama3.2',
      base_url:baseUrl||'http://localhost:11434'};
  }else if(prov==='gemini'){
    payload.llm.gemini={model:model||'gemini-2.0-flash'};
    if(apiKey&&apiKey!=='\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7\u00b7')
      payload.llm.gemini.api_key=apiKey;
  }
  try{
    var r=await fetch('/api/settings',{method:'PATCH',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)});
    var d=await r.json();
    if(r.ok){
      status.textContent='\u2705 Saved!';status.style.color='#4ade80';
      updateChatProvBadge();
      // Refresh footer interval label
      var m=Math.round(pollSecs/60);
      var lbl=document.getElementById('intervalLabel');
      if(lbl)lbl.textContent=m>=60?(m/60)+'h':m+' min';
      setTimeout(function(){status.textContent='';},2500);
    }else{status.textContent=d.error||'Error.';status.style.color='#ef4444';}
  }catch(e){status.textContent='Failed: '+e.message;status.style.color='#ef4444';}
}

async function revokeT3(){
  if(!confirm('Revoke Tier 3 access immediately?'))return;
  await fetch('/api/settings',{method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent:{tier3:{enabled:false,expires_at:null}}})});
  openChatSettings();
  refreshAgentStatus();
}

async function clearChatHistory(){
  if(!confirm('Clear all chat history?'))return;
  try{
    await fetch('/api/chat/clear',{method:'POST'});
    chatHistory=[];renderChatMessages();
    document.getElementById('csStatus').textContent='\u2705 History cleared.';
    document.getElementById('csStatus').style.color='#4ade80';
    setTimeout(function(){document.getElementById('csStatus').textContent='';},2500);
  }catch(e){}
}

/* ── FAB alert dot: red when any printer has active critical alert ── */
function updateChatFabDot(){
  var hasCrit=allPrinters.some(function(p){
    return(p.active_alerts||[]).some(function(a){return a.level==='critical';});
  });
  var dot=document.getElementById('chatFabDot');
  if(dot)dot.style.display=hasCrit?'block':'none';
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
        self.send_header("Access-Control-Allow-Methods","GET, POST, PATCH, DELETE, OPTIONS")
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

        elif path == "/api/chat/history":
            with chat_lock:
                history_copy = list(chat_history)
            self.send_json(200, {"history": history_copy})

        elif path == "/api/agent/status":
            self.send_json(200, agent_status_dict())

        elif path == "/api/alerts":
            with global_lock: lg = list(alert_log)
            self.send_json(200, {"alert_log": lg})

        elif path == "/api/config":
            safe = json.loads(json.dumps(config))
            for ch in ("twilio","email"):
                for k in ("auth_token","password"):
                    if safe.get(ch,{}).get(k): safe[ch][k] = "••••••••"
            for _prov in ("anthropic","openai","gemini"):
                if safe.get("llm",{}).get(_prov,{}).get("api_key"):
                    safe["llm"][_prov]["api_key"] = "••••••••"
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

        elif path == "/api/chat":
            body = self._read_body()
            msg  = body.get("message","").strip()
            if not msg:
                self.send_json(400, {"error": "message is required"}); return
            try:
                result = process_chat_agentic(msg)
                self.send_json(200, result)
            except ValueError as e:
                self.send_json(400, {"error": str(e)})
            except urllib.error.HTTPError as e:
                err_body = e.read().decode(errors="ignore")
                self.send_json(502, {"error": f"LLM API error {e.code}: {err_body[:300]}"})
            except Exception as e:
                self.send_json(500, {"error": f"LLM error: {e}"})

        elif path == "/api/chat/action":
            body       = self._read_body()
            action_id  = body.get("action_id","")
            confirmed  = bool(body.get("confirmed", False))
            trust_hrs  = int(body.get("trust_hours", 0))
            if not action_id:
                self.send_json(400, {"error": "action_id required"}); return
            try:
                result = confirm_pending(action_id, confirmed, trust_hrs)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif path == "/api/trust":
            body  = self._read_body()
            hours = int(body.get("hours", 24))
            activate_trust(hours)
            self.send_json(200, {"ok": True, **agent_status_dict()})

        elif path == "/api/chat/clear":
            with chat_lock:
                chat_history.clear()
            try:
                if os.path.exists(CHAT_HISTORY_FILE):
                    os.remove(CHAT_HISTORY_FILE)
            except Exception:
                pass
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

    def do_PATCH(self):
        """PATCH /api/printers/<id>  — rename, change host, or toggle enabled.
        PATCH /api/settings           — update LLM and other server settings."""
        path = self.path.split("?")[0]

        if path == "/api/settings":
            body = self._read_body()
            changed = False
            # ── Poll interval (min 300 s = 5 min, max 86400 s = 24 h) ──
            if "poll_interval_seconds" in body:
                val = int(body["poll_interval_seconds"])
                config["poll_interval_seconds"] = max(300, min(val, 86400))
                changed = True
            # ── Pause escalation window (0 = disabled, else >= 5 min) ──
            if "pause_escalate_minutes" in body:
                val = int(body["pause_escalate_minutes"])
                config["pause_escalate_minutes"] = 0 if val == 0 else max(5, val)
                changed = True
            # ── Agent tier (1 or 2 only via web — tier3 requires CLI) ──
            if "agent" in body:
                a_patch = body["agent"]
                a_conf  = config.setdefault("agent", {})
                # tier3 changes are silently ignored here — CLI only
                if "tier" in a_patch:
                    a_conf["tier"] = max(1, min(2, int(a_patch["tier"])))
                if "trust_mode" in a_patch:
                    tm = a_patch["trust_mode"]
                    if "duration_hours" in tm:
                        a_conf.setdefault("trust_mode",{})["duration_hours"] = \
                            max(1, min(168, int(tm["duration_hours"])))
                changed = True
            # ── LLM settings ──
            if "llm" in body:
                llm_patch = body["llm"]
                llm_conf  = config.setdefault("llm", {})
                for k in ("enabled","provider","history_enabled","history_max_messages"):
                    if k in llm_patch:
                        llm_conf[k] = llm_patch[k]
                for prov in ("anthropic","openai","ollama","gemini"):
                    if prov in llm_patch:
                        llm_conf.setdefault(prov, {}).update(llm_patch[prov])
                changed = True
            if changed: save_config()
            self.send_json(200, {"ok": True,
                "poll_interval_seconds": config.get("poll_interval_seconds", 1800),
                "pause_escalate_minutes": config.get("pause_escalate_minutes", 30)}); return

        if not path.startswith("/api/printers/"):
            self.send_response(404); self.end_headers(); return

        pid  = path[len("/api/printers/"):]
        body = self._read_body()
        if not pid:
            self.send_json(400, {"error": "printer id required"}); return

        found = False
        for p in config.get("printers", []):
            if p["id"] == pid:
                if "name" in body:
                    p["name"] = body["name"].strip()
                if "host" in body:
                    p["host"] = body["host"].strip().rstrip("/")
                if "enabled" in body:
                    p["enabled"] = bool(body["enabled"])
                if "api_token" in body:
                    p["api_token"] = body["api_token"].strip()
                found = True
                save_config()
                # If re-enabled, ensure poll thread is running
                if p.get("enabled", True):
                    start_printer_thread(p)
                self.send_json(200, {"ok": True, "printer": p})
                break

        if not found:
            self.send_json(404, {"error": f"Printer '{pid}' not found"})

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

def cli_configure_llm():
    print()
    print("  +-------------------------------------------------+")
    print("  |   Configure LLM Chat Assistant                  |")
    print("  +-------------------------------------------------+")
    print()
    load_config()
    llm  = config.get("llm", {})
    prov = llm.get("provider","anthropic")
    print(f"  Current: {'ENABLED' if llm.get('enabled') else 'DISABLED'} | Provider: {prov}")
    print()
    ans = input("  Enable LLM chat? [y/N]: ").strip().lower()
    if ans not in ("y","yes"):
        config.setdefault("llm",{})["enabled"] = False
        save_config()
        print("  LLM chat disabled and saved.")
        return
    print("\n  Providers:")
    print("    1. anthropic  — Anthropic Claude (cloud)")
    print("    2. openai     — OpenAI-compatible  (Groq, Mistral, LM Studio, Together...)")
    print("    3. ollama     — Ollama (local, no API key needed)")
    print("    4. gemini     — Google Gemini (free tier: 1500 req/day)")
    choice = input("  Select [1/2/3/4] (default 1): ").strip()
    prov_map = {"1":"anthropic","2":"openai","3":"ollama","4":"gemini",
                "anthropic":"anthropic","openai":"openai",
                "ollama":"ollama","gemini":"gemini"}
    prov = prov_map.get(choice, "anthropic")
    upd  = {"enabled": True, "provider": prov}
    if prov == "anthropic":
        key   = input("  Anthropic API key (sk-ant-...): ").strip()
        model = input("  Model [claude-haiku-4-5-20251001]: ").strip() or "claude-haiku-4-5-20251001"
        upd["anthropic"] = {"api_key": key, "model": model}
    elif prov == "openai":
        base  = input("  Base URL [https://api.openai.com/v1]: ").strip() or "https://api.openai.com/v1"
        key   = input("  API key: ").strip()
        model = input("  Model [gpt-4o-mini]: ").strip() or "gpt-4o-mini"
        upd["openai"] = {"api_key": key, "base_url": base, "model": model}
    elif prov == "ollama":
        base  = input("  Ollama URL [http://localhost:11434]: ").strip() or "http://localhost:11434"
        model = input("  Model [llama3.2]: ").strip() or "llama3.2"
        upd["ollama"] = {"base_url": base, "model": model}
    elif prov == "gemini":
        print("  Get a free API key at: https://aistudio.google.com")
        key   = input("  Gemini API key (AIza...): ").strip()
        model = input("  Model [gemini-2.0-flash]: ").strip() or "gemini-2.0-flash"
        upd["gemini"] = {"api_key": key, "model": model}
    hist = input("\n  Persist chat history across restarts? [Y/n]: ").strip().lower()
    upd["history_enabled"] = hist not in ("n","no")
    max_h = input("  Max messages to keep [100]: ").strip()
    upd["history_max_messages"] = int(max_h) if max_h.isdigit() else 100
    cfg_llm = config.setdefault("llm",{})
    for k, v in upd.items():
        if isinstance(v, dict):
            cfg_llm.setdefault(k, {}).update(v)
        else:
            cfg_llm[k] = v
    save_config()
    print(f"\n  LLM configured: {prov} | history: {'on' if upd['history_enabled'] else 'off'}")
    print("  Restart monitor_server.py to apply.\n")

def cli_enable_tier3():
    load_config()
    print()
    print("  +--------------------------------------------------+")
    print("  |  Enable Tier 3 (Irreversible Actions)            |")
    print("  |  cancel_print / delete_file / emergency_stop     |")
    print("  |  home_axes                                        |")
    print("  +--------------------------------------------------+")
    print()
    t3 = config.get("agent", {}).get("tier3", {})
    if t3.get("enabled") and t3.get("expires_at"):
        print(f"  Tier 3 currently ACTIVE until: {t3['expires_at'][:16]}")
    else:
        print("  Tier 3 currently: INACTIVE")
    print()
    print("  WARNING: Tier 3 allows the LLM to cancel prints,")
    print("  delete files, and perform emergency stops.")
    print("  These actions are IRREVERSIBLE.")
    print()
    yn = input("  Enable Tier 3? [y/N]: ").strip().lower()
    if yn not in ("y", "yes"):
        print("  Aborted."); return
    raw = input("  Duration in hours [24] (max 168 = 7 days): ").strip()
    hours = int(raw) if raw.isdigit() else 24
    hours = max(1, min(168, hours))
    enable_tier3(hours)
    from datetime import datetime, timedelta
    exp = (datetime.now() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    print(f"  Tier 3 ENABLED for {hours}h — expires {exp}")
    print("  Restart monitor_server.py to apply (or it takes effect live).")
    print()

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if ADD_PRINTER_MODE:
        cli_add_printer()
        sys.exit(0)
    if CONFIGURE_LLM_MODE:
        cli_configure_llm()
        sys.exit(0)
    if ENABLE_TIER3_MODE:
        cli_enable_tier3()
        sys.exit(0)

    load_config()
    load_chat_history()

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
    llm_cfg = config.get("llm",{})
    if llm_cfg.get("enabled"):
        _prov  = llm_cfg.get("provider","anthropic")
        _model = llm_cfg.get(_prov,{}).get("model","?")
        _prov_labels = {"anthropic":"Anthropic","openai":"OpenAI-compat",
                        "ollama":"Ollama (local)","gemini":"Gemini (free)"}
        _ls    = f"ON  ({_prov_labels.get(_prov,_prov)} / {_model})"
    else:
        _ls    = "OFF  (run: python3 monitor_server.py configure-llm)"
    print(f"  AI Chat  : {_ls}")
    _tier = get_agent_tier()
    _t3   = config.get("agent",{}).get("tier3",{})
    _t3s  = f" [Tier 3 active until {_t3['expires_at'][:16]}]" if _t3.get("enabled") else ""
    print(f"  Agent    : Tier {_tier}{_t3s}  |  Trust: {'ON' if is_trust_active() else 'off'}")
    print(f"  enable-t3: python3 monitor_server.py enable-tier3")
    print(f"  Config   : monitor_config.json")
    print(f"  Add more : python3 monitor_server.py add-printer")
    print(f"  LLM setup: python3 monitor_server.py configure-llm")
    print(f"  {sep}")
    print(f"\n  Open  http://localhost:{PORT}  in your browser.")
    print(f"  Ctrl+C to stop.\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
