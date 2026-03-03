#!/usr/bin/env bash
# Self-elevate permissions
chmod +x "$0" 2>/dev/null || true
# =============================================================================
#  fluidd-klipper-mcp — Interactive Setup Script (Multi-Printer)
# =============================================================================

set -euo pipefail

BOLD="\033[1m"; DIM="\033[2m"; RED="\033[0;31m"; GREEN="\033[0;32m"
YELLOW="\033[0;33m"; CYAN="\033[0;36m"; WHITE="\033[0;37m"; RESET="\033[0m"

header()  { echo -e "\n${BOLD}${CYAN}━━━  $1  ━━━${RESET}"; }
info()    { echo -e "${WHITE}  $1${RESET}"; }
success() { echo -e "${GREEN}  ✅  $1${RESET}"; }
warn()    { echo -e "${YELLOW}  ⚠️   $1${RESET}"; }
error()   { echo -e "${RED}  ❌  $1${RESET}"; }
ask()     { echo -e "${BOLD}${CYAN}  ➜  $1${RESET}"; }

prompt_default() {
  local _var=$1 _question=$2 _default=$3 _input
  ask "${_question}"
  echo -en "     ${DIM}[default: ${_default}]${RESET} : "
  read -r _input
  [[ -z "${_input}" ]] && _input="${_default}"
  eval "${_var}=\"\${_input}\""
}

prompt_required() {
  local _var=$1 _question=$2 _input
  while true; do
    ask "${_question}"
    echo -n "     : "
    read -r _input
    [[ -n "${_input}" ]] && break
    warn "This field is required."
  done
  eval "${_var}=\"\${_input}\""
}

prompt_secret() {
  local _var=$1 _question=$2 _default=$3 _input
  ask "${_question}"
  echo -en "     ${DIM}[leave blank to skip]${RESET} : "
  read -rs _input
  echo
  [[ -z "${_input}" ]] && _input="${_default}"
  eval "${_var}=\"\${_input}\""
}

confirm() {
  local message=$1
  echo -e "\n${BOLD}${YELLOW}  ❓  ${message} [y/N]${RESET} "
  echo -n "     : "
  read -r answer
  case "${answer}" in [Yy]|[Yy][Ee][Ss]) return 0 ;; *) return 1 ;; esac
}

detect_os() {
  case "$(uname -s)" in
    Darwin) echo "mac" ;; Linux) echo "linux" ;;
    CYGWIN*|MINGW*|MSYS*) echo "windows" ;; *) echo "unknown" ;;
  esac
}

claude_config_path() {
  local os=$1
  case "${os}" in
    mac)     echo "${HOME}/Library/Application Support/Claude/claude_desktop_config.json" ;;
    linux)   echo "${HOME}/.config/Claude/claude_desktop_config.json" ;;
    windows) echo "${APPDATA}/Claude/claude_desktop_config.json" ;;
    *)       echo "${HOME}/.config/Claude/claude_desktop_config.json" ;;
  esac
}

INSTALL_DIR="${HOME}/fluidd-klipper-mcp"

# ─────────────────────────────────────────────────────────────────────────────
# Subcommand: add-printer
# Delegates to the Python CLI in monitor_server.py
# ─────────────────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "add-printer" ]]; then
  CONFIG_FILE="${2:-${INSTALL_DIR}/monitor_config.json}"
  SCRIPT="${INSTALL_DIR}/monitor_server.py"
  if [[ ! -f "${SCRIPT}" ]]; then
    error "monitor_server.py not found at ${INSTALL_DIR}."
    error "Run full setup first, or pass the correct install path:"
    error "  ./setup.sh add-printer /path/to/monitor_config.json"
    exit 1
  fi
  exec python3 "${SCRIPT}" add-printer
fi

