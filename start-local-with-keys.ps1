<#
.SYNOPSIS
    Loads Groq / Gemini API keys from the clipboard and (re)starts the local app.

.DESCRIPTION
    You copy a key to the clipboard, press Enter, and this script takes it from
    there. The key is never typed into the shell, never echoed and never written
    into the repository. The clipboard is overwritten afterwards.

    After the keys are loaded, stale backend/frontend processes on ports 8000 and
    5173 are stopped and both servers are started again with the keys in their
    environment. If the virtual environment or node_modules are missing, the
    existing start-local.ps1 is used instead (it installs dependencies first).

.PARAMETER GroqOnly
    Ask only for the Groq key. Gemini stays unset.

.PARAMETER Persist
    Also save the loaded keys in your Windows user environment so new terminals
    inherit them. This stores them in the registry; omit the switch to keep them
    memory-only for this session.

.PARAMETER NoRestart
    Only load the keys and print a summary; do not stop or start any server.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start-local-with-keys.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start-local-with-keys.ps1 -GroqOnly -NoRestart
#>
[CmdletBinding()]
param(
    [switch]$GroqOnly,
    [switch]$Persist,
    [switch]$NoRestart
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Read-KeyFromClipboard {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$Prefix = ''
    )
    # Two attempts: clipboard reads happen fast, so a copy that lands a moment
    # late (or a clipboard that a manager cleared) would otherwise look "empty".
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        if ($attempt -eq 1) {
            Write-Host ("1. Copy your {0} API key to the clipboard (Ctrl+C)." -f $Name) -ForegroundColor White
            Write-Host '2. Wait until you see it copied, then press Enter here. Do not paste it into this window.' -ForegroundColor DarkGray
            $null = Read-Host "Press Enter when the $Name key is on the clipboard (Enter on an empty clipboard skips it)"
        } else {
            Write-Host "  The clipboard was empty. Nothing was read, so nothing was skipped by mistake yet." -ForegroundColor Yellow
            Write-Host ("  Copy the {0} key again, then press Enter to retry. Press Enter twice to skip." -f $Name) -ForegroundColor Yellow
            $null = Read-Host "Press Enter to retry reading the $Name key"
            Start-Sleep -Milliseconds 150
        }

        $raw = ''
        try { $raw = Get-Clipboard -Raw } catch { $raw = '' }
        $value = ''
        if ($raw) { $value = ($raw -replace '\s', '') }

        if (-not $value) { continue }

        # Wipe the clipboard only after a successful read.
        try { Set-Clipboard -Value ' ' } catch { }

        if ($Prefix -and -not $value.StartsWith($Prefix)) {
            Write-Warning ("{0} : read {1} characters, but this does not look like a {0} key (expected it to start with '{2}')." -f $Name, $value.Length, $Prefix)
            Write-Warning 'Check that you copied the right provider key. Keeping it anyway; re-run the script to replace it.'
        } else {
            Write-Host ("  {0} : loaded {1} characters." -f $Name, $value.Length) -ForegroundColor Green
        }
        return $value
    }

    Write-Host ("  {0} : skipped (clipboard still empty)." -f $Name) -ForegroundColor DarkGray
    return $null
}

Write-Host 'Personal AI Clip Generator - start with cloud AI keys' -ForegroundColor Cyan
Write-Host 'Keys stay in this process only (and in the two server windows it opens).' -ForegroundColor DarkGray
Write-Host 'They are never written into the repository, logs or the database.' -ForegroundColor DarkGray
Write-Host ''

$groq = Read-KeyFromClipboard -Name 'Groq' -Prefix 'gsk_'
if ($GroqOnly) {
    $gemini = $null
    Write-Host '  Gemini : skipped (-GroqOnly).' -ForegroundColor DarkGray
} else {
    $gemini = Read-KeyFromClipboard -Name 'Gemini' -Prefix 'AIza'
}

