#!/usr/bin/env bash
# Paper PPT Agent — Unix (macOS / Linux) startup launcher.
#
# Flow:
#   1. Print a startup banner.
#   2. Detect REQUIRED prerequisites (uv, Node.js). If any are missing, show a
#      numbered menu to install them. Loop until both are available.
#   3. Show OPTIONAL Agent runtimes (Claude Code, Codex) as a traffic light
#      and offer a menu to install them. These never block startup.
#   4. Sync deps and launch the dev stack.
#
# Language: auto-detected from the system locale (LANG starts with zh -> Chinese,
# else English). Override with PPT_LANG=en|zh.

set -eu

ROOT="$(CDPATH= cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$ROOT/frontend"

# ── i18n: detect system language ─────────────────────────────────────────────
detect_lang() {
  case "${PPT_LANG:-}" in
    zh*|ZH*) echo "zh"; return ;;
    en*|EN*) echo "en"; return ;;
  esac
  case "${LANG:-}${LC_ALL:-}${LC_MESSAGES:-}" in
    zh*|ZH*|*zh*|*ZH*) echo "zh"; return ;;
  esac
  echo "en"
}
LANG_CODE="$(detect_lang)"

# Localized string lookup: l <key>  -> prints the string for $LANG_CODE.
l() {
  case "$1" in
    subtitle)             case "$LANG_CODE" in zh) printf '%s' "本地开发启动器";; *) printf '%s' "Local development launcher";; esac ;;
    checkingPrereq)       case "$LANG_CODE" in zh) printf '%s' "正在检查前置依赖";; *) printf '%s' "Checking prerequisites";; esac ;;
    checkingRuntime)      case "$LANG_CODE" in zh) printf '%s' "AI 开发者工具（可选 - 仅 Agent 模式使用）";; *) printf '%s' "AI coding agents (optional - only used by Agent mode)";; esac ;;
    syncBackend)          case "$LANG_CODE" in zh) printf '%s' "同步后端依赖 (uv)";; *) printf '%s' "Syncing backend dependencies (uv)";; esac ;;
    installFrontend)      case "$LANG_CODE" in zh) printf '%s' "安装前端依赖 (npm)";; *) printf '%s' "Installing frontend dependencies (npm)";; esac ;;
    starting)             case "$LANG_CODE" in zh) printf '%s' "正在启动 Paper PPT Agent";; *) printf '%s' "Starting Paper PPT Agent";; esac ;;
    allPrereqReady)       case "$LANG_CODE" in zh) printf '%s' "所有前置依赖已就绪";; *) printf '%s' "All required prerequisites are ready.";; esac ;;
    bothRuntimeReady)     case "$LANG_CODE" in zh) printf '%s' "两个 AI 开发者工具均已安装";; *) printf '%s' "Both AI coding agents are installed.";; esac ;;
    runtimeSkipped)       case "$LANG_CODE" in zh) printf '%s' "已跳过。之后可在 Agent 模式里再安装";; *) printf '%s' "Skipping. You can install them later from Agent mode.";; esac ;;
    runtimeUpdated)       case "$LANG_CODE" in zh) printf '%s' "更新后的 AI 开发者工具状态：";; *) printf '%s' "Updated AI coding agent status:";; esac ;;
    prereqMissingPrompt)  case "$LANG_CODE" in zh) printf '%s' "缺少必要的前置依赖，请选择操作：";; *) printf '%s' "Required prerequisites are missing. Choose an action:";; esac ;;
    runtimePrompt)        case "$LANG_CODE" in zh) printf '%s' "是否配置 AI 开发者工具？（可选）";; *) printf '%s' "Configure AI coding agents? (optional)";; esac ;;
    installUv)            case "$LANG_CODE" in zh) printf '%s' "安装 uv";; *) printf '%s' "Install uv";; esac ;;
    installNode)          case "$LANG_CODE" in zh) printf '%s' "安装 Node.js";; *) printf '%s' "Install Node.js";; esac ;;
    installAll)           case "$LANG_CODE" in zh) printf '%s' "安装全部缺失项";; *) printf '%s' "Install all missing";; esac ;;
    recheck)              case "$LANG_CODE" in zh) printf '%s' "重新检查环境";; *) printf '%s' "Re-check environment";; esac ;;
    exitOption)           case "$LANG_CODE" in zh) printf '%s' "退出";; *) printf '%s' "Exit";; esac ;;
    installClaudeCode)    case "$LANG_CODE" in zh) printf '%s' "安装 Claude Code";; *) printf '%s' "Install Claude Code";; esac ;;
    installCodex)         case "$LANG_CODE" in zh) printf '%s' "安装 Codex";; *) printf '%s' "Install Codex";; esac ;;
    skipLaunch)           case "$LANG_CODE" in zh) printf '%s' "跳过 - 直接启动";; *) printf '%s' "Skip - launch now";; esac ;;
    abortMissing)         case "$LANG_CODE" in zh) printf '%s' "前置依赖未就绪，已中止";; *) printf '%s' "Required prerequisites are not ready. Aborting.";; esac ;;
    invalidSel)           case "$LANG_CODE" in zh) printf '%s' "无效选择";; *) printf '%s' "Invalid selection.";; esac ;;
    outOfRange)           case "$LANG_CODE" in zh) printf '%s' "超出范围";; *) printf '%s' "Out of range.";; esac ;;
    installingUv)         case "$LANG_CODE" in zh) printf '%s' "正在安装 uv（官方安装脚本）";; *) printf '%s' "Installing uv (official installer)";; esac ;;
    installingNode)       case "$LANG_CODE" in zh) printf '%s' "正在安装 Node.js";; *) printf '%s' "Installing Node.js";; esac ;;
    installingClaudeCode) case "$LANG_CODE" in zh) printf '%s' "正在安装 Claude Code (npm)";; *) printf '%s' "Installing Claude Code (npm)";; esac ;;
    installingCodex)      case "$LANG_CODE" in zh) printf '%s' "正在安装 Codex CLI (npm)";; *) printf '%s' "Installing Codex CLI (npm)";; esac ;;
    uvInstalled)          case "$LANG_CODE" in zh) printf '%s' "uv 已安装";; *) printf '%s' "uv installed.";; esac ;;
    uvFailed)             case "$LANG_CODE" in zh) printf '%s' "uv 安装脚本失败";; *) printf '%s' "uv installer failed.";; esac ;;
    nodeBrewOk)           case "$LANG_CODE" in zh) printf '%s' "已通过 Homebrew 安装 Node.js";; *) printf '%s' "Node.js installed via Homebrew.";; esac ;;
    nodeBrewFail)         case "$LANG_CODE" in zh) printf '%s' "Homebrew 安装失败";; *) printf '%s' "Homebrew install failed.";; esac ;;
    nodeAptHint)          case "$LANG_CODE" in zh) printf '%s' "apt 不提供最新 Node.js LTS，建议使用 NodeSource 或 nvm";; *) printf '%s' "apt does not ship current Node.js LTS. Recommended: use NodeSource or nvm.";; esac ;;
    nodeRetry)            case "$LANG_CODE" in zh) printf '%s' "安装完成后，请重新运行本脚本";; *) printf '%s' "Install it, then re-run this script.";; esac ;;
    nodeGenericFail)      case "$LANG_CODE" in zh) printf '%s' "无法为此系统自动安装 Node.js";; *) printf '%s' "Could not install Node.js automatically for this system.";; esac ;;
    ccInstalled)          case "$LANG_CODE" in zh) printf '%s' "Claude Code 已安装";; *) printf '%s' "Claude Code installed.";; esac ;;
    ccFailed)             case "$LANG_CODE" in zh) printf '%s' "Claude Code 安装失败";; *) printf '%s' "Claude Code install failed.";; esac ;;
    cxInstalled)          case "$LANG_CODE" in zh) printf '%s' "Codex 已安装";; *) printf '%s' "Codex installed.";; esac ;;
    cxFailed)             case "$LANG_CODE" in zh) printf '%s' "Codex 安装失败";; *) printf '%s' "Codex install failed.";; esac ;;
    *) printf '%s' "$1" ;;
  esac
}

