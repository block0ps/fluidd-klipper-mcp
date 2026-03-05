# Fleet AI — Free & Local LLM Setup

The Fleet AI assistant works with four providers. Two are completely free with no credit card required, one is free-tier cloud, and one is local/offline. All support agentic tool use (the LLM can actually control your printers, not just chat).

---

## Recommended: Ollama (local, completely free forever)

Runs entirely on your machine. No account, no API key, no internet dependency, no usage limits.

**Requirements:** macOS/Linux host running the monitor server. Apple Silicon M1+ or a GPU gives best performance, but CPU-only works for smaller models.

### Setup

```bash
# macOS
brew install ollama

# Linux
curl -fsSL https://ollama.com/install.sh | sh
```

```bash
# Pull a model — pick one based on your hardware:
ollama pull llama3.2          # 2 GB  — fast on Apple Silicon, good for status/control
ollama pull llama3.1:8b       # 5 GB  — recommended for agentic tool calling (more reliable)
ollama pull mistral-nemo      # 7 GB  — strong reasoning, reliable tool calling
ollama pull qwen2.5:7b        # 5 GB  — excellent for structured outputs / tool use

# Start the Ollama server (auto-starts on macOS after install)
ollama serve
```

### Configure in Fleet AI

Open the chat bubble → ⚙ → Provider: **Ollama (local / offline)**
- Base URL: `http://localhost:11434`
- Model: `llama3.1:8b` (recommended for tool calling) or `llama3.2`

Or via CLI:
```bash
python3 monitor_server.py configure-llm
# Select 3 (Ollama)
```

**Tool calling note:** Models below 7B parameters (e.g. `llama3.2:1b`, `phi3-mini`) may give unreliable results when executing printer actions. Use `llama3.1:8b` or larger if you want the agent to actually pause/resume/adjust printers.

---

## Groq (free tier, extremely fast cloud inference)

14,400 requests/day free. Uses the same OpenAI-compatible adapter already built in.
Responses arrive in ~1 second — faster than a local model on most hardware.

**Get a free API key:** https://console.groq.com (no credit card required)

### Configure in Fleet AI

Open the chat bubble → ⚙ → Provider: **OpenAI-compatible**
- Base URL: `https://api.groq.com/openai/v1`
- API Key: your Groq key (`gsk_...`)
- Model: `llama-3.3-70b-versatile` (best balance) or `llama-3.1-8b-instant` (fastest)

**Free tier models on Groq:**
| Model | Speed | Best for |
|---|---|---|
| `llama-3.3-70b-versatile` | Fast | Complex reasoning, tool calling |
| `llama-3.1-8b-instant` | Very fast | Quick status queries |
| `mixtral-8x7b-32768` | Fast | Long context (alert history) |

---

## Google Gemini (free tier, 1,500 req/day)

**Get a free API key:** https://aistudio.google.com (no billing required, just a Google account)

### Configure in Fleet AI

Open the chat bubble → ⚙ → Provider: **Google Gemini (free tier)**
- API Key: your Gemini key (`AIza...`)
- Model: `gemini-2.0-flash` (recommended) or `gemini-1.5-flash`

Or via CLI:
```bash
python3 monitor_server.py configure-llm
# Select 4 (Gemini)
```

**Free tier limits:**
| Model | Requests/day | Notes |
|---|---|---|
| `gemini-2.0-flash` | 1,500 | Best overall for this use case |
| `gemini-1.5-flash` | 1,500 | Slightly older, still excellent |
| `gemini-1.5-pro` | 50 | Overkill for printer monitoring |

At typical monitor usage (a few queries per print job) you will not hit the daily limit.

---

## OpenRouter (free models, no rate limits on free tier)

Aggregates many providers. Has genuinely free models with no daily caps.
Uses the OpenAI-compatible adapter.

**Get a free API key:** https://openrouter.ai (no credit card for free models)

### Configure in Fleet AI

Open the chat bubble → ⚙ → Provider: **OpenAI-compatible**
- Base URL: `https://openrouter.ai/api/v1`
- API Key: your OpenRouter key (`sk-or-...`)
- Model: `google/gemma-3-27b-it:free` or `meta-llama/llama-3.2-3b-instruct:free`

