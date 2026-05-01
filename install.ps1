# claude2bot install script (Windows / PowerShell)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File install.ps1 `
#       -Token "1234567:AAA..." `
#       -ChatId "1796415913"
#
# Optional:
#   -PythonPath  C:\Python314\python.exe   (default: %USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe)
#   -PluginRoot  <path to installed Telegram plugin>  (default: auto-detect)

param(
    [Parameter(Mandatory = $true)]
    [string]$Token,

    [Parameter(Mandatory = $true)]
    [string]$ChatId,

    [string]$PythonPath = "C:\Python314\python.exe",

    [string]$PluginRoot = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
$ClaudeDir = Join-Path $env:USERPROFILE ".claude"
$HooksDir = Join-Path $ClaudeDir "hooks"
$ChannelsDir = Join-Path $ClaudeDir "channels\telegram"
$EnvFile = Join-Path $ChannelsDir ".env"
$SettingsFile = Join-Path $ClaudeDir "settings.json"
$McpFile = Join-Path $ClaudeDir "mcp.json"

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }
function Die($msg)  { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

# ── 0. Sanity checks ─────────────────────────────────────────────────────────
if (-not (Test-Path $PythonPath)) { Die "Python not found at $PythonPath. Pass -PythonPath if it lives elsewhere." }
if (-not (Get-Command bun -ErrorAction SilentlyContinue)) { Die "bun not on PATH. Install: https://bun.sh/" }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Warn "claude CLI not on PATH. The hook config will still install; just make sure Claude Code is installed."
}

# ── 1. Locate / install the upstream Telegram plugin ─────────────────────────
if (-not $PluginRoot) {
    $candidates = Get-ChildItem -Path "$ClaudeDir\plugins\cache\claude-plugins-official\telegram" -Directory -ErrorAction SilentlyContinue
    if ($candidates) { $PluginRoot = $candidates[0].FullName }
}
if (-not $PluginRoot -or -not (Test-Path $PluginRoot)) {
    Die "Telegram plugin not found in ~/.claude/plugins/cache/claude-plugins-official/telegram/. Install it first via /plugin marketplace install telegram@claude-plugins-official, then re-run this script."
}
Info "Plugin root: $PluginRoot"

Info "Running 'bun install' in plugin dir (so MCP attach doesn't race on it later)..."
Push-Location $PluginRoot
try { & bun install --silent | Out-Null } finally { Pop-Location }

# ── 2. Patch server.ts (Windows stdin race fix) ──────────────────────────────
$ServerSrc = Join-Path $RepoRoot "plugin\server.patched.ts"
$ServerDst = Join-Path $PluginRoot "server.ts"
if (-not (Test-Path $ServerSrc)) { Die "Missing $ServerSrc" }
$ServerBak = "$ServerDst.upstream.bak"
if (-not (Test-Path $ServerBak)) { Copy-Item $ServerDst $ServerBak; Info "Backed up upstream server.ts -> $ServerBak" }
Copy-Item -Force $ServerSrc $ServerDst
Info "Patched server.ts"

# ── 3. Token in .env ─────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $ChannelsDir | Out-Null
"TELEGRAM_BOT_TOKEN=$Token" | Out-File -FilePath $EnvFile -Encoding ASCII -NoNewline
Info "Wrote bot token to $EnvFile"

# ── 4. Patch mcp.json ────────────────────────────────────────────────────────
$McpJson = if (Test-Path $McpFile) {
    Get-Content $McpFile -Raw | ConvertFrom-Json
} else {
    @{ mcpServers = @{} } | ConvertTo-Json | ConvertFrom-Json
}
if (-not $McpJson.mcpServers) { $McpJson | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force }
$telegramServer = [pscustomobject]@{
    command = "bun"
    args    = @("--cwd", $PluginRoot, "server.ts")
    env     = [pscustomobject]@{ TELEGRAM_BOT_TOKEN = $Token }
}
$McpJson.mcpServers | Add-Member -NotePropertyName telegram -NotePropertyValue $telegramServer -Force
$McpJson | ConvertTo-Json -Depth 10 | Out-File -FilePath $McpFile -Encoding UTF8
Info "Updated $McpFile"

# ── 5. Copy hook scripts ─────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $HooksDir | Out-Null
Copy-Item -Force (Join-Path $RepoRoot "hooks\telegram_mirror.py") $HooksDir
Copy-Item -Force (Join-Path $RepoRoot "hooks\telegram_worker.py") $HooksDir
Info "Copied hooks to $HooksDir"

# Patch CHAT_ID in both hook files so the user doesn't have to edit them by hand
foreach ($f in @("telegram_mirror.py", "telegram_worker.py")) {
    $p = Join-Path $HooksDir $f
    (Get-Content $p -Raw) -replace 'CHAT_ID\s*=\s*"[^"]*"', "CHAT_ID = `"$ChatId`"" | Set-Content $p -Encoding UTF8 -NoNewline
}
Info "Set CHAT_ID = $ChatId in hook scripts"

# ── 6. Merge hook block into settings.json ───────────────────────────────────
$Settings = if (Test-Path $SettingsFile) {
    Get-Content $SettingsFile -Raw | ConvertFrom-Json
} else {
    @{} | ConvertTo-Json | ConvertFrom-Json
}
if (-not $Settings.hooks) { $Settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{}) -Force }

# Convert paths to git-bash form expected by the harness
$MirrorBash = ($PythonPath -replace '\\', '/' -replace '^([A-Z]):', '/$1').ToLower() -replace '/^c/', '/c/'
$MirrorBash = ($PythonPath -replace '\\', '/' -replace '^C:', '/c').ToLower()
$ScriptBash = ((Join-Path $HooksDir "telegram_mirror.py") -replace '\\', '/' -replace '^C:', '/c')
$LogBash    = ((Join-Path $HooksDir "telegram_mirror.log") -replace '\\', '/' -replace '^C:', '/c')

$stopHook = [pscustomobject]@{
    hooks = @(
        [pscustomobject]@{
            type    = "command"
            command = "$MirrorBash $ScriptBash 2>>$LogBash || true"
            timeout = 30
        }
    )
}
$promptHook = [pscustomobject]@{
    hooks = @(
        [pscustomobject]@{
            type    = "command"
            command = "$MirrorBash $ScriptBash UserPromptSubmit 2>>$LogBash || true"
            timeout = 30
        }
    )
}
$Settings.hooks | Add-Member -NotePropertyName Stop -NotePropertyValue @($stopHook) -Force
$Settings.hooks | Add-Member -NotePropertyName UserPromptSubmit -NotePropertyValue @($promptHook) -Force
$Settings | ConvertTo-Json -Depth 10 | Out-File -FilePath $SettingsFile -Encoding UTF8
Info "Merged Stop + UserPromptSubmit hooks into $SettingsFile"

# ── 7. Done ──────────────────────────────────────────────────────────────────
Write-Host ""
Info "Install complete. Next steps:"
Write-Host "  1. Restart Claude Code (close and reopen)."
Write-Host "  2. DM your bot from Telegram, copy the 6-char pairing code."
Write-Host "  3. In Claude Code: /telegram:access pair <code>"
Write-Host "  4. (optional) /telegram:access policy allowlist"
Write-Host ""
Info "Logs: $HooksDir\telegram_mirror.log  /  telegram_worker.log"