# Fall back to anything already present in this shell's environment.
if (-not $groq -and $env:GROQ_API_KEY) {
    $groq = $env:GROQ_API_KEY
    Write-Host '  Groq : reusing GROQ_API_KEY already set in this shell.' -ForegroundColor DarkGray
}
if (-not $gemini -and -not $GroqOnly -and $env:GEMINI_API_KEY) {
    $gemini = $env:GEMINI_API_KEY
    Write-Host '  Gemini : reusing GEMINI_API_KEY already set in this shell.' -ForegroundColor DarkGray
}

if (-not $groq -and -not $gemini) {
    Write-Warning 'No keys loaded. Cloud AI needs GROQ_API_KEY or GEMINI_API_KEY.'
    Write-Host 'For local-only mode (no cloud AI) run: powershell -ExecutionPolicy Bypass -File .\start-local.ps1' -ForegroundColor Yellow
    exit 1
}

$env:GROQ_API_KEY = $groq
$env:GEMINI_API_KEY = $gemini

$groqState = if ($groq) { 'loaded' } else { 'not set' }
$geminiState = if ($gemini) { 'loaded' } else { 'not set' }
Write-Host ''
Write-Host ("Summary: Groq = {0} | Gemini = {1}" -f $groqState, $geminiState) -ForegroundColor Cyan
if (-not $gemini) {
    Write-Host 'Cloud AI will use Groq only; there is no fallback provider.' -ForegroundColor DarkGray
}
Write-Host ''

if ($Persist) {
    foreach ($pair in @(@('GROQ_API_KEY', $groq), @('GEMINI_API_KEY', $gemini))) {
        if ($pair[1]) {
            [Environment]::SetEnvironmentVariable($pair[0], $pair[1], 'User')
        }
    }
    Write-Host 'Saved to your Windows user environment; new terminals will inherit them.' -ForegroundColor Yellow
}

if ($NoRestart) {
    Write-Host ''
    Write-Host 'Keys loaded into this PowerShell process only (-NoRestart).' -ForegroundColor Green
    Write-Host 'Start the servers from this same window so they inherit the keys.' -ForegroundColor DarkGray
    exit 0
}

$python = Join-Path $root '.venv\Scripts\python.exe'
$frontendDir = Join-Path $root 'frontend'
$backendDir = Join-Path $root 'backend'
$hasEnvironment = (Test-Path $python) -and (Test-Path (Join-Path $frontendDir 'node_modules'))

if ($hasEnvironment) {
    # Fast restart: stop only the servers bound to our two ports.
    $stopped = $false
    foreach ($port in 8000, 5173) {
        $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($listener in $listeners) {
            $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            if ($process -and $process.ProcessName -in @('python', 'pythonw', 'node')) {
                Write-Host ("Stopping {0} (PID {1}) on port {2}" -f $process.ProcessName, $process.Id, $port) -ForegroundColor Yellow
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
                $stopped = $true
            }
        }
    }
    if (-not $stopped) { Write-Host 'No running servers found; starting fresh.' -ForegroundColor DarkGray }
    Start-Sleep -Milliseconds 1000

    foreach ($port in 8000, 5173) {
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
            throw "Port $port is still in use. Close the process using it, then run this script again."
        }
    }

    # Quote paths for the child PowerShell processes.
    $backendArg = $backendDir.Replace("'", "''")
    $frontendArg = $frontendDir.Replace("'", "''")
    $pythonArg = $python.Replace("'", "''")

    Start-Process powershell.exe -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location -LiteralPath '$backendArg'; & '$pythonArg' -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    )
    Start-Process powershell.exe -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location -LiteralPath '$frontendArg'; & npm.cmd run dev -- --host 127.0.0.1 --port 5173 --strictPort"
    )

    Write-Host ''
    Write-Host 'Started two server windows with the keys in their environment.' -ForegroundColor Green
} else {
    Write-Host 'Virtual environment or node_modules missing - running start-local.ps1 (installs dependencies first).' -ForegroundColor DarkGray
    & (Join-Path $root 'start-local.ps1')
    Write-Host ''
    Write-Host 'Servers started via start-local.ps1 with the keys in their environment.' -ForegroundColor Green
}

Write-Host 'Open http://127.0.0.1:5173 once both windows report ready.' -ForegroundColor Green
Write-Host 'Close those two windows to unload the keys. Never paste keys into chat, screenshots or Git.' -ForegroundColor DarkGray