**Current free models on OpenRouter** (check https://openrouter.ai/models?q=free for the latest):
- `google/gemma-3-27b-it:free`
- `meta-llama/llama-3.2-3b-instruct:free`
- `mistralai/mistral-7b-instruct:free`
- `microsoft/phi-3-mini-128k-instruct:free`

---

## Provider comparison

| Provider | Cost | Speed | Tool calling | Privacy | Setup |
|---|---|---|---|---|---|
| Ollama | Free forever | Local GPU/CPU | ✅ (llama3.1:8b+) | 🔒 100% local | Medium |
| Groq | Free (14K/day) | ⚡ Very fast | ✅ Excellent | Cloud | Easy |
| Gemini | Free (1.5K/day) | Fast | ✅ Good | Cloud/Google | Easy |
| OpenRouter | Free models | Medium | ⚠️ Model-dependent | Cloud | Easy |
| Anthropic | Paid | Fast | ✅ Best-in-class | Cloud | Easy |
| OpenAI | Paid | Fast | ✅ Excellent | Cloud | Easy |

**My recommendation:**
- **Primary:** Ollama with `llama3.1:8b` — completely self-contained, no external dependencies
- **Cloud fallback:** Groq — fastest free cloud option, excellent tool reliability

---

## Agentic tool calling (printer control)

The Fleet AI assistant can do more than answer questions — it can actually control your printers. The capabilities depend on which **Tier** you've configured:

| Tier | Tools available | Notes |
|---|---|---|
| **Tier 1** | Status, file listing, alert log | Read-only, safe |
| **Tier 2** | + Pause, resume, set temps, speed, flow | Reversible, default |
| **Tier 3** | + Cancel print, delete files, emergency stop | Irreversible — requires explicit enable |

### Enabling Tier 3 (irreversible actions)

Tier 3 can only be enabled via CLI or the web settings drawer — the LLM cannot enable it for itself.

```bash
python3 monitor_server.py enable-tier3
# Follow prompts — set duration (1–168 hours, default 24)
# Tier 3 auto-reverts after the window expires
```

Or via Settings → Agent section → Tier 3 (when active, shows countdown + Revoke button).

### Trust mode

When the LLM proposes an action, you'll see a confirmation card with optional trust mode:

- **Single action:** Confirm / Deny + optional "Trust LLM for X hours" checkbox
- **Multi-printer action:** One confirmation → 30-second countdown with Abort option
- **Trust mode active:** Actions in the current tier execute immediately without confirmation

Trust mode is set per-session through the confirmation dialog and expires automatically.

---

## Local Reasoning Models (Ollama)

Reasoning models emit `<think>...</think>` blocks before answering. The monitor
strips these automatically so they never appear in the chat UI.

### Qwen3 32B (Recommended for high-end hardware)
```bash
ollama pull qwen3:32b     # ~20GB Q4 — fits comfortably in 128GB RAM
```
- Best tool-calling accuracy of any local model under 70B
- Excellent at multi-step reasoning (diagnosing print issues, etc.)
- M1 Ultra / M2 Ultra / M3 Ultra with ≥64GB RAM recommended
- Append `/no_think` to model name for faster non-reasoning responses:
  `qwen3:32b/no_think`

### DeepSeek-R1
```bash
ollama pull deepseek-r1:8b    # ~5GB — reliable tool calling
ollama pull deepseek-r1:14b   # ~9GB — better reasoning
ollama pull deepseek-r1:70b   # ~43GB — near GPT-4 level
```

### Hardware guidance (Apple Silicon)
| Model       | VRAM/RAM needed | M1 Pro 32GB | M1 Max 64GB | M1 Ultra 128GB |
|-------------|----------------|-------------|-------------|----------------|
| qwen3:8b    | ~5GB           | ✅           | ✅           | ✅              |
| qwen3:14b   | ~9GB           | ✅           | ✅           | ✅              |
| qwen3:32b   | ~20GB          | ⚠️ tight    | ✅           | ✅              |
| qwen3:70b   | ~43GB          | ❌           | ⚠️ tight    | ✅              |
| deepseek-r1:70b | ~43GB     | ❌           | ⚠️ tight    | ✅              |
