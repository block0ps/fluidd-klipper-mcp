#!/usr/bin/env bash
# =============================================================================
#  fluidd-klipper-mcp — Interactive Setup Script
#  Automates all Quick Start steps from the README
# =============================================================================

set -euo pipefail

# ── Colors & formatting ───────────────────────────────────────────────────────
BOLD="\033[1m"
DIM="\033[2m"
RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
CYAN="\033[0;36m"
WHITE="\033[0;37m"
RESET="\033[0m"

# ── Helpers ───────────────────────────────────────────────────────────────────
header()  { echo -e "\n${BOLD}${CYAN}━━━  $1  ━━━${RESET}"; }
info()    { echo -e "${WHITE}  $1${RESET}"; }
success() { echo -e "${GREEN}  ✅  $1${RESET}"; }
warn()    { echo -e "${YELLOW}  ⚠️   $1${RESET}"; }
error()   { echo -e "${RED}  ❌  $1${RESET}"; }
ask()     { echo -e "${BOLD}${CYAN}  ➜  $1${RESET}"; }

# Prompt with a default value. Usage: prompt_default VAR "Question" "default"
prompt_default() {
  local _var=$1
  local _question=$2
  local _default=$3
  local _input
  ask "${_question}"
  echo -en "     ${DIM}[default: ${_default}]${RESET} : "
  read -r _input
  if [[ -z "${_input}" ]]; then
    _input="${_default}"
  fi
  eval "${_var}=\"\${_input}\""
}

# Prompt required (no default). Usage: prompt_required VAR "Question"
prompt_required() {
  local _var=$1
  local _question=$2
  local _input
  while true; do
    ask "${_question}"
    echo -n "     : "
    read -r _input
    if [[ -n "${_input}" ]]; then break; fi
    warn "This field is required."
  done
  eval "${_var}=\"\${_input}\""
}

# Prompt secret (masked input)
prompt_secret() {
  local _var=$1
  local _question=$2
  local _default=$3
  local _input
  ask "${_question}"
  echo -en "     ${DIM}[leave blank to skip / default: ${_default}]${RESET} : "
  read -rs _input
  echo
  if [[ -z "${_input}" ]]; then
    _input="${_default}"
  fi
  eval "${_var}=\"\${_input}\""
}

# Ask for yes/no confirmation before proceeding
confirm() {
  local message=$1
  echo -e "\n${BOLD}${YELLOW}  ❓  ${message} [y/N]${RESET} "
  echo -n "     : "
  read -r answer
  case "${answer}" in
    [Yy]|[Yy][Ee][Ss]) return 0 ;;
    *) return 1 ;;
  esac
}

# Detect OS
detect_os() {
  case "$(uname -s)" in
    Darwin) echo "mac" ;;
    Linux)  echo "linux" ;;
    CYGWIN*|MINGW*|MSYS*) echo "windows" ;;
    *) echo "unknown" ;;
  esac
}

# Get home dir (handles Windows via Git Bash)
get_home() {
  echo "${HOME}"
}

# Get Claude Desktop config path by OS
claude_config_path() {
  local os=$1
  case "${os}" in
    mac)     echo "${HOME}/Library/Application Support/Claude/claude_desktop_config.json" ;;
    linux)   echo "${HOME}/.config/Claude/claude_desktop_config.json" ;;
    windows) echo "${APPDATA}/Claude/claude_desktop_config.json" ;;
    *)       echo "${HOME}/.config/Claude/claude_desktop_config.json" ;;
  esac
}


# ── Global defaults (may be overridden interactively) ─────────────────
INSTALL_DIR="${HOME}/fluidd-klipper-mcp"
PRINTER_HOST=""

# ── Preflight checks ──────────────────────────────────────────────────────────
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

  # Check Docker is running
  if docker info &>/dev/null 2>&1; then
    success "Docker daemon is running"
  else
    error "Docker daemon is not running. Please start Docker Desktop."
    ok=false
  fi

  # Check docker mcp plugin
  if docker mcp version &>/dev/null 2>&1; then
    success "Docker MCP plugin found"
  else
    warn "docker mcp plugin not found. Secret management step will be skipped."
    warn "Install it from: https://docs.docker.com/desktop/extensions/marketplace/"
  fi

  if [[ "${ok}" == false ]]; then
    echo
    error "One or more prerequisites are missing. Please fix them and re-run."
    exit 1
  fi
}

