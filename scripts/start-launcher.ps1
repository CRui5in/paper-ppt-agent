# Paper PPT Agent — interactive startup launcher (Windows / PowerShell).
#
# Invoked by start-dev.bat. Responsibilities:
#   1. Print a startup banner.
#   2. Detect REQUIRED prerequisites (uv, Node.js). If any are missing,
#      show an arrow-key menu to install them (uv via the official installer,
#      Node.js via winget with a manual-download fallback). Loop until both are
#      available — the app cannot start without them.
#   3. Show OPTIONAL AI coding agents (Claude Code, Codex) as a traffic light
#      (green = installed, red = missing) and offer a menu to install them.
#      These never block startup; they only matter for Agent mode.
#   4. Once the environment is ready, sync deps and launch the dev stack.
#
# Language: auto-detected from the system locale (zh-CN -> Chinese, else English).
# Override with the PPT_LANG environment variable (e.g. set PPT_LANG=en).

#requires -Version 5.1

[CmdletBinding()]
param(
  [switch]$SkipOptional,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"

# Force UTF-8 output so the banner / CJK text render correctly on Windows.
try {
  [System.Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $PSDefaultParameterValues["Out-File:Encoding"] = "utf8"
} catch {}

# -----------------------------------------------------------------------------
# i18n: detect system language
# -----------------------------------------------------------------------------

function Get-DetectedLang {
  # Explicit override wins.
  $envLang = "$env:PPT_LANG".Trim().ToLower()
  if ($envLang -in @("zh", "en")) { return $envLang }
  if ($envLang.StartsWith("zh")) { return "zh" }
  if ($envLang.StartsWith("en")) { return "en" }
  # Fall back to OS locale.
  try {
    $culture = (Get-Culture).Name  # e.g. "zh-CN", "en-US"
    if ($culture -and $culture.StartsWith("zh")) { return "zh" }
  } catch {}
  return "en"
}

$LANG = Get-DetectedLang

# Localized strings. Each key maps to { en, zh }.
function L([string]$Key) {
  $strings = @{
    subtitle             = @{ en = "Local development launcher";                                  zh = "本地开发启动器" }
    checkingPrereq       = @{ en = "Checking prerequisites";                                       zh = "正在检查前置依赖" }
    checkingRuntime      = @{ en = "AI coding agents (optional - only used by Agent mode)";         zh = "AI 开发者工具（可选 - 仅 Agent 模式使用）" }
    syncBackend          = @{ en = "Syncing backend dependencies (uv)";                             zh = "同步后端依赖 (uv)" }
    installFrontend      = @{ en = "Installing frontend dependencies (npm)";                        zh = "安装前端依赖 (npm)" }
    starting             = @{ en = "Starting Paper PPT Agent";                                     zh = "正在启动 Paper PPT Agent" }
    allPrereqReady       = @{ en = "All required prerequisites are ready.";                         zh = "所有前置依赖已就绪。" }
    bothRuntimeReady     = @{ en = "Both AI coding agents are installed.";                           zh = "两个 AI 开发者工具均已安装。" }
    skipRuntimeYes       = @{ en = "Skipping agent install prompt (-Yes).";                         zh = "跳过开发者工具安装提示 (-Yes)。" }
    runtimeSkipped       = @{ en = "Skipping. You can install them later from Agent mode.";          zh = "已跳过。之后可在 Agent 模式里再安装。" }
    runtimeUpdated       = @{ en = "Updated AI coding agent status:";                               zh = "更新后的 AI 开发者工具状态：" }
    prereqMissingPrompt  = @{ en = "Required prerequisites are missing. Choose an action:";         zh = "缺少必要的前置依赖，请选择操作：" }
    runtimePrompt        = @{ en = "Configure AI coding agents? (optional)";                         zh = "是否配置 AI 开发者工具？（可选）" }
    menuHint             = @{ en = "  Up/Down select   Enter confirm";                              zh = "  上/下 选择   回车 确认" }
    installUv            = @{ en = "Install uv";                                                    zh = "安装 uv" }
    installNode          = @{ en = "Install Node.js";                                               zh = "安装 Node.js" }
    installAll           = @{ en = "Install all missing";                                           zh = "安装全部缺失项" }
    recheck              = @{ en = "Re-check environment";                                          zh = "重新检查环境" }
    exitOption           = @{ en = "Exit";                                                          zh = "退出" }
    installClaudeCode    = @{ en = "Install Claude Code";                                           zh = "安装 Claude Code" }
    installCodex         = @{ en = "Install Codex";                                                 zh = "安装 Codex" }
    skipLaunch           = @{ en = "Skip - launch now";                                             zh = "跳过 - 直接启动" }
    abortMissing         = @{ en = "Required prerequisites are not ready. Aborting.";               zh = "前置依赖未就绪，已中止。" }
    noRoot               = @{ en = "Could not locate the project root (backend/app.py).";           zh = "无法定位项目根目录 (backend/app.py)。" }
    installingUv         = @{ en = "Installing uv (official installer)";                            zh = "正在安装 uv（官方安装脚本）" }
    installingNode       = @{ en = "Installing Node.js (winget)";                                   zh = "正在安装 Node.js (winget)" }
    installingClaudeCode = @{ en = "Installing Claude Code (npm)";                                  zh = "正在安装 Claude Code (npm)" }
    installingCodex      = @{ en = "Installing Codex CLI (npm)";                                    zh = "正在安装 Codex CLI (npm)" }
    wingetMissing        = @{ en = "winget is not available on this system.";                       zh = "当前系统未安装 winget。" }
    nodeManualGuide      = @{ en = "Automatic install unavailable. Please install Node.js LTS manually:"; zh = "无法自动安装，请手动安装 Node.js LTS：" }
    nodeManualHint       = @{ en = "After installing, close this window and run start-dev.bat again.";    zh = "安装完成后请关闭此窗口，再次运行 start-dev.bat。" }
  }
  $entry = $strings[$Key]
  if ($null -eq $entry) { return $Key }
  return $entry[$LANG]
}

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

function Write-Info  { param([string]$Msg) Write-Host $Msg -ForegroundColor DarkGray }
function Write-Step  { param([string]$Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Msg) Write-Host $Msg -ForegroundColor Green }
function Write-Warn2 { param([string]$Msg) Write-Host $Msg -ForegroundColor Yellow }
function Write-Err2  { param([string]$Msg) Write-Host $Msg -ForegroundColor Red }

function Print-Banner {
  # Keep the startup banner readable across terminal fonts and encodings.
  $banner = @'
 ____                         ____  ____ _____      _                    _
|  _ \ __ _ _ __   ___ _ __  |  _ \|  _ \_   _|    / \   __ _  ___ _ __ | |_
| |_) / _` | '_ \ / _ \ '__| | |_) | |_) || |     / _ \ / _` |/ _ \ '_ \| __|
|  __/ (_| | |_) |  __/ |    |  __/|  __/ | |    / ___ \ (_| |  __/ | | | |_
|_|   \__,_| .__/ \___|_|    |_|   |_|    |_|   /_/   \_\__, |\___|_| |_|\__|
           |_|                                           |___/
'@
  Write-Host $banner -ForegroundColor DarkCyan
  Write-Host "  $(L subtitle)" -ForegroundColor DarkGray
  Write-Host ""
}

# True if a command is on PATH (equivalent of `where` / `command -v`).
function Test-Cmd {
  param([string]$Name)
  return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# Best-effort version string for a CLI (first stdout line).
function Get-CmdVersion {
  param([string]$Name)
  try {
    $out = & $Name --version 2>$null
    if ($LASTEXITCODE -eq 0 -and $out) {
      return ($out | Select-Object -First 1).ToString().Trim()
    }
  } catch {}
  return $null
}

# Reload PATH from the registry so a freshly-installed tool is visible in
# this process without a restart.
function Update-EnvPath {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user    = [Environment]::GetEnvironmentVariable("Path", "User")
  # Preserve anything already in the live env that is not in the registry yet
  # (e.g. session-local additions) to avoid shrinking PATH.
  $combined = @()
  if ($machine) { $combined += ($machine -split ";") | Where-Object { $_ } }
  if ($user)    { $combined += ($user -split ";")    | Where-Object { $_ } }
  $existing = $env:Path -split ";" | Where-Object { $_ }
  foreach ($p in $existing) { if ($combined -notcontains $p) { $combined += $p } }
  $env:Path = ($combined -join ";")
}

# Render a coloured status dot + label line.
function Write-StatusLine {
  param(
    [string]$Label,
    [string]$Detail,
    [bool]$Ok,
    [switch]$Optional
  )
  if ($Ok) {
    Write-Host "  [" -NoNewline
    Write-Host "OK" -NoNewline -ForegroundColor Green
    Write-Host "] $Label" -NoNewline
    if ($Detail) { Write-Host "  -  $Detail" -NoNewline -ForegroundColor DarkGray }
    Write-Host ""
  } else {
    Write-Host "  [" -NoNewline
    if ($Optional) {
      Write-Host "--" -NoNewline -ForegroundColor DarkYellow
      Write-Host "] $Label" -NoNewline -ForegroundColor DarkYellow
    } else {
      Write-Host "XX" -NoNewline -ForegroundColor Red
      Write-Host "] $Label" -NoNewline -ForegroundColor Red
    }
    if ($Detail) { Write-Host "  -  $Detail" -NoNewline -ForegroundColor DarkGray }
    Write-Host ""
  }
}

# -----------------------------------------------------------------------------
# Arrow-key menu. Options is a fixed-size list; returns the selected index.
# Redraws in place (options count is constant during a session).
# -----------------------------------------------------------------------------

function Show-Menu {
  param(
    [Parameter(Mandatory)] [string[]]$Options,
    [string]$Hint = "  Up/Down select   Enter confirm"
  )

  $selected = 0
  $count = $Options.Count

  function Build-Frame([int]$Selected) {
    $frame = @()
    for ($i = 0; $i -lt $count; $i++) {
      if ($i -eq $Selected) {
        $frame += "  > $($Options[$i])"
      } else {
        $frame += "    $($Options[$i])"
      }
    }
    if ($Hint) { $frame += $Hint }
    return $frame
  }

  $frame = Build-Frame $selected
  $lines = $frame.Count

  # First paint.
  foreach ($line in $frame) { Write-Host $line }

  while ($true) {
    $key = [System.Console]::ReadKey($true)
    $prev = $selected
    switch ($key.Key) {
      "UpArrow"   { $selected = ($selected - 1 + $count) % $count }
      "DownArrow" { $selected = ($selected + 1) % $count }
      "Home"      { $selected = 0 }
      "End"       { $selected = $count - 1 }
      "Enter"     {
        # Move the cursor back above the menu so the caller's next output
        # doesn't leave a gap, and clear the frame lines.
        Clear-Lines $lines
        return $selected
      }
      default { <# ignore other keys #> }
    }
    if ($selected -ne $prev) {
      Clear-Lines $lines
      $frame = Build-Frame $selected
      $lines = $frame.Count
      foreach ($line in $frame) { Write-Host $line }
    }
  }
}

# Erase N previously-drawn lines (move up, clear each, land back at the top).
function Clear-Lines([int]$N) {
  for ($i = 0; $i -lt $N; $i++) {
    [System.Console]::SetCursorPosition(0, [System.Console]::CursorTop - 1)
    [System.Console]::Write(" " * [Math]::Max(0, [System.Console]::BufferWidth - 1))
    [System.Console]::Write("`r")
  }
}

# -----------------------------------------------------------------------------
# Installers
#
# All installers stream their output to this console so the user can watch
# progress (downloads, version resolution, etc.). We do NOT silence them.
# -----------------------------------------------------------------------------

function Install-Uv {
  Write-Step (L installingUv)
  Write-Host ""
  # Official Windows installer per https://docs.astral.sh/uv/
  # Run in-process so its progress prints directly to this terminal.
  try {
    & powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if ($LASTEXITCODE -ne 0) { throw "uv installer exited with code $LASTEXITCODE" }
  } catch {
    Write-Err2 "uv installer failed: $($_.Exception.Message)"
    return $false
  }
  Update-EnvPath
  # Belt-and-suspenders: the installer also drops uv here.
  $localBin = Join-Path $env:USERPROFILE ".local\bin"
  if ((Test-Path $localBin) -and (($env:Path -split ";") -notcontains $localBin)) {
    $env:Path = "$localBin;" + $env:Path
  }
  Write-Host ""
  return $true
}

function Install-Node {
  Write-Step (L installingNode)
  Write-Host ""
  if (Test-Cmd "winget") {
    try {
      # Pin to Node.js 22 LTS (the "LTS" winget id currently points at v24,
      # which ships npm 11 with known module-load regressions). v22 is the
      # verified, stable long-term-support line.
      & winget install --id OpenJS.NodeJS.22 -e --accept-source-agreements --accept-package-agreements
      if ($LASTEXITCODE -eq 0) {
        Update-EnvPath
        Write-Host ""
        return $true
      }
      Write-Warn2 "winget exited with code $LASTEXITCODE."
    } catch {
      Write-Warn2 "winget invocation failed: $($_.Exception.Message)"
    }
  } else {
    Write-Warn2 (L wingetMissing)
  }
  # Fallback: guide manual install.
  Write-Host ""
  Write-Warn2 (L nodeManualGuide)
  Write-Host  "  - https://nodejs.org/" -ForegroundColor Cyan
  Write-Host  "  - winget install OpenJS.NodeJS.22" -ForegroundColor Cyan
  Write-Host  (L nodeManualHint)
  return $false
}

function Install-ClaudeCode {
  Write-Step (L installingClaudeCode)
  Write-Host ""
  try {
    & npm install -g @anthropic-ai/claude-code
    if ($LASTEXITCODE -ne 0) { throw "npm exited with code $LASTEXITCODE" }
    Update-EnvPath
    Write-Host ""
    return $true
  } catch {
    Write-Err2 "Claude Code install failed: $($_.Exception.Message)"
    return $false
  }
}

function Install-Codex {
  Write-Step (L installingCodex)
  Write-Host ""
  try {
    & npm install -g @openai/codex
    if ($LASTEXITCODE -ne 0) { throw "npm exited with code $LASTEXITCODE" }
    Update-EnvPath
    Write-Host ""
    return $true
  } catch {
    Write-Err2 "Codex install failed: $($_.Exception.Message)"
    return $false
  }
}

# -----------------------------------------------------------------------------
# Stage 2: required prerequisites
# -----------------------------------------------------------------------------

function Get-PrereqState {
  $uvOk   = Test-Cmd "uv"
  $nodeOk = Test-Cmd "node"
  return [pscustomobject]@{
    Uv   = [pscustomobject]@{ Ok = $uvOk;   Version = if ($uvOk)   { Get-CmdVersion "uv" }   else { $null } }
    Node = [pscustomobject]@{ Ok = $nodeOk; Version = if ($nodeOk) { Get-CmdVersion "node" } else { $null } }
  }
}

function Ensure-Prerequisites {
  while ($true) {
    $st = Get-PrereqState
    Write-Host ""
    Write-Step (L checkingPrereq)
    Write-StatusLine "uv"      $st.Uv.Version   $st.Uv.Ok
    Write-StatusLine "Node.js" $st.Node.Version $st.Node.Ok

    if ($st.Uv.Ok -and $st.Node.Ok) {
      Write-Ok (L allPrereqReady)
      return
    }

    # Build a menu of missing items + actions.
    $missing = @()
    if (-not $st.Uv.Ok)   { $missing += "uv" }
    if (-not $st.Node.Ok) { $missing += "Node.js" }

    $options = @()
    $map = @{}  # option index -> action token
    foreach ($m in $missing) {
      $map[$options.Count] = "install:$m"
      if ($m -eq "uv")       { $options += (L installUv) }
      if ($m -eq "Node.js")  { $options += (L installNode) }
    }
    if ($missing.Count -gt 1) {
      $map[$options.Count] = "install:all"
      $options += (L installAll)
    }
    $map[$options.Count] = "recheck"
    $options += (L recheck)
    $map[$options.Count] = "exit"
    $options += (L exitOption)

    Write-Host ""
    Write-Host (L prereqMissingPrompt) -ForegroundColor Yellow
    $choice = Show-Menu -Options $options -Hint (L menuHint)
    $token = $map[[int]$choice]

    switch -Wildcard ($token) {
      "install:uv"        { $null = Install-Uv }
      "install:Node.js"   { $null = Install-Node }
      "install:all"       {
        if ($missing -contains "uv")       { $null = Install-Uv }
        if ($missing -contains "Node.js")  { $null = Install-Node }
      }
      "recheck"           { <# loop will re-evaluate #> }
      "exit"              {
        Write-Warn2 (L abortMissing)
        exit 1
      }
    }
  }
}

# -----------------------------------------------------------------------------
# Stage 3: optional AI coding agents (traffic light)
# -----------------------------------------------------------------------------

function Get-RuntimeState {
  $cc = Test-Cmd "claude"
  $cx = Test-Cmd "codex"
  return [pscustomobject]@{
    ClaudeCode = [pscustomobject]@{ Ok = $cc; Version = if ($cc) { Get-CmdVersion "claude" } else { $null } }
    Codex      = [pscustomobject]@{ Ok = $cx; Version = if ($cx) { Get-CmdVersion "codex" } else { $null } }
  }
}

function Show-OptionalRuntimes {
  if ($SkipOptional) { return }
  $st = Get-RuntimeState

  Write-Host ""
  Write-Step (L checkingRuntime)
  Write-StatusLine "Claude Code" $st.ClaudeCode.Version $st.ClaudeCode.Ok -Optional
  Write-StatusLine "Codex"       $st.Codex.Version      $st.Codex.Ok      -Optional

  # Nothing to install and user opted out of prompting when all present.
  if ($st.ClaudeCode.Ok -and $st.Codex.Ok) {
    Write-Ok (L bothRuntimeReady)
    return
  }
  if ($Yes) {
    Write-Info (L skipRuntimeYes)
    return
  }

  $options = @()
  $map = @{}
  if (-not $st.ClaudeCode.Ok) { $map[$options.Count] = "cc"; $options += (L installClaudeCode) }
  if (-not $st.Codex.Ok)      { $map[$options.Count] = "cx"; $options += (L installCodex) }
  $map[$options.Count] = "skip"
  $options += (L skipLaunch)

  Write-Host ""
  Write-Host (L runtimePrompt) -ForegroundColor Yellow
  $choice = Show-Menu -Options $options -Hint (L menuHint)
  switch ($map[[int]$choice]) {
    "cc"   { $null = Install-ClaudeCode }
    "cx"   { $null = Install-Codex }
    "skip" { Write-Info (L runtimeSkipped) }
  }

  # Show refreshed status.
  $st2 = Get-RuntimeState
  Write-Host ""
  Write-Info (L runtimeUpdated)
  Write-StatusLine "Claude Code" $st2.ClaudeCode.Version $st2.ClaudeCode.Ok -Optional
  Write-StatusLine "Codex"       $st2.Codex.Version      $st2.Codex.Ok      -Optional
}

# -----------------------------------------------------------------------------
# Stage 4: launch
# -----------------------------------------------------------------------------

function Launch-Dev {
  $root = $PSScriptRoot
  $marker = Join-Path (Join-Path $root "backend") "app.py"
  while ($root -and -not (Test-Path $marker)) {
    $parent = Split-Path $root -Parent
    if ($parent -eq $root) { break }
    $root = $parent
    $marker = Join-Path (Join-Path $root "backend") "app.py"
  }
  if (-not (Test-Path $marker)) {
    Write-Err2 (L noRoot)
    exit 1
  }

  Write-Host ""
  Write-Step (L syncBackend)
  Push-Location $root
  try {
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed (code $LASTEXITCODE)" }
  } catch {
    Pop-Location
    Write-Err2 $_.Exception.Message
    exit 1
  }

  $nodeModules = Join-Path $root "frontend\node_modules"
  if (-not (Test-Path $nodeModules)) {
    Write-Step (L installFrontend)
    Push-Location (Join-Path $root "frontend")
    try {
      & npm install
      if ($LASTEXITCODE -ne 0) { throw "npm install failed (code $LASTEXITCODE)" }
    } catch {
      Pop-Location; Pop-Location
      Write-Err2 $_.Exception.Message
      exit 1
    }
    Pop-Location
  }

  Write-Host ""
  Write-Step (L starting)
  $env:PYTHONUNBUFFERED = "1"
  & uv run python -m backend.dev_launcher
  Pop-Location
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

Print-Banner
Ensure-Prerequisites
Show-OptionalRuntimes
Launch-Dev