# ── Pretty output ────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  CYAN="$(printf '\033[36m')"
  GREEN="$(printf '\033[32m')"
  RED="$(printf '\033[31m')"
  YELLOW="$(printf '\033[33m')"
  DARKGRAY="$(printf '\033[90m')"
  DARKCYAN="$(printf '\033[38;5;30m')"
  RESET="$(printf '\033[0m')"
else
  CYAN=""; GREEN=""; RED=""; YELLOW=""; DARKGRAY=""; DARKCYAN=""; RESET=""
fi

info()  { printf '%s\n' "${DARKGRAY}$1${RESET}"; }
step()  { printf '%s\n' "${CYAN}==> $1${RESET}"; }
ok()    { printf '%s\n' "${GREEN}$1${RESET}"; }
warn()  { printf '%s\n' "${YELLOW}$1${RESET}"; }
err()   { printf '%s\n' "${RED}$1${RESET}"; }

print_banner() {
  # Keep the startup banner readable across terminal fonts and encodings.
  local art
  art="$(cat <<'BANNER'
 ____                         ____  ____ _____      _                    _
|  _ \ __ _ _ __   ___ _ __  |  _ \|  _ \_   _|    / \   __ _  ___ _ __ | |_
| |_) / _` | '_ \ / _ \ '__| | |_) | |_) || |     / _ \ / _` |/ _ \ '_ \| __|
|  __/ (_| | |_) |  __/ |    |  __/|  __/ | |    / ___ \ (_| |  __/ | | | |_
|_|   \__,_| .__/ \___|_|    |_|   |_|    |_|   /_/   \_\__, |\___|_| |_|\__|
           |_|                                           |___/
BANNER
)"
  printf '%s\n' "${DARKCYAN}${art}${RESET}"
  printf '%s\n' "${DARKGRAY}  $(l subtitle)${RESET}"
  echo ""
}