# ─────────────────────────────────────────────────────────────────────────────
check_prereqs() {
  header "Step 0 — Checking Prerequisites"
  local ok=true
  for cmd in git docker python3; do
    if command -v "${cmd}" &>/dev/null; then
      success "${cmd} found ($(${cmd} --version 2>&1 | head -1))"
    else
      error "${cmd} not found — please install it before continuing."
      ok=false
    fi
  done
  if docker info &>/dev/null 2>&1; then
    success "Docker daemon is running"
  else
    error "Docker daemon is not running. Please start Docker Desktop."
    ok=false
  fi
  if docker mcp version &>/dev/null 2>&1; then
    success "Docker MCP plugin found"
  else
    warn "docker mcp plugin not found. Secret management step will be skipped."
  fi
  if [[ "${ok}" == false ]]; then
    error "One or more prerequisites are missing. Please fix them and re-run."
    exit 1
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
step_clone_and_build() {
  header "Step 1 — Clone Repository & Build Docker Image"
  prompt_default INSTALL_DIR "Where should the repo be cloned?" "${INSTALL_DIR}"

  if [[ -d "${INSTALL_DIR}" ]]; then
    warn "Directory ${INSTALL_DIR} already exists."
    if confirm "Pull latest changes instead of fresh clone?"; then
      git -C "${INSTALL_DIR}" pull
      success "Repository updated."
    else
      info "Using existing directory as-is."
    fi
  else
    if confirm "Clone https://github.com/block0ps/fluidd-klipper-mcp into ${INSTALL_DIR}?"; then
      git clone https://github.com/block0ps/fluidd-klipper-mcp.git "${INSTALL_DIR}"
      success "Repository cloned to ${INSTALL_DIR}"
    else
      error "Cannot continue without the repository. Exiting."
      exit 1
    fi
  fi

  echo
  if confirm "Build Docker image 'fluidd-klipper-mcp-server:latest'?"; then
    docker build -t fluidd-klipper-mcp-server:latest "${INSTALL_DIR}"
    success "Docker image built."
  else
    warn "Skipping Docker build. Run manually:\n     docker build -t fluidd-klipper-mcp-server:latest ${INSTALL_DIR}"
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Collect one printer interactively. Outputs to PRINTER_NAME, PRINTER_HOST,
# PRINTER_TOKEN, PRINTER_FILAMENT, PRINTER_POWER, PRINTER_WATTS, PRINTER_MARKUP
# ─────────────────────────────────────────────────────────────────────────────
collect_one_printer() {
  local index=$1
  echo
  info "${BOLD}Printer #${index}${RESET}"
  prompt_required PRINTER_NAME  "  Name for this printer (e.g. 'Ender 3 Pro')"
  prompt_required PRINTER_HOST  "  Host URL (e.g. http://192.168.1.100)"
  PRINTER_HOST="${PRINTER_HOST%/}"
  prompt_secret   PRINTER_TOKEN "  Moonraker API token (if auth is enabled)" ""
  prompt_default  PRINTER_FILAMENT "  Filament cost per kg (USD)" "25.0"
  prompt_default  PRINTER_POWER    "  Electricity cost per kWh (USD)" "0.12"
  prompt_default  PRINTER_WATTS_V  "  Printer average wattage" "150.0"
  prompt_default  PRINTER_MARKUP   "  Markup percentage" "30.0"

  # Test connectivity
  echo
  info "Testing connectivity to ${PRINTER_HOST} …"
  if curl -sf --max-time 5 "${PRINTER_HOST}/server/info" -o /dev/null 2>&1; then
    success "Printer '${PRINTER_NAME}' at ${PRINTER_HOST} is reachable ✅"
  else
    warn "Could not reach ${PRINTER_HOST} — ensure it is on the same network."
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
step_set_secrets() {
  header "Step 2 — Configure Printers & Secrets"

  if ! docker mcp version &>/dev/null 2>&1; then
    warn "docker mcp plugin not found — skipping Docker secret setup."
    warn "Set PRINTER_HOSTS env var manually (JSON array) when running the container."
    return
  fi

  info "You will be prompted for each printer. Press Enter to use defaults."
  echo

  # ── Collect printers in a loop ──────────────────────────────────────────────
  local printers_json="["
  local first=true
  local printer_index=1

  while true; do
    collect_one_printer "${printer_index}"

    PRINTER_ID=$(echo "${PRINTER_NAME}" | tr '[:upper:]' '[:lower:]' | tr ' /' '__')

    if [[ "${first}" == true ]]; then
      printers_json="${printers_json}{\"id\":\"${PRINTER_ID}\",\"name\":\"${PRINTER_NAME}\",\"host\":\"${PRINTER_HOST}\",\"enabled\":true,\"api_token\":\"${PRINTER_TOKEN}\"}"
      first=false
    else
      printers_json="${printers_json},{\"id\":\"${PRINTER_ID}\",\"name\":\"${PRINTER_NAME}\",\"host\":\"${PRINTER_HOST}\",\"enabled\":true,\"api_token\":\"${PRINTER_TOKEN}\"}"
    fi

    printer_index=$((printer_index + 1))
    echo
    if ! confirm "Add another printer?"; then
      break
    fi
  done

  printers_json="${printers_json}]"

  # Use the last printer's cost settings as global defaults (or first printer's)
  echo
  info "About to set Docker MCP secrets:"
  info "  PRINTER_HOSTS          = ${printers_json}"
  info "  FILAMENT_COST_PER_KG   = ${PRINTER_FILAMENT}"
  info "  POWER_COST_PER_KWH     = ${PRINTER_POWER}"
  info "  PRINTER_WATTS          = ${PRINTER_WATTS_V}"
  info "  MARKUP_PERCENTAGE      = ${PRINTER_MARKUP}"

  if confirm "Write these secrets to Docker MCP?"; then
    docker mcp secret set PRINTER_HOSTS="${printers_json}"
    docker mcp secret set FILAMENT_COST_PER_KG="${PRINTER_FILAMENT}"
    docker mcp secret set POWER_COST_PER_KWH="${PRINTER_POWER}"
    docker mcp secret set PRINTER_WATTS="${PRINTER_WATTS_V}"
    docker mcp secret set MARKUP_PERCENTAGE="${PRINTER_MARKUP}"
    success "All secrets saved."
    echo
    info "Verifying secrets:"
    docker mcp secret list
  else
    warn "Skipping secret setup. Set PRINTER_HOSTS manually:\n     docker mcp secret set PRINTER_HOSTS='[{\"name\":\"...\",\"host\":\"http://...\"}]'"
  fi

  # ── Write monitor_config.json ────────────────────────────────────────────────
  local config_path="${INSTALL_DIR}/monitor_config.json"
  echo
  if confirm "Write initial monitor_config.json to ${config_path}?"; then
    python3 - "${config_path}" "${printers_json}" <<'PYEOF'
import sys, json, os

config_path = sys.argv[1]
printers_raw = sys.argv[2]

printers = json.loads(printers_raw)

# Load existing config if present (to preserve alert channel settings)
existing = {}
if os.path.exists(config_path):
    with open(config_path) as f:
        existing = json.load(f)

existing["printers"] = printers
# Remove legacy single-printer key if migrating
existing.pop("printer_host", None)

with open(config_path, "w") as f:
    json.dump(existing, f, indent=2)

print(f"  Written {len(printers)} printer(s) to {config_path}")
PYEOF
    success "monitor_config.json updated with ${printer_index-1} printer(s)."
    info "Edit ${config_path} to enable alert channels (ntfy, SMS, email, iMessage)."
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
step_register_catalog() {
  header "Step 3 — Register Docker MCP Catalog"

  local catalog_dir="${HOME}/.docker/mcp/catalogs"
  local catalog_dest="${catalog_dir}/custom.yaml"
  local catalog_src="${INSTALL_DIR}/custom.yaml"

  if [[ ! -f "${catalog_src}" ]]; then
    warn "custom.yaml not found at ${catalog_src}. Skipping catalog registration."
    return
  fi

  if [[ -f "${catalog_dest}" ]]; then
    success "Catalog already present at ${catalog_dest}"
    if confirm "Overwrite with latest version?"; then
      cp "${catalog_src}" "${catalog_dest}"
      success "Catalog updated."
    fi
    return
  fi

  if confirm "Copy custom.yaml MCP catalog to ${catalog_dest}?"; then
    mkdir -p "${catalog_dir}"
    cp "${catalog_src}" "${catalog_dest}"
    success "Catalog registered."
  fi

  # Registry entry
  local registry_file="${HOME}/.docker/mcp/registry.yaml"
  if [[ -f "${registry_file}" ]]; then
    if grep -q "fluidd-klipper" "${registry_file}" 2>/dev/null; then
      success "Registry entry already present."
    else
      if confirm "Add fluidd-klipper entry to ${registry_file}?"; then
        python3 - "${registry_file}" <<'PYEOF'
import sys, os
path = sys.argv[1]
with open(path) as f: content = f.read()
if "fluidd-klipper" not in content:
    with open(path, "a") as f:
        f.write('\n  fluidd-klipper:\n    ref: ""\n')
    print("  Registry entry added.")
else:
    print("  Already present.")
PYEOF
        success "Registry entry added."
      fi
    fi
  else
    if confirm "Create ${registry_file} with fluidd-klipper entry?"; then
      mkdir -p "$(dirname "${registry_file}")"
      cat > "${registry_file}" <<EOF
registry:
  fluidd-klipper:
    ref: ""
EOF
      success "Registry file created."
    fi
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
step_configure_claude() {
  header "Step 4 — Configure Claude Desktop"

  local os config_path home_escaped
  os=$(detect_os)
  config_path=$(claude_config_path "${os}")
  home_escaped="${HOME}"
  [[ "${os}" == "windows" ]] && home_escaped=$(echo "${HOME}" | sed 's/\//\\\\\//g')

  info "Detected OS     : ${os}"
  info "Config location : ${config_path}"

  if [[ ! -f "${config_path}" ]]; then
    warn "Claude Desktop config not found at: ${config_path}"
    if confirm "Create a new config with the MCP gateway entry?"; then
      mkdir -p "$(dirname "${config_path}")"
      cat > "${config_path}" <<EOF
{
  "mcpServers": {
    "mcp-toolkit-gateway": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", "${home_escaped}/.docker/mcp:/mcp",
        "docker/mcp-gateway",
        "--catalog=/mcp/catalogs/docker-mcp.yaml",
        "--catalog=/mcp/catalogs/custom.yaml",
        "--config=/mcp/config.yaml",
        "--registry=/mcp/registry.yaml",
        "--tools-config=/mcp/tools.yaml",
        "--transport=stdio"
      ]
    }
  }
}
EOF
      success "Claude Desktop config created."
    else
      warn "Skipping. Add the custom.yaml catalog entry manually."
    fi
    return
  fi

  if grep -q "custom.yaml" "${config_path}" 2>/dev/null; then
    success "custom.yaml catalog already present in Claude Desktop config."
    return
  fi

  info "Found existing config. Will add custom catalog entry."
  cp "${config_path}" "${config_path}.backup.$(date +%Y%m%d_%H%M%S)"
  success "Backup saved."

  if confirm "Patch Claude Desktop config to add custom.yaml catalog?"; then
    python3 - "${config_path}" "${home_escaped}" <<'PYEOF'
import sys, json
config_path, home = sys.argv[1], sys.argv[2]
with open(config_path) as f: config = json.load(f)
servers = config.setdefault("mcpServers", {})
gateway = servers.get("mcp-toolkit-gateway", {})
args    = gateway.get("args", [])
custom  = "--catalog=/mcp/catalogs/custom.yaml"
if custom not in args:
    inserted = False
    for i, a in enumerate(args):
        if "docker-mcp.yaml" in a:
            args.insert(i+1, custom); inserted = True; break
    if not inserted:
        for i, a in enumerate(args):
            if "--config" in a:
                args.insert(i, custom); inserted = True; break
    if not inserted: args.append(custom)
gateway["args"] = args
servers["mcp-toolkit-gateway"] = gateway
config["mcpServers"] = servers
with open(config_path, "w") as f: json.dump(config, f, indent=2)
print("  Config patched successfully.")
PYEOF
    success "Claude Desktop config updated."
  else
    warn "Skipping. Add this manually to the args array:"
    info '  "--catalog=/mcp/catalogs/custom.yaml"'
  fi
}

# ─────────────────────────────────────────────────────────────────────────────
step_finish() {
  header "Step 5 — Final Checks & Next Steps"

  if docker image inspect fluidd-klipper-mcp-server:latest &>/dev/null 2>&1; then
    success "Docker image fluidd-klipper-mcp-server:latest is present"
  else
    warn "Docker image not found. Run:\n     docker build -t fluidd-klipper-mcp-server:latest ${INSTALL_DIR}"
  fi

  if [[ -f "${HOME}/.docker/mcp/catalogs/custom.yaml" ]]; then
    success "MCP catalog registered at ~/.docker/mcp/catalogs/custom.yaml"
  else
    warn "MCP catalog not found. Copy custom.yaml to ~/.docker/mcp/catalogs/"
  fi

  echo
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}${GREEN}  🎉  Setup Complete!${RESET}"
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo
  info "Start the live monitor:"
  info "  python3 ${INSTALL_DIR}/monitor_server.py"
  echo
  info "Add another printer later:"
  info "  ./setup.sh add-printer"
  info "  — or —"
  info "  python3 ${INSTALL_DIR}/monitor_server.py add-printer"
  echo
  info "Next steps for Claude Desktop:"
  info "  1. Quit Claude Desktop completely (Cmd+Q / File → Quit)"
  info "  2. Relaunch Claude Desktop"
  info "  3. The printer tools will appear in the tools panel"
  echo
  info "Try asking Claude:"
  info "  • \"List all my printers\""
  info "  • \"What's Printer 1 doing right now?\""
  info "  • \"Pause the print on [printer name]\""
  info "  • \"How much did this print cost?\""
  echo
  info "GitHub : https://github.com/block0ps/fluidd-klipper-mcp"
  echo
}

# ─────────────────────────────────────────────────────────────────────────────
main() {
  clear
  echo
  echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}${CYAN}║   Fluidd / Klipper MCP Server — Interactive Setup        ║${RESET}"
  echo -e "${BOLD}${CYAN}║   Multi-Printer Edition                                  ║${RESET}"
  echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${RESET}"
  echo
  echo -e "${DIM}  Supports multiple printers. Add more any time with:${RESET}"
  echo -e "${DIM}  ./setup.sh add-printer${RESET}"
  echo

  if ! confirm "Ready to begin setup?"; then
    echo "Exiting. Run this script again whenever you're ready."
    exit 0
  fi

  echo
  info "Which steps would you like to run?"
  RUN_PREREQS=true; RUN_CLONE=true; RUN_SECRETS=true; RUN_CATALOG=true; RUN_CLAUDE=true

  if confirm "Skip prerequisite checks? (recommended: No)"; then RUN_PREREQS=false; fi
  if confirm "Skip clone & Docker build? (only if already done)"; then RUN_CLONE=false; fi
  if confirm "Skip printer config & secret setup?"; then RUN_SECRETS=false; fi
  if confirm "Skip MCP catalog registration?"; then RUN_CATALOG=false; fi
  if confirm "Skip Claude Desktop config patch?"; then RUN_CLAUDE=false; fi

  echo
  if confirm "Proceed with the selected steps?"; then
    [[ "${RUN_PREREQS}" == true ]] && check_prereqs
    [[ "${RUN_CLONE}"   == true ]] && step_clone_and_build
    [[ "${RUN_SECRETS}" == true ]] && step_set_secrets
    [[ "${RUN_CATALOG}" == true ]] && step_register_catalog
    [[ "${RUN_CLAUDE}"  == true ]] && step_configure_claude
    step_finish
  else
    echo "Exiting. No changes were made."
    exit 0
  fi
}

main "$@"