# ── Step 1 — Clone & Build ────────────────────────────────────────────────────
step_clone_and_build() {
  header "Step 1 — Clone Repository & Build Docker Image"

  prompt_default INSTALL_DIR "Where should the repo be cloned?" "${INSTALL_DIR}"

  # Check if directory already exists
  if [[ -d "${INSTALL_DIR}" ]]; then
    warn "Directory ${INSTALL_DIR} already exists."
    if confirm "Pull latest changes instead of fresh clone?"; then
      info "Pulling latest changes..."
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

  # Build Docker image
  echo
  if confirm "Build Docker image 'fluidd-klipper-mcp-server:latest'? (may take 1–2 min)"; then
    info "Building Docker image..."
    docker build -t fluidd-klipper-mcp-server:latest "${INSTALL_DIR}"
    success "Docker image built: fluidd-klipper-mcp-server:latest"
  else
    warn "Skipping Docker build. You can run it manually:\n     docker build -t fluidd-klipper-mcp-server:latest ${INSTALL_DIR}"
  fi
}

# ── Step 2 — Set Secrets ──────────────────────────────────────────────────────
step_set_secrets() {
  header "Step 2 — Configure Printer Settings & Secrets"

  # Check if docker mcp is available
  if ! docker mcp version &>/dev/null 2>&1; then
    warn "docker mcp plugin not found — skipping secret setup."
    warn "Set these environment variables manually when running the container:"
    warn "  PRINTER_HOST, PRINTER_API_TOKEN, FILAMENT_COST_PER_KG, POWER_COST_PER_KWH, PRINTER_WATTS, MARKUP_PERCENTAGE"
    return
  fi

  info "You'll be prompted for each setting. Press Enter to use the default."
  echo

  # Printer host
  prompt_required PRINTER_HOST "Printer IP or URL (e.g. http://192.168.1.100)"
  # Strip trailing slash
  PRINTER_HOST="${PRINTER_HOST%/}"

  # API token (optional)
  prompt_secret PRINTER_API_TOKEN "Moonraker API token (if auth is enabled on your printer)" ""

  # Cost settings
  prompt_default FILAMENT_COST "Filament cost per kg (USD)" "25.0"
  prompt_default POWER_COST    "Electricity cost per kWh (USD)" "0.12"
  prompt_default PRINTER_WATTS "Printer average power draw (watts)" "150.0"
  prompt_default MARKUP_PCT    "Markup percentage for sale price calculation" "30.0"

  # Confirm before writing
  echo
  info "About to set the following Docker MCP secrets:"
  info "  PRINTER_HOST           = ${PRINTER_HOST}"
  info "  PRINTER_API_TOKEN      = ${PRINTER_API_TOKEN:-(not set)}"
  info "  FILAMENT_COST_PER_KG   = ${FILAMENT_COST}"
  info "  POWER_COST_PER_KWH     = ${POWER_COST}"
  info "  PRINTER_WATTS          = ${PRINTER_WATTS}"
  info "  MARKUP_PERCENTAGE      = ${MARKUP_PCT}"

  if confirm "Write these secrets to Docker MCP?"; then
    docker mcp secret set PRINTER_HOST="${PRINTER_HOST}"
    [[ -n "${PRINTER_API_TOKEN}" ]] && docker mcp secret set PRINTER_API_TOKEN="${PRINTER_API_TOKEN}"
    docker mcp secret set FILAMENT_COST_PER_KG="${FILAMENT_COST}"
    docker mcp secret set POWER_COST_PER_KWH="${POWER_COST}"
    docker mcp secret set PRINTER_WATTS="${PRINTER_WATTS}"
    docker mcp secret set MARKUP_PERCENTAGE="${MARKUP_PCT}"
    success "All secrets saved."
    echo
    info "Verifying secrets:"
    docker mcp secret list
  else
    warn "Skipping secret setup. Set them manually with: docker mcp secret set KEY=\"value\""
  fi
}