# ── Detection helpers ────────────────────────────────────────────────────────
has_cmd()  { command -v "$1" >/dev/null 2>&1; }

cmd_version() {
  local name="$1" out
  out="$(command "$name" --version 2>/dev/null | head -n1 || true)"
  [ -n "$out" ] && printf '%s' "$out"
}

# Reload PATH so a just-installed tool is visible. Unix installers typically
# add to ~/.local/bin or ~/.cargo/bin or a version manager dir; we extend PATH
# with the common ones rather than re-reading shell rc files.
refresh_path() {
  local extra=""
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) extra="$HOME/.local/bin" ;;
  esac
  if [ -d "$HOME/.cargo/bin" ]; then
    case ":$PATH:" in *":$HOME/.cargo/bin:"*) ;; *) extra="${extra:+$extra:}$HOME/.cargo/bin" ;; esac
  fi
  for bp in "/opt/homebrew/bin" "/usr/local/bin"; do
    if [ -d "$bp" ]; then
      case ":$PATH:" in *":$bp:"*) ;; *) extra="${extra:+$extra:}$bp" ;; esac
    fi
  done
  if [ -n "$extra" ]; then export PATH="$extra:$PATH"; fi
}

status_line() {
  # label detail ok optional
  local label="$1" detail="${2:-}" is_ok="$3" optional="${4:-0}"
  if [ "$is_ok" = "1" ]; then
    printf '  [%sOK%s] %s' "${GREEN}" "${RESET}" "${label}"
  elif [ "$optional" = "1" ]; then
    printf '  [%s--%s] %s' "${YELLOW}" "${RESET}" "${YELLOW}${label}${RESET}"
  else
    printf '  [%sXX%s] %s' "${RED}" "${RESET}" "${RED}${label}${RESET}"
  fi
  if [ -n "$detail" ]; then printf '  %s-  %s%s' "${DARKGRAY}" "$detail" "${RESET}"; fi
  echo ""
}

# ── Installers ───────────────────────────────────────────────────────────────
# All installers stream their output to this terminal so progress is visible.
install_uv() {
  step "$(l installingUv)"
  echo ""
  # https://docs.astral.sh/uv/
  if curl -LsSf https://astral.sh/uv/install.sh | sh; then
    refresh_path
    echo ""
    ok "$(l uvInstalled)"
  else
    err "$(l uvFailed)"
  fi
}

install_node() {
  step "$(l installingNode)"
  echo ""
  # Pin to Node.js 22 LTS (the "LTS" channel has moved to v24 on some
  # package managers, which ships npm 11 with known module-load issues).
  if has_cmd brew; then
    if brew install node@22; then
      # node@22 is keg-only; link it so it lands on PATH.
      brew link --force --overwrite node@22 2>/dev/null || true
      refresh_path
      echo ""
      ok "$(l nodeBrewOk)"
      return
    fi
    warn "$(l nodeBrewFail)"
  fi
  if has_cmd apt-get; then
    warn "$(l nodeAptHint)"
    echo "${CYAN}  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs${RESET}"
    echo "${CYAN}  or: https://nodejs.org/en/download/package-manager${RESET}"
    echo "$(l nodeRetry)"
    return
  fi
  warn "$(l nodeGenericFail)"
  echo "${CYAN}  https://nodejs.org/${RESET}"
  echo "$(l nodeRetry)"
}

install_claude_code() {
  step "$(l installingClaudeCode)"
  echo ""
  if npm install -g @anthropic-ai/claude-code; then refresh_path; echo ""; ok "$(l ccInstalled)"; else err "$(l ccFailed)"; fi
}

install_codex() {
  step "$(l installingCodex)"
  echo ""
  if npm install -g @openai/codex; then refresh_path; echo ""; ok "$(l cxInstalled)"; else err "$(l cxFailed)"; fi
}

