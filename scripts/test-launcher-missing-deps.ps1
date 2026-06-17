# Test harness: run start-launcher.ps1 with uv/node/claude/codex temporarily
# HIDDEN from PATH, so you can exercise the detection + install menu without
# actually uninstalling anything.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\test-launcher-missing-deps.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\test-launcher-missing-deps.ps1 -Hide uv
#   powershell -ExecutionPolicy Bypass -File scripts\test-launcher-missing-deps.ps1 -Hide uv,node
#   powershell -ExecutionPolicy Bypass -File scripts\test-launcher-missing-deps.ps1 -Hide uv -Hide node
#
# What it does:
#   - Spawns a CHILD PowerShell whose $env:Path has the chosen tools' directories
#     removed (only within that child process — your real machine PATH is untouched).
#   - Runs start-launcher.ps1 there.
#   - When you pick "Install" in the menu, the installer will actually run and
#     RE-ADD the tool — so to test the full loop, pass the tool again on re-run.

param(
  [string[]]$Hide
)

$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "start-launcher.ps1"
if (-not (Test-Path $launcher)) { Write-Error "start-launcher.ps1 not found at $launcher"; exit 1 }

# Normalise $Hide into a clean array. When passed on the command line,
# "-Hide uv,node" arrives as a single string "uv,node" — split on comma so
# both forms work: "-Hide uv,node" and "-Hide uv -Hide node".
if (-not $Hide) { $Hide = @("uv", "node", "claude", "codex") }
$tools = @()
foreach ($h in $Hide) {
  foreach ($part in ($h -split ",")) {
    $t = $part.Trim().ToLower()
    if ($t) { $tools += $t }
  }
}
# Deduplicate while preserving order.
$tools = $tools | Select-Object -Unique

# Map each tool to the directory it lives in (resolved from the live PATH).
function Resolve-ToolDir([string]$Name) {
  $cmd = switch ($Name) {
    "uv"     { "uv" }
    "node"   { "node.exe" }
    "npm"    { "npm.cmd" }
    "claude" { "claude" }
    "codex"  { "codex" }
    default  { $Name }
  }
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if (-not $found) { return $null }
  return Split-Path $found.Source -Parent
}

# Build the filtered PATH: drop the directories of the tools we want to hide.
# Collect both the resolved dir and the tool name so we can report accurately.
$dirsToRemove = @()
$resolved = @()   # tools we actually found and will hide
$notFound = @()   # tools requested but not on PATH
foreach ($tool in $tools) {
  $dir = Resolve-ToolDir $tool
  if ($dir) {
    if ($dirsToRemove -notcontains $dir) { $dirsToRemove += $dir }
    $resolved += $tool
  } else {
    $notFound += $tool
  }
}

# Rebuild PATH without the hidden dirs. Match case-insensitively and trimmed.
$cleanPath = ($env:Path -split ";" | Where-Object {
  $entry = $_.Trim()
  if (-not $entry) { return $false }
  foreach ($d in $dirsToRemove) {
    if ($entry -ieq $d) { return $false }
  }
  return $true
}) -join ";"

Write-Host "=== Test mode ===" -ForegroundColor Cyan
Write-Host ("Hiding tools: " + ($tools -join ', ')) -ForegroundColor Cyan
if ($resolved.Count -gt 0) {
  Write-Host ("  resolved + hidden: " + ($resolved -join ', ')) -ForegroundColor Green
}
if ($notFound.Count -gt 0) {
  Write-Host ("  not on PATH (already missing): " + ($notFound -join ', ')) -ForegroundColor DarkGray
}
foreach ($d in $dirsToRemove) { Write-Host "    removed dir: $d" -ForegroundColor DarkGray }
Write-Host "Your real machine PATH is NOT modified." -ForegroundColor Green
Write-Host "================" -ForegroundColor Cyan
Write-Host ""

if ($dirsToRemove.Count -eq 0 -and $resolved.Count -eq 0) {
  Write-Host "None of the requested tools were found on PATH." -ForegroundColor Yellow
  Write-Host "The launcher will see them all as missing already." -ForegroundColor Yellow
  Write-Host ""
}

# Spawn a child process with the scrubbed PATH and run the launcher there.
# -NoExit keeps the window open after the launcher exits so you can read output.
& powershell -NoExit -Command {
  param($CleanPath, $Launcher)
  $env:Path = $CleanPath
  [System.Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Write-Host "[test] child PATH set; tool availability in this window:" -ForegroundColor DarkCyan
  foreach ($c in "uv","node","claude","codex") {
    $ok = [bool](Get-Command $c -ErrorAction SilentlyContinue)
    $label = $(if ($ok) {"VISIBLE"} else {"hidden"})
    $color = $(if ($ok) {"Gray"} else {"Green"})
    Write-Host ("  {0,-7} {1}" -f $c, $label) -ForegroundColor $color
  }
  Write-Host ""
  Write-Host "[test] launching start-launcher.ps1 ..." -ForegroundColor DarkCyan
  Write-Host ""
  & powershell -NoProfile -ExecutionPolicy Bypass -File $Launcher
} -args $cleanPath, $launcher