# ── Step 3 — Register Catalog ─────────────────────────────────────────────────
step_register_catalog() {
  header "Step 3 — Register Docker MCP Catalog"

  local catalog_dir="${HOME}/.docker/mcp/catalogs"
  local catalog_dest="${catalog_dir}/custom.yaml"
  local catalog_src="${INSTALL_DIR}/custom.yaml"
  local registry_file="${HOME}/.docker/mcp/registry.yaml"

  if [[ ! -f "${catalog_src}" ]]; then
    error "custom.yaml not found at ${catalog_src}"
    warn "Make sure the repo was cloned correctly."
    return
  fi

  info "Catalog source : ${catalog_src}"
  info "Catalog dest   : ${catalog_dest}"

  if confirm "Copy custom.yaml to ${catalog_dest}?"; then
    mkdir -p "${catalog_dir}"
    cp "${catalog_src}" "${catalog_dest}"
    success "Catalog copied to ${catalog_dest}"
  else
    warn "Skipping catalog registration."
    return
  fi

  # Update registry.yaml
  echo
  if [[ -f "${registry_file}" ]]; then
    if grep -q "fluidd-klipper" "${registry_file}" 2>/dev/null; then
      success "fluidd-klipper already present in ${registry_file}"
    else
      if confirm "Add fluidd-klipper entry to ${registry_file}?"; then
        # Append under registry: key using Python for safe YAML editing
        python3 - "${registry_file}" <<'PYEOF'
import sys, re

path = sys.argv[1]
with open(path, "r") as f:
    content = f.read()

entry = "\n  fluidd-klipper:\n    ref: \"\"\n"

if "registry:" in content:
    content = content.rstrip() + entry
else:
    content = content.rstrip() + "\nregistry:" + entry

with open(path, "w") as f:
    f.write(content)
print("  Registry updated.")
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

# ── Step 4 — Configure Claude Desktop ────────────────────────────────────────
step_configure_claude() {
  header "Step 4 — Configure Claude Desktop"

  local os
  os=$(detect_os)
  local config_path
  config_path=$(claude_config_path "${os}")

  info "Detected OS     : ${os}"
  info "Config location : ${config_path}"

  local home_escaped
  if [[ "${os}" == "windows" ]]; then
    home_escaped=$(echo "${HOME}" | sed 's/\//\\\\\//g')
  else
    home_escaped="${HOME}"
  fi

  # Build the catalog args snippet to add
  local custom_catalog_arg='"--catalog=/mcp/catalogs/custom.yaml"'

  # Check if config exists
  if [[ ! -f "${config_path}" ]]; then
    warn "Claude Desktop config not found at: ${config_path}"
    if confirm "Create a new config file with the MCP gateway entry?"; then
      mkdir -p "$(dirname "${config_path}")"
      cat > "${config_path}" <<EOF
{
  "mcpServers": {
    "mcp-toolkit-gateway": {
      "command": "docker",
      "args": [
        "run",
        "-i",
        "--rm",
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
      warn "Skipping Claude Desktop config. Add the custom.yaml catalog entry manually."
    fi
    return
  fi

  # Config exists — check if custom.yaml catalog is already in it
  if grep -q "custom.yaml" "${config_path}" 2>/dev/null; then
    success "custom.yaml catalog already present in Claude Desktop config."
    return
  fi

  info "Found existing config. Will add the custom catalog entry."

  # Backup first
  cp "${config_path}" "${config_path}.backup.$(date +%Y%m%d_%H%M%S)"
  success "Backup saved: ${config_path}.backup.*"

  if confirm "Patch Claude Desktop config at ${config_path} to add custom.yaml catalog?"; then
    python3 - "${config_path}" "${home_escaped}" <<'PYEOF'
import sys, json, os

config_path = sys.argv[1]
home = sys.argv[2]

with open(config_path, "r") as f:
    config = json.load(f)

servers = config.setdefault("mcpServers", {})
gateway = servers.get("mcp-toolkit-gateway", {})
args = gateway.get("args", [])

custom_catalog = "--catalog=/mcp/catalogs/custom.yaml"

if custom_catalog in args:
    print("  custom.yaml already present in args — no changes needed.")
    sys.exit(0)

# Find where to insert (after docker-mcp.yaml catalog if present, else before --config)
inserted = False
for i, arg in enumerate(args):
    if "docker-mcp.yaml" in arg:
        args.insert(i + 1, custom_catalog)
        inserted = True
        break

if not inserted:
    for i, arg in enumerate(args):
        if "--config" in arg:
            args.insert(i, custom_catalog)
            inserted = True
            break

if not inserted:
    args.append(custom_catalog)

# Also ensure the mcp-gateway entry has the right volume mount if missing
mcp_vol = f"-v"
mcp_vol_val = f"{home}/.docker/mcp:/mcp"
if mcp_vol_val not in args:
    # Find -v flags and check
    pass  # Don't auto-modify volume — too risky

gateway["args"] = args
servers["mcp-toolkit-gateway"] = gateway
config["mcpServers"] = servers

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

print("  Config patched successfully.")
PYEOF
    success "Claude Desktop config updated."
  else
    warn "Skipping config patch."
    info "Add this line manually to the args array in your Claude Desktop config:"
    info '  "--catalog=/mcp/catalogs/custom.yaml"'
    info "Config location: ${config_path}"
  fi
}

# ── Step 5 — Test & Finish ────────────────────────────────────────────────────
step_finish() {
  header "Step 5 — Final Checks & Next Steps"

  # Verify Docker image
  if docker image inspect fluidd-klipper-mcp-server:latest &>/dev/null 2>&1; then
    success "Docker image fluidd-klipper-mcp-server:latest is present"
  else
    warn "Docker image not found. Run: docker build -t fluidd-klipper-mcp-server:latest ${INSTALL_DIR:-./fluidd-klipper-mcp}"
  fi

  # Verify catalog
  if [[ -f "${HOME}/.docker/mcp/catalogs/custom.yaml" ]]; then
    success "MCP catalog registered at ~/.docker/mcp/catalogs/custom.yaml"
  else
    warn "MCP catalog not found. Copy custom.yaml to ~/.docker/mcp/catalogs/"
  fi

  # Quick printer connectivity test
  echo
  if [[ -n "${PRINTER_HOST:-}" ]]; then
    info "Testing connectivity to ${PRINTER_HOST} ..."
    if curl -sf --max-time 5 "${PRINTER_HOST}/server/info" -o /dev/null 2>&1; then
      success "Printer at ${PRINTER_HOST} is reachable ✅"
    else
      warn "Could not reach ${PRINTER_HOST} — ensure the printer is on the same network as this machine."
    fi
  fi

  echo
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo -e "${BOLD}${GREEN}  🎉  Setup Complete!${RESET}"
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
  echo
  info "Next steps:"
  info "  1. Quit Claude Desktop completely (Cmd+Q / File → Quit)"
  info "  2. Relaunch Claude Desktop"
  info "  3. The 25 printer tools will appear in the tools panel"
  echo
  info "Try asking Claude:"
  info "  • \"What's my printer doing right now?\""
  info "  • \"Are there any alerts on the printer?\""
  info "  • \"How much did this print cost?\""
  info "  • \"Pause the current print\""
  echo
  info "GitHub repo : https://github.com/block0ps/fluidd-klipper-mcp"
  echo
}

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
  clear
  echo
  echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════╗${RESET}"
  echo -e "${BOLD}${CYAN}║     Fluidd / Klipper MCP Server — Interactive Setup      ║${RESET}"
  echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════╝${RESET}"
  echo
  echo -e "${DIM}  This script will walk you through all Quick Start steps.${RESET}"
  echo -e "${DIM}  You'll be asked to confirm before any changes are made.${RESET}"
  echo

  if ! confirm "Ready to begin setup?"; then
    echo "Exiting. Run this script again whenever you're ready."
    exit 0
  fi

  # Allow skipping individual steps
  echo
  info "Which steps would you like to run?"
  info "  (You can re-run this script to redo any step at any time)"

  RUN_PREREQS=true
  RUN_CLONE=true
  RUN_SECRETS=true
  RUN_CATALOG=true
  RUN_CLAUDE=true

  if confirm "Skip prerequisite checks? (recommended: No)"; then RUN_PREREQS=false; fi
  if confirm "Skip clone & Docker build? (only if already done)"; then RUN_CLONE=false; fi
  if confirm "Skip secret/config setup?"; then RUN_SECRETS=false; fi
  if confirm "Skip MCP catalog registration?"; then RUN_CATALOG=false; fi
  if confirm "Skip Claude Desktop config patch?"; then RUN_CLAUDE=false; fi

  echo
  if confirm "Proceed with the selected steps?"; then
    [[ "${RUN_PREREQS}" == true ]]  && check_prereqs
    [[ "${RUN_CLONE}" == true ]]    && step_clone_and_build
    [[ "${RUN_SECRETS}" == true ]]  && step_set_secrets
    [[ "${RUN_CATALOG}" == true ]]  && step_register_catalog
    [[ "${RUN_CLAUDE}" == true ]]   && step_configure_claude
    step_finish
  else
    echo "Exiting. No changes were made."
    exit 0
  fi
}

main "$@"