# ── Stage 2: required prerequisites ──────────────────────────────────────────
ensure_prerequisites() {
  while true; do
    refresh_path
    local uv_ok=0 node_ok=0 uv_ver="" node_ver=""
    if has_cmd uv;   then uv_ok=1; uv_ver="$(cmd_version uv)";   fi
    if has_cmd node; then node_ok=1; node_ver="$(cmd_version node)"; fi

    echo ""
    step "$(l checkingPrereq)"
    status_line "uv"      "$uv_ver"   "$uv_ok"
    status_line "Node.js" "$node_ver" "$node_ok"

    if [ "$uv_ok" = "1" ] && [ "$node_ok" = "1" ]; then
      ok "$(l allPrereqReady)"
      return 0
    fi

    local missing=()
    [ "$uv_ok"   = "0" ] && missing+=("uv")
    [ "$node_ok" = "0" ] && missing+=("Node.js")

    local opts=() actmap=()
    for m in "${missing[@]}"; do
      case "$m" in
        uv)      opts+=("$(l installUv)"); actmap+=("install:uv") ;;
        Node.js) opts+=("$(l installNode)"); actmap+=("install:Node.js") ;;
      esac
    done
    if [ "${#missing[@]}" -gt 1 ]; then
      opts+=("$(l installAll)"); actmap+=("install:all")
    fi
    opts+=("$(l recheck)"); actmap+=("recheck")
    opts+=("$(l exitOption)"); actmap+=("exit")

    echo ""
    warn "$(l prereqMissingPrompt)"
    PS3="Select [1-${#opts[@]}]: "
    select choice in "${opts[@]}"; do
      case "$REPLY" in
        ''|*[!0-9]*) echo "$(l invalidSel)"; continue ;;
      esac
      if [ "$REPLY" -ge 1 ] && [ "$REPLY" -le "${#opts[@]}" ]; then
        break
      fi
      echo "$(l outOfRange)"
    done
    local act="${actmap[$((REPLY-1))]}"
    case "$act" in
      install:uv)       install_uv ;;
      install:Node.js)  install_node ;;
      install:all)
        for m in "${missing[@]}"; do
          case "$m" in uv) install_uv ;; Node.js) install_node ;; esac
        done ;;
      recheck) ;;
      exit) warn "$(l abortMissing)"; exit 1 ;;
    esac
  done
}

# ── Stage 3: optional Agent runtimes ─────────────────────────────────────────
show_optional_runtimes() {
  refresh_path
  local cc_ok=0 cx_ok=0 cc_ver="" cx_ver=""
  if has_cmd claude; then cc_ok=1; cc_ver="$(cmd_version claude)"; fi
  if has_cmd codex;  then cx_ok=1; cx_ver="$(cmd_version codex)";  fi

  echo ""
  step "$(l checkingRuntime)"
  status_line "Claude Code" "$cc_ver" "$cc_ok" 1
  status_line "Codex"       "$cx_ver" "$cx_ok" 1

  if [ "$cc_ok" = "1" ] && [ "$cx_ok" = "1" ]; then
    ok "$(l bothRuntimeReady)"
    return 0
  fi

  local opts=() actmap=()
  if [ "$cc_ok" = "0" ]; then opts+=("$(l installClaudeCode)"); actmap+=("cc"); fi
  if [ "$cx_ok" = "0" ]; then opts+=("$(l installCodex)");       actmap+=("cx"); fi
  opts+=("$(l skipLaunch)"); actmap+=("skip")

  echo ""
  warn "$(l runtimePrompt)"
  PS3="Select [1-${#opts[@]}]: "
  select choice in "${opts[@]}"; do
    case "$REPLY" in
      ''|*[!0-9]*) echo "$(l invalidSel)"; continue ;;
    esac
    if [ "$REPLY" -ge 1 ] && [ "$REPLY" -le "${#opts[@]}" ]; then break; fi
    echo "$(l outOfRange)"
  done
  local act="${actmap[$((REPLY-1))]}"
  case "$act" in
    cc)   install_claude_code ;;
    cx)   install_codex ;;
    skip) info "$(l runtimeSkipped)" ;;
  esac

  refresh_path
  local cc_ok2=0 cx_ok2=0 cc_ver2="" cx_ver2=""
  if has_cmd claude; then cc_ok2=1; cc_ver2="$(cmd_version claude)"; fi
  if has_cmd codex;  then cx_ok2=1; cx_ver2="$(cmd_version codex)";  fi
  echo ""
  info "$(l runtimeUpdated)"
  status_line "Claude Code" "$cc_ver2" "$cc_ok2" 1
  status_line "Codex"       "$cx_ver2" "$cx_ok2" 1
}

# ── Stage 4: launch ──────────────────────────────────────────────────────────
launch_dev() {
  echo ""
  step "$(l syncBackend)"
  cd "$ROOT"
  uv sync --locked

  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    step "$(l installFrontend)"
    cd "$FRONTEND_DIR"
    npm install
  fi

  echo ""
  step "$(l starting)"
  cd "$ROOT"
  PYTHONUNBUFFERED=1 uv run python -m backend.dev_launcher
}

# ── Main ─────────────────────────────────────────────────────────────────────
print_banner
ensure_prerequisites
show_optional_runtimes
launch_dev
